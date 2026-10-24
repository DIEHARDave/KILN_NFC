"""Reader interface, and tag reading shared by every reader.

A reader driver only finds a card and offers the primitives its hardware
has (CardSession). Turning those into a full memory dump - how many NTAG
pages there are, which MIFARE Classic keys open which sector, how big an
ISO 15693 tag is, how to find the NDEF file of a Type 4 tag - is the same
for every reader and lives here."""

import logging

from ..tag import (
    KIND_MIFARE_CLASSIC, KIND_TYPE2, KIND_TYPE4, KIND_TYPE5, MIFARE_BLOCK,
    TECH_ISO14443A, TECH_ISO15693, Tag, mifare_sector_layout,
)

LOGGER = logging.getLogger("kiln_nfc")

# Keys that open most blank or NDEF-formatted MIFARE Classic tags.
DEFAULT_MIFARE_KEYS = [
    bytes.fromhex("FFFFFFFFFFFF"),   # factory default
    bytes.fromhex("A0A1A2A3A4A5"),   # MAD sector key A
    bytes.fromhex("D3F7D3F7D3F7"),   # NDEF sector key A
    bytes.fromhex("000000000000"),
]

# Type 2 tags without a capability container are read up to an NTAG213's size.
TYPE2_PAGES_WITHOUT_CC = 45
TYPE2_MAX_PAGES = 231          # NTAG216
TYPE5_MAX_BLOCKS = 80          # ICODE SLIX2 (320 bytes)


class ReaderError(IOError):
    """The reader itself failed (unplugged, not answering); it is reopened."""


class KeySet(object):
    """MIFARE Classic keys for one tag family. keys_a/keys_b are lists of
    6-byte keys indexed by sector; a missing or None entry is not tried."""

    def __init__(self, name, keys_a, keys_b=None):
        self.name = name
        self.keys_a = list(keys_a)
        self.keys_b = list(keys_b or [])

    def keys_for(self, sector):
        keys = []
        for key_b, table in ((False, self.keys_a), (True, self.keys_b)):
            if sector < len(table) and table[sector]:
                keys.append((key_b, bytes(table[sector])))
        return keys

    @classmethod
    def uniform(cls, name, key, sectors=40):
        return cls(name, [key] * sectors, [key] * sectors)


class CardSession(object):
    """The card currently selected by a reader. Drivers implement what their
    hardware supports; the rest returns None (unsupported)."""

    uid = b""
    atqa = b""
    sak = None
    tech = TECH_ISO14443A

    def type2_read(self, page):
        """16 bytes (4 pages) from page, or None."""
        return None

    def mifare_auth(self, block, key, key_b=False):
        """True when key opens block's sector."""
        return False

    def mifare_read(self, block):
        """16 bytes of an authenticated block, or None."""
        return None

    def reselect(self):
        """Select the card again after a failed MIFARE authentication left it
        halted. False when the card is gone."""
        return False

    def iso15693_read(self, first_block, count):
        """count blocks from first_block as bytes, or None."""
        return None

    def iso15693_system_info(self):
        """(block_count, block_size) or None."""
        return None

    def apdu(self, command):
        """ISO 7816 APDU exchange for ISO-DEP cards; response with SW1 SW2."""
        return None


class Reader(object):
    """A connected reader. poll() returns a fully read Tag when a card is
    newly presented and None otherwise; it raises ReaderError when the
    reader stops working."""

    name = "reader"

    def __init__(self, keysets_for_uid=None):
        # Callable(uid) -> [KeySet], from the decoders that read MIFARE Classic.
        self.keysets_for_uid = keysets_for_uid or (lambda uid: [])

    def open(self):
        raise NotImplementedError

    def close(self):
        pass

    def poll(self):
        raise NotImplementedError

    def forget(self):
        """Treat the card on the reader as new, so the next poll reads it
        again."""
        self._present = None

    def describe(self):
        return self.name

    def signal(self, complete):
        """Tell whoever holds the tag that reading is over (complete) or
        that the tag left too soon. Readers with a buzzer beep."""
        pass

    # Shared tag reading, for drivers ------------------------------------

    def read_card(self, session):
        """Read everything a session offers into a Tag."""
        if session.tech == TECH_ISO15693:
            return read_type5(session, self.name)
        sak = session.sak if session.sak is not None else 0
        if sak & 0x08 or sak in (0x01, 0x09, 0x10, 0x11, 0x18, 0x19, 0x28, 0x38):
            return read_mifare_classic(session, self.keysets_for_uid(session.uid),
                                       self.name)
        if sak & 0x20:
            return read_type4(session, self.name)
        return read_type2(session, self.name)


