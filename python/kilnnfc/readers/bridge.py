"""The kiln_nfc USB-to-SPI bridge: a small microcontroller (RP2040,
ESP32-S3, ...) that appears as a USB serial port and runs SPI transactions
for the host. It carries the readers that have no USB of their own, the
PN5180 and the RC522; their drivers hold the whole chip command layer, so
the bridge firmware stays a thin SPI pipe. The protocol is documented in
docs/pn5180-bridge.md."""

import struct
import time

from ..transport.serial_port import SerialError
from ..transport.usb_serial import make_port
from .base import ReaderError


class BridgeError(ReaderError):
    """The USB bridge failed; the reader is reopened."""


class NoBridgeError(BridgeError):
    """No bridge answers on the port."""


def _crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class SerialBridge(object):
    """SPI over the kiln_nfc serial bridge protocol (v1)."""

    SYNC = 0xA5
    CMD_HELLO = 0x01
    CMD_RESET = 0x02
    CMD_EXCHANGE = 0x03
    CMD_TRANSFER = 0x04
    MAGIC = b"PN5180BRIDGE"

    def __init__(self, path, baud=115200):
        self.port = make_port(path, baud)
        self.path = path
        self.version = None

    def open(self):
        try:
            self.port.open()
            reply = self._call(self.CMD_HELLO, b"", timeout=0.5)
        except (SerialError, BridgeError) as exc:
            self.port.close()
            raise NoBridgeError("%s: %s" % (self.path, exc))
        if not reply.startswith(self.MAGIC):
            self.port.close()
            raise NoBridgeError("%s: not a kiln_nfc bridge" % self.path)
        self.version = reply[len(self.MAGIC):].decode("ascii", "replace")

    def close(self):
        self.port.close()

    def reset(self):
        self._call(self.CMD_RESET, b"", timeout=1.0)

    def exchange(self, tx, rx_len=0, timeout=1.0):
        """Send one PN5180 command frame and, if rx_len, read the response
        frame; the bridge waits on BUSY around both."""
        return self._call(self.CMD_EXCHANGE, struct.pack("<H", rx_len) + bytes(tx), timeout)

    def transfer(self, tx, timeout=1.0):
        """One full-duplex SPI transaction (chip select held for all of tx,
        no BUSY handling): the bytes read while tx was clocked out."""
        return self._call(self.CMD_TRANSFER, bytes(tx), timeout)

    def _call(self, cmd, payload, timeout):
        body = bytes([cmd]) + struct.pack("<H", len(payload)) + payload
        try:
            self.port.flush_input()
            self.port.write(bytes([self.SYNC]) + body + bytes([_crc8(body)]))
            deadline = time.monotonic() + timeout
            while True:
                byte = self.port.read_exact(1, max(0., deadline - time.monotonic()))
                if not byte:
                    raise BridgeError("bridge did not answer")
                if byte[0] == self.SYNC:
                    break
            head = self.port.read_exact(3, timeout)
            if len(head) < 3:
                raise BridgeError("short bridge reply")
            length = struct.unpack_from("<H", head, 1)[0]
            rest = self.port.read_exact(length + 1, timeout)
        except SerialError as exc:
            raise BridgeError(str(exc))
        if len(rest) < length + 1 or _crc8(head + rest[:-1]) != rest[-1]:
            raise BridgeError("corrupt bridge reply")
        if head[0] != cmd | 0x80:
            raise BridgeError("bridge answered command 0x%02X" % head[0])
        data = rest[:-1]
        if not data or data[0] != 0:
            raise BridgeError("bridge error %d" % (data[0] if data else -1))
        return data[1:]

