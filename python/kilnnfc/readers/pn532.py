"""NXP PN532 over HSU (UART), usually on a board with a USB-serial bridge
(CH340, CP2102, PL2303, FTDI) or wired to one. ISO 14443A only: NTAG /
Ultralight, MIFARE Classic and ISO-DEP (Type 4). The PN532 cannot read
ISO 15693 (OpenPrintTag) tags; use a PN5180 or an ACR1552U for those.

PN532 boards must be switched to HSU/UART mode (both DIP switches off on
the common red and blue boards)."""

import logging
import time

from ..tag import TECH_ISO14443A
from ..transport.serial_port import SerialError
from ..transport.usb_serial import make_port
from .base import CardSession, Reader, ReaderError

LOGGER = logging.getLogger("kiln_nfc")

_PREAMBLE = b"\x00\x00\xff"
_ACK = b"\x00\x00\xff\x00\xff\x00"
# Long enough of a 0x55 run to wake a PN532 from power-down in HSU mode.
_WAKEUP = b"\x55\x55" + b"\x00" * 14

CMD_GET_FIRMWARE_VERSION = 0x02
CMD_SAM_CONFIGURATION = 0x14
CMD_RF_CONFIGURATION = 0x32
CMD_IN_DATA_EXCHANGE = 0x40
CMD_IN_COMMUNICATE_THRU = 0x42
CMD_IN_LIST_PASSIVE_TARGET = 0x4A
CMD_IN_RELEASE = 0x52

MIFARE_READ = 0x30
MIFARE_AUTH_A = 0x60
MIFARE_AUTH_B = 0x61

# Polls in a row that must miss a card before it counts as removed; one
# missed poll is often just the card at the edge of the field.
REMOVAL_MISSES = 2


class Pn532Error(IOError):
    """The PN532 answered wrongly or not at all (often: no card in range)."""


class Pn532LinkError(ReaderError):
    """The serial link failed (adapter unplugged); the reader is reopened."""


# Failed commands in a row after which the PN532 is reopened.
MAX_FAILURES = 5


class Pn532(object):
    """PN532 command layer on a serial port."""

    def __init__(self, port):
        self.port = port
        # Bytes received but not consumed yet: the ACK and the response
        # often arrive in the same read.
        self._rx = bytearray()

    def wake(self):
        """Wake the PN532 into normal mode (no SAM). After power-up it is in
        LowVbat mode and accepts only SAMConfiguration, sent right behind
        the 0x55 wakeup run (as libnfc does); other commands go unanswered."""
        self.command(CMD_SAM_CONFIGURATION, b"\x01\x14\x01", timeout=0.5, wake=True)

    def command(self, cmd, params=b"", timeout=1.0, wake=False):
        """Send a command and return the response data after the response
        code, or raise Pn532Error. wake sends the wakeup run in front."""
        body = bytes([0xD4, cmd]) + bytes(params)
        length = len(body)
        frame = (_PREAMBLE + bytes([length, (-length) & 0xFF]) + body
                 + bytes([(-sum(body)) & 0xFF, 0x00]))
        if wake:
            frame = _WAKEUP + frame
        self.port.flush_input()
        del self._rx[:]
        try:
            self.port.write(frame)
            ack = self._read_frame(min(timeout, 0.5))
            if ack != "ack":
                raise Pn532Error("PN532 did not acknowledge command 0x%02X" % cmd)
            response = self._read_frame(timeout)
        except SerialError as exc:
            raise Pn532LinkError(str(exc))
        if response in (None, "ack"):
            raise Pn532Error("PN532 did not answer command 0x%02X" % cmd)
        if len(response) < 2 or response[0] != 0xD5 or response[1] != cmd + 1:
            raise Pn532Error("PN532 sent an unexpected response to 0x%02X" % cmd)
        return response[2:]

    def _read_frame(self, timeout):
        """'ack', the frame body (TFI onwards), or None on timeout."""
        deadline = time.monotonic() + timeout
        buf = self._rx
        while True:
            start = buf.find(_PREAMBLE)
            if start >= 0 and len(buf) >= start + 5:
                length, lcs = buf[start + 3], buf[start + 4]
                if length == 0 and lcs == 0xFF:
                    del buf[:start + 6]
                    return "ack"
                if (length + lcs) & 0xFF != 0:
                    del buf[:start + 1]   # not a frame start; resync
                    continue
                end = start + 5 + length + 2
                if len(buf) >= end:
                    body = bytes(buf[start + 5:start + 5 + length])
                    checksum = buf[start + 5 + length]
                    del buf[:end]
                    if (sum(body) + checksum) & 0xFF != 0:
                        raise Pn532Error("PN532 frame checksum error")
                    if body == b"\x7f":
                        raise Pn532Error("PN532 reported a syntax error")
                    return body
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            buf += self.port.read(64, remaining)


