"""Two ordered, bounded byte streams over a single BLE GATT connection.

The radio role is independent of the input direction. Channel 0 is opened by the
central; channel 1 by the peripheral. Both carry ordinary Beamer v6 encrypted links.
No LAN proxy, IP routing, or automatic network fallback is involved.
"""
import asyncio
import collections
import logging
import secrets
import socket
import struct
import sys
import threading
import time

from core.bluetooth_setup import service_uuid
from core.i18n import tr

LOGGER = logging.getLogger("Beamer")
RX_UUID = "e63ddf52-1610-40dd-9d67-af80c6444735"
TX_UUID = "e63ddf52-1610-40dd-9d67-af80c6444736"
# Revision 2 is deliberately incompatible with the first prototype. Update both ends.
HELLO, READY, DATA, ACK = 17, 18, 3, 4
CHANNEL_BIT = 0x40
HEADER = struct.Struct("!BI")
BUFFER_LIMIT = 128 * 1024
WINDOW = 32
MESSAGE_WINDOW = 4
WINDOW_BYTES = 256
BULK_WINDOW_BYTES = 2048
ACK_BATCH_SECONDS = 0.002
ACK_SECONDS = 1.5
_registry = {}
_registry_lock = threading.RLock()


class BluetoothSocket:
    """The subset of socket used by Beamer. Timeout controls reads; sends either
    complete or tear down the connection, so a retry cannot duplicate partial input.
    """
    is_bluetooth = True

    def __init__(self, session, channel):
        self.session, self.channel = session, channel
        self._timeout = None
        self._buffer = bytearray()
        self._condition = threading.Condition()
        self._closed = False

    def feed(self, data):
        with self._condition:
            if len(self._buffer) + len(data) > BUFFER_LIMIT:
                raise ConnectionError("Bluetooth receive buffer full")
            self._buffer.extend(data)
            self._condition.notify_all()

    def end(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def recv(self, size):
        if size == 0:
            return b""
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        with self._condition:
            while not self._buffer and not self._closed:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    raise TimeoutError("Bluetooth read timed out")
                self._condition.wait(left)
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            return data

    def send(self, data):
        if self._closed:
            raise ConnectionError("Bluetooth link is closed")
        data = bytes(data[:BUFFER_LIMIT])
        if not data:
            return 0
        future = asyncio.run_coroutine_threadsafe(self.session.send(self.channel, data), self.session.loop)
        try:
            future.result(timeout=10)
        except Exception as exc:
            future.cancel()
            self.close()
            raise ConnectionError("Bluetooth send failed") from exc
        return len(data)

    def sendall(self, data):
        at = 0
        while at < len(data):
            at += self.send(data[at:])

    def settimeout(self, timeout):
        self._timeout = timeout

    def gettimeout(self):
        return self._timeout

    def setsockopt(self, *_args):
        pass  # TCP options have no meaning for a GATT stream.

    def getsockname(self):
        return ("bluetooth-local", self.channel)

    def getpeername(self):
        return ("bluetooth-peer", self.channel)

    def shutdown(self, _how=socket.SHUT_RDWR):
        self.close()

    def close(self):
        self.end()
        try:
            self.session.loop.call_soon_threadsafe(self.session.close)
        except RuntimeError:
            pass


class StreamSession:
    """A bounded sliding window, independent of Beamer message boundaries.

    send() returns once bytes are submitted to the native adapter. Cumulative ACKs
    release credits asynchronously; only a full window backpressures the writer.
    A watchdog closes a lost link even when the last send was a lone key release.
    Native GATT callbacks are bound to this physical connection, so its handshake
    nonce need not be repeated in every packet. Beamer authenticates every frame.
    """
    def __init__(self, loop, epoch, packet_size, outgoing_channel, transmit, incoming):
        if len(epoch) != 8 or not HEADER.size < packet_size <= 244:
            raise ValueError("invalid Bluetooth stream parameters")
        self.loop, self.epoch, self.packet_size = loop, epoch, packet_size
        self.transmit, self.incoming = transmit, incoming
        self.outgoing_channel = outgoing_channel
        self.sockets = [BluetoothSocket(self, 0), BluetoothSocket(self, 1)]
        self.outgoing = self.sockets[outgoing_channel]
        self.incoming_started = False
        self.closed = asyncio.Event()
        self.send_locks = [asyncio.Lock(), asyncio.Lock()]
        self.seq = [0, 0]
        self.received = [0, 0]
        self.acked = [0, 0]
        self.ack_events = [asyncio.Event(), asyncio.Event()]
        self.pending = [collections.deque(), collections.deque()]
        self.pending_bytes = [0, 0]
        self.pending_messages = [collections.deque(), collections.deque()]
        self.pending_changed = asyncio.Event()
        self.ack_changed = asyncio.Event()
        self.ack_due = [0, 0]
        self.failure = None
        self.tasks = [loop.create_task(self._watch_pending()), loop.create_task(self._write_acks())]

    def close(self):
        self.closed.set()
        for event, sock in zip(self.ack_events, self.sockets, strict=True):
            event.set()
            sock.end()
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()

    def _check_open(self):
        if self.closed.is_set():
            raise ConnectionError(self.failure or "Bluetooth stream closed")

    async def send(self, channel, data):
        async with self.send_locks[channel]:
            self._check_open()
            while len(self.pending_messages[channel]) >= MESSAGE_WINDOW:
                self.ack_events[channel].clear()
                self._check_open()
                await self.ack_events[channel].wait()
            self._check_open()
            limit = BULK_WINDOW_BYTES if len(data) > WINDOW_BYTES else WINDOW_BYTES
            payload = self.packet_size - HEADER.size
            for at in range(0, len(data), payload):
                part = data[at:at + payload]
                while len(self.pending[channel]) >= WINDOW or self.pending_bytes[channel] + len(part) > limit:
                    self.ack_events[channel].clear()
                    self._check_open()
                    await self.ack_events[channel].wait()
                self._check_open()
                self.seq[channel] += 1
                seq = self.seq[channel]
                if seq > 0xffffffff:
                    self.failure = "Bluetooth sequence exhausted"
                    self.close()
                    self._check_open()
                # Reserve before transmit: an in-memory adapter (or a fast real callback) can
                # acknowledge the packet before the native write operation returns.
                self.pending[channel].append((seq, len(part), self.loop.time()))
                self.pending_bytes[channel] += len(part)
                self.pending_changed.set()
                try:
                    await self.transmit(HEADER.pack(DATA | channel * CHANNEL_BIT, seq) + part)
                except BaseException:
                    self.close()
                    raise
            if data and self.acked[channel] < self.seq[channel]:
                self.pending_messages[channel].append(self.seq[channel])

    async def drain(self):
        """For tests/diagnostics: wait for delivery, without forcing each send to wait."""
        for channel in (0, 1):
            while self.pending[channel]:
                self.ack_events[channel].clear()
                self._check_open()
                await self.ack_events[channel].wait()
        self._check_open()

    async def _watch_pending(self):
        while not self.closed.is_set():
            self.pending_changed.clear()
            oldest = [queue[0][2] for queue in self.pending if queue]
            if not oldest:
                await self.pending_changed.wait()
                continue
            left = min(oldest) + ACK_SECONDS - self.loop.time()
            if left <= 0:
                self.failure = "Bluetooth delivery acknowledgement timed out"
                self.close()
                return
            try:
                await asyncio.wait_for(self.pending_changed.wait(), left)
            except TimeoutError:
                continue

    async def _write_acks(self):
        try:
            while not self.closed.is_set():
                await self.ack_changed.wait()
                # Collapse a burst into one cumulative ACK. This task never holds up the
                # receive worker while the OS notification queue is full.
                await asyncio.sleep(ACK_BATCH_SECONDS)
                self.ack_changed.clear()
                for channel in (0, 1):
                    seq, self.ack_due[channel] = self.ack_due[channel], 0
                    if seq:
                        await self.transmit(HEADER.pack(ACK | channel * CHANNEL_BIT, seq))
        except Exception as exc:
            self.failure = str(exc)
            self.close()

    async def receive(self, packet):
        if self.closed.is_set():
            return  # callbacks from retired GATT connections stay bound to their closed session
        if not HEADER.size <= len(packet) <= self.packet_size:
            raise ConnectionError("invalid Bluetooth packet length")
        flags, seq = HEADER.unpack_from(packet)
        channel, kind = flags // CHANNEL_BIT, flags % CHANNEL_BIT
        if channel not in (0, 1):
            raise ConnectionError("invalid Bluetooth channel")
        if kind == ACK:
            if len(packet) != HEADER.size or not self.acked[channel] <= seq <= self.seq[channel]:
                raise ConnectionError("invalid Bluetooth acknowledgement")
            self.acked[channel] = seq
            while self.pending[channel] and self.pending[channel][0][0] <= seq:
                _seq, size, _at = self.pending[channel].popleft()
                self.pending_bytes[channel] -= size
            while self.pending_messages[channel] and self.pending_messages[channel][0] <= seq:
                self.pending_messages[channel].popleft()
            self.ack_events[channel].set()
            self.pending_changed.set()
        elif kind == DATA:
            if seq != self.received[channel] + 1 or len(packet) == HEADER.size:
                raise ConnectionError("Bluetooth packet lost or reordered")
            self.received[channel] = seq
            self.sockets[channel].feed(packet[HEADER.size:])
            if channel != self.outgoing_channel and not self.incoming_started:
                self.incoming_started = True
                self.incoming(self.sockets[channel])
            self.ack_due[channel] = seq
            self.ack_changed.set()
        else:
            raise ConnectionError("unknown Bluetooth packet")


async def negotiated_packet_size(get_size, timeout=1.0):
    """Bleak/WinRT can initially expose 20 while the ATT MTU exchange is pending.

    Use the characteristic's actual write limit, not client.mtu_size (which can
    remain a placeholder on some backends). The peripheral independently caps
    the value against its current notification limit in the READY handshake.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        size = min(244, int(get_size()))
        if size < 20:
            raise ConnectionError("Invalid Bluetooth packet size")
        if size > 20 or loop.time() >= deadline:
            return size
        await asyncio.sleep(min(0.05, max(0, deadline - loop.time())))


class BluetoothPeer:
    def __init__(self, entry, incoming):
        self.token, self.role = entry["token"], entry["bluetooth"]
        self.service = service_uuid(self.token)
        self.incoming = incoming
        self.condition = threading.Condition()
        self.session = None
        self._generation = None
        self.claimed = False
        self.status = "Starting Bluetooth"
        self.stopped = threading.Event()
        self.loop = None
        self.thread = threading.Thread(target=self._thread, name="Beamer-Bluetooth", daemon=True)
        self.thread.start()

    def _status(self, text):
        with self.condition:
            self.status = text
            self.condition.notify_all()
        LOGGER.info("Bluetooth: %s", text)

    def _thread(self):
        # WinRT must run in an MTA worker, independently of Qt's COM apartment.
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        try:
            asyncio.run(self._run())
        finally:
            if sys.platform == "win32":
                ctypes.windll.ole32.CoUninitialize()

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        while not self.stopped.is_set():
            try:
                if self.role == "connect":
                    await self._central()
                else:
                    await self._peripheral()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # native backends expose platform-specific exception types
                LOGGER.debug("Bluetooth backend failed", exc_info=True)
                self._status(tr("Bluetooth: {error}").format(error=exc))
            finally:
                self._generation = None
                with self.condition:
                    if self.session is not None:
                        self.session.close()
                    self.session = None
                    self.claimed = False
                    self.condition.notify_all()
            try:
                await asyncio.wait_for(self.stop_event.wait(), 2)
            except TimeoutError:
                pass

    def _ready(self, session):
        with self.condition:
            self.session, self.claimed = session, False
            self.status = "Bluetooth connected; authenticating Beamer"
            self.condition.notify_all()

    def connect(self, timeout):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not self.stopped.is_set():
                if self.session is not None and not self.session.closed.is_set() and not self.claimed:
                    self.claimed = True
                    return self.session.outgoing
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ConnectionError(self.status)
                self.condition.wait(left)
        raise ConnectionError("Bluetooth stopped")

    def stop(self):
        self.stopped.set()
        if self.loop is not None:
            def cancel():
                self.stop_event.set()
                if self.session is not None:
                    self.session.close()
                for task in asyncio.all_tasks(self.loop):
                    task.cancel()
            try:
                self.loop.call_soon_threadsafe(cancel)
            except RuntimeError:
                pass
        with self.condition:
            self.condition.notify_all()

    async def _receive_worker(self, queue, receive):
        # Native callbacks can arrive on different threads. One consumer preserves wire order
        # and keeps malicious/slow senders from creating an unbounded number of asyncio tasks.
        while True:
            await receive(await queue.get())

    def _packet_callback(self, queue):
        generation = self._generation
        def callback(data):
            if self.stopped.is_set() or self._generation is not generation:
                return
            packet = bytes(data)
            def put():
                if self.stopped.is_set() or self._generation is not generation:
                    return
                try:
                    queue.put_nowait(packet)
                except asyncio.QueueFull:
                    if self.session is not None:
                        self.session.close()
            self.loop.call_soon_threadsafe(put)
        return callback

    async def _central(self):
        from bleak import BleakClient, BleakScanner
        self._status(tr("Looking for the other computer over Bluetooth"))
        device = await BleakScanner.find_device_by_filter(
            lambda _device, adv: self.service in [s.lower() for s in adv.service_uuids],
            timeout=8, service_uuids=[self.service])
        if device is None:
            raise ConnectionError("Other computer not found. Use Advertise there and exchange both setup codes.")
        disconnected = asyncio.Event()
        async with BleakClient(device, disconnected_callback=lambda _c: self.loop.call_soon_threadsafe(disconnected.set), timeout=15) as client:
            self._generation = object()
            queue = asyncio.Queue(maxsize=256)
            packet_callback = self._packet_callback(queue)
            await client.start_notify(TX_UUID, lambda _c, data: packet_callback(data))
            epoch = secrets.token_bytes(8)
            characteristic = client.services.get_characteristic(RX_UUID)
            packet_size = await negotiated_packet_size(lambda: characteristic.max_write_without_response_size)
            write_lock = asyncio.Lock()
            async def transmit(packet):
                async with write_lock:
                    await client.write_gatt_char(RX_UUID, packet, response=False)
            # HELLO and READY always fit the minimum ATT value length of 20 bytes.
            await transmit(bytes([HELLO]) + epoch + struct.pack("!H", packet_size))
            try:
                ready = await asyncio.wait_for(queue.get(), 5)
            except TimeoutError:
                raise ConnectionError("Bluetooth handshake timed out. Update Beamer Bluetooth on both computers.") from None
            if len(ready) != 11 or ready[:9] != bytes([READY]) + epoch:
                raise ConnectionError("Invalid Bluetooth setup response")
            agreed = struct.unpack("!H", ready[9:])[0]
            if not HEADER.size < agreed <= packet_size:
                raise ConnectionError("Invalid Bluetooth packet size")
            LOGGER.info("Bluetooth transport v2: packet=%d payload=%d window_bytes=%d message_window=%d", agreed, agreed - HEADER.size, WINDOW_BYTES, MESSAGE_WINDOW)
            session = StreamSession(self.loop, epoch, agreed, 0, transmit, self.incoming)
            self._ready(session)
            worker = asyncio.create_task(self._receive_worker(queue, session.receive))
            closed = asyncio.create_task(session.closed.wait())
            lost = asyncio.create_task(disconnected.wait())
            try:
                done, _ = await asyncio.wait((worker, closed, lost), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                session.close()
                for task in (worker, closed, lost):
                    task.cancel()
                await asyncio.gather(worker, closed, lost, return_exceptions=True)

    async def _peripheral(self):
        from core.bluetooth_native import make_peripheral
        self._generation = object()
        queue = asyncio.Queue(maxsize=256)
        backend = make_peripheral(self.service, self._packet_callback(queue))
        try:
            await backend.start()
            self._status(tr("Advertising over Bluetooth; choose Connect on the other computer"))
            async def receive(packet):
                if self.session is None:
                    if len(packet) != 11 or packet[0] != HELLO:
                        return
                    epoch = packet[1:9]
                    available = await negotiated_packet_size(lambda: backend.packet_size, timeout=0.5)
                    size = min(available, struct.unpack("!H", packet[9:])[0])
                    if size <= HEADER.size:
                        raise ConnectionError("Bluetooth MTU too small")
                    LOGGER.info("Bluetooth transport v2: packet=%d payload=%d window_bytes=%d message_window=%d", size, size - HEADER.size, WINDOW_BYTES, MESSAGE_WINDOW)
                    session = StreamSession(self.loop, epoch, size, 1, backend.send, self.incoming)
                    await backend.send(bytes([READY]) + epoch + struct.pack("!H", size))
                    self._ready(session)
                else:
                    await self.session.receive(packet)
            worker = asyncio.create_task(self._receive_worker(queue, receive))
            async def wait_closed():
                while self.session is None:
                    await asyncio.sleep(0.05)
                await self.session.closed.wait()
            closed = asyncio.create_task(wait_closed())
            lost = asyncio.create_task(backend.disconnected.wait())
            try:
                done, _ = await asyncio.wait((worker, closed, lost), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in (worker, closed, lost):
                    task.cancel()
                await asyncio.gather(worker, closed, lost, return_exceptions=True)
        finally:
            await backend.stop()


def sync(owner, peers, incoming):
    wanted = {p["token"]: p for p in peers if p.get("bluetooth") in ("connect", "advertise")
              and p.get("in_use", True) is True}
    with _registry_lock:
        for key, (held_owner, runtime) in list(_registry.items()):
            token = key[1]
            if held_owner is owner and (token not in wanted or wanted[token]["bluetooth"] != runtime.role):
                runtime.stop()
                del _registry[key]
        for token, entry in wanted.items():
            key = (id(owner), token)
            if key not in _registry:
                _registry[key] = (owner, BluetoothPeer(entry, incoming))


def stop(owner):
    with _registry_lock:
        for key, (held_owner, runtime) in list(_registry.items()):
            if held_owner is owner:
                runtime.stop()
                del _registry[key]


def connect(book, entry, timeout):
    with _registry_lock:
        pair = _registry.get((id(getattr(book, "_bluetooth_owner", None)), entry["token"]))
    if pair is None:
        raise ConnectionError("Bluetooth listener is starting")
    return pair[1].connect(timeout)
