"""Offline Bluetooth pairing. Exchange the two private setup codes once, out of band.

The codes contain random secrets, not short passwords. The resulting token goes through
Beamer's existing authenticated, forward-secret wire handshake; Bluetooth names are never trust.
"""
import base64
import copy
import hashlib
import json
import secrets
import socket
import time
import uuid

from core import peerlist, protocol

PREFIX = "BB1."
TEXT_LIMIT = 16 * 1024


def encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def own_code(book, platform):
    with book.lock:
        settings = copy.deepcopy(book._load())
        secret = settings.get("bluetooth_setup_secret")
        if not secret:
            secret = encode(secrets.token_bytes(32))
            settings["bluetooth_setup_secret"] = secret
            book._save(settings)
        value = {"id": settings["machine_id"], "secret": secret,
                 "name": (settings.get("name") or socket.gethostname())[:48], "platform": platform}
    return PREFIX + encode(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def parse_code(code):
    try:
        if not isinstance(code, str) or not code.startswith(PREFIX) or len(code) > 1024:
            raise ValueError()
        value = json.loads(decode(code[len(PREFIX):]))
        if (not isinstance(value, dict) or protocol.read_id(value.get("id")) is None
                or not isinstance(value.get("secret"), str) or len(decode(value["secret"])) != 32
                or encode(decode(value["secret"])) != value["secret"]
                or value.get("platform") not in ("macos", "windows", "linux")
                or not isinstance(value.get("name"), str) or not 1 <= len(value["name"]) <= 48):
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("Paste the complete BB1. setup code from the other computer.") from None


def pairing_token(own, other):
    ordered = sorted((own, other), key=lambda value: value["id"])
    material = b"".join(protocol.read_id(value["id"]) + decode(value["secret"]) for value in ordered)
    return encode(protocol.hkdf_sha256(material, b"beamer-bluetooth-setup-v1", b"pair-token"))


def service_uuid(token):
    # Public discovery identifier; neither secret nor a Bluetooth address is advertised.
    digest = hashlib.sha256(b"beamer-bluetooth-service-v1" + protocol.key_id(token)).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def pair(book, own, other_code, role):
    """Called after an explicit Save in the native setup dialog. No network pairing needed."""
    if role not in ("advertise", "connect"):
        raise ValueError("Choose Advertise on one computer and Connect on the other.")
    local, other = parse_code(own), parse_code(other_code.strip())
    if local["id"] == other["id"]:
        raise ValueError("That is this computer's code. Paste the other computer's code.")
    token = pairing_token(local, other)
    with book.lock:
        settings = copy.deepcopy(book._load())
        if settings["machine_id"] != local["id"] or settings.get("bluetooth_setup_secret") != local["secret"]:
            raise ValueError("This computer's setup code changed. Reopen Bluetooth setup.")
        previous = next((p for p in settings["peers"] if p.get("id") == other["id"]), None)
        if previous is None and len(settings["peers"]) >= protocol.MAX_PEERS:
            raise ValueError("Remove an existing computer before adding another.")
        entry = dict(previous or {})
        entry.update(id=other["id"], name=other["name"], platform=other["platform"], token=token,
                     host="ble:" + service_uuid(token), port=protocol.DEFAULT_PORT, hw="",
                     host_pinned=True, send=True, allow_drive=True, in_use=True,
                     from_1_4=False, bluetooth=role, paired_at=int(time.time()))
        entry.pop("dormant", None)
        entry.pop("dormant_reason", None)
        peerlist.add_peer(settings, entry, previous)
        book._save(settings)
    return entry


def capabilities(caps):
    # Large images and per-sample motion paths create queues on BLE. Summed deltas keep
    # current motion useful when a high-polling-rate mouse produces more input than the radio.
    return [cap for cap in caps if cap not in ("clipboard_image", "motion_path")]


def message_for_bluetooth(message):
    if message.get("type") != protocol.MSG_CLIPBOARD:
        return message
    text = (message.get("data") or {}).get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > TEXT_LIMIT:
        return None
    return protocol.clipboard_msg(text)
