"""Raw serial port over termios, standing in for pyserial (which Kiln's
Python does not ship). With kernel drivers, USB-serial bridges (CH340,
CP210x, PL2303, FTDI) and CDC-ACM devices appear as /dev/ttyUSB* and
/dev/ttyACM*; without them usb_serial.py drives the common ones."""

import errno
import fcntl
import glob
import os
import select
import struct
import termios
import time


class SerialError(IOError):
    pass


class SerialPort(object):
    def __init__(self, path, baud=115200):
        self.path = path
        self.baud = baud
        self.fd = None

    def open(self):
        speed = getattr(termios, "B%d" % self.baud, None)
        if speed is None:
            raise SerialError("unsupported baud rate %d" % self.baud)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise SerialError("%s: %s" % (self.path, exc.strerror))
        try:
            # Exclusive use, so a second plugin instance cannot interleave.
            fcntl.ioctl(fd, termios.TIOCEXCL)
            attrs = termios.tcgetattr(fd)
            cc = attrs[6]
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            # Raw 8N1, no flow control, no modem-line hangup on close.
            termios.tcsetattr(fd, termios.TCSANOW, [
                0, 0, termios.CS8 | termios.CREAD | termios.CLOCAL, 0,
                speed, speed, cc,
            ])
            # Keep DTR/RTS low: some PN532 boards wire them to reset or
            # power, and toggling them disturbs the chip.
            lines = struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)
            try:
                fcntl.ioctl(fd, termios.TIOCMBIC, lines)
            except OSError:
                pass  # CDC-ACM devices without modem lines
            termios.tcflush(fd, termios.TCIOFLUSH)
        except (OSError, termios.error) as exc:
            os.close(fd)
            raise SerialError("%s: %s" % (self.path, exc))
        self.fd = fd

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def write(self, data, timeout=1.0):
        deadline = time.monotonic() + timeout
        view = memoryview(bytes(data))
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialError("%s: write timed out" % self.path)
            _, ready, _ = select.select([], [self.fd], [], remaining)
            if not ready:
                continue
            try:
                count = os.write(self.fd, view)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    continue
                raise SerialError("%s: %s" % (self.path, exc.strerror))
            view = view[count:]

    def read(self, size, timeout):
        """Up to size bytes, waiting at most timeout for the first ones."""
        ready, _, _ = select.select([self.fd], [], [], max(0., timeout))
        if not ready:
            return b""
        try:
            data = os.read(self.fd, size)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EINTR):
                return b""
            raise SerialError("%s: %s" % (self.path, exc.strerror))
        if not data:
            # select() said readable but there was nothing: device unplugged.
            raise SerialError("%s: device disconnected" % self.path)
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
        termios.tcflush(self.fd, termios.TCIFLUSH)


def usb_serial_ports():
    """[(path, vid, pid, product)] for USB serial ports, from sysfs."""
    ports = []
    for path in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        name = os.path.basename(path)
        vid = pid = product = ""
        device = os.path.realpath("/sys/class/tty/%s/device" % name)
        # Walk up from the interface to the USB device that has idVendor.
        for _ in range(4):
            if os.path.exists(os.path.join(device, "idVendor")):
                vid = _read_sysfs(device, "idVendor")
                pid = _read_sysfs(device, "idProduct")
                product = _read_sysfs(device, "product")
                break
            device = os.path.dirname(device)
        ports.append((path, vid.lower(), pid.lower(), product))
    return ports


def _read_sysfs(directory, name):
    try:
        with open(os.path.join(directory, name)) as handle:
            return handle.read().strip()
    except (IOError, OSError):
        return ""
