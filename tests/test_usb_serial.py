"""User-space USB serial (usb_serial.py) against fake usbfs devices."""

import os
import shutil
import struct
import tempfile
import unittest

import helpers  # noqa: F401  (sets sys.path)

from kilnnfc.readers import detect
from kilnnfc.transport import usb_serial, usbfs
from kilnnfc.transport.serial_port import SerialError


def iface(number, cls, eps=(0x82, 0x02), max_packet=64):
    return usbfs.UsbInterface(number, cls, 0, 0, [
        usbfs.UsbEndpoint(ep, "Bulk", max_packet) for ep in eps], [])


class FakeDevice(object):
    def __init__(self, vid, pid, interfaces, name="1-1.2"):
        self.vid = vid
        self.pid = pid
        self.interfaces = interfaces
        self.sysfs_path = os.path.join(tempfile.gettempdir(), "kiln_nfc_test_" + name)
        self.product = "Fake"
        self.node = "/dev/bus/usb/001/005"

    @property
    def ident(self):
        return "%04x:%04x" % (self.vid, self.pid)

    def describe(self):
        return "Fake (%s)" % self.ident


CH340 = lambda: FakeDevice(0x1a86, 0x7523, [iface(0, 0xFF, max_packet=32)])
CP2102 = lambda: FakeDevice(0x10c4, 0xea60, [iface(0, 0xFF)])
ESP32C3 = lambda: FakeDevice(0x303a, 0x1001, [
    iface(0, 0x02, eps=(0x83,)), iface(1, 0x0A), iface(2, 0xFF)], "1-1.4")


class FakeHandle(object):
    def __init__(self, device, packets=(), version=0x31):
        self.device = device
        self.packets = list(packets)
        self.controls = []
        self.written = []
        self.claimed = []
        self.reads = []
        self.version = version
        self.closed = False

    def open(self):
        pass

    def close(self):
        self.closed = True

    def claim(self, number):
        self.claimed.append(number)

    def control(self, request_type, request, value, index, data_or_length, timeout_ms=1000):
        self.controls.append((request_type, request, value, index, data_or_length))
        if request_type & 0x80:
            return bytes([self.version, 0])[:data_or_length]
        return b""

    def bulk_write(self, endpoint, data, timeout_ms=1000):
        self.written.append((endpoint, bytes(data)))
        return len(data)

    def bulk_read(self, endpoint, size, timeout_ms=1000):
        self.reads.append(size)
        if not self.packets:
            raise usbfs.UsbTimeout("timed out")
        packet = self.packets.pop(0)
        if isinstance(packet, Exception):
            raise packet
        assert len(packet) <= size
        return packet


