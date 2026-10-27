"""Radio-independent tests: minimum MTU, ordering, backpressure and encrypted streams.

These deliberately do not claim to measure physical Bluetooth latency or driver support.
"""
import asyncio
import copy
import os
import socket
import unittest
from unittest import mock

from core import bluetooth_codec as codec
from core import bluetooth_setup as setup
from core import protocol
from core.bluetooth_transport import (
    ACK,
    BUFFER_LIMIT,
    DATA,
    HEADER,
    MESSAGE_WINDOW,
    WINDOW,
    WINDOW_BYTES,
    StreamSession,
    negotiated_packet_size,
)
from core.receiver import PeerBook


class Book(PeerBook):
    def __init__(self):
        self.settings = {"schema": 2, "machine_id": protocol.id_text(os.urandom(16)),
                         "name": "Computer", "peers": [], "zones": []}
        super().__init__(lambda: copy.deepcopy(self.settings), self.save)

    def save(self, settings):
        self.settings = copy.deepcopy(settings)


class PairingTests(unittest.TestCase):
    def test_offline_pairing_is_symmetric_and_binds_both_machine_ids(self):
        a, b = Book(), Book()
        ac, bc = setup.own_code(a, "windows"), setup.own_code(b, "macos")
        ap = setup.pair(a, ac, bc, "connect")
        bp = setup.pair(b, bc, ac, "advertise")
        self.assertEqual(ap["token"], bp["token"])
        self.assertTrue(protocol.linkable(ap))
        self.assertEqual(ap["id"], b.settings["machine_id"])
        self.assertEqual(bp["id"], a.settings["machine_id"])
        self.assertEqual(ap["host"], bp["host"])
        self.assertEqual(setup.own_code(a, "windows"), ac)

    def test_repair_changes_role_without_adding_another_peer(self):
        a, b = Book(), Book()
        ac, bc = setup.own_code(a, "windows"), setup.own_code(b, "macos")
        setup.pair(a, ac, bc, "connect")
        setup.pair(a, ac, bc, "advertise")
        self.assertEqual(len(a.peers()), 1)
        self.assertEqual(a.peers()[0]["bluetooth"], "advertise")

    def test_wrong_self_and_corrupt_codes_are_refused(self):
        a = Book()
        code = setup.own_code(a, "macos")
        for other in (code, "BB1.bad", "not a code", "BB1." + "a" * 2000):
            with self.subTest(other=other[:12]), self.assertRaises(ValueError):
                setup.pair(a, code, other, "connect")
        self.assertEqual(a.peers(), [])

    def test_clipboard_limits_do_not_truncate_or_forward_images(self):
        self.assertEqual(setup.message_for_bluetooth(protocol.clipboard_msg("a" * 16384))["data"]["text"], "a" * 16384)
        self.assertIsNone(setup.message_for_bluetooth(protocol.clipboard_msg("a" * 16385)))
        self.assertIsNone(setup.message_for_bluetooth(protocol.clipboard_msg("😀" * 4097)))
        self.assertIsNone(setup.message_for_bluetooth(protocol.clipboard_msg(image=b"image")))
        self.assertEqual(setup.capabilities(["clipboard", "clipboard_image", "motion_path", "text"]), ["clipboard", "text"])

    def test_diagonal_motion_coalesces_without_crossing_key_boundaries(self):
        previous = protocol.mousemove_msg(2, 3, 1)
        merged = setup.coalesce_mousemove(previous, protocol.mousemove_msg(-1, 4, 2))
        self.assertEqual(merged, protocol.mousemove_msg(1, 7, 2))
        self.assertIsNone(setup.coalesce_mousemove(previous, protocol.key_msg("keydown", "a", seq=2)))


