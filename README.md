# KILN_NFC

A [Kiln OS](https://github.com/djvalic/kiln-os) plugin for the
**Flashforge AD5X** that reads **NFC/RFID filament tags**. Hold a tagged
spool to a reader, pick the IFS slot it goes in (or just insert the
filament into an empty slot), and the slot gets the spool's material,
exact color and nozzle temperature. If Spoolman is set up, the slot also
gets its Spoolman spool.

```
NFC tag ──► decode (Bambu, Creality, Snapmaker, OpenSpool, OpenPrintTag, ...)
        ──► look up in Spoolman (spool ID from the tag, or the tag's UID)
        ──► prompt: [Slot 1] [Slot 2] [Slot 3] [Slot 4]  or insert filament
        ──► IFS_SET_MATERIAL: type, color, temperature
            (+ SPOOLMAN_SET_SLOT with Kiln's spoolman plugin)
```

It is a port of [zmod_nfc](https://github.com/DIEHARDave/zmod_nfc) (the
same plugin for zMod) to Kiln OS: the same readers, tag formats, Spoolman
handling and prompt, writing into Kiln's own IFS slots instead of zMod's.

> **Status: early.** Running on an AD5X with Kiln OS 2026.02.3: the plugin
> loads, and a CH340 "PN532 V2.0" board is found and talks to the plugin
> through its user-space USB serial driver. Reading a tag into a slot on
> Kiln has not been tried yet, nor CCID readers, CP210x boards or ESP32
> bridges. The decoders, readers and Kiln integration have unit tests,
> which pass under Kiln's Python 3.14. Reports are very welcome.

## Readers

Kiln OS on the AD5X runs Flashforge's own 5.10 kernel, which has **no
USB serial drivers** (no `ch341`, `cp210x` or `cdc_acm`): a PN532 board
or an ESP32 never gets a `/dev/ttyUSB*` or `/dev/ttyACM*`. KILN_NFC
drives those chips itself over usbfs, so they work anyway, once the
[root hook](#install) gives Klipper access to them.

| Reader | Connection | Tags | Notes |
|--------|------------|------|-------|
| **ACR122U**, **ACR1552U**, other PC/SC readers | USB (CCID) | ISO 14443A (+ ISO 15693 on the ACR1552U) | Driven directly over usbfs; no pcscd. Tested with zmod_nfc on zMod. |
| **PN532** | USB serial board (CH340, CP210x) | ISO 14443A | Through KILN_NFC's own CH340 / CP210x driver. Set the board to **HSU/UART** mode. |
| **[esp32_rfidnfc2usb_bridge](https://github.com/DIEHARDave/esp32_rfidnfc2usb_bridge)** | USB: an ESP32 with a PN532, RC522 or PN5180, seen as a PN532 | ISO 14443A | ESP32-C3/S3/C6 native USB through KILN_NFC's CDC-ACM driver; classic ESP32 boards through their CP210x or CH340. |
| **[LaneTag](https://github.com/DIEHARDave/LaneTag)** | Wi-Fi | ISO 14443A; ISO 15693 with a PN5180 | Needs no root hook. LaneTag doesn't know KILN_NFC yet (it looks for zmod_nfc); see [Network readers](#network-readers). |

PL2303 and FTDI adapters only work on a kernel that has their drivers.

`reader: auto` (the default) uses the first CCID reader it finds, then
probes USB serial chips for a PN532 (a bridge answers as one) or a
PN5180/RC522 bridge. It only probes chips from known bridge vendors.

## Tag formats

| Format | Tag | Notes |
|--------|-----|-------|
| [OpenPrintTag](https://specs.openprinttag.org) (Prusa) | ICODE SLIX2 (ISO 15693) | ACR1552U only. |
| [OpenSpool](https://openspool.io) | NTAG21x | JSON NDEF record. |
| [OpenTag3D](https://opentag3d.info) | NTAG21x / ISO 15693 | |
| [TigerTag](https://tigertag.io) | NTAG21x | Offline ID database. |
| Bambu Lab, Creality (CFS), Snapmaker (U1), Qidi | MIFARE Classic 1K | Keys built in, from their public write-ups. |
| Anycubic (ACE), Elegoo | NTAG21x | |
| [SpoolEase](https://spoolease.io) | NTAG21x | URL record. |
| Spoolman references | any NDEF tag | `web+spoolman:s-<id>`, `.../spool/show/<id>`, nfc2klipper `SPOOL:<id>`. |
| Plain text | any NDEF tag | `PLA:FF0000`, `PETG #00FF00`, `PLA Silk;123456;Brand`. |
| Any other tag | | Its UID is still matched against Spoolman spools. |

The [zmod_nfc README](https://github.com/DIEHARDave/zmod_nfc#tag-formats)
has the details and credits for each format.

## Install

Over SSH as root (Kiln's default password is `root`):

```sh
cd /data/ad5x/plugins
su klipper -s /bin/sh -c 'git clone https://github.com/DIEHARDave/KILN_NFC.git nfc'
sh /data/ad5x/plugins/nfc/install.sh
```

Then in the console (Fluidd, Mainsail or HelixScreen):

```
ENABLE_PLUGIN NAME=nfc
```

This restarts Klipper. Plug the reader in and run `NFC_STATUS`: it names
the reader once it is found, and says what is missing if it isn't.

**The root hook** (`install.sh`) is only needed for USB readers. Kiln's
`/dev/bus/usb` nodes belong to root, and a plugin runs as user `klipper`.
`install.sh` copies `root/nfc-hook.sh` to `/data/ad5x/nfc-hook.sh` and
adds a line to `/data/ad5x/camera.conf`, which Kiln's camera service
sources as root at every boot (KILN_Camera and KILN_Mainsail use the
same file). The hook runs a small watcher that, whenever USB devices
come or go, gives group `klipper` access to devices that can be readers:
CCID-class devices and USB serial chips from the vendors above. Nothing
else is changed. `uninstall.sh` removes it.

To remove the plugin: `DISABLE_PLUGIN NAME=nfc`, `sh uninstall.sh` as
root, then delete `/data/ad5x/plugins/nfc`.

## Updates

Fluidd > Settings > Software Updates lists `kiln-nfc`. After an update,
`RESTART` Klipper. If `NFC_STATUS` then says the USB hook is outdated,
run `install.sh` again as root.

## Configuration

[`plugin.cfg`](plugin.cfg) lists every option. Updates replace it, so put
your settings in `local.cfg`:

```ini
[kiln_nfc]
prompt_timeout: 90
spoolman_auto_create: True
```

Klipper merges both `[kiln_nfc]` sections, and yours win.

## Using it

1. Hold a tag to the reader. A prompt appears in Fluidd, Mainsail and
   HelixScreen with the filament and one button per IFS slot. Empty slots
   are highlighted.
2. Tap a slot, **or insert the filament into an empty slot** before the
   prompt times out.
3. The slot gets its material, color and nozzle temperature through
   Kiln's `IFS_SET_MATERIAL`, so HelixScreen, auto-refill and OrcaSlicer
   (through Kiln's lane data) see it.

Kiln takes any material name, so the tag's material is kept as it is
(ASA stays ASA, `PLA Silk` becomes `PLA-SILK`). The color is the exact
color from the tag or Spoolman; there is no palette to snap to.

### With Spoolman

When Moonraker has a `[spoolman]` section, tags are looked up the same
way as in zmod_nfc: a spool ID on the tag, then the tag's UID in the
spools' `card_uids` (or nfc2klipper's `nfc_id`) field, then a unique
vendor + material + color match. Unknown tags can be added with the
prompt's **Add to Spoolman** button, and the tag's UID is recorded on the
spool it is assigned as.

With **Kiln's spoolman plugin** on (`INSTALL_PLUGIN NAME=spoolman
ENABLE=1`), the assignment runs `SPOOLMAN_SET_SLOT`: each slot keeps its
spool, and the spool becomes Moonraker's active spool whenever that slot
loads. Without it, the spool is made active only when its slot is the one
loaded at the time.

## Network readers

KILN_NFC answers network readers through Moonraker, as zmod_nfc does,
under `kiln_nfc/scan`, `kiln_nfc/assign`, `kiln_nfc/cancel` and
`kiln_nfc/info`, with the outcome in `printer.kiln_nfc.remote`
([docs/remote-api.md](docs/remote-api.md)). LaneTag's "zmod_nfc" method
still uses the zmod_nfc names, so it needs an update before it can send
to KILN_NFC.

## Not ported (yet)

- **Camera QR labels** (zmod_nfc's `NFC_QR_SCAN`): Kiln has no ffmpeg or
  libturbojpeg to decode the camera's JPEGs.
- **Writing lane data**: Kiln's own `kiln_lanes` Moonraker component
  mirrors the IFS slots into lane data for OrcaSlicer, so KILN_NFC leaves
  it alone. That data has no Spoolman spool ID or vendor yet.

## Commands

| Command | |
|---------|--|
| `NFC_STATUS` | Reader state, the last tag, formats and keys, and any problem with the USB hook. |
| `NFC_ASSIGN SLOT=<n>` | Assign the scanned spool to a slot (the prompt's buttons run this). |
| `NFC_CANCEL` | Close the prompt. |
| `NFC_PROMPT` | Show the prompt for the last tag again. |
| `NFC_CREATE_SPOOL` | Add the scanned filament to Spoolman. |
| `NFC_DUMP [RAW=1]` | Everything read from the last tag; `RAW=1` adds a memory dump. |
| `NFC_RESCAN` | Read the tag on the reader again. |
| `NFC_TEST_SCAN TEXT=PLA:FF0000` / `SPOOL=<id>` / `UID=<hex>` | Simulate a scan, to try the prompt and Spoolman flow without a reader. |
| `NFC_ENABLE` / `NFC_DISABLE` | Resume or ignore scans. |

`printer.kiln_nfc` reports `state`, `reader`, `error`, `pending`,
`remote` and the `last` scan (UID, format, material, color, spool ID).

## Testing a reader without Klipper

[`tools/nfc_scan.py`](tools/nfc_scan.py) lists USB devices and reads tags.
Disable the plugin first so it doesn't hold the reader:

```sh
python3 /data/ad5x/plugins/nfc/tools/nfc_scan.py --list
python3 /data/ad5x/plugins/nfc/tools/nfc_scan.py --raw
```

## Development

```sh
cd tests
python3 -m unittest
```

The tests need Linux (`fcntl`, `termios`); run them on the printer.
`python/kilnnfc` is shared with zmod_nfc's `zmodnfc` package apart from
`klipper_plugin.py` and `transport/usb_serial.py`; fixes to decoders and
readers belong in both.

## Credits

The decoders come from zmod_nfc, which adapts most of them from
[OpenRFID](https://github.com/suchmememanyskill/OpenRFID) (GPL-3.0); see
[its credits](https://github.com/DIEHARDave/zmod_nfc#credits). The USB
serial chip setup follows the Linux kernel's `ch341`, `cp210x` and
`cdc-acm` drivers.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
