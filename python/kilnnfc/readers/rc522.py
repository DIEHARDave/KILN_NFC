"""NXP MFRC522 ("RC522" modules) through the USB bridge.

The RC522 boards are wired for SPI, so on a printer they need the same USB
bridge as the PN5180 (bridge.py); this module drives the chip one register
access at a time over the bridge's TRANSFER command. ISO 14443A only:
NTAG / Ultralight and MIFARE Classic (the chip does Crypto1 itself). Like
the PN532 it cannot read ISO 15693 (OpenPrintTag), and ISO-DEP (Type 4)
framing is not implemented. The register use matches LaneTag's
RC522 driver.

Status: written from the MFRC522 datasheet and not yet run against
hardware; the bridge firmware is not part of this release."""

import logging
import time

from ..tag import TECH_ISO14443A
from .base import CardSession, Reader
from .bridge import BridgeError

LOGGER = logging.getLogger("kiln_nfc")

# Registers.
REG_COMMAND = 0x01
REG_COM_IRQ = 0x04
REG_ERROR = 0x06
REG_STATUS2 = 0x08
REG_FIFO_DATA = 0x09
REG_FIFO_LEVEL = 0x0A
REG_CONTROL = 0x0C
REG_BIT_FRAMING = 0x0D
REG_MODE = 0x11
REG_TX_MODE = 0x12
REG_RX_MODE = 0x13
REG_TX_CONTROL = 0x14
REG_TX_ASK = 0x15
REG_MOD_WIDTH = 0x24
REG_RF_CFG = 0x26
REG_T_MODE = 0x2A
REG_T_PRESCALER = 0x2B
REG_T_RELOAD_H = 0x2C
REG_T_RELOAD_L = 0x2D
REG_VERSION = 0x37

# Commands.
CMD_IDLE = 0x00
CMD_TRANSCEIVE = 0x0C
CMD_MF_AUTHENT = 0x0E
CMD_SOFT_RESET = 0x0F

IRQ_TIMER = 0x01
IRQ_IDLE = 0x10
IRQ_RX = 0x20
ERR_COLLISION = 0x08
ERR_FRAME = 0x13            # BufferOvfl, ParityErr, ProtocolErr
STATUS2_CRYPTO1_ON = 0x08

FIFO_SIZE = 64
# The timer runs at 13.56 MHz / (2 * 0xA9 + 1) = 40 kHz: 25 us a tick.
TIMER_PRESCALER = 0xA9
TICKS_PER_MS = 40
# Card answers to ISO 14443-3 and MIFARE commands come within a few ms.
CARD_TIMEOUT_MS = 25
WAKE_TIMEOUT_MS = 3

WUPA = 0x52
SEL_CL1 = 0x93
CASCADE_TAG = 0x88

# VersionReg values of MFRC522 chips and their common clones.
VERSIONS = {0x91: "1.0", 0x92: "2.0", 0x88: "FM17522", 0x89: "FM17522E",
            0xB2: "FM17522", 0x12: "clone"}

REMOVAL_MISSES = 2


class Rc522Error(IOError):
    """No or a bad answer from the card."""


def crc_a(data):
    """ISO 14443-3 CRC_A, low byte first."""
    crc = 0x6363
    for byte in data:
        byte ^= crc & 0xFF
        byte = (byte ^ (byte << 4)) & 0xFF
        crc = (crc >> 8) ^ (byte << 8) ^ (byte << 3) ^ (byte >> 4)
    return bytes([crc & 0xFF, crc >> 8])


