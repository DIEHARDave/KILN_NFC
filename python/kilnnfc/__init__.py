"""kiln_nfc: NFC/RFID filament tags for Kiln OS (Flashforge AD5X), ported
from zmod_nfc.

Everything in this package uses only the Python standard library (Kiln
runs Klipper under Python 3.14; the code stays compatible with 3.8).
Readers are reached through termios or user-space usbfs drivers
(USB-serial bridges) and raw usbfs ioctls (CCID readers), so no pyserial,
libusb, pcscd or pyscard is needed.
"""

__version__ = "0.1.0"
