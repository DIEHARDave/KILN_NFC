"""Serial ports in user space over usbfs, for kernels without USB serial
drivers: Kiln OS on the AD5X's stock 5.10 kernel has no ch341, cp210x or
cdc_acm, so a PN532 board or a bridge never gets a /dev/ttyUSB*.

Covers the chips NFC boards use: WCH CH340/CH341, Silicon Labs CP210x and
CDC-ACM (ESP32-C3/S3 native USB, RP2040, CH9102). Ports are named after
their USB port, usb:<sysfs device name> (usb:1-1.2), and offer the same
calls as serial_port.SerialPort. Devices a kernel driver already serves
are left to it (they have a /dev/tty node instead).

Reads ask for one packet at a time: usbfs drops what a bulk transfer had
received when it times out, and a transfer for one packet completes as
soon as that packet arrives."""

import os
import struct
import time

from .serial_port import SerialError
from . import usbfs

PREFIX = "usb:"

CLASS_CDC = 0x02
CLASS_CDC_DATA = 0x0A
CLASS_VENDOR = 0xFF

# CH340/CH341 (Linux drivers/usb/serial/ch341.c)
CH341_VIDS = {0x1a86, 0x4348}
CH341_REQ_READ_VERSION = 0x5F
CH341_REQ_WRITE_REG = 0x9A
CH341_REQ_SERIAL_INIT = 0xA1
CH341_REQ_MODEM_CTRL = 0xA4
CH341_REG_DIVISOR_PRESCALER = 0x1312
CH341_REG_LCR2_LCR = 0x2518
CH341_LCR_8N1 = 0x80 | 0x40 | 0x03     # RX and TX on, 8 data bits
CH341_CLKRATE = 48000000

# CP210x (Linux drivers/usb/serial/cp210x.c, Silicon Labs AN571)
CP210X_VIDS = {0x10c4}
CP210X_IFC_ENABLE = 0x00
CP210X_SET_LINE_CTL = 0x03
CP210X_SET_MHS = 0x07
CP210X_PURGE = 0x12
CP210X_SET_BAUDRATE = 0x1E
CP210X_LINE_8N1 = 0x0800
CP210X_MHS_LOW = 0x0300                 # DTR and RTS: change both, to low

# CDC-ACM
ACM_SET_LINE_CODING = 0x20
ACM_SET_CONTROL_LINE_STATE = 0x22

VENDOR_OUT = 0x40       # vendor request to the device
VENDOR_IN = 0xC0
VENDOR_IFACE_OUT = 0x41  # vendor request to an interface (CP210x)
CLASS_IFACE_OUT = 0x21


def _bound(device, number):
    """True when a kernel driver serves interface number."""
    # Interface directories are <device>:<configuration>.<interface>.
    name = os.path.basename(device.sysfs_path)
    try:
        entries = os.listdir(device.sysfs_path)
    except OSError:
        return False
    for entry in entries:
        if entry.startswith(name + ":") and entry.endswith(".%d" % number):
            return os.path.exists(os.path.join(device.sysfs_path, entry, "driver"))
    return False


def chip_of(device):
    """("ch341" | "cp210x" | "acm", data interface) or None."""
    for iface in device.interfaces:
        if iface.cls == CLASS_CDC_DATA and iface.endpoint("Bulk", True) \
                and iface.endpoint("Bulk", False):
            return "acm", iface
    for iface in device.interfaces:
        if iface.cls != CLASS_VENDOR or not (iface.endpoint("Bulk", True)
                                             and iface.endpoint("Bulk", False)):
            continue
        if device.vid in CH341_VIDS:
            return "ch341", iface
        if device.vid in CP210X_VIDS:
            return "cp210x", iface
    return None


def _comm_interface(device):
    for iface in device.interfaces:
        if iface.cls == CLASS_CDC:
            return iface
    return None


def port_name(device):
    return PREFIX + os.path.basename(device.sysfs_path)


def usb_ports():
    """[(path, vid, pid, product)] of serial chips no kernel driver serves,
    like serial_port.usb_serial_ports()."""
    ports = []
    for device in usbfs.list_devices():
        found = chip_of(device)
        if found is None or _bound(device, found[1].number):
            continue
        ports.append((port_name(device), "%04x" % device.vid, "%04x" % device.pid,
                      device.product))
    return ports