class Pn532Session(CardSession):
    def __init__(self, reader, target):
        self.reader = reader
        self.tech = TECH_ISO14443A
        self.uid = target["uid"]
        self.atqa = target["atqa"]
        self.sak = target["sak"]

    def _exchange(self, data, timeout=1.0):
        try:
            resp = self.reader.chip.command(CMD_IN_DATA_EXCHANGE, b"\x01" + data, timeout)
        except Pn532Error:
            return None
        if not resp or resp[0] & 0x3F:
            return None
        return resp[1:]

    def type2_read(self, page):
        data = self._exchange(bytes([MIFARE_READ, page]))
        return data if data is not None and len(data) >= 16 else None

    def mifare_auth(self, block, key, key_b=False):
        cmd = MIFARE_AUTH_B if key_b else MIFARE_AUTH_A
        return self._exchange(bytes([cmd, block]) + key + self.uid[-4:]) is not None

    def mifare_read(self, block):
        return self.type2_read(block)

    def reselect(self):
        target = self.reader.list_target()
        return target is not None and target["uid"] == self.uid

    def apdu(self, command):
        return self._exchange(command, timeout=2.0)


class Pn532Reader(Reader):
    def __init__(self, path, baud=115200, keysets_for_uid=None):
        Reader.__init__(self, keysets_for_uid)
        self.path = path
        self.baud = baud
        self.port = None
        self.chip = None
        self.firmware = None
        self.name = "PN532 on %s" % path
        self._present = None
        self._misses = 0
        self._failures = 0

    def open(self):
        self.port = make_port(self.path, self.baud)
        try:
            self.port.open()
            self.chip = Pn532(self.port)
            self.chip.wake()
            fw = self.chip.command(CMD_GET_FIRMWARE_VERSION, timeout=0.5)
            if len(fw) < 4 or fw[0] != 0x32:
                raise Pn532Error("%s: not a PN532 (IC 0x%02X)" % (self.path, fw[0] if fw else 0))
            self.firmware = "%d.%d" % (fw[1], fw[2])
            # Few passive activation retries, so a poll without a card
            # returns in tens of milliseconds instead of blocking.
            self.chip.command(CMD_RF_CONFIGURATION, b"\x05\xff\x01\x02")
        except (SerialError, Pn532Error) as exc:
            self.close()
            raise Pn532LinkError(str(exc))
        self.name = "PN532 v%s on %s" % (self.firmware, self.path)

    def close(self):
        if self.port is not None:
            self.port.close()
        self.port = None
        self.chip = None

    def list_target(self):
        try:
            resp = self.chip.command(CMD_IN_LIST_PASSIVE_TARGET, b"\x01\x00", timeout=1.0)
        except Pn532Error as exc:
            # Usually no card; a PN532 that stopped answering is reopened.
            self._failures += 1
            LOGGER.debug("kiln_nfc: PN532 list target: %s", exc)
            if self._failures >= MAX_FAILURES:
                raise Pn532LinkError("PN532 on %s stopped answering" % self.path)
            return None
        self._failures = 0
        if not resp or resp[0] == 0 or len(resp) < 6:
            return None
        uid_len = resp[5]
        return {
            "atqa": bytes(resp[2:4]),
            "sak": resp[4],
            "uid": bytes(resp[6:6 + uid_len]),
        }

    def poll(self):
        if self.chip is None:
            raise ReaderError("PN532 is not open")
        target = self.list_target()
        if target is None:
            self._misses += 1
            if self._misses >= REMOVAL_MISSES:
                self._present = None
            return None
        self._misses = 0
        if target["uid"] == self._present:
            self._release()
            return None
        tag = self.read_card(Pn532Session(self, target))
        self._release()
        if tag is not None and not tag.incomplete:
            self._present = target["uid"]
        return tag

    def _release(self):
        try:
            self.chip.command(CMD_IN_RELEASE, b"\x00", timeout=0.3)
        except Pn532Error:
            pass

    def describe(self):
        return self.name


def probe(path, baud=115200):
    """True when a PN532 answers on path."""
    reader = Pn532Reader(path, baud)
    try:
        reader.open()
        return True
    except ReaderError:
        return False
    finally:
        reader.close()