class Rc522(object):
    """Register access and the chip's Transceive/MFAuthent commands."""

    def __init__(self, transport):
        self.t = transport

    def read_register(self, reg):
        return self.t.transfer(bytes([0x80 | (reg << 1), 0x00]))[1]

    def write_register(self, reg, data):
        if isinstance(data, int):
            data = bytes([data])
        self.t.transfer(bytes([reg << 1]) + bytes(data))

    def set_bits(self, reg, mask):
        self.write_register(reg, self.read_register(reg) | mask)

    def clear_bits(self, reg, mask):
        self.write_register(reg, self.read_register(reg) & ~mask & 0xFF)

    def read_fifo(self, count):
        # Each byte clocked out names the register again for the next read.
        if not count:
            return b""
        address = 0x80 | (REG_FIFO_DATA << 1)
        return bytes(self.t.transfer(bytes([address] * count + [0x00]))[1:])

    def set_timeout(self, ms):
        ticks = min(max(ms, 1) * TICKS_PER_MS, 0xFFFF)
        self.write_register(REG_T_RELOAD_H, ticks >> 8)
        self.write_register(REG_T_RELOAD_L, ticks & 0xFF)

    def init(self):
        """Reset and configure the chip; its version name, or None when no
        RC522 answers."""
        self.write_register(REG_COMMAND, CMD_SOFT_RESET)
        time.sleep(0.05)
        for _ in range(20):
            if not self.read_register(REG_COMMAND) & 0x10:   # PowerDown
                break
            time.sleep(0.005)
        version = self.read_register(REG_VERSION)
        if version not in VERSIONS:
            return None
        self.write_register(REG_TX_MODE, 0x00)       # 106 kbps, no hardware CRC
        self.write_register(REG_RX_MODE, 0x00)
        self.write_register(REG_MOD_WIDTH, 0x26)
        self.write_register(REG_T_MODE, 0x80)        # timer starts when a frame is sent
        self.write_register(REG_T_PRESCALER, TIMER_PRESCALER)
        self.set_timeout(CARD_TIMEOUT_MS)
        self.write_register(REG_TX_ASK, 0x40)        # 100 % ASK
        self.write_register(REG_MODE, 0x3D)          # CRC preset 0x6363
        self.write_register(REG_RF_CFG, 0x70)        # 48 dB receiver gain: best range
        return VERSIONS[version]

    def antenna(self, on):
        if on:
            self.set_bits(REG_TX_CONTROL, 0x03)
            time.sleep(0.005)                       # let the card power up
        else:
            self.clear_bits(REG_TX_CONTROL, 0x03)

    def run(self, command, data, tx_bits=0):
        """Run Transceive or MFAuthent. Returns (answer, rx_bits): the bytes
        received and the valid bits of the last one (0: all 8). Raises
        Rc522Error on a timeout, a collision or a framing error."""
        if len(data) > FIFO_SIZE:
            raise Rc522Error("frame too long")
        self.write_register(REG_COMMAND, CMD_IDLE)
        self.write_register(REG_COM_IRQ, 0x7F)       # clear the interrupt bits
        self.write_register(REG_FIFO_LEVEL, 0x80)    # flush the FIFO
        self.write_register(REG_FIFO_DATA, data)
        self.write_register(REG_BIT_FRAMING, tx_bits & 0x07)
        self.write_register(REG_COMMAND, command)
        if command == CMD_TRANSCEIVE:
            self.set_bits(REG_BIT_FRAMING, 0x80)     # StartSend
        # Transceive ends with a received frame; MFAuthent goes idle when
        # done. Either way the timer ends a wait for a card that does not
        # answer; the deadline only ends a wait on a chip that stopped.
        done = IRQ_RX | IRQ_IDLE if command == CMD_TRANSCEIVE else IRQ_IDLE
        deadline = time.monotonic() + 2.0
        while True:
            irq = self.read_register(REG_COM_IRQ)
            if irq & done:
                break
            if irq & IRQ_TIMER:
                self.write_register(REG_COMMAND, CMD_IDLE)
                raise Rc522Error("no answer")
            if time.monotonic() > deadline:
                self.write_register(REG_COMMAND, CMD_IDLE)
                raise BridgeError("RC522 stopped answering")
        error = self.read_register(REG_ERROR)
        if error & ERR_FRAME:
            raise Rc522Error("receive error 0x%02X" % error)
        if command != CMD_TRANSCEIVE:
            return b"", 0
        answer = self.read_fifo(self.read_register(REG_FIFO_LEVEL))
        rx_bits = self.read_register(REG_CONTROL) & 0x07
        if error & ERR_COLLISION:
            raise Rc522Error("collision")
        return answer, rx_bits

    def transceive(self, data):
        """A frame with CRC_A added; the answer with CRC_A checked and
        removed."""
        frame = bytes(data)
        answer, rx_bits = self.run(CMD_TRANSCEIVE, frame + crc_a(frame))
        # A 4 bit answer is a MIFARE NAK.
        if len(answer) < 3 or rx_bits:
            raise Rc522Error("NAK")
        if crc_a(answer[:-2]) != answer[-2:]:
            raise Rc522Error("CRC error")
        return answer[:-2]

    def mifare_authenticate(self, key, key_b, block, uid4):
        try:
            self.run(CMD_MF_AUTHENT, bytes([0x61 if key_b else 0x60, block]) + key + uid4)
        except Rc522Error:
            return False
        return bool(self.read_register(REG_STATUS2) & STATUS2_CRYPTO1_ON)