class PortTest(unittest.TestCase):
    def open(self, device, packets=(), version=0x31):
        handle = FakeHandle(device, packets, version)
        originals = usb_serial.find_device, usbfs.UsbHandle
        usb_serial.find_device = lambda path: device
        usbfs.UsbHandle = lambda dev: handle
        try:
            port = usb_serial.UsbSerialPort("usb:1-1.2", 115200)
            port.open()
        finally:
            usb_serial.find_device, usbfs.UsbHandle = originals
        return port, handle

    def test_ch341_divisor_matches_linux(self):
        self.assertEqual(usb_serial.ch341_divisor(115200), 0xCC03)
        self.assertEqual(usb_serial.ch341_divisor(9600), 0xB202)

    def test_ch340_setup(self):
        port, handle = self.open(CH340())
        self.assertEqual(handle.claimed, [0])
        self.assertEqual(handle.controls, [
            (0xC0, 0x5F, 0, 0, 2),               # version
            (0x40, 0xA1, 0, 0, b""),             # serial init
            (0x40, 0x9A, 0x1312, 0xCC83, b""),   # 115200, no packet buffering
            (0x40, 0x9A, 0x2518, 0xC3, b""),     # 8N1, RX and TX on
            (0x40, 0xA4, 0xFF, 0, b"")])         # DTR and RTS off
        self.assertEqual(port.packet, 32)

    def test_old_ch340_keeps_bit_7_clear(self):
        _, handle = self.open(CH340(), version=0x27)
        self.assertEqual(handle.controls[2][3], 0xCC03)

    def test_cp210x_setup_and_close(self):
        port, handle = self.open(CP2102())
        self.assertEqual(handle.controls, [
            (0x41, 0x00, 1, 0, b""),
            (0x41, 0x1E, 0, 0, struct.pack("<I", 115200)),
            (0x41, 0x03, 0x0800, 0, b""),
            (0x41, 0x07, 0x0300, 0, b"")])
        port.close()
        self.assertEqual(handle.controls[-1], (0x41, 0x00, 0, 0, b""))
        self.assertTrue(handle.closed)

    def test_cdc_acm_setup(self):
        _, handle = self.open(ESP32C3())
        self.assertEqual(handle.claimed, [0, 1])
        self.assertEqual(handle.controls, [
            (0x21, 0x20, 0, 0, struct.pack("<IBBB", 115200, 0, 0, 8)),
            (0x21, 0x22, 0, 0, b"")])

    def test_reads_one_packet_at_a_time_and_buffers(self):
        port, handle = self.open(CP2102(), [b"\x00\x00\xff\x00\xff\x00", b"abc"])
        self.assertEqual(port.read(4, 0.1), b"\x00\x00\xff\x00")
        self.assertEqual(port.read_exact(5, 0.1), b"\xff\x00abc")
        self.assertEqual(set(handle.reads), {64})
        self.assertEqual(port.read(4, 0.01), b"")

    def test_zero_length_packets_are_skipped(self):
        port, _ = self.open(CP2102(), [b"", b"x"])
        self.assertEqual(port.read(4, 0.5), b"x")

    def test_flush_input_drains(self):
        port, handle = self.open(CH340(), [b"old", b"older"])
        port.flush_input()
        self.assertEqual(handle.packets, [])
        self.assertEqual(port.read(4, 0.01), b"")

    def test_write(self):
        port, handle = self.open(CH340())
        port.write(b"\x55\x55")
        self.assertEqual(handle.written, [(0x02, b"\x55\x55")])

    def test_unplug(self):
        port, _ = self.open(CP2102(), [usbfs.UsbError("endpoint 0x82: No such device")])
        with self.assertRaises(SerialError) as caught:
            port.read(4, 0.1)
        self.assertIn("disconnected", str(caught.exception))

    def test_no_access_names_the_fix(self):
        device = CP2102()

        class Denied(FakeHandle):
            def open(self):
                raise usbfs.UsbError("/dev/bus/usb/001/005: Permission denied")

        originals = usb_serial.find_device, usbfs.UsbHandle
        usb_serial.find_device = lambda path: device
        usbfs.UsbHandle = lambda dev: Denied(dev)
        try:
            with self.assertRaises(SerialError) as caught:
                usb_serial.UsbSerialPort("usb:1-1.2").open()
        finally:
            usb_serial.find_device, usbfs.UsbHandle = originals
        self.assertIn("install.sh", str(caught.exception))

    def test_make_port(self):
        self.assertIsInstance(usb_serial.make_port("usb:1-1"), usb_serial.UsbSerialPort)
        self.assertNotIsInstance(usb_serial.make_port("/dev/ttyUSB0"), usb_serial.UsbSerialPort)


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.devices = [CH340(), ESP32C3(), FakeDevice(0x0c45, 0x6366, [iface(0, 0x0E)], "1-1.3")]
        for device in self.devices:
            os.makedirs(device.sysfs_path, exist_ok=True)
        self.addCleanup(lambda: [shutil.rmtree(d.sysfs_path, True) for d in self.devices])
        original = usbfs.list_devices
        usbfs.list_devices = lambda: self.devices
        self.addCleanup(setattr, usbfs, "list_devices", original)

    def test_chips(self):
        self.assertEqual(usb_serial.chip_of(CH340())[0], "ch341")
        self.assertEqual(usb_serial.chip_of(CP2102())[0], "cp210x")
        found = usb_serial.chip_of(ESP32C3())
        self.assertEqual((found[0], found[1].number), ("acm", 1))
        self.assertIsNone(usb_serial.chip_of(self.devices[2]))

    def test_ports_are_probed_without_kernel_drivers(self):
        ports = [p for p in detect.candidate_ports("", []) if p.startswith("usb:")]
        self.assertEqual(ports, ["usb:kiln_nfc_test_1-1.2", "usb:kiln_nfc_test_1-1.4"])

    def test_kernel_bound_devices_are_left_alone(self):
        device = self.devices[0]
        bound = os.path.join(device.sysfs_path, os.path.basename(device.sysfs_path) + ":1.0")
        os.makedirs(os.path.join(bound, "driver"), exist_ok=True)
        self.assertEqual([p for p in usb_serial.usb_ports() if "1-1.2" in p[0]], [])


if __name__ == "__main__":
    unittest.main()
