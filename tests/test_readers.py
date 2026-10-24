import ctypes
import unittest

import helpers  # noqa: F401  (sets sys.path)

from kilnnfc.readers import base, ccid, pn532, rc522
from kilnnfc.readers.base import KeySet
from kilnnfc.tag import KIND_MIFARE_CLASSIC, KIND_TYPE2
from kilnnfc.transport import usbfs


class FakeCard(base.CardSession):
    """A tag in a fake reader: memory plus per-sector keys."""

    def __init__(self, memory, sak=0x00, keys=None):
        self.uid = b"\x01\x02\x03\x04"
        self.sak = sak
        self.memory = memory
        self.keys = keys or {}
        self.authed = None
        self.auth_attempts = 0

    def type2_read(self, page):
        if page * 4 >= len(self.memory):
            return None
        return (self.memory + self.memory)[page * 4:page * 4 + 16]

    def mifare_auth(self, block, key, key_b=False):
        self.auth_attempts += 1
        sector = block // 4
        self.authed = sector if self.keys.get(sector) == key else None
        return self.authed is not None

    def mifare_read(self, block):
        if self.authed != block // 4:
            return None
        return self.memory[block * 16:block * 16 + 16]

    def reselect(self):
        return True


class ReadTest(unittest.TestCase):
    def test_type2_stops_at_cc_size(self):
        memory = bytearray(180)
        memory[12:16] = b"\xe1\x10\x12\x00"    # NTAG213: 144 data bytes
        tag = base.read_type2(FakeCard(bytes(memory)))
        self.assertEqual((tag.kind, len(tag.memory)), (KIND_TYPE2, 160))

    def test_mifare_vendor_keys_then_defaults(self):
        vendor = bytes.fromhex("A1A2A3A4A5A6")
        keys = {s: b"\xff" * 6 for s in range(16)}
        keys[1] = vendor
        memory = bytes(range(256)) * 4
        card = FakeCard(memory, sak=0x08, keys=keys)
        keysets = [KeySet("Other", [b"\x99" * 6] * 16), KeySet("Vendor", [None, vendor] + [None] * 14)]
        tag = base.read_mifare_classic(card, keysets)
        self.assertEqual(tag.kind, KIND_MIFARE_CLASSIC)
        self.assertEqual(tag.sectors_read, set(range(16)))
        self.assertEqual(tag.sector_keys[1], "Vendor")
        self.assertEqual(tag.sector_keys[0], "default")
        self.assertFalse(tag.incomplete)
        # Data blocks are read; sector trailers are skipped and stay zero.
        for sector in range(16):
            start = sector * 64
            self.assertEqual(tag.memory[start:start + 48], memory[start:start + 48])
            self.assertEqual(tag.memory[start + 48:start + 64], b"\x00" * 16)

    def test_mifare_trailers_not_read(self):
        card = FakeCard(b"\x55" * 1024, sak=0x08, keys={s: b"\xff" * 6 for s in range(16)})
        reads = []
        card_read = card.mifare_read
        card.mifare_read = lambda block: reads.append(block) or card_read(block)
        base.read_mifare_classic(card, [])
        self.assertEqual(len(reads), 16 * 3)
        self.assertFalse(any(block % 4 == 3 for block in reads))

    def test_mifare_unreadable_sectors_are_zero(self):
        keys = {0: b"\xff" * 6}
        card = FakeCard(b"\x55" * 1024, sak=0x08, keys=keys)
        tag = base.read_mifare_classic(card, [])
        self.assertEqual(tag.sectors_read, {0})
        self.assertEqual(tag.memory[64:80], b"\x00" * 16)
        self.assertFalse(tag.incomplete)

    def test_mifare_card_leaving_marks_tag_incomplete(self):
        # Sectors 0 and 1 open, then the card leaves the field.
        card = FakeCard(b"\x55" * 1024, sak=0x08, keys={0: b"\xff" * 6, 1: b"\xff" * 6})
        card.reselect = lambda: False
        tag = base.read_mifare_classic(card, [])
        self.assertEqual(tag.sectors_read, {0, 1})
        self.assertTrue(tag.incomplete)


class IoctlTest(unittest.TestCase):
    def test_generic_layout(self):
        # x86/ARM: dir bits at 30, READ|WRITE = 3.
        size = ctypes.sizeof(usbfs._BulkTransfer)
        expected = (3 << 30) | (size << 16) | (ord("U") << 8) | 2
        if usbfs._IOC_SIZEBITS == 14:
            self.assertEqual(usbfs.USBDEVFS_BULK, expected)
            self.assertEqual(usbfs.USBDEVFS_CLAIMINTERFACE, 0x8004550F)

    def test_mips_layout(self):
        # MIPS: dir bits at 29, READ=2 WRITE=4; CLAIMINTERFACE is _IOR.
        value = (2 << 29) | (4 << 16) | (ord("U") << 8) | 15
        self.assertEqual(value, 0x4004550F)


