"""NFC Forum NDEF: finding the NDEF message in a tag's memory and splitting
it into records."""

import re

from ..tag import (
    KIND_MIFARE_CLASSIC, KIND_QR, KIND_TYPE2, KIND_TYPE4, KIND_TYPE5, MIFARE_BLOCK,
    mifare_sector_layout,
)

TNF_WELL_KNOWN = 0x01
TNF_MIME = 0x02
TNF_ABSOLUTE_URI = 0x03
TNF_EXTERNAL = 0x04

URI_PREFIXES = [
    "", "http://www.", "https://www.", "http://", "https://", "tel:",
    "mailto:", "ftp://anonymous:anonymous@", "ftp://ftp.", "ftps://",
    "sftp://", "smb://", "nfs://", "ftp://", "dav://", "news:", "telnet://",
    "imap:", "rtsp://", "urn:", "pop:", "sip:", "sips:", "tftp:", "btspp://",
    "btl2cap://", "btgoep://", "tcpobex://", "irdaobex://", "file://",
    "urn:epc:id:", "urn:epc:tag:", "urn:epc:pat:", "urn:epc:raw:", "urn:epc:",
    "urn:nfc:",
]

# MAD application ID of NDEF sectors on MIFARE Classic.
MAD_NDEF_AID = 0x03E1


class NdefRecord(object):
    def __init__(self, tnf, rtype, rid, payload):
        self.tnf = tnf
        self.type = rtype            # bytes
        self.id = rid
        self.payload = payload

    @property
    def mime(self):
        if self.tnf == TNF_MIME:
            return self.type.decode("ascii", "replace").lower()
        return ""

    def is_text(self):
        return self.tnf == TNF_WELL_KNOWN and self.type == b"T"

    def is_uri(self):
        return ((self.tnf == TNF_WELL_KNOWN and self.type == b"U")
                or self.tnf == TNF_ABSOLUTE_URI)

    def text(self):
        """Text record content, or the payload as UTF-8 for other records."""
        if self.is_text() and self.payload:
            status = self.payload[0]
            lang_len = status & 0x3F
            encoding = "utf-16" if status & 0x80 else "utf-8"
            return self.payload[1 + lang_len:].decode(encoding, "replace")
        return self.payload.decode("utf-8", "replace")

    def uri(self):
        if self.tnf == TNF_ABSOLUTE_URI:
            return self.type.decode("utf-8", "replace")
        if not self.payload:
            return ""
        code = self.payload[0]
        prefix = URI_PREFIXES[code] if code < len(URI_PREFIXES) else ""
        return prefix + self.payload[1:].decode("utf-8", "replace")

    def __repr__(self):
        return "NdefRecord(tnf=%d, type=%r, %d bytes)" % (self.tnf, self.type, len(self.payload))


def parse_records(message):
    """Records of an NDEF message; chunked records are joined."""
    records = []
    pos = 0
    chunk = None
    message = bytes(message)
    while pos + 3 <= len(message):
        header = message[pos]
        tnf = header & 0x07
        short = header & 0x10
        has_id = header & 0x08
        chunked = header & 0x20
        last = header & 0x40
        type_len = message[pos + 1]
        pos += 2
        if short:
            payload_len = message[pos]
            pos += 1
        else:
            if pos + 4 > len(message):
                break
            payload_len = int.from_bytes(message[pos:pos + 4], "big")
            pos += 4
        id_len = 0
        if has_id:
            id_len = message[pos]
            pos += 1
        end = pos + type_len + id_len + payload_len
        if end > len(message):
            break
        rtype = message[pos:pos + type_len]
        rid = message[pos + type_len:pos + type_len + id_len]
        payload = message[pos + type_len + id_len:end]
        pos = end
        if chunk is not None:
            chunk.payload += payload       # continuation chunk (TNF 0x06)
            if not chunked:
                records.append(chunk)
                chunk = None
        elif chunked:
            chunk = NdefRecord(tnf, rtype, rid, payload)
        else:
            records.append(NdefRecord(tnf, rtype, rid, payload))
        if last:
            break
    return records


