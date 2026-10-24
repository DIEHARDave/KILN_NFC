"""USB CCID (PC/SC class) contactless readers, driven directly over usbfs
instead of through pcscd: ACS ACR1552U, ACR122U, ACR1252U, ACR1255U and
other readers that implement the PC/SC Part 3 storage-card commands.

The reader activates the card itself; the card type comes from the PC/SC
Part 3 ATR it builds (RID A0 00 00 03 06, then standard and card name), and
tag memory is read with the pseudo-APDUs every PC/SC contactless reader
supports: GET DATA (FF CA), READ BINARY (FF B0), LOAD KEY (FF 82) and
GENERAL AUTHENTICATE (FF 86)."""

import logging
import struct

from ..tag import TECH_ISO14443A, TECH_ISO15693
from ..transport import usbfs
from .base import CardSession, Reader, ReaderError

LOGGER = logging.getLogger("kiln_nfc")

CCID_CLASS = 0x0B

PC_TO_RDR_ICC_POWER_ON = 0x62
PC_TO_RDR_ICC_POWER_OFF = 0x63
PC_TO_RDR_GET_SLOT_STATUS = 0x65
PC_TO_RDR_XFR_BLOCK = 0x6F
PC_TO_RDR_ESCAPE = 0x6B
RDR_TO_PC_DATA_BLOCK = 0x80
RDR_TO_PC_SLOT_STATUS = 0x81

ICC_PRESENT_ACTIVE = 0
ICC_PRESENT_INACTIVE = 1
ICC_ABSENT = 2

PCSC_RID = b"\xa0\x00\x00\x03\x06"
# PC/SC Part 3 card names -> the SAK a card of that type reports.
PCSC_CARD_SAK = {
    0x0001: 0x08,   # MIFARE Classic 1K
    0x0002: 0x18,   # MIFARE Classic 4K
    0x0003: 0x00,   # MIFARE Ultralight / NTAG
    0x0026: 0x09,   # MIFARE Mini
    0x0036: 0x08,   # MIFARE Plus 2K (security level 1)
    0x0037: 0x18,   # MIFARE Plus 4K (security level 1)
    0x003A: 0x00,   # MIFARE Ultralight C
}

REMOVAL_MISSES = 2

# ACR122U: it beeps when it detects a card, long before a MIFARE Classic tag
# is read, so its detection beep is turned off and it beeps once a read is
# over instead. Pseudo-APDUs from the ACR122U API, sent as CCID escapes so
# they work with no card on the reader.
ACR122U = (0x072F, 0x2200)
ACR122U_DETECTION_BEEP_OFF = b"\xff\x00\x52\x00\x00"
ACR122U_DETECTION_BEEP_ON = b"\xff\x00\x52\xff\x00"
# LED/buzzer control FF 00 40 <LED> 04 <T1> <T2> <repeats> <buzzer>: blink
# green and beep once (100 ms), or blink red and beep three times.
ACR122U_BEEP_DONE = b"\xff\x00\x40\xa0\x04\x01\x01\x01\x01"
ACR122U_BEEP_TOO_SOON = b"\xff\x00\x40\x50\x04\x01\x01\x03\x01"


class CcidError(ReaderError):
    pass


def ccid_interfaces(device):
    return [i for i in device.interfaces if i.cls == CCID_CLASS]


def find_devices(match=""):
    """CCID readers, optionally only those whose name or VID:PID contains
    match (case-insensitive)."""
    match = match.lower()
    found = []
    for device in usbfs.list_devices():
        if not ccid_interfaces(device):
            continue
        if match and match not in device.describe().lower():
            continue
        found.append(device)
    return found