class CodecTests(unittest.TestCase):
    def test_motion_and_ack_round_trip_boundaries(self):
        for seq in (1, 127, 128, 2**32, protocol.MAX_SAFE_INTEGER):
            for dx, dy in ((0, 0), (-1, 2), (protocol.MOVE_MAX, -protocol.MOVE_MAX)):
                message = protocol.mousemove_msg(dx, dy, seq)
                self.assertEqual(protocol.parse_message(codec.decode(codec.encode(message))), message)
            for held in (0, 15000, protocol.HELD_US_MAX):
                message = protocol.ack_v6(seq, held)
                self.assertEqual(protocol.parse_message(codec.decode(codec.encode(message))), message)

    def test_metadata_and_discrete_events_retain_their_original_encoding(self):
        messages = [protocol.key_msg("keydown", "a", seq=1),
                    protocol.key_msg("keyup", "a", seq=2),
                    {"type": "mousedown", "data": {"button": "left", "seq": 3}},
                    protocol.clipboard_msg("copy"), protocol.ack_v6(1, 0, locked=True),
                    {"type": "mousemove", "data": {"dx": 1, "dy": 2, "seq": 4, "samples": [[1, 2]]}}]
        for message in messages:
            self.assertEqual(codec.encode(message), protocol.dumps_message(message))

    def test_typical_motion_is_four_plaintext_bytes_and_still_authenticated(self):
        token = setup.encode(os.urandom(32))
        a = codec.BluetoothLinkSession(token, protocol.ROLE_INITIATOR)
        b = codec.BluetoothLinkSession(token, protocol.ROLE_RESPONDER)
        b.accept_preamble(a.preamble())
        a.accept_preamble(b.preamble())
        move = protocol.mousemove_msg(2, -3, 1)
        self.assertEqual(len(codec.encode(move)), 4)
        frame = a.seal(move)
        self.assertEqual(len(frame), 28)
        # Ciphertext corruption is rejected before compact records can reach the injector.
        changed = bytearray(frame[4:])
        changed[-1] ^= 1
        with self.assertRaises(protocol.AuthenticationError):
            b.open(bytes(changed))
        self.assertEqual(b.open(frame[4:]), move)
        with self.assertRaises(protocol.ProtocolError):
            b.open(frame[4:])  # replay still fails

    def test_malformed_compact_records_are_refused(self):
        invalid = [b"\x01", b"\x01\x80", b"\x01\x81\x00\x00\x00",
                   b"\x01\x00\x00\x00", b"\x01\x01\x00\x00extra",
                   b"\x02\x01" + codec._varint(protocol.HELD_US_MAX + 1),
                   b"\x01" + codec._varint(protocol.MAX_SAFE_INTEGER + 1) + b"\0\0",
                   b"\x01\x01" + codec._varint(2 * protocol.MOVE_MAX + 2) + b"\0"]
        for plain in invalid:
            with self.subTest(plain=plain), self.assertRaises(protocol.ProtocolError):
                codec.decode(plain)


