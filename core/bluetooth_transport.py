"""Two ordered, bounded byte streams over a single BLE GATT connection.

The radio role is independent of the input direction. Channel 0 is opened by the
central; channel 1 by the peripheral. Both carry ordinary Beamer v6 encrypted links.
No LAN proxy, IP routing, or automatic network fallback is involved.
"""
import asyncio
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
HELLO, READY, DATA, ACK = range(1, 5)
HEADER = struct.Struct("!B8sBI")
BUFFER_LIMIT = 128 * 1024
WINDOW = 8
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

    def close(self):
        self.closed.set()
        for event, sock in zip(self.ack_events, self.sockets, strict=True):
            event.set()
            sock.end()

    async def send(self, channel, data):
        async with self.send_locks[channel]:
            if self.closed.is_set():
                raise ConnectionError("Bluetooth stream closed")
            payload = self.packet_size - HEADER.size
            parts = [data[i:i + payload] for i in range(0, len(data), payload)]
            for index, part in enumerate(parts):
                self.seq[channel] += 1
                seq = self.seq[channel]
                if seq > 0xffffffff:
                    raise ConnectionError("Bluetooth sequence exhausted")
                flush = (index + 1) % WINDOW == 0 or index == len(parts) - 1
                await self.transmit(HEADER.pack(DATA | (0x80 if flush else 0), self.epoch, channel, seq) + part)
                if flush:
                    async def wait_ack(expected=seq):
                        while self.acked[channel] < expected and not self.closed.is_set():
                            self.ack_events[channel].clear()
                            await self.ack_events[channel].wait()
                        if self.closed.is_set():
                            raise ConnectionError("Bluetooth disconnected")
                    await asyncio.wait_for(wait_ack(), ACK_SECONDS)

    async def receive(self, packet):
        if self.closed.is_set() or len(packet) < HEADER.size or len(packet) > self.packet_size:
            return
        kind, epoch, channel, seq = HEADER.unpack_from(packet)
        if epoch != self.epoch:
            return  # a notification queued by an earlier physical connection
        if channel not in (0, 1):
            raise ConnectionError("invalid Bluetooth channel")
        if kind == ACK:
            if len(packet) != HEADER.size or not self.acked[channel] <= seq <= self.seq[channel]:
                raise ConnectionError("invalid Bluetooth acknowledgement")
            self.acked[channel] = seq
            self.ack_events[channel].set()
        elif kind & 0x7f == DATA:
            if seq != self.received[channel] + 1 or len(packet) == HEADER.size:
                raise ConnectionError("Bluetooth packet lost or reordered")
            self.received[channel] = seq
            self.sockets[channel].feed(packet[HEADER.size:])
            if channel != self.outgoing_channel and not self.incoming_started:
                self.incoming_started = True
                self.incoming(self.sockets[channel])
            if kind & 0x80:
                await self.transmit(HEADER.pack(ACK, self.epoch, channel, seq))
        else:
            raise ConnectionError("unknown Bluetooth packet")


class BluetoothPeer:
    def __init__(self, entry, incoming):
        self.token, self.role = entry["token"], entry["bluetooth"]
        self.service = service_uuid(self.token)
        self.incoming = incoming
        self.condition = threading.Condition()
        self.session = None
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
        def callback(data):
            packet = bytes(data)
            def put():
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
            queue = asyncio.Queue(maxsize=256)
            packet_callback = self._packet_callback(queue)
            await client.start_notify(TX_UUID, lambda _c, data: packet_callback(data))
            epoch = secrets.token_bytes(8)
            packet_size = min(244, client.mtu_size - 3, client.services.get_characteristic(RX_UUID).max_write_without_response_size)
            write_lock = asyncio.Lock()
            async def transmit(packet):
                async with write_lock:
                    await client.write_gatt_char(RX_UUID, packet, response=False)
            # HELLO and READY always fit the minimum ATT value length of 20 bytes.
            await transmit(bytes([HELLO]) + epoch + struct.pack("!H", packet_size))
            ready = await asyncio.wait_for(queue.get(), 5)
            if len(ready) != 11 or ready[:9] != bytes([READY]) + epoch:
                raise ConnectionError("Invalid Bluetooth setup response")
            agreed = struct.unpack("!H", ready[9:])[0]
            if not HEADER.size < agreed <= packet_size:
                raise ConnectionError("Invalid Bluetooth packet size")
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
                    size = min(244, backend.packet_size, struct.unpack("!H", packet[9:])[0])
                    if size <= HEADER.size:
                        raise ConnectionError("Bluetooth MTU too small")
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