def read_type2(session, reader_name=""):
    first = session.type2_read(0)
    memory = bytearray()
    if first is not None and len(first) >= 16:
        memory += first[:16]
        cc = first[12:16]
        if cc[0] == 0xE1 and cc[2]:
            pages = min(4 + cc[2] * 8 // 4, TYPE2_MAX_PAGES)
        else:
            pages = TYPE2_PAGES_WITHOUT_CC
        page = 4
        while page < pages:
            chunk = session.type2_read(page)
            if chunk is None or len(chunk) < 16:
                break
            # MIFARE Ultralight wraps around past its last page.
            if page >= 16 and chunk[:16] == bytes(memory[:16]):
                break
            memory += chunk[:16]
            page += 4
            # Stop once a complete NDEF message is in; NTAG216 tags are
            # mostly empty and slow readers take seconds for the rest. The
            # first 176 bytes (an NTAG213) are always read for the vendor
            # formats that keep fixed-offset data there.
            end = _ndef_end(memory)
            if end is not None and len(memory) >= max(end, 176):
                break
        del memory[pages * 4:]
    elif first is None:
        return None
    return Tag(session.uid, TECH_ISO14443A, KIND_TYPE2, memory,
               atqa=session.atqa, sak=session.sak, reader=reader_name)


def _ndef_end(memory):
    """Offset just past the NDEF TLV of a Type 2 memory image, or None."""
    pos = 16
    while pos + 1 < len(memory) and memory[pos] == 0x00:
        pos += 1          # NULL TLVs
    if pos + 1 >= len(memory) or memory[pos] != 0x03:
        return None
    length = memory[pos + 1]
    if length == 0xFF:
        if pos + 4 > len(memory):
            return None
        return pos + 4 + int.from_bytes(memory[pos + 2:pos + 4], "big")
    return pos + 2 + length


def read_mifare_classic(session, keysets, reader_name=""):
    sak = session.sak or 0
    if sak in (0x18, 0x38):
        size = 4096
    elif sak in (0x09,):
        size = 320            # MIFARE Mini
    else:
        size = 1024
    memory = bytearray(size)
    tag = Tag(session.uid, TECH_ISO14443A, KIND_MIFARE_CLASSIC, b"",
              atqa=session.atqa, sak=session.sak, reader=reader_name)
    defaults = KeySet("default", [], [])
    default_keys = [(False, key) for key in DEFAULT_MIFARE_KEYS] + \
                   [(True, key) for key in DEFAULT_MIFARE_KEYS[:1]]

    # Every failed authentication halts the card and costs a reselect, so
    # find which family's keys fit on sector 1 (where every supported vendor
    # format keeps data) and try that family first everywhere.
    ordered = list(keysets)
    for keyset in keysets:
        hit = _auth_sector(session, 1, keyset.keys_for(1))
        if hit is None:
            return None
        if hit:
            ordered.remove(keyset)
            ordered.insert(0, keyset)
            break

    card_gone = False
    for sector, (first_block, count) in enumerate(mifare_sector_layout(size)):
        candidates = []
        for keyset in ordered:
            candidates.extend((keyset, k) for k in keyset.keys_for(sector))
            # Vendor formats leave sectors other than their own at the
            # factory key, so after the vendor's key try the defaults.
            if keyset is ordered[0]:
                break
        candidates.extend((defaults, k) for k in default_keys)
        opened = None
        for keyset, (key_b, key) in candidates:
            result = session.mifare_auth(first_block, key, key_b)
            if result:
                opened = keyset
                break
            if not session.reselect():
                card_gone = True
                break
        if card_gone:
            tag.incomplete = True
            break
        if opened is None:
            continue
        # The sector trailer holds only keys and access bits; skipping it
        # saves a quarter of the reads while the tag has to stay put.
        data = bytearray()
        for block in range(first_block, first_block + count - 1):
            chunk = session.mifare_read(block)
            if chunk is None or len(chunk) < MIFARE_BLOCK:
                data = None
                break
            data += chunk[:MIFARE_BLOCK]
        if data is None:
            if not session.reselect():
                tag.incomplete = True
                break
            continue
        start = first_block * MIFARE_BLOCK
        memory[start:start + len(data)] = data
        tag.sectors_read.add(sector)
        tag.sector_keys[sector] = opened.name
    tag.memory = bytes(memory)
    LOGGER.debug("kiln_nfc: MIFARE Classic sectors read: %s", sorted(tag.sector_keys.items()))
    if tag.incomplete:
        LOGGER.info("kiln_nfc: tag %s left the reader after %d sectors",
                    tag.uid_hex, len(tag.sectors_read))
    elif not tag.sectors_read:
        detail = session.describe_failure() if hasattr(session, "describe_failure") else ""
        LOGGER.info("kiln_nfc: no MIFARE Classic sector opened with %s (%s)",
                    ", ".join(k.name for k in keysets) or "the default keys", detail)
    return tag


def _auth_sector(session, sector, keys):
    """True/False for whether one of keys opens sector, None if the card
    disappeared."""
    first_block = mifare_sector_layout(1024)[sector][0]
    for key_b, key in keys:
        if session.mifare_auth(first_block, key, key_b):
            return True
        if not session.reselect():
            return None
    return False


def read_type5(session, reader_name=""):
    info = session.iso15693_system_info()
    block_count, block_size = info if info else (TYPE5_MAX_BLOCKS, 4)
    block_count = min(block_count, 256)
    memory = bytearray()
    step = 8
    block = 0
    while block < block_count:
        count = min(step, block_count - block)
        chunk = session.iso15693_read(block, count)
        if chunk is None and count > 1:
            step = 1          # read multiple blocks is optional in ISO 15693
            continue
        if chunk is None:
            break
        memory += chunk
        block += count
    tag = Tag(session.uid, TECH_ISO15693, KIND_TYPE5, memory,
              block_size=block_size, reader=reader_name)
    return tag


def read_type4(session, reader_name=""):
    """NDEF file of an NFC Forum Type 4 tag (ISO-DEP, e.g. DESFire)."""
    tag = Tag(session.uid, TECH_ISO14443A, KIND_TYPE4, b"",
              atqa=session.atqa, sak=session.sak, reader=reader_name)

    def ok(resp):
        return resp is not None and len(resp) >= 2 and resp[-2:] == b"\x90\x00"

    if not ok(session.apdu(bytes.fromhex("00A4040007D276000085010100"))):
        return tag
    if not ok(session.apdu(bytes.fromhex("00A4000C02E103"))):
        return tag
    cc = session.apdu(bytes.fromhex("00B000000F"))
    if not ok(cc) or len(cc) < 17:
        return tag
    max_read = max(1, min(int.from_bytes(cc[3:5], "big"), 0xF0))
    file_id = cc[9:11]
    if not ok(session.apdu(bytes.fromhex("00A4000C02") + file_id)):
        return tag
    head = session.apdu(bytes.fromhex("00B0000002"))
    if not ok(head):
        return tag
    length = int.from_bytes(head[:2], "big")
    ndef = bytearray()
    offset = 2
    while len(ndef) < length:
        chunk = min(max_read, length - len(ndef))
        resp = session.apdu(bytes([0x00, 0xB0, offset >> 8, offset & 0xFF, chunk]))
        if not ok(resp):
            break
        ndef += resp[:-2]
        offset += len(resp) - 2
    tag.ndef = bytes(ndef[:length])
    return tag
