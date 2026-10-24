# PN5180 / RC522 USB bridge protocol (v1)

The PN5180 and the RC522 (MFRC522) have no USB interface: both only talk
SPI. To use either on a printer, a small microcontroller bridges it to USB
and appears as a USB serial port (CDC-ACM, or a USB-UART chip). The same
bridge and the same firmware serve both chips. kiln_nfc runs the whole chip
command layer on the printer
([`python/kilnnfc/readers/pn5180.py`](../python/kilnnfc/readers/pn5180.py),
[`python/kilnnfc/readers/rc522.py`](../python/kilnnfc/readers/rc522.py)), so the bridge
firmware only moves SPI frames.

**Status:** the host side is written. No bridge firmware has been written
yet, and no hardware has been chosen for it. Any board that can do SPI and
USB serial will do (RP2040, ESP32-S3, STM32, ATmega32U4 with level
shifting).

## Wiring

### PN5180

| PN5180 | Bridge MCU |
|--------|------------|
| `NSS`  | any GPIO (chip select, active low) |
| `MOSI`, `MISO`, `SCK` | SPI, mode 0, up to 7 MHz (1-2 MHz is plenty) |
| `BUSY` | any GPIO input |
| `RST`  | any GPIO output (active low) |
| `5V`   | 5 V (RF stage) |
| `3V3`  | 3.3 V (logic) |
| `GND`  | GND |

### RC522

The RC522 is wired to the same bridge pins. It has no `BUSY` line, so the
bridge's `BUSY` input is tied to GND (the PN5180 probe that `reader: auto`
runs first then fails quickly instead of waiting on a floating pin).

| RC522 | Bridge MCU |
|-------|------------|
| `SDA` (`NSS`) | the chip-select GPIO |
| `MOSI`, `MISO`, `SCK` | SPI, mode 0, up to 10 MHz (1-2 MHz is plenty) |
| `RST` | the reset GPIO (or 3.3 V; kiln_nfc soft-resets the chip) |
| `3.3V` | 3.3 V. **Not 5 V**: the RC522 is a 3.3 V part, logic included. |
| `GND` | GND |
| `IRQ` | not connected |
| (bridge `BUSY` input) | GND |

The common blue RC522 boards come wired for SPI. Their small antenna gives
a read range of a few centimetres, enough for a spool held at the reader.

The RC522 reads ISO 14443A tags (NTAG, MIFARE Ultralight, MIFARE Classic)
like the PN532. It cannot read ISO 15693 (OpenPrintTag); use the PN5180 for
those.

## Framing

Both directions use the same frame:

```
0xA5 | CMD | LEN_L | LEN_H | PAYLOAD (LEN bytes) | CRC8
```

* `CRC8` uses polynomial 0x07 with initial value 0 (CRC-8/SMBUS), computed
  over `CMD`, `LEN_L`, `LEN_H` and `PAYLOAD`.
* The bridge answers every request with `CMD | 0x80`. The first payload byte
  of the answer is a status: `0` for success, anything else for an error.
* Serial settings: 115200 8N1. A CDC-ACM bridge can ignore the baud rate.

## Commands

### 0x01 HELLO

Request payload: empty.
Answer payload: `00` followed by `PN5180BRIDGE` and the firmware version as
ASCII, e.g. `PN5180BRIDGE1.0`. kiln_nfc uses this to recognise the bridge
while probing serial ports.

### 0x02 RESET

Request payload: empty.
The bridge pulls `RST` low for at least 10 µs, releases it, and waits for
`BUSY` to go low (with a timeout of about 100 ms).
Answer payload: `00`.

### 0x03 EXCHANGE

Request payload: `RX_LEN_L RX_LEN_H TX...`

The bridge performs one PN5180 host-interface transaction:

1. Wait for `BUSY` low.
2. Assert `NSS`, clock out the `TX` bytes, release `NSS`.
3. Wait for `BUSY` low.
4. If `RX_LEN > 0`: assert `NSS`, clock out `RX_LEN` bytes of `0xFF` while
   reading `MISO`, release `NSS`, then wait for `BUSY` low again.

Answer payload: `00` followed by the `RX_LEN` bytes that were read.

Each `BUSY` wait times out after 1 s. On a timeout, answer with status `1`
and nothing else.

The PN5180 driver uses this command.

### 0x04 TRANSFER

Request payload: `TX...`

The bridge performs one full-duplex SPI transaction, without looking at
`BUSY`: assert `NSS`, clock out the `TX` bytes while reading `MISO`,
release `NSS`.

Answer payload: `00` followed by as many bytes as were sent, each the byte
read while the matching `TX` byte was clocked out.

The RC522 driver uses this command: one transaction per register access.

## Examples

The host reads the PN5180 firmware version (EEPROM address 0x12, 2 bytes)
with `READ_EEPROM` (command 0x07):

```
host   -> A5 03 05 00 02 00 07 12 02 CRC
bridge <- A5 83 03 00 00 03 04 CRC        (firmware 4.3)
```

The host reads the RC522's `VersionReg` (register 0x37: address byte
`0x80 | 0x37 << 1` = `0xEE`, then a dummy byte):

```
host   -> A5 04 02 00 EE 00 CRC
bridge <- A5 84 03 00 00 00 92 CRC        (MFRC522 version 2.0)
```