class CcidSlot(object):
    """One CCID slot on one claimed interface."""

    def __init__(self, handle, interface, slot):
        self.handle = handle
        self.slot = slot
        self.bulk_out = interface.endpoint("Bulk", False)
        self.bulk_in = interface.endpoint("Bulk", True)
        if self.bulk_out is None or self.bulk_in is None:
            raise CcidError("CCID interface %d has no bulk endpoints" % interface.number)
        self.max_message = 271
        for desc in interface.extra:
            if len(desc) >= 48 and desc[1] == 0x21:
                self.max_message = max(64, struct.unpack_from("<I", desc, 44)[0])
        packet = self.bulk_in.max_packet or 64
        self.read_size = -(-(self.max_message + 10) // packet) * packet
        self.seq = 0

    def transact(self, msg_type, data=b"", params=b"\x00\x00\x00", timeout_ms=3000):
        self.seq = (self.seq + 1) & 0xFF
        header = struct.pack("<BIBB", msg_type, len(data), self.slot, self.seq) + params
        try:
            self.handle.bulk_write(self.bulk_out.address, header + data, timeout_ms)
            while True:
                reply = self.handle.bulk_read(self.bulk_in.address, self.read_size, timeout_ms)
                if len(reply) < 10:
                    raise CcidError("short CCID reply")
                if reply[6] != self.seq:
                    continue  # a stale reply to an earlier, timed-out request
                status = reply[7]
                if status >> 6 == 2:
                    continue  # time extension requested; the reply follows
                length = struct.unpack_from("<I", reply, 1)[0]
                return reply[0], status, reply[8], reply[10:10 + length]
        except usbfs.UsbError as exc:
            raise CcidError(str(exc))

    def icc_status(self):
        _, status, _, _ = self.transact(PC_TO_RDR_GET_SLOT_STATUS)
        return status & 0x03

    def power_on(self):
        msg, status, error, atr = self.transact(PC_TO_RDR_ICC_POWER_ON)
        if status >> 6 != 0:
            return None
        return atr

    def power_off(self):
        try:
            self.transact(PC_TO_RDR_ICC_POWER_OFF)
        except CcidError:
            pass

    def apdu(self, command):
        msg, status, error, data = self.transact(PC_TO_RDR_XFR_BLOCK, bytes(command))
        if status >> 6 != 0:
            return None
        return data

    def escape(self, command):
        """A reader command (not for the card) as a CCID escape; the reply,
        or None when the reader refuses it."""
        msg, status, error, data = self.transact(PC_TO_RDR_ESCAPE, bytes(command))
        if status >> 6 != 0:
            return None
        return data


class CcidSession(CardSession):
    def __init__(self, slot, atr):
        self.slot = slot
        self.atr = atr
        self.tech, self.sak = classify_atr(atr)
        uid = self._ok(self.slot.apdu(b"\xff\xca\x00\x00\x00"))
        uid = uid or b""
        # Readers return ISO 15693 UIDs least significant byte first; show
        # them the usual way (starting E0).
        if self.tech == TECH_ISO15693 and len(uid) == 8 and uid[-1] == 0xE0:
            uid = uid[::-1]
        self.uid = uid
        self.atqa = b""
        self._key_loaded = None
        self._type2_method = None
        self._pn532_ok = None
        self._last_status = None

    @staticmethod
    def _ok(resp):
        if resp is not None and len(resp) >= 2 and resp[-2:] == b"\x90\x00":
            return resp[:-2]
        return None

    def _read_binary(self, block, length):
        resp = self.slot.apdu(bytes([0xFF, 0xB0, block >> 8, block & 0xFF, length]))
        data = self._ok(resp)
        if data is None:
            LOGGER.debug("kiln_nfc: READ BINARY %d: %s", block, resp.hex() if resp else "no reply")
        return data if data is not None and len(data) >= length else None

    def _read_page4(self, page):
        """16 bytes as four 4-byte READ BINARYs."""
        data = bytearray()
        for offset in range(4):
            chunk = self._read_binary(page + offset, 4)
            if chunk is None:
                return None
            data += chunk[:4]
        return bytes(data)

    def _read_passthrough(self, page):
        """NTAG READ sent through the ACR122U's PN532 (InCommunicateThru)."""
        resp = self._ok(self.slot.apdu(bytes([0xFF, 0x00, 0x00, 0x00, 0x04, 0xD4, 0x42, 0x30, page])))
        if resp is None or len(resp) < 19 or resp[:3] != b"\xd5\x43\x00":
            return None
        return resp[3:19]

    def type2_read(self, page):
        # The ACR122U refuses READ BINARY beyond an NTAG213's size (page 44
        # and up on an NTAG215/216), so fall back to smaller reads and then
        # to a raw READ, and stick with whichever worked.
        methods = [self._read_binary_16, self._read_page4, self._read_passthrough]
        if self._type2_method in methods:
            methods.remove(self._type2_method)
            methods.insert(0, self._type2_method)
        for method in methods:
            data = method(page)
            if data is not None:
                self._type2_method = method
                return data
        return None

    def _read_binary_16(self, page):
        return self._read_binary(page, 16)

    # PN532 passthrough (ACR122U) -----------------------------------------

    def _pn532(self, command, params=b""):
        """A PN532 command sent through the reader (direct transmit,
        FF 00 00 00); the response data after D5 <cmd+1>, or None."""
        body = bytes([0xD4, command]) + bytes(params)
        resp = self._ok(self.slot.apdu(bytes([0xFF, 0x00, 0x00, 0x00, len(body)]) + body))
        if resp is None or len(resp) < 2 or resp[0] != 0xD5 or resp[1] != command + 1:
            return None
        return resp[2:]

    def _has_pn532(self):
        """True when the reader is a PN532 underneath (ACR122U). Its PC/SC
        MIFARE commands cannot recover a card halted by a wrong key, so
        MIFARE Classic goes through the PN532 like libnfc does."""
        if self._pn532_ok is None:
            fw = self._pn532(0x02)                  # GetFirmwareVersion
            self._pn532_ok = bool(fw) and fw[0] == 0x32
            LOGGER.debug("kiln_nfc: CCID PN532 passthrough: %s", self._pn532_ok)
        return self._pn532_ok

    def _exchange(self, data):
        resp = self._pn532(0x40, b"\x01" + data)    # InDataExchange, target 1
        if resp is None or not resp or resp[0] & 0x3F:
            self._last_status = resp[0] if resp else None
            return None
        return resp[1:]

    # MIFARE Classic -------------------------------------------------------

    def mifare_auth(self, block, key, key_b=False):
        cmd = 0x61 if key_b else 0x60
        if self._has_pn532():
            return self._exchange(bytes([cmd, block]) + key + self.uid[-4:]) is not None
        if self._key_loaded != key:
            # Volatile key slot 0.
            resp = self.slot.apdu(b"\xff\x82\x00\x00\x06" + key)
            if self._ok(resp) is None:
                self._last_status = resp
                return False
            self._key_loaded = key
        resp = self.slot.apdu(bytes([0xFF, 0x86, 0x00, 0x00, 0x05, 0x01, 0x00, block, cmd, 0x00]))
        if self._ok(resp) is None:
            self._last_status = resp
            return False
        return True

    def mifare_read(self, block):
        if self._has_pn532():
            data = self._exchange(bytes([0x30, block]))
            return data[:16] if data is not None and len(data) >= 16 else None
        return self._read_binary(block, 16)

    def reselect(self):
        self._key_loaded = None
        if self._has_pn532():
            # InListPassiveTarget wakes the halted card again.
            resp = self._pn532(0x4A, b"\x01\x00")
            if resp and resp[0] >= 1 and len(resp) >= 6:
                return bytes(resp[6:6 + resp[5]]) == self.uid
            return False
        self.slot.power_off()
        atr = self.slot.power_on()
        if atr is None:
            return False
        return self._ok(self.slot.apdu(b"\xff\xca\x00\x00\x00")) is not None

    def describe_failure(self):
        """Last refused MIFARE response, for the log."""
        status = self._last_status
        if isinstance(status, bytes):
            return status.hex()
        return "PN532 status %s" % (("0x%02X" % status) if status is not None else "none")

    def iso15693_read(self, first_block, count):
        data = bytearray()
        for block in range(first_block, first_block + count):
            chunk = self._read_binary(block, 4)
            if chunk is None:
                return None if not data else bytes(data)
            data += chunk[:4]
        return bytes(data)

    def apdu(self, command):
        return self.slot.apdu(command)


def classify_atr(atr):
    """(tech, sak) for a contactless card's ATR, or (None, None) when the
    ATR is not one a contactless reader builds."""
    rid = atr.find(PCSC_RID)
    if rid >= 0 and len(atr) >= rid + 8:
        standard = atr[rid + 5]
        name = (atr[rid + 6] << 8) | atr[rid + 7]
        if 0x09 <= standard <= 0x0C:
            return TECH_ISO15693, None
        return TECH_ISO14443A, PCSC_CARD_SAK.get(name, 0x00)
    # ISO 14443-4 cards: 3B 8n 80 01 + historical bytes.
    if len(atr) >= 4 and atr[0] == 0x3B and atr[1] & 0xF0 == 0x80 and atr[2:4] == b"\x80\x01":
        return TECH_ISO14443A, 0x20
    return None, None


class CcidReader(Reader):
    def __init__(self, device, keysets_for_uid=None):
        Reader.__init__(self, keysets_for_uid)
        self.device = device
        self.handle = None
        self.slots = []
        self.name = "CCID %s" % device.describe()
        self._present = {}
        self._misses = {}
        self._buzzer = None

    def open(self):
        self.handle = usbfs.UsbHandle(self.device)
        try:
            self.handle.open()
            for interface in ccid_interfaces(self.device):
                self.handle.claim(interface.number)
                max_slot = 0
                for desc in interface.extra:
                    if len(desc) >= 5 and desc[1] == 0x21:
                        max_slot = desc[4]
                for slot in range(min(max_slot, 3) + 1):
                    self.slots.append(CcidSlot(self.handle, interface, slot))
            if not self.slots:
                raise CcidError("%s has no CCID interface" % self.device.describe())
            # Answering a slot status proves the reader is ours to talk to.
            for slot in self.slots:
                slot.icc_status()
        except (usbfs.UsbError, CcidError) as exc:
            self.close()
            raise CcidError("%s: %s" % (self.device.describe(), exc))
        if (self.device.vid, self.device.pid) == ACR122U:
            if self.slots[0].escape(ACR122U_DETECTION_BEEP_OFF) is not None:
                self._buzzer = self.slots[0]
            else:
                LOGGER.info("kiln_nfc: %s refused the buzzer setting; it beeps on detection",
                            self.device.describe())

    def close(self):
        if self.handle is not None:
            if self._buzzer is not None:
                try:
                    self._buzzer.escape(ACR122U_DETECTION_BEEP_ON)
                except CcidError:
                    pass
            self.handle.close()
        self.handle = None
        self.slots = []
        self._buzzer = None

    def signal(self, complete):
        if self._buzzer is None:
            return
        try:
            self._buzzer.escape(ACR122U_BEEP_DONE if complete else ACR122U_BEEP_TOO_SOON)
        except CcidError as exc:
            LOGGER.debug("kiln_nfc: ACR122U beep: %s", exc)

    def poll(self):
        if self.handle is None:
            raise ReaderError("CCID reader is not open")
        for slot in self.slots:
            key = (slot.bulk_out.address, slot.slot)
            if slot.icc_status() == ICC_ABSENT:
                self._misses[key] = self._misses.get(key, 0) + 1
                if self._misses[key] >= REMOVAL_MISSES:
                    self._present.pop(key, None)
                continue
            self._misses[key] = 0
            if self._present.get(key):
                continue  # still the card we already read
            atr = slot.power_on()
            if atr is None:
                continue
            tech, _ = classify_atr(atr)
            if tech is None:
                # A contact smart card in a combined reader; not a tag.
                self._present[key] = True
                slot.power_off()
                continue
            session = CcidSession(slot, atr)
            if not session.uid:
                slot.power_off()
                continue
            LOGGER.debug("kiln_nfc: CCID card ATR %s UID %s", atr.hex(), session.uid.hex())
            tag = self.read_card(session)
            slot.power_off()
            if tag is not None:
                self.signal(not tag.incomplete)
                # A tag that left mid-read is read again when it comes back.
                self._present[key] = not tag.incomplete
                return tag
        return None

    def forget(self):
        self._present = {}

    def describe(self):
        return self.name
