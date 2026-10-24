#!/usr/bin/env python3
"""Read tags without Klipper, to check a reader and see what a tag holds.

On the AD5X with Kiln OS, over SSH:
    python3 /data/ad5x/plugins/nfc/tools/nfc_scan.py --raw

Stop Klipper's use of the reader first (NFC_DISABLE is not enough: the
plugin keeps the reader open), e.g. DISABLE_PLUGIN NAME=nfc, or run this
before enabling the plugin. As root it reaches USB readers without the
root hook.
"""

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                                "python"))

from kilnnfc import decoders  # noqa: E402
from kilnnfc.readers.base import ReaderError  # noqa: E402
from kilnnfc.readers.detect import READER_TYPES, open_reader  # noqa: E402
from kilnnfc.transport import usbfs  # noqa: E402
from kilnnfc.transport.serial_port import usb_serial_ports  # noqa: E402
from kilnnfc.transport.usb_serial import usb_ports  # noqa: E402


def list_hardware():
    print("USB devices:")
    for dev in usbfs.list_devices():
        classes = ",".join("%02x" % i.cls for i in dev.interfaces)
        print("  %s  %s  interfaces %s" % (dev.node, dev.describe(), classes or "-"))
    print("USB serial ports:")
    for path, vid, pid, product in usb_serial_ports() + usb_ports():
        print("  %s  %s:%s %s" % (path, vid, pid, product))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reader", default="auto", choices=READER_TYPES)
    parser.add_argument("--device", default="")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--ccid", default="", help="CCID reader name/VID:PID filter")
    parser.add_argument("--once", action="store_true", help="exit after one tag")
    parser.add_argument("--raw", action="store_true", help="print the memory dump")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument("--list", action="store_true", help="list USB hardware and exit")
    parser.add_argument("--key", action="append", default=[], metavar="NAME=HEX",
                        help="vendor key: bambu_key, creality_key, creality_aes_key, snapmaker_key")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    if args.list:
        list_hardware()
        return 0

    settings = {"extra_mifare_keys": []}
    for item in args.key:
        name, _, value = item.partition("=")
        settings[name.strip()] = bytes.fromhex(value.strip())

    try:
        reader = open_reader(args.reader, args.device, args.baud, args.ccid,
                             lambda uid: decoders.mifare_keysets(uid, settings))
    except ReaderError as exc:
        print("No reader: %s" % exc, file=sys.stderr)
        list_hardware()
        return 1
    print("Reader: %s. Present a tag (Ctrl-C to stop)." % reader.describe())
    try:
        while True:
            try:
                tag = reader.poll()
            except ReaderError as exc:
                print("Reader lost: %s" % exc, file=sys.stderr)
                return 1
            if tag is None:
                time.sleep(0.2)
                continue
            filament, records = decoders.decode(tag, settings)
            if args.json:
                print(json.dumps({
                    "tag": tag.describe(), "uid": tag.uid_hex,
                    "sector_keys": tag.sector_keys,
                    "records": [{"tnf": r.tnf, "type": r.type.decode("ascii", "replace"),
                                 "payload": r.payload.hex()} for r in records],
                    "filament": filament.as_dict() if filament else None,
                    "memory": (tag.memory or tag.ndef or b"").hex() if args.raw else None,
                }, indent=2))
            else:
                print("\n%s" % tag.describe())
                if tag.sector_keys:
                    print("  sectors: %s" % ", ".join("%d=%s" % kv for kv in sorted(tag.sector_keys.items())))
                for record in records:
                    print("  NDEF %r" % record)
                if filament is None:
                    print("  no known filament format")
                else:
                    for key, value in sorted(filament.as_dict().items()):
                        print("  %-12s %s" % (key, value))
                if args.raw:
                    memory = tag.memory or tag.ndef or b""
                    for offset in range(0, len(memory), 16):
                        print("  %04X: %s" % (offset, memory[offset:offset + 16].hex(" ")))
            if args.once:
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        reader.close()


if __name__ == "__main__":
    sys.exit(main())