class CcidTest(unittest.TestCase):
    def test_classify(self):
        def atr(standard, name):
            return bytes.fromhex("3B8F8001804F0CA000000306%02X%04X00000000" % (standard, name))
        self.assertEqual(ccid.classify_atr(atr(0x03, 0x0001)), ("iso14443a", 0x08))
        self.assertEqual(ccid.classify_atr(atr(0x03, 0x0003)), ("iso14443a", 0x00))
        self.assertEqual(ccid.classify_atr(atr(0x0B, 0x0000))[0], "iso15693")
        self.assertEqual(ccid.classify_atr(bytes.fromhex("3B8180018080")), ("iso14443a", 0x20))
        self.assertEqual(ccid.classify_atr(bytes.fromhex("3B9F958131FE9F006576")), (None, None))


class FakeAcr122uSlot(object):
    """An ACR122U with an NTAG216: READ BINARY fails from page 44 on, the
    PN532 passthrough works everywhere."""

    def __init__(self, memory):
        self.memory = memory

    def apdu(self, cmd):
        ok = b"\x90\x00"
        if cmd[:2] == b"\xff\xca":
            return b"\x53\xa1\xe8\xbf\x52\x00\x01" + ok
        if cmd[:2] == b"\xff\xb0":
            page, length = cmd[3], cmd[4]
            if page >= 44:
                return b"\x6a\x82"
            return self.memory[page * 4:page * 4 + length] + ok
        if cmd[:8] == b"\xff\x00\x00\x00\x04\xd4\x42\x30":
            page = cmd[8]
            return b"\xd5\x43\x00" + self.memory[page * 4:page * 4 + 16] + ok
        return b"\x6d\x00"


class Acr122uTest(unittest.TestCase):
    def test_ntag216_beyond_page_44(self):
        from helpers import mime_record, type2_tag
        from kilnnfc import decoders
        payload = (b'{"protocol":"openspool","version":"1.0","brand":"SUNLU","type":"PLA",'
                   b'"color_hex":"0022FF","min_temp":"190","max_temp":"230",'
                   b'"bed_min_temp":"50","bed_max_temp":"60"}')
        image = type2_tag(mime_record("application/json", payload), pages=231).memory
        atr = bytes.fromhex("3B8F8001804F0CA000000306030003000000006A")
        session = ccid.CcidSession(FakeAcr122uSlot(image), atr)
        tag = base.read_type2(session, "fake")
        filament, _ = decoders.decode(tag)
        self.assertEqual((filament.fmt, filament.brand, filament.material, filament.color),
                         ("OpenSpool", "SUNLU", "PLA", "0022FF"))
        # Stops after the NDEF message instead of reading all 231 pages.
        self.assertLess(len(tag.memory), 300)


class FakeAcr122uMifare(object):
    """ACR122U with a MIFARE Classic card: PC/SC auth always fails (like the
    real reader once the card is halted), the PN532 passthrough works."""

    def __init__(self, uid, memory, keys_a):
        self.uid = uid
        self.memory = memory
        self.keys_a = keys_a          # sector -> key
        self.authed = None
        self.halted = False

    def apdu(self, cmd):
        ok = b"\x90\x00"
        if cmd[:2] == b"\xff\xca":
            return self.uid + ok
        if cmd[:2] in (b"\xff\x82", b"\xff\x86"):
            return b"\x63\x00"
        if cmd[:5] == b"\xff\x00\x00\x00" + bytes([len(cmd) - 5]) and cmd[5] == 0xD4:
            pn = cmd[6]
            body = cmd[7:]
            if pn == 0x02:
                return b"\xd5\x03\x32\x01\x06\x07" + ok
            if pn == 0x4A:
                self.halted = False
                self.authed = None
                return b"\xd5\x4b\x01\x01\x00\x04\x08\x04" + self.uid + ok
            if pn == 0x40 and not self.halted:
                op, block = body[1], body[2]
                if op in (0x60, 0x61):
                    sector = block // 4
                    if body[3:9] == self.keys_a.get(sector) and body[9:13] == self.uid:
                        self.authed = sector
                        return b"\xd5\x41\x00" + ok
                    self.halted = True
                    return b"\xd5\x41\x14" + ok
                if op == 0x30 and self.authed == block // 4:
                    return b"\xd5\x41\x00" + self.memory[block * 16:block * 16 + 16] + ok
            return b"\xd5\x41\x01" + ok
        return b"\x6d\x00"


