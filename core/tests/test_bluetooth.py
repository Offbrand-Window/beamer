"""Radio-independent tests: minimum MTU, ordering, backpressure and encrypted streams.

These deliberately do not claim to measure physical Bluetooth latency or driver support.
"""
import asyncio
import copy
import os
import socket
import unittest
from unittest import mock

from core import bluetooth_setup as setup
from core import protocol
from core.bluetooth_transport import (
    ACK,
    BUFFER_LIMIT,
    DATA,
    HEADER,
    StreamSession,
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

    async def test_stale_epoch_is_ignored_and_out_of_order_is_refused(self):
        await self.a.receive(HEADER.pack(DATA, b"old-epoc", 0, 1) + b"x")
        self.assertEqual(self.a.received, [0, 0])
        with self.assertRaises(ConnectionError):
            await self.a.receive(HEADER.pack(DATA, self.a.epoch, 0, 2) + b"x")

    async def test_impossible_ack_is_refused(self):
        with self.assertRaises(ConnectionError):
            await self.a.receive(HEADER.pack(ACK, self.a.epoch, 0, 99))

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
        with self.assertRaises(TimeoutError):
            await a.send(0, b"a mouse click")
        a.close()

    async def test_real_outbound_and_responder_forward_input_and_release_on_disconnect(self):
        from core.tests.responder_harness import B, HERE, Machine, entry
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
                self.a.close()
                self.b.close()  # physical link loss closes both endpoints
                await until(lambda: ("key", "a", False) in machine.injected())
                self.assertIsNone(machine.responder.owner)
        finally:
            await asyncio.to_thread(link.stop)
            await asyncio.to_thread(machine.stop)