def activate_iso14443a(chip):
    """(uid, atqa, sak) of the card in the field, or None. WUPA, not REQA:
    it also wakes a card halted by a refused MIFARE authentication."""
    chip.clear_bits(REG_STATUS2, STATUS2_CRYPTO1_ON)
    chip.set_timeout(WAKE_TIMEOUT_MS)
    try:
        atqa, rx_bits = chip.run(CMD_TRANSCEIVE, bytes([WUPA]), tx_bits=7)
    except Rc522Error:
        return None
    finally:
        chip.set_timeout(CARD_TIMEOUT_MS)
    if len(atqa) != 2 or rx_bits:
        return None
    # Anticollision and select through the cascade levels. One card at a
    # time: a collision (two cards on the reader) fails the select.
    uid = b""
    for level in range(3):
        cascade = SEL_CL1 + 2 * level
        try:
            answer, _ = chip.run(CMD_TRANSCEIVE, bytes([cascade, 0x20]))
            if len(answer) != 5 or answer[0] ^ answer[1] ^ answer[2] ^ answer[3] != answer[4]:
                return None
            sak = chip.transceive(bytes([cascade, 0x70]) + answer)
        except Rc522Error:
            return None
        if len(sak) != 1:
            return None
        sak = sak[0]
        if sak & 0x04:                     # UID not complete
            if answer[0] != CASCADE_TAG:
                return None
            uid += answer[1:4]
            continue
        return uid + answer[:4], bytes(atqa), sak
    return None


class Iso14443Session(CardSession):
    def __init__(self, chip, uid, atqa, sak):
        self.chip = chip
        self.tech = TECH_ISO14443A
        self.uid = uid
        self.atqa = atqa
        self.sak = sak

    def type2_read(self, page):
        try:
            data = self.chip.transceive(bytes([0x30, page]))
        except Rc522Error:
            return None
        return data if len(data) >= 16 else None

    def mifare_auth(self, block, key, key_b=False):
        return self.chip.mifare_authenticate(key, key_b, block, self.uid[-4:])

    def mifare_read(self, block):
        return self.type2_read(block)

    def reselect(self):
        found = activate_iso14443a(self.chip)
        return found is not None and found[0] == self.uid


class Rc522Reader(Reader):
    def __init__(self, transport, keysets_for_uid=None):
        Reader.__init__(self, keysets_for_uid)
        self.transport = transport
        self.chip = Rc522(transport)
        self.name = "RC522"
        self._present = None
        self._misses = 0

    def open(self):
        self.transport.open()
        try:
            version = self.chip.init()
        except BridgeError as exc:
            self.transport.close()
            raise BridgeError("RC522: %s" % exc)
        if version is None:
            self.transport.close()
            raise BridgeError("RC522: no RC522 on the bridge")
        self.name = "RC522 %s via %s" % (version, getattr(self.transport, "path", "bridge"))

    def close(self):
        try:
            self.chip.antenna(False)
        except Exception:
            pass
        self.transport.close()

    def poll(self):
        # The field is only on while polling: the card starts from scratch
        # each time, and the RC522 stays cool between polls.
        self.chip.antenna(True)
        try:
            found = activate_iso14443a(self.chip)
            if found is None:
                self._misses += 1
                if self._misses >= REMOVAL_MISSES:
                    self._present = None
                return None
            self._misses = 0
            session = Iso14443Session(self.chip, *found)
            if session.uid == self._present:
                return None
            tag = self.read_card(session)
            if tag is not None and not tag.incomplete:
                self._present = session.uid
            return tag
        finally:
            self.chip.clear_bits(REG_STATUS2, STATUS2_CRYPTO1_ON)
            self.chip.antenna(False)

    def describe(self):
        return self.name