class Acr122uMifareTest(unittest.TestCase):
    def test_snapmaker_tag_through_pn532(self):
        from kilnnfc import decoders
        from kilnnfc.decoders import vendors
        uid = bytes.fromhex("012772F0")
        keyset = vendors.snapmaker_keysets(uid, {})[0]
        memory = bytearray(1024)
        memory[16:25] = b"Snapmaker"
        memory[66] = 1              # PLA
        memory[68] = 4              # Silk
        memory[72] = 1              # one color
        memory[80:83] = b"\x12\x34\x56"
        memory[128:130] = (175).to_bytes(2, "little")
        memory[130:132] = (500).to_bytes(2, "little")
        memory[160:168] = b"20260312"
        slot = FakeAcr122uMifare(uid, bytes(memory), {s: keyset.keys_a[s] for s in range(16)})
        atr = bytes.fromhex("3B8F8001804F0CA000000306030001000000006A")
        session = ccid.CcidSession(slot, atr)
        tag = base.read_mifare_classic(session, decoders.mifare_keysets(uid, {}))
        self.assertEqual(tag.sectors_read, set(range(16)))
        filament, _ = decoders.decode(tag)
        self.assertEqual((filament.fmt, filament.material, filament.modifiers, filament.color,
                          filament.weight, filament.diameter),
                         ("Snapmaker", "PLA", ["Silk"], "123456", 500, 1.75))
        self.assertEqual(filament.manufactured, "2026-03-12")


class FakeAcr122uReaderSlot(FakeAcr122uMifare):
    """The MIFARE card above in a CCID slot, with the reader's escapes
    recorded. leave_after: the card leaves after that many successful
    authentications."""

    bulk_out = type("Endpoint", (), {"address": 0x02})()
    slot = 0

    def __init__(self, uid, memory, keys_a, leave_after=None):
        FakeAcr122uMifare.__init__(self, uid, memory, keys_a)
        self.escapes = []
        self.leave_after = leave_after
        self.auths = 0
        self.gone = False

    def icc_status(self):
        return ccid.ICC_ABSENT if self.gone else ccid.ICC_PRESENT_ACTIVE

    def power_on(self):
        return bytes.fromhex("3B8F8001804F0CA000000306030001000000006A")

    def power_off(self):
        pass

    def escape(self, command):
        self.escapes.append(bytes(command))
        return b""

    def apdu(self, cmd):
        if self.gone:
            if cmd[5:7] == b"\xd4\x4a":
                return b"\xd5\x4b\x00\x90\x00"     # no target
            return b"\x63\x00"
        resp = FakeAcr122uMifare.apdu(self, cmd)
        if cmd[5:7] == b"\xd4\x40" and cmd[8] in (0x60, 0x61) and resp[2] == 0x00:
            self.auths += 1
            if self.leave_after is not None and self.auths >= self.leave_after:
                self.gone = True
        return resp


class Acr122uBeepTest(unittest.TestCase):
    def reader(self, leave_after=None):
        from kilnnfc import decoders
        uid = bytes.fromhex("40F7EA3E")
        keyset = decoders.mifare_keysets(uid, {})
        snapmaker = [k for k in keyset if k.name == "Snapmaker"][0]
        slot = FakeAcr122uReaderSlot(uid, bytes(1024), {s: snapmaker.keys_a[s] for s in range(16)},
                                     leave_after)
        device = type("Device", (), {"vid": 0x072F, "pid": 0x2200, "describe": lambda self: "ACR122U"})()
        reader = ccid.CcidReader(device, lambda u: decoders.mifare_keysets(u, {}))
        reader.handle = object()
        reader.slots = [slot]
        reader._buzzer = slot
        return reader, slot

    def test_beeps_once_after_a_full_read(self):
        reader, slot = self.reader()
        tag = reader.poll()
        self.assertEqual(len(tag.sectors_read), 16)
        self.assertFalse(tag.incomplete)
        self.assertEqual(slot.escapes, [ccid.ACR122U_BEEP_DONE])
        self.assertIsNone(reader.poll())    # still the same card

    def test_tag_leaving_mid_read(self):
        reader, slot = self.reader(leave_after=3)
        tag = reader.poll()
        self.assertTrue(tag.incomplete)
        self.assertEqual(slot.escapes, [ccid.ACR122U_BEEP_TOO_SOON])
        # Put back on the reader, the tag is read again.
        slot.gone, slot.leave_after = False, None
        tag = reader.poll()
        self.assertEqual(len(tag.sectors_read), 16)
        self.assertFalse(tag.incomplete)


