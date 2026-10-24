"""Minimal user-space USB through Linux usbfs (/dev/bus/usb/BBB/DDD) ioctls,
standing in for libusb/pyusb, which Kiln OS does not ship.

Devices are enumerated from sysfs. ioctl request numbers are computed for
the running architecture: MIPS (the AD5X's Ingenic X2600), PowerPC and
SPARC encode direction and size differently from ARM and x86."""

import ctypes
import errno
import fcntl
import glob
import os
import platform
import struct


class UsbError(IOError):
    pass


class UsbTimeout(UsbError):
    pass


# --- ioctl numbers ---------------------------------------------------------

def _ioc_layout():
    machine = platform.machine().lower()
    if machine.startswith(("mips", "ppc", "powerpc", "sparc")):
        return 1, 2, 4, 13   # none, read, write, size bits
    return 0, 2, 1, 14


_IOC_NONE, _IOC_READ, _IOC_WRITE, _IOC_SIZEBITS = _ioc_layout()


def _ioc(direction, nr, size):
    return ((direction << (16 + _IOC_SIZEBITS)) | (size << 16)
            | (ord("U") << 8) | nr)


class _CtrlTransfer(ctypes.Structure):
    _fields_ = [
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


class _BulkTransfer(ctypes.Structure):
    _fields_ = [
        ("ep", ctypes.c_uint),
        ("len", ctypes.c_uint),
        ("timeout", ctypes.c_uint),
        ("data", ctypes.c_void_p),
    ]


class _IoctlRequest(ctypes.Structure):
    _fields_ = [
        ("ifno", ctypes.c_int),
        ("ioctl_code", ctypes.c_int),
        ("data", ctypes.c_void_p),
    ]


_RW = _IOC_READ | _IOC_WRITE
USBDEVFS_CONTROL = _ioc(_RW, 0, ctypes.sizeof(_CtrlTransfer))
USBDEVFS_BULK = _ioc(_RW, 2, ctypes.sizeof(_BulkTransfer))
USBDEVFS_CLAIMINTERFACE = _ioc(_IOC_READ, 15, 4)
USBDEVFS_RELEASEINTERFACE = _ioc(_IOC_READ, 16, 4)
USBDEVFS_IOCTL = _ioc(_RW, 18, ctypes.sizeof(_IoctlRequest))
USBDEVFS_CLEAR_HALT = _ioc(_IOC_READ, 21, 4)
USBDEVFS_DISCONNECT = _ioc(_IOC_NONE, 22, 0)


# --- enumeration -----------------------------------------------------------

class UsbEndpoint(object):
    def __init__(self, address, kind, max_packet):
        self.address = address        # bit 7 set for IN
        self.kind = kind              # "Bulk", "Interrupt", ...
        self.max_packet = max_packet

    @property
    def is_in(self):
        return bool(self.address & 0x80)


class UsbInterface(object):
    def __init__(self, number, cls, subclass, protocol, endpoints, extra):
        self.number = number
        self.cls = cls
        self.subclass = subclass
        self.protocol = protocol
        self.endpoints = endpoints
        # Class-specific descriptors that follow the interface descriptor,
        # e.g. the CCID functional descriptor (type 0x21).
        self.extra = extra

    def endpoint(self, kind, is_in):
        for ep in self.endpoints:
            if ep.kind == kind and ep.is_in == is_in:
                return ep
        return None


class UsbDevice(object):
    def __init__(self, sysfs_path):
        self.sysfs_path = sysfs_path
        self.vid = int(_read(sysfs_path, "idVendor") or "0", 16)
        self.pid = int(_read(sysfs_path, "idProduct") or "0", 16)
        self.manufacturer = _read(sysfs_path, "manufacturer")
        self.product = _read(sysfs_path, "product")
        self.serial = _read(sysfs_path, "serial")
        self.busnum = int(_read(sysfs_path, "busnum") or 0)
        self.devnum = int(_read(sysfs_path, "devnum") or 0)
        self.interfaces = _parse_interfaces(sysfs_path)

    @property
    def node(self):
        return "/dev/bus/usb/%03d/%03d" % (self.busnum, self.devnum)

    @property
    def ident(self):
        return "%04x:%04x" % (self.vid, self.pid)

    def describe(self):
        name = " ".join(p for p in (self.manufacturer, self.product) if p)
        return "%s (%s)" % (name or "USB device", self.ident)


def list_devices():
    devices = []
    for path in sorted(glob.glob("/sys/bus/usb/devices/*")):
        name = os.path.basename(path)
        if ":" in name or not os.path.exists(os.path.join(path, "idVendor")):
            continue
        try:
            devices.append(UsbDevice(path))
        except (IOError, OSError, ValueError):
            continue
    return devices


def _read(directory, name):
    try:
        with open(os.path.join(directory, name)) as handle:
            return handle.read().strip()
    except (IOError, OSError):
        return ""


def _parse_interfaces(sysfs_path):
    """Interfaces of the active configuration, from the raw descriptors that
    sysfs exposes (device descriptor followed by the configuration)."""
    try:
        with open(os.path.join(sysfs_path, "descriptors"), "rb") as handle:
            raw = handle.read()
    except (IOError, OSError):
        return []
    interfaces = []
    current = None
    pos = 0
    while pos + 2 <= len(raw):
        length, dtype = raw[pos], raw[pos + 1]
        if length < 2:
            break
        desc = raw[pos:pos + length]
        if dtype == 0x02 and interfaces:
            break  # a second configuration; only the first is active
        if dtype == 0x04 and len(desc) >= 9:
            # Alternate settings other than 0 are not used here.
            if desc[3] == 0:
                current = UsbInterface(desc[2], desc[5], desc[6], desc[7], [], [])
                interfaces.append(current)
            else:
                current = None
        elif dtype == 0x05 and len(desc) >= 7 and current is not None:
            kind = ("Control", "Isochronous", "Bulk", "Interrupt")[desc[3] & 3]
            current.endpoints.append(UsbEndpoint(
                desc[2], kind, struct.unpack_from("<H", desc, 4)[0]))
        elif current is not None and dtype not in (0x04, 0x05):
            current.extra.append(bytes(desc))
        pos += length
    return interfaces


# --- device handle ---------------------------------------------------------

class UsbHandle(object):
    def __init__(self, device):
        self.device = device
        self.fd = None
        self.claimed = []

    def open(self):
        try:
            self.fd = os.open(self.device.node, os.O_RDWR)
        except OSError as exc:
            raise UsbError("%s: %s" % (self.device.node, exc.strerror))

    def close(self):
        for number in self.claimed:
            try:
                fcntl.ioctl(self.fd, USBDEVFS_RELEASEINTERFACE, struct.pack("I", number))
            except OSError:
                pass
        self.claimed = []
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def claim(self, number):
        arg = struct.pack("I", number)
        try:
            fcntl.ioctl(self.fd, USBDEVFS_CLAIMINTERFACE, arg)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                raise UsbError("claim interface %d: %s" % (number, exc.strerror))
            # A kernel driver holds the interface; detach it and retry.
            request = _IoctlRequest(number, USBDEVFS_DISCONNECT, None)
            try:
                fcntl.ioctl(self.fd, USBDEVFS_IOCTL, request)
                fcntl.ioctl(self.fd, USBDEVFS_CLAIMINTERFACE, arg)
            except OSError as exc2:
                raise UsbError("claim interface %d: %s" % (number, exc2.strerror))
        self.claimed.append(number)

    def clear_halt(self, endpoint):
        try:
            fcntl.ioctl(self.fd, USBDEVFS_CLEAR_HALT, struct.pack("I", endpoint))
        except OSError:
            pass

    def bulk_write(self, endpoint, data, timeout_ms=1000):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        return self._bulk(endpoint, buf, len(data), timeout_ms)

    def bulk_read(self, endpoint, size, timeout_ms=1000):
        buf = ctypes.create_string_buffer(size)
        count = self._bulk(endpoint, buf, size, timeout_ms)
        return buf.raw[:count]

    def _bulk(self, endpoint, buf, size, timeout_ms):
        transfer = _BulkTransfer(endpoint, size, timeout_ms,
                                 ctypes.cast(buf, ctypes.c_void_p))
        try:
            return fcntl.ioctl(self.fd, USBDEVFS_BULK, transfer)
        except OSError as exc:
            if exc.errno == errno.ETIMEDOUT:
                raise UsbTimeout("endpoint 0x%02x timed out" % endpoint)
            if exc.errno == errno.EPIPE:
                self.clear_halt(endpoint)
            raise UsbError("endpoint 0x%02x: %s" % (endpoint, exc.strerror))

    def control(self, request_type, request, value, index, data_or_length,
                timeout_ms=1000):
        if isinstance(data_or_length, int):
            buf = ctypes.create_string_buffer(data_or_length)
            length = data_or_length
        else:
            buf = ctypes.create_string_buffer(bytes(data_or_length), len(data_or_length))
            length = len(data_or_length)
        transfer = _CtrlTransfer(request_type, request, value, index, length,
                                 timeout_ms, ctypes.cast(buf, ctypes.c_void_p))
        try:
            count = fcntl.ioctl(self.fd, USBDEVFS_CONTROL, transfer)
        except OSError as exc:
            raise UsbError("control transfer: %s" % exc.strerror)
        return buf.raw[:count]
