"""Network readers (LaneTag): tag reads that arrive through Moonraker.

kiln_nfc registers Klipper webhooks endpoints (kiln_nfc/scan, ...), which
Moonraker publishes on its own HTTP API and websocket - no extra port or
server on the printer, and Moonraker's authorization applies. A reader
sends the raw read - UID, tag kind, memory dump and, for MIFARE Classic,
which key family opened which sector - not decoded filament data, so every
format kiln_nfc decodes works over the network too. See docs/remote-api.md.

This module turns a request's parameters into a Tag."""

import base64
import binascii

from .tag import (
    KIND_MIFARE_CLASSIC, KIND_TYPE2, KIND_TYPE4, KIND_TYPE5, TECH_ISO14443A,
    TECH_ISO15693, Tag,
)

API_VERSION = 1
MAX_MEMORY = 8192            # a MIFARE Classic 4K dump is 4096 bytes
KINDS = (KIND_TYPE2, KIND_MIFARE_CLASSIC, KIND_TYPE4, KIND_TYPE5)


class RemoteError(ValueError):
    """The request is malformed."""


def _base64(data, key):
    text = data.get(key) or ""
    if not isinstance(text, str):
        raise RemoteError("%s must be a base64 string" % key)
    try:
        value = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise RemoteError("%s is not valid base64" % key)
    if len(value) > MAX_MEMORY:
        raise RemoteError("%s is too long" % key)
    return value


def _hex(data, key, default=""):
    text = str(data.get(key) or default).replace(":", "").replace(" ", "")
    try:
        return bytes.fromhex(text)
    except ValueError:
        raise RemoteError("%s must be hex" % key)


def tag_from_json(data):
    """A Tag from a kiln_nfc/scan request's parameters, or RemoteError."""
    if not isinstance(data, dict):
        raise RemoteError("expected a JSON object")
    kind = data.get("kind")
    if kind not in KINDS:
        raise RemoteError("unknown tag kind %r" % (kind,))
    uid = _hex(data, "uid")
    if not 4 <= len(uid) <= 10:
        raise RemoteError("uid must be 4 to 10 bytes")
    sak = data.get("sak")
    if sak is not None:
        if not isinstance(sak, int) or not 0 <= sak <= 0xFF:
            raise RemoteError("sak must be a byte")
    block_size = data.get("block_size", 4)
    if not isinstance(block_size, int) or not 1 <= block_size <= 32:
        raise RemoteError("block_size must be 1 to 32")
    reader = str(data.get("reader") or "network reader")[:64]
    tech = TECH_ISO15693 if kind == KIND_TYPE5 else TECH_ISO14443A
    memory = _base64(data, "memory")
    tag = Tag(uid, tech, kind, memory, atqa=_hex(data, "atqa"), sak=sak,
              block_size=block_size, reader=reader)
    if kind == KIND_TYPE4:
        tag.ndef = _base64(data, "ndef") or None
    if kind == KIND_MIFARE_CLASSIC:
        if len(memory) % 16:
            raise RemoteError("MIFARE Classic memory must be whole 16-byte blocks")
        keys = data.get("sector_keys") or {}
        if not isinstance(keys, dict):
            raise RemoteError("sector_keys must be an object")
        for sector, name in keys.items():
            try:
                sector = int(sector)
            except ValueError:
                raise RemoteError("sector_keys keys must be sector numbers")
            if not 0 <= sector < 40:
                raise RemoteError("sector %d does not exist" % sector)
            tag.sector_keys[sector] = str(name)[:32]
            tag.sectors_read.add(sector)
    tag.incomplete = bool(data.get("incomplete"))
    return tag