def find_device(path):
    """The device for usb:<sysfs name> or usb:<vid>:<pid>."""
    want = path[len(PREFIX):].lower()
    for device in usbfs.list_devices():
        if os.path.basename(device.sysfs_path).lower() == want or device.ident == want:
            return device
    return None


def ch341_divisor(baud):
    """Prescaler/divisor register value, as ch341_get_divisor() computes it
    (0xCC03 for 115200)."""
    def clk_div(ps, fact):
        return 1 << (12 - 3 * ps - fact)

    min_rates = [CH341_CLKRATE // (clk_div(ps, 1) * 512) for ps in range(4)]
    baud = max(min_rates[0], min(baud, CH341_CLKRATE // (clk_div(3, 0) * 2)))
    for ps in (3, 2, 1, 0):
        if baud > min_rates[ps]:
            break
    else:
        raise SerialError("unsupported baud rate %d" % baud)
    fact = 1
    div_clk = clk_div(ps, fact)
    div = CH341_CLKRATE // (div_clk * baud)
    if div < 9 or div > 255:
        div //= 2
        div_clk *= 2
        fact = 0
    if div < 2:
        raise SerialError("unsupported baud rate %d" % baud)
    # The next divisor when its rate is closer.
    if (16 * CH341_CLKRATE // (div_clk * div) - 16 * baud
            >= 16 * baud - 16 * CH341_CLKRATE // (div_clk * (div + 1))):
        div += 1
    if fact == 1 and div % 2 == 0:
        div //= 2
        fact = 0
    return (0x100 - div) << 8 | fact << 2 | ps


class UsbSerialPort(object):
    def __init__(self, path, baud=115200):
        self.path = path
        self.baud = baud
        self.handle = None
        self.chip = None
        self.iface = None
        self.ep_in = None
        self.ep_out = None
        self.packet = 64
        self._buffer = b""

    def open(self):
        device = find_device(self.path)
        if device is None:
            raise SerialError("%s: no such USB device" % self.path)
        found = chip_of(device)
        if found is None:
            raise SerialError("%s: %s is not a known USB serial chip"
                              % (self.path, device.describe()))
        self.chip, self.iface = found
        handle = usbfs.UsbHandle(device)
        try:
            handle.open()
        except usbfs.UsbError as exc:
            if "ermission" in str(exc):
                raise SerialError("%s: no access to %s (run install.sh as root)"
                                  % (self.path, device.node))
            raise SerialError("%s: %s" % (self.path, exc))
        self.handle = handle
        try:
            if self.chip == "acm":
                comm = _comm_interface(device)
                if comm is not None:
                    handle.claim(comm.number)
            handle.claim(self.iface.number)
            ep_in = self.iface.endpoint("Bulk", True)
            self.ep_in = ep_in.address
            self.ep_out = self.iface.endpoint("Bulk", False).address
            self.packet = ep_in.max_packet or 64
            getattr(self, "_setup_" + self.chip)(device)
        except (usbfs.UsbError, SerialError) as exc:
            self.close()
            raise SerialError("%s: %s" % (self.path, exc))
        self._buffer = b""

    # DTR and RTS stay low, as SerialPort keeps them: some PN532 boards
    # wire them to reset or power, and on the ESP32 they reset the chip.

    def _setup_ch341(self, device):
        version = self.handle.control(VENDOR_IN, CH341_REQ_READ_VERSION, 0, 0, 2)
        self.handle.control(VENDOR_OUT, CH341_REQ_SERIAL_INIT, 0, 0, b"")
        value = ch341_divisor(self.baud)
        # Newer chips hold data back until a full packet without bit 7.
        if version and version[0] > 0x27:
            value |= 0x80
        self.handle.control(VENDOR_OUT, CH341_REQ_WRITE_REG,
                            CH341_REG_DIVISOR_PRESCALER, value, b"")
        self.handle.control(VENDOR_OUT, CH341_REQ_WRITE_REG,
                            CH341_REG_LCR2_LCR, CH341_LCR_8N1, b"")
        # Modem lines are active low: 0xFF is DTR and RTS off.
        self.handle.control(VENDOR_OUT, CH341_REQ_MODEM_CTRL, 0xFF, 0, b"")

    def _setup_cp210x(self, device):
        n = self.iface.number
        self.handle.control(VENDOR_IFACE_OUT, CP210X_IFC_ENABLE, 1, n, b"")
        self.handle.control(VENDOR_IFACE_OUT, CP210X_SET_BAUDRATE, 0, n,
                            struct.pack("<I", self.baud))
        self.handle.control(VENDOR_IFACE_OUT, CP210X_SET_LINE_CTL, CP210X_LINE_8N1, n, b"")
        self.handle.control(VENDOR_IFACE_OUT, CP210X_SET_MHS, CP210X_MHS_LOW, n, b"")

    def _setup_acm(self, device):
        comm = _comm_interface(device)
        n = comm.number if comm is not None else self.iface.number
        # Native USB serial ignores these; USB-UART chips (CH9102) need them.
        try:
            self.handle.control(CLASS_IFACE_OUT, ACM_SET_LINE_CODING, 0, n,
                                struct.pack("<IBBB", self.baud, 0, 0, 8))
            self.handle.control(CLASS_IFACE_OUT, ACM_SET_CONTROL_LINE_STATE, 0, n, b"")
        except usbfs.UsbError:
            pass

    def close(self):
        if self.handle is not None:
            if self.chip == "cp210x" and self.iface is not None:
                try:
                    self.handle.control(VENDOR_IFACE_OUT, CP210X_IFC_ENABLE, 0,
                                        self.iface.number, b"")
                except usbfs.UsbError:
                    pass
            self.handle.close()
            self.handle = None
        self._buffer = b""

    def _error(self, exc):
        if isinstance(exc, usbfs.UsbError) and ("No such device" in str(exc)
                                                or "o such file" in str(exc)):
            return SerialError("%s: device disconnected" % self.path)
        return SerialError("%s: %s" % (self.path, exc))

    def write(self, data, timeout=1.0):
        deadline = time.monotonic() + timeout
        data = bytes(data)
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialError("%s: write timed out" % self.path)
            try:
                count = self.handle.bulk_write(self.ep_out, data[:4096],
                                               max(1, int(remaining * 1000)))
            except usbfs.UsbTimeout:
                continue
            except usbfs.UsbError as exc:
                raise self._error(exc)
            data = data[count:]

    def _packet(self, timeout):
        """One packet, or b"" when none came within timeout."""
        # usbfs takes 0 ms as "wait forever".
        try:
            return self.handle.bulk_read(self.ep_in, self.packet,
                                         max(1, int(timeout * 1000)))
        except usbfs.UsbTimeout:
            return b""
        except usbfs.UsbError as exc:
            raise self._error(exc)

    def read(self, size, timeout):
        """Up to size bytes, waiting at most timeout for the first ones."""
        if not self._buffer:
            deadline = time.monotonic() + max(0., timeout)
            while True:
                self._buffer = self._packet(max(0., deadline - time.monotonic()))
                # Zero-length packets end transfers; they carry nothing.
                if self._buffer or time.monotonic() >= deadline:
                    break
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def read_exact(self, size, timeout):
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            buf += self.read(size - len(buf), remaining)
        return bytes(buf)

    def flush_input(self):
        self._buffer = b""
        if self.chip == "cp210x":
            try:
                self.handle.control(VENDOR_IFACE_OUT, CP210X_PURGE, 0x000F,
                                    self.iface.number, b"")
            except usbfs.UsbError:
                pass
        # Whatever the chip had already received.
        for _ in range(16):
            if not self._packet(0.002):
                break


def is_usb_path(path):
    return str(path).startswith(PREFIX)


def make_port(path, baud=115200):
    """A SerialPort for a tty, a UsbSerialPort for usb:<port>."""
    if is_usb_path(path):
        return UsbSerialPort(path, baud)
    from .serial_port import SerialPort
    return SerialPort(path, baud)