def find_tlv_message(data, start=0):
    """Value of the first NDEF TLV (type 0x03) in a TLV area, or None."""
    pos = start
    data = bytes(data)
    while pos < len(data):
        tlv = data[pos]
        if tlv == 0x00:                # NULL TLV: no length
            pos += 1
            continue
        if tlv == 0xFE:                # terminator
            return None
        if pos + 1 >= len(data):
            return None
        length = data[pos + 1]
        pos += 2
        if length == 0xFF:
            if pos + 2 > len(data):
                return None
            length = int.from_bytes(data[pos:pos + 2], "big")
            pos += 2
        if tlv == 0x03:
            return data[pos:pos + length]
        pos += length
    return None


def tag_message(tag):
    """The NDEF message of a tag, or None."""
    if tag.kind == KIND_TYPE4:
        return tag.ndef
    memory = tag.memory
    if tag.kind == KIND_TYPE2:
        # Capability container in page 3, TLVs from page 4.
        if len(memory) > 16 and memory[12] == 0xE1:
            return find_tlv_message(memory, 16)
        return None
    if tag.kind == KIND_TYPE5:
        # 4-byte CC (E1/E2 ...), or 8 bytes when its size byte is 0.
        if len(memory) > 8 and memory[0] in (0xE1, 0xE2):
            start = 8 if memory[2] == 0 else 4
            return find_tlv_message(memory, start)
        return None
    if tag.kind == KIND_MIFARE_CLASSIC:
        return _mifare_message(tag)
    return None


def _mifare_message(tag):
    """NDEF on MIFARE Classic: the MAD in sector 0 lists which sectors hold
    NDEF (AID 0x03E1); their data blocks form one TLV area."""
    if 0 not in tag.sectors_read:
        return None
    mad = tag.memory[MIFARE_BLOCK:MIFARE_BLOCK * 3]
    layout = mifare_sector_layout(len(tag.memory))
    area = bytearray()
    for sector in range(1, min(16, len(layout))):
        aid = int.from_bytes(mad[2 + (sector - 1) * 2:4 + (sector - 1) * 2], "little")
        if aid != MAD_NDEF_AID:
            if area:
                break          # NDEF sectors are contiguous
            continue
        if sector not in tag.sectors_read:
            break
        first, count = layout[sector]
        start = first * MIFARE_BLOCK
        area += tag.memory[start:start + (count - 1) * MIFARE_BLOCK]
    if not area:
        return None
    return find_tlv_message(area)


def text_message(text, lang=b"en"):
    """An NDEF message with one text record."""
    payload = bytes([len(lang)]) + lang + text.encode("utf-8")
    if len(payload) < 256:
        return bytes([0xD1, 0x01, len(payload)]) + b"T" + payload
    return bytes([0xC1, 0x01]) + len(payload).to_bytes(4, "big") + b"T" + payload


def type2_image(message, uid, pages=135):
    """NTAG215-style memory holding message, for simulated scans."""
    memory = bytearray(pages * 4)
    memory[0:3] = uid[:3]
    memory[12:16] = bytes([0xE1, 0x10, (pages - 4) * 4 // 8, 0x00])
    if len(message) < 0xFF:
        area = bytes([0x03, len(message)]) + message + bytes([0xFE])
    else:
        area = bytes([0x03, 0xFF]) + len(message).to_bytes(2, "big") + message + bytes([0xFE])
    memory[16:16 + len(area)] = area[:len(memory) - 16]
    return bytes(memory)


# URIs as QR codes carry them: "scheme://..." or a web+ scheme (Spoolman's
# "web+spoolman:s-12"). "PLA:FF0000" is text, not a URI.
_URI = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://|web\+[a-z]+:)\S+$", re.I)


def text_records(text):
    """Records for text that did not come from NDEF (a QR code), so the
    decoders can treat it like a tag: a URI record for a URI, a JSON record
    for a JSON object, otherwise a text record."""
    text = text.strip()
    if _URI.match(text):
        return [NdefRecord(TNF_WELL_KNOWN, b"U", b"", b"\x00" + text.encode("utf-8"))]
    if text.startswith("{"):
        return [NdefRecord(TNF_MIME, b"application/json", b"", text.encode("utf-8"))]
    return [NdefRecord(TNF_WELL_KNOWN, b"T", b"", b"\x02en" + text.encode("utf-8"))]


def tag_records(tag):
    if tag.kind == KIND_QR:
        return text_records(tag.memory.decode("utf-8", "replace"))
    message = tag_message(tag)
    return parse_records(message) if message else []
