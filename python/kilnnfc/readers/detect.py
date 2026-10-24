"""Find and open the attached reader."""

import logging
import os

from ..transport.serial_port import usb_serial_ports
from ..transport.usb_serial import usb_ports
from .base import ReaderError
from . import ccid, pn532, pn5180, rc522
from .bridge import NoBridgeError, SerialBridge

LOGGER = logging.getLogger("kiln_nfc")

READER_TYPES = ("auto", "pn532", "ccid", "acr1552u", "acr122u", "pn5180", "rc522")

# USB-serial bridge chips that PN532 boards use, and MCU boards that can
# run the PN5180/RC522 bridge. Other serial ports are only probed when named.
PROBE_VIDS = {
    "1a86",   # WCH CH340/CH341/CH9102
    "10c4",   # Silicon Labs CP210x
    "067b",   # Prolific PL2303
    "0403",   # FTDI
    "2e8a",   # Raspberry Pi RP2040/RP2350
    "303a",   # Espressif ESP32-S2/S3/C3
}
KLIPPER_USB_ID = ("1d50", "614e")


def candidate_ports(device, excluded):
    """Serial ports to probe: the named device, or USB serial ports from a
    known bridge vendor that no Klipper MCU uses: kernel ttys, and chips
    without a kernel driver through usbfs (usb:<port>)."""
    excluded = set(os.path.realpath(p) for p in excluded)
    if device:
        return [device]
    ports = []
    for path, vid, pid, product in usb_serial_ports() + usb_ports():
        if os.path.realpath(path) in excluded:
            continue
        if (vid, pid) == KLIPPER_USB_ID or vid not in PROBE_VIDS:
            continue
        ports.append(path)
    return ports


def open_reader(kind="auto", device="", baud=115200, ccid_match="",
                keysets_for_uid=None, excluded_ports=()):
    """An opened Reader, or raise ReaderError explaining what was tried."""
    kind = kind.lower()
    if kind not in READER_TYPES:
        raise ReaderError("unknown reader type %r" % kind)
    tried = []

    if kind in ("auto", "ccid", "acr1552u", "acr122u"):
        match = ccid_match
        if not match and kind in ("acr1552u", "acr122u"):
            match = kind
        for dev in ccid.find_devices(match):
            reader = ccid.CcidReader(dev, keysets_for_uid)
            try:
                reader.open()
                return reader
            except ReaderError as exc:
                tried.append(str(exc))
        if kind != "auto":
            raise ReaderError("no CCID reader found" + _detail(tried))

    for path in candidate_ports(device, excluded_ports):
        if kind in ("auto", "pn532"):
            reader = pn532.Pn532Reader(path, baud, keysets_for_uid)
            try:
                reader.open()
                return reader
            except ReaderError as exc:
                tried.append(str(exc))
        # The PN5180 and the RC522 share the bridge; a PN5180 is tried first
        # because the RC522 probe would send it register writes it does not
        # understand.
        for name, cls in (("pn5180", pn5180.Pn5180Reader), ("rc522", rc522.Rc522Reader)):
            if kind not in ("auto", name):
                continue
            reader = cls(SerialBridge(path, baud), keysets_for_uid)
            try:
                reader.open()
                return reader
            except NoBridgeError as exc:
                tried.append(str(exc))
                break             # no bridge on this port: skip the other chip
            except ReaderError as exc:
                tried.append(str(exc))
    raise ReaderError("no NFC reader found" + _detail(tried))


def _detail(tried):
    return (" (" + "; ".join(tried) + ")") if tried else ""