class FakePort(object):
    def __init__(self, replies):
        self.replies = list(replies)
        self.written = []
        self.buffer = b""

    def write(self, data, timeout=1.0):
        self.written.append(bytes(data))
        if self.replies:
            self.buffer += self.replies.pop(0)

    def read(self, size, timeout):
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def flush_input(self):
        pass


def pn532_frame(body):
    body = b"\xd5" + body
    return (b"\x00\x00\xff" + bytes([len(body), (-len(body)) & 0xFF]) + body
            + bytes([(-sum(body)) & 0xFF, 0x00]))


class Pn532Test(unittest.TestCase):
    def test_command_round_trip(self):
        port = FakePort([pn532._ACK + pn532_frame(b"\x03\x32\x01\x06\x07")])
        chip = pn532.Pn532(port)
        self.assertEqual(chip.command(pn532.CMD_GET_FIRMWARE_VERSION), b"\x32\x01\x06\x07")
        frame = port.written[-1]
        self.assertEqual(frame[:5], b"\x00\x00\xff\x02\xfe")
        self.assertEqual(frame[5:7], b"\xd4\x02")

    def test_garbage_before_frame(self):
        port = FakePort([b"\x00\xff\x12" + pn532._ACK + b"\x00" + pn532_frame(b"\x4b\x00")])
        self.assertEqual(pn532.Pn532(port).command(pn532.CMD_IN_LIST_PASSIVE_TARGET, b"\x01\x00"),
                         b"\x00")

    def test_wake_sends_sam_configuration_with_wakeup(self):
        # A PN532 fresh from power-up only answers SAMConfiguration, and only
        # when it arrives in the same transmission as the 0x55 run.
        port = FakePort([pn532._ACK + pn532_frame(b"\x15")])
        pn532.Pn532(port).wake()
        frame = port.written[-1]
        self.assertTrue(frame.startswith(pn532._WAKEUP))
        self.assertEqual(frame[len(pn532._WAKEUP) + 5:len(pn532._WAKEUP) + 7], b"\xd4\x14")

    def test_no_ack(self):
        port = FakePort([b""])
        with self.assertRaises(pn532.Pn532Error):
            pn532.Pn532(port).command(pn532.CMD_GET_FIRMWARE_VERSION, timeout=0.05)


