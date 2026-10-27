"""Small authenticated motion/ACK records for Bluetooth transport revision 2.

The cipher, nonce/counter handling, handshake and validators remain Beamer's.
Only the plaintext representation of two frequent message types changes. Other
messages, including keys, buttons and clipboard, retain their ordinary encoding.
"""
from core import protocol

MOTION, ACK = 1, 2


def _varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _read_varint(data, at):
    value = 0
    start = at
    for shift in range(0, 56, 7):
        if at >= len(data):
            break
        byte = data[at]
        at += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            if value > protocol.MAX_SAFE_INTEGER or data[start:at] != _varint(value):
                break
            return value, at
    raise protocol.ProtocolError("invalid compact Bluetooth integer")


def encode(message):
    data = message.get("data") or {}
    if not isinstance(data, dict):
        return protocol.dumps_message(message)
    kind = message.get("type")
    if kind == protocol.MSG_MOUSEMOVE and set(data) == {"dx", "dy", "seq"}:
        move = protocol.read_input(message)
        if move is not None:
            # Zig-zag integers keep the usual small positive/negative deltas to one byte.
            values = (move["seq"], move["dx"] * 2 if move["dx"] >= 0 else -move["dx"] * 2 - 1,
                      move["dy"] * 2 if move["dy"] >= 0 else -move["dy"] * 2 - 1)
            return bytes([MOTION]) + b"".join(_varint(value) for value in values)
    elif kind == protocol.MSG_ACK and set(data) == {"seq", "held_us"}:
        ack = protocol.read_ack(message)
        return bytes([ACK]) + _varint(ack["seq"]) + _varint(ack["held_us"])
    return protocol.dumps_message(message)


def decode(plain):
    if not plain or plain[0] not in (MOTION, ACK):
        return plain
    kind, at = plain[0], 1
    values = []
    for _ in range(3 if kind == MOTION else 2):
        value, at = _read_varint(plain, at)
        values.append(value)
    if at != len(plain):
        raise protocol.ProtocolError("trailing compact Bluetooth data")
    if kind == MOTION:
        seq, dx, dy = values
        dx, dy = (dx >> 1) ^ -(dx & 1), (dy >> 1) ^ -(dy & 1)
        message = protocol.mousemove_msg(dx, dy, seq)
        if protocol.read_input(message) is None:
            raise protocol.ProtocolError("invalid compact Bluetooth motion")
    else:
        if values[1] > protocol.HELD_US_MAX:
            raise protocol.ProtocolError("invalid compact Bluetooth acknowledgement")
        message = protocol.ack_v6(*values)
        protocol.read_ack(message)
    return protocol.dumps_message(message)


class BluetoothLinkSession(protocol.LinkSession):
    def seal(self, message):
        return self.seal_raw(encode(message))

    def open_raw(self, body):
        return decode(super().open_raw(body))