class Streams(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.incoming_a, self.incoming_b = [], []
        self.packets = []
        self.a, self.b = self.radio(20)

    def radio(self, mtu):
        loop = asyncio.get_running_loop()
        epoch = os.urandom(8)
        async def a_to_b(packet):
            self.packets.append(packet)
            await b.receive(packet)
        async def b_to_a(packet):
            self.packets.append(packet)
            await a.receive(packet)
        a = StreamSession(loop, epoch, mtu, 0, a_to_b, self.incoming_a.append)
        b = StreamSession(loop, epoch, mtu, 1, b_to_a, self.incoming_b.append)
        return a, b

    async def asyncTearDown(self):
        self.a.close()
        self.b.close()
        await asyncio.gather(*self.a.tasks, *self.b.tasks, return_exceptions=True)

    async def test_fragmentation_at_minimum_mtu_in_both_directions(self):
        forward, reverse = os.urandom(4096), os.urandom(5001)
        await asyncio.gather(self.a.send(0, forward), self.b.send(1, reverse))
        self.assertEqual(self.b.sockets[0].recv(9999), forward)
        self.assertEqual(self.a.sockets[1].recv(9999), reverse)
        self.assertEqual(len(self.incoming_a), 1)
        self.assertEqual(len(self.incoming_b), 1)
        self.assertTrue(all(len(p) <= 20 for p in self.packets))

    async def test_encrypted_beamer_handshake_and_clipboard_over_bluetooth_sockets(self):
        token = setup.encode(os.urandom(32))
        def initiator():
            sock = self.a.outgoing
            sock.settimeout(5)
            secure = protocol.LinkSession(token, protocol.ROLE_INITIATOR)
            sock.sendall(secure.preamble())
            secure.accept_preamble(protocol.recv_link_preamble(sock))
            protocol.send_msg(sock, secure, protocol.clipboard_msg("Hello from Windows — copied text"))
            return protocol.recv_msg(sock, secure)
        def responder():
            sock = self.b.sockets[0]
            sock.settimeout(5)
            secure = protocol.LinkSession(token, protocol.ROLE_RESPONDER)
            secure.accept_preamble(protocol.recv_link_preamble(sock))
            sock.sendall(secure.preamble())
            received = protocol.recv_msg(sock, secure)
            protocol.send_msg(sock, secure, protocol.clipboard_msg("Hello from Mac"))
            return received
        forward, reverse = await asyncio.gather(asyncio.to_thread(initiator), asyncio.to_thread(responder))
        self.assertEqual(forward["data"]["text"], "Hello from Mac")
        self.assertEqual(reverse["data"]["text"], "Hello from Windows — copied text")
        self.assertFalse(any(b"Hello from" in p for p in self.packets))

    async def test_retired_connection_callbacks_are_ignored_and_out_of_order_is_refused(self):
        with self.assertRaises(ConnectionError):
            await self.a.receive(HEADER.pack(DATA, 2) + b"x")
        self.a.close()
        await self.a.receive(HEADER.pack(DATA, 1) + b"x")
        self.assertEqual(self.a.received, [0, 0])

    async def test_impossible_ack_is_refused(self):
        with self.assertRaises(ConnectionError):
            await self.a.receive(HEADER.pack(ACK, 99))

    async def test_receive_buffer_is_bounded(self):
        sock = self.a.outgoing
        sock.feed(b"x" * BUFFER_LIMIT)
        with self.assertRaises(ConnectionError):
            sock.feed(b"y")

    async def test_read_timeout_and_disconnect_wake_reader(self):
        sock = self.a.outgoing
        sock.settimeout(0.001)
        with self.assertRaises(socket.timeout):
            await asyncio.to_thread(sock.recv, 1)
        sock.settimeout(None)
        task = asyncio.create_task(asyncio.to_thread(sock.recv, 1))
        await asyncio.sleep(0.01)
        self.a.close()
        self.assertEqual(await asyncio.wait_for(task, 1), b"")

    async def test_closed_session_rejects_sends(self):
        self.a.close()
        with self.assertRaises(ConnectionError):
            await self.a.send(0, b"x")

    async def test_notification_loss_times_out_instead_of_silently_dropping_input(self):
        async def drop(_packet):
            pass
        a = StreamSession(asyncio.get_running_loop(), os.urandom(8), 244, 0, drop, lambda _s: None)
        await a.send(0, b"a mouse click")  # submission succeeds without waiting for delivery
        with self.assertRaisesRegex(ConnectionError, "acknowledgement timed out"):
            await a.drain()
        self.assertTrue(a.closed.is_set())
        await asyncio.gather(*a.tasks, return_exceptions=True)

    async def test_multiple_messages_submit_before_any_ack_arrives(self):
        submitted = []
        async def transmit(packet):
            submitted.append(packet)
        a = StreamSession(asyncio.get_running_loop(), os.urandom(8), 244, 0, transmit, lambda _s: None)
        try:
            # The old implementation stalled on the first one for a complete round trip.
            async with asyncio.timeout(0.2):
                for _ in range(MESSAGE_WINDOW):
                    await a.send(0, b"x" * 28)
            self.assertEqual(len(submitted), MESSAGE_WINDOW)
            self.assertEqual(a.acked[0], 0)
            self.assertEqual(a.pending_bytes[0], MESSAGE_WINDOW * 28)
        finally:
            a.close()
            await asyncio.gather(*a.tasks, return_exceptions=True)

    async def test_window_backpressures_and_resumes_on_a_cumulative_ack(self):
        async def transmit(_packet):
            pass
        a = StreamSession(asyncio.get_running_loop(), os.urandom(8), 20, 0, transmit, lambda _s: None)
        try:
            await a.send(0, b"x" * (WINDOW * 15))
            self.assertEqual(len(a.pending[0]), WINDOW)
            blocked = asyncio.create_task(a.send(0, b"next"))
            await asyncio.sleep(0.01)
            self.assertFalse(blocked.done())
            self.assertLessEqual(a.pending_bytes[0], 2048)
            await a.receive(HEADER.pack(ACK, a.seq[0]))
            await asyncio.wait_for(blocked, 0.2)
            self.assertEqual(a.pending_bytes[0], 4)
            self.assertTrue(a.timing["credit_wait"])
            self.assertTrue(a.timing["delivery_rtt"])
        finally:
            a.close()
            await asyncio.gather(*a.tasks, return_exceptions=True)

    async def test_small_message_byte_budget_stays_bounded(self):
        async def transmit(_packet):
            pass
        a = StreamSession(asyncio.get_running_loop(), os.urandom(8), 244, 0, transmit, lambda _s: None)
        try:
            for _ in range(2):
                await a.send(0, b"x" * 128)
            blocked = asyncio.create_task(a.send(0, b"x" * 32))
            await asyncio.sleep(0.01)
            self.assertFalse(blocked.done())
            self.assertEqual(a.pending_bytes[0], WINDOW_BYTES)
            a.close()
            with self.assertRaises(ConnectionError):
                await blocked
        finally:
            a.close()
            await asyncio.gather(*a.tasks, return_exceptions=True)

    async def test_cumulative_acks_do_not_hold_up_receive_when_notifications_block(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        async def blocked_transmit(_packet):
            entered.set()
            await resume.wait()
        a = StreamSession(asyncio.get_running_loop(), os.urandom(8), 20, 0, blocked_transmit, lambda _s: None)
        try:
            await a.receive(HEADER.pack(DATA, 1) + b"first")
            await asyncio.wait_for(entered.wait(), 0.5)
            await asyncio.wait_for(a.receive(HEADER.pack(DATA, 2) + b"second"), 0.1)
            self.assertEqual(a.sockets[0].recv(99), b"firstsecond")
        finally:
            resume.set()
            a.close()
            await asyncio.gather(*a.tasks, return_exceptions=True)

    async def test_packet_negotiation_waits_for_the_real_limit(self):
        size = [20]
        asyncio.get_running_loop().call_later(0.06, size.__setitem__, 0, 185)
        self.assertEqual(await negotiated_packet_size(lambda: size[0], timeout=0.3), 185)
        self.assertEqual(await negotiated_packet_size(lambda: 20, timeout=0), 20)
        self.assertEqual(await negotiated_packet_size(lambda: 512), 244)

    async def test_windows_peripheral_reads_updated_mtu_instead_of_caching_20(self):
        from types import SimpleNamespace

        from core.bluetooth_native import WindowsPeripheral
        backend = WindowsPeripheral("unused", lambda _p: None)
        backend.client = SimpleNamespace(session=SimpleNamespace(max_pdu_size=23))
        self.assertEqual(backend.packet_size, 20)
        backend.client.session.max_pdu_size = 247
        self.assertEqual(backend.packet_size, 244)

    async def test_retired_native_callbacks_cannot_deliver_into_or_close_the_new_connection(self):
        import threading

        from core.bluetooth_transport import BluetoothPeer
        peer = BluetoothPeer.__new__(BluetoothPeer)
        peer.loop = asyncio.get_running_loop()
        peer.stopped = threading.Event()
        peer._generation = object()
        peer.session = mock.Mock()
        queue = asyncio.Queue(maxsize=1)
        callback = peer._packet_callback(queue)
        callback(b"queued before reconnect")
        peer._generation = object()
        callback(b"late after reconnect")
        await asyncio.sleep(0)
        self.assertTrue(queue.empty())
        peer.session.close.assert_not_called()

    async def test_delayed_radio_pipelines_encrypted_motion_and_preserves_key_order(self):
        # A connection event every 10 ms, four packets per direction per event. A reply
        # cannot be generated and returned until the next event (at least 20 ms RTT).
        # Count connection events instead of timing assertions that depend on CI load.
        for mtu in (20, 244):
            with self.subTest(mtu=mtu):
                queues = [[], []]
                ticks = [0]
                async def to_b(packet, queue=queues[0]):
                    queue.append(packet)
                async def to_a(packet, queue=queues[1]):
                    queue.append(packet)
                loop = asyncio.get_running_loop()
                epoch = os.urandom(8)
                a = StreamSession(loop, epoch, mtu, 0, to_b, lambda _s: None)
                b = StreamSession(loop, epoch, mtu, 1, to_a, lambda _s: None)
                async def radio(ticks=ticks, peers=((queues[0], b), (queues[1], a))):
                    while True:
                        await asyncio.sleep(0.01)
                        ticks[0] += 1
                        for queue, peer in peers:
                            for _ in range(min(4, len(queue))):
                                await peer.receive(queue.pop(0))
                driver = asyncio.create_task(radio())
                token = setup.encode(os.urandom(32))
                outgoing = codec.BluetoothLinkSession(token, protocol.ROLE_INITIATOR)
                incoming = codec.BluetoothLinkSession(token, protocol.ROLE_RESPONDER)
                incoming.accept_preamble(outgoing.preamble())
                outgoing.accept_preamble(incoming.preamble())
                messages = ([protocol.mousemove_msg(1, -1, seq) for seq in range(1, 21)]
                            + [protocol.key_msg("keydown", "a", seq=21), protocol.key_msg("keyup", "a", seq=22)]
                            + [protocol.mousemove_msg(1, -1, seq) for seq in range(23, 43)])
                try:
                    async with asyncio.timeout(5):
                        for message in messages:
                            await a.send(0, outgoing.seal(message))
                        await a.drain()
                    data = b.sockets[0].recv(BUFFER_LIMIT)
                    decoded = []
                    while data:
                        length = int.from_bytes(data[:4], "big")
                        decoded.append(incoming.open(data[4:4 + length]))
                        data = data[4 + length:]
                    self.assertEqual(decoded, messages)
                    # Stop-and-wait would take at least 84 events for these 42 messages,
                    # even before accounting for fragmentation at the minimum MTU.
                    self.assertLess(ticks[0], 42)
                    print(f"Simulated BLE: MTU value {mtu}, 42 encrypted messages in {ticks[0]} connection events (10 ms each)")
                finally:
                    driver.cancel()
                    a.close()
                    b.close()
                    await asyncio.gather(driver, *a.tasks, *b.tasks, return_exceptions=True)

    async def test_real_outbound_and_responder_forward_input_and_release_on_disconnect(self):
        from core.tests.responder_harness import HERE, B, Machine, entry
        from core.tests.test_link import Side
        machine = Machine([entry(B, "PC")])
        # Use the actual accept/handshake/serve path, with an in-memory radio and fake OS
        # injection. No TCP listener or system Bluetooth permission is needed for this test.
        machine.responder._requested_running = True
        self.b.incoming = lambda sock: machine.responder._accept_connection(sock, "bluetooth:test")
        side = Side()
        side.peer()["bluetooth"] = "connect"
        link = side.link()
        async def until(predicate):
            async with asyncio.timeout(4):
                while not predicate():
                    await asyncio.sleep(0.005)
        try:
            with mock.patch("core.bluetooth_transport.connect", return_value=self.a.outgoing):
                link.start()
                await until(link.live)
                self.assertNotIn("clipboard_image", link.caps)
                self.assertTrue(link.post(protocol.focus_v6(1, HERE, edge="left", offset=0.5)))
                await until(lambda: machine.responder.owner == B)
                self.assertTrue(link.send_input(protocol.mousemove_msg(5, 7)))
                self.assertTrue(link.send_input(protocol.key_msg("keydown", "a")))
                self.assertTrue(link.post(protocol.clipboard_msg("Copied over Bluetooth")))
                await until(lambda: ("key", "a", True) in machine.injected())
                await until(lambda: machine.clipboard.text == "Copied over Bluetooth")
                self.assertIn(("move", 5, 7), machine.injected())
                # Verify the real writer batches before encryption while preserving total
                # travel and the key transition following the burst. Use a longer interval
                # here so the assertion is robust on Windows CI's timer granularity.
                import time
                motion_times = []
                original_seal = codec.BluetoothLinkSession.seal
                def measured_seal(session, message):
                    if message.get("type") == protocol.MSG_MOUSEMOVE:
                        motion_times.append(time.monotonic())
                    return original_seal(session, message)
                with mock.patch("core.link.BLUETOOTH_MOTION_INTERVAL", 0.03), \
                        mock.patch.object(codec.BluetoothLinkSession, "seal", measured_seal):
                    for _ in range(10):
                        for _ in range(10):
                            self.assertTrue(link.send_input(protocol.mousemove_msg(1, 1)))
                        await asyncio.sleep(0.003)
                    self.assertTrue(link.send_input(protocol.key_msg("keydown", "b")))
                    self.assertTrue(link.send_input(protocol.key_msg("keyup", "b")))
                    await until(lambda: ("key", "b", False) in machine.injected())
                moves = [call for call in machine.injected() if call[0] == "move"]
                self.assertEqual(sum(call[1] for call in moves), 105)
                self.assertEqual(sum(call[2] for call in moves), 107)
                self.assertLess(len(motion_times), 100)
                for before, after in zip(motion_times, motion_times[1:], strict=False):
                    self.assertGreaterEqual(after - before, 0.025)
                self.a.close()
                self.b.close()  # physical link loss closes both endpoints
                await until(lambda: ("key", "a", False) in machine.injected())
                self.assertIsNone(machine.responder.owner)
        finally:
            await asyncio.to_thread(link.stop)
            await asyncio.to_thread(machine.stop)