class FakeRc522Bridge(object):
    """An MFRC522 behind the USB bridge's TRANSFER command, with one card:
    an NTAG with a 7-byte UID (two cascade levels) that also answers MIFARE
    authentication with KEY."""

    KEY = bytes.fromhex("A0A1A2A3A4A5")

    def __init__(self, memory, uid, version=0x92):
        self.memory = memory
        self.uid = uid
        self.regs = {rc522.REG_VERSION: version}
        self.fifo = bytearray()
        self.answer_bits = 0
        self.authent_frames = []

    def open(self):
        pass

    def close(self):
        pass

    def transfer(self, tx):
        addr = tx[0]
        reg = (addr >> 1) & 0x3F
        if not addr & 0x80:
            for value in tx[1:]:
                self._write(reg, value)
            return bytes(len(tx))
        # Byte i read back belongs to the address clocked out as byte i-1.
        return bytes([0] + [self._read((a >> 1) & 0x3F) for a in tx[:-1]])

    def _read(self, reg):
        if reg == rc522.REG_FIFO_DATA:
            return self.fifo.pop(0) if self.fifo else 0
        if reg == rc522.REG_FIFO_LEVEL:
            return len(self.fifo)
        if reg == rc522.REG_CONTROL:
            return self.answer_bits
        if reg == rc522.REG_COMMAND:
            return 0
        return self.regs.get(reg, 0)

    def _write(self, reg, value):
        if reg == rc522.REG_FIFO_DATA:
            self.fifo.append(value)
        elif reg == rc522.REG_FIFO_LEVEL:
            if value & 0x80:
                self.fifo = bytearray()
        elif reg == rc522.REG_COM_IRQ:
            if not value & 0x80:
                self.regs[reg] = self.regs.get(reg, 0) & ~value
        elif reg == rc522.REG_BIT_FRAMING:
            self.regs[reg] = value
            if value & 0x80 and self.regs.get(rc522.REG_COMMAND) == rc522.CMD_TRANSCEIVE:
                self._transceive(bytes(self.fifo), value & 0x07)
        elif reg == rc522.REG_COMMAND:
            self.regs[reg] = value
            if value == rc522.CMD_MF_AUTHENT:
                frame = bytes(self.fifo)
                self.authent_frames.append(frame)
                self.fifo = bytearray()
                ok = frame[2:8] == self.KEY and frame[8:12] == self.uid[-4:]
                if ok:
                    self.regs[rc522.REG_STATUS2] = rc522.STATUS2_CRYPTO1_ON
                self.regs[rc522.REG_COM_IRQ] = rc522.IRQ_IDLE if ok else rc522.IRQ_TIMER
        else:
            self.regs[reg] = value

    def _transceive(self, frame, tx_bits):
        self.fifo = bytearray()
        field = self.regs.get(rc522.REG_TX_CONTROL, 0) & 0x03 == 0x03
        answer = None
        if field and tx_bits == 7 and frame == bytes([rc522.WUPA]):
            answer = b"\x44\x00"
        elif field and tx_bits == 0:
            answer = self._card(frame)
        self.answer_bits = 0
        if answer is None:
            self.regs[rc522.REG_COM_IRQ] = rc522.IRQ_TIMER
            return
        self.fifo += answer
        self.regs[rc522.REG_COM_IRQ] = rc522.IRQ_RX | rc522.IRQ_IDLE

    def _card(self, frame):
        def crc(data):
            return data + rc522.crc_a(data)
        uid = self.uid
        cl1 = b"\x88" + uid[:3]
        cl2 = uid[3:7]
        for cascade, part, sak in ((0x93, cl1, 0x04), (0x95, cl2, 0x00)):
            bcc = bytes([part[0] ^ part[1] ^ part[2] ^ part[3]])
            if frame == bytes([cascade, 0x20]):
                return part + bcc
            if frame == crc(bytes([cascade, 0x70]) + part + bcc):
                return crc(bytes([sak]))
        if len(frame) == 4 and frame[0] == 0x30 and frame == crc(frame[:2]):
            page = frame[1]
            return crc(bytes(self.memory[page * 4:page * 4 + 16]))
        return None


class Rc522Test(unittest.TestCase):
    UID = bytes.fromhex("04112233445566")

    def test_crc_a(self):
        self.assertEqual(rc522.crc_a(b"\x30\x00"), b"\x02\xa8")

    def test_register_spi_bytes(self):
        bridge = FakeRc522Bridge(b"", self.UID)
        chip = rc522.Rc522(bridge)
        self.assertEqual(chip.read_register(rc522.REG_VERSION), 0x92)
        chip.write_register(rc522.REG_MODE, 0x3D)
        self.assertEqual(bridge.regs[rc522.REG_MODE], 0x3D)

    def test_reads_ntag(self):
        from helpers import mime_record, type2_tag
        from kilnnfc import decoders
        payload = (b'{"protocol":"openspool","version":"1.0","brand":"SUNLU","type":"PLA",'
                   b'"color_hex":"0022FF","min_temp":"190","max_temp":"230"}')
        image = type2_tag(mime_record("application/json", payload), uid=self.UID).memory
        reader = rc522.Rc522Reader(FakeRc522Bridge(image, self.UID))
        reader.open()
        self.assertTrue(reader.describe().startswith("RC522 2.0"))
        tag = reader.poll()
        self.assertEqual(tag.uid, self.UID)
        filament, _ = decoders.decode(tag)
        self.assertEqual((filament.fmt, filament.brand, filament.color),
                         ("OpenSpool", "SUNLU", "0022FF"))
        # The same card again is not read twice; the field is off between polls.
        self.assertIsNone(reader.poll())
        self.assertEqual(reader.transport.regs[rc522.REG_TX_CONTROL] & 0x03, 0)

    def test_mifare_authenticate(self):
        bridge = FakeRc522Bridge(b"", self.UID)
        chip = rc522.Rc522(bridge)
        self.assertFalse(chip.mifare_authenticate(bytes(6), False, 4, self.UID[-4:]))
        self.assertTrue(chip.mifare_authenticate(bridge.KEY, True, 4, self.UID[-4:]))
        self.assertEqual(bridge.authent_frames[-1][:2], b"\x61\x04")

    def test_no_rc522_on_bridge(self):
        reader = rc522.Rc522Reader(FakeRc522Bridge(b"", self.UID, version=0x00))
        with self.assertRaises(base.ReaderError):
            reader.open()


if __name__ == "__main__":
    unittest.main()
