"""Builders for synthetic tag memory images."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

from kilnnfc.tag import Tag  # noqa: E402


def ndef_record(tnf, rtype, payload, first=True, last=True):
    header = tnf | 0x10  # short record
    if first:
        header |= 0x80
    if last:
        header |= 0x40
    return bytes([header, len(rtype), len(payload)]) + rtype + payload


def text_record(text, **kw):
    return ndef_record(0x01, b"T", b"\x02en" + text.encode(), **kw)


def uri_record(code, rest, **kw):
    return ndef_record(0x01, b"U", bytes([code]) + rest.encode(), **kw)


def mime_record(mime, payload, **kw):
    return ndef_record(0x02, mime.encode(), payload, **kw)


def long_mime_record(mime, payload):
    """Non-short record (4-byte length), for payloads over 255 bytes."""
    return (bytes([0xC0 | 0x02, len(mime)]) + len(payload).to_bytes(4, "big")
            + mime.encode() + payload)


def tlv(message, null_padding=0):
    head = b"\x00" * null_padding
    if len(message) < 0xFF:
        return head + bytes([0x03, len(message)]) + message + b"\xfe"
    return head + b"\x03\xff" + len(message).to_bytes(2, "big") + message + b"\xfe"


def type2_tag(message=None, user=None, uid=b"\x04\x11\x22\x33\x44\x55\x66", pages=135):
    """NTAG215-sized memory: UID pages, CC in page 3, user data from page 4."""
    memory = bytearray(pages * 4)
    memory[0:7] = uid[:3] + b"\x88" + uid[3:6]
    memory[12:16] = bytes([0xE1, 0x10, (pages - 4) * 4 // 8, 0x00])
    data = tlv(message) if message is not None else (user or b"")
    memory[16:16 + len(data)] = data
    return Tag(uid, "iso14443a", "type2", bytes(memory), sak=0x00)
