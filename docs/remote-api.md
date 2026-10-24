# Network reader API

Readers on the network, such as
[LaneTag](https://github.com/DIEHARDave/LaneTag), send tag
reads to kiln_nfc **through Moonraker**. kiln_nfc registers Klipper webhooks
endpoints, and Moonraker publishes every Klipper endpoint on its own HTTP
API and websocket, so there is no extra port or server on the printer:

| Endpoint | Moonraker URL |
|----------|---------------|
| `kiln_nfc/scan` | `POST http://<printer>:7125/printer/kiln_nfc/scan` |
| `kiln_nfc/assign` | `POST http://<printer>:7125/printer/kiln_nfc/assign` |
| `kiln_nfc/cancel` | `POST http://<printer>:7125/printer/kiln_nfc/cancel` |
| `kiln_nfc/info` | `GET http://<printer>:7125/printer/kiln_nfc/info` |

Over the websocket the same endpoints are JSON-RPC methods `printer.kiln_nfc.scan`
and so on. Bodies are JSON objects; Moonraker wraps replies as
`{"result": ...}` and errors as HTTP 400 with
`{"error": {"message": "..."}}`. Moonraker's authorization applies: a reader
whose address is not in `[authorization] trusted_clients` sends an API key
(`X-Api-Key` header).

A reader sends the raw read, not decoded filament data, so the printer
decodes every format it knows and handles the scan like one from a USB
reader: Spoolman lookup, the slot prompt on the printer's screen, filament
insert picking the slot, and the slot's material and color.

## POST /printer/kiln_nfc/scan

One tag read:

```json
{
  "reader": "LaneTag lanetag-a1b2",
  "uid": "04A1B2C3D4E5F6",
  "kind": "type2",
  "sak": 0,
  "atqa": "0044",
  "memory": "<base64>",
  "sector_keys": {"0": "default", "1": "Bambu"},
  "ndef": "<base64>",
  "incomplete": false
}
```

| Field | |
|-------|--|
| `uid` | Tag UID, hex, 4 to 10 bytes. |
| `kind` | `type2` (NTAG / Ultralight), `mifare_classic`, `type4` (ISO-DEP) or `type5` (ISO 15693). |
| `memory` | The tag's memory from its first page or block, as on the datasheet. Type 2: pages 0..n. MIFARE Classic: every block (1024 or 4096 bytes); sectors that were not read and sector trailers are zero. Type 5: blocks 0..n. Type 4: empty. At most 8 KiB. |
| `sector_keys` | MIFARE Classic: which key family opened each sector that was read: `Bambu`, `Creality`, `Snapmaker` or `default`. The vendor decoders only accept data from sectors opened with their family's keys, derived from the UID as in `python/kilnnfc/decoders/vendors.py`. |
| `ndef` | Type 4: the NDEF file. |
| `block_size` | Type 5 block size (default 4). |
| `sak`, `atqa` | Optional, from anticollision. |
| `incomplete` | The tag left the reader before it was fully read; nothing is done. |
| `slot` | Optional. A reader fixed to one slot (one reader per IFS slot or toolhead) names it here: the spool is assigned to that slot straight away, without the prompt. `0` or missing: prompt as usual. |

Klipper endpoints must answer at once, and decoding plus the Spoolman
lookup take longer, so the scan is accepted and processed in the
background:

```json
{"result": {"accepted": true, "id": "9f3a61c2"}}
```

The outcome then appears in the `kiln_nfc` printer object. Poll
`GET /printer/objects/query?kiln_nfc=remote` (or subscribe) until
`remote.id` is the scan's id:

```json
{"result": {"status": {"kiln_nfc": {"remote": {
  "id": "9f3a61c2",
  "outcome": "prompt",
  "scan": {"uid": "...", "format": "Bambu", "title": "Bambu Lab PLA Matte",
           "material": "PLA", "color": "000000", "spool_id": 12, "filament": {}},
  "spoolman": "",
  "slot": null,
  "message": ""
}}}}}
```

`outcome` is `prompt` (the slot prompt is open), `assigned` (the request
named a `slot`, or the printer has a single slot; `message` says what was
set), `no_slot` (the printer has no such slot), `disabled` (`NFC_DISABLE`), `printing` (scans are ignored while
printing unless `scan_while_printing`), `no_data`, or `error` (with
`error`). `spoolman` explains a missing spool ("not in Spoolman", ...). An
`incomplete` read is answered at once with
`{"accepted": false, "outcome": "incomplete"}`.

## GET /printer/kiln_nfc/info

The printer's slots, the scan waiting for a slot and the last network
scan's outcome:

```json
{"result": {
  "plugin": "kiln_nfc", "api": 1, "enabled": true, "printing": false,
  "pending": null, "remote": null,
  "toolchanger": false, "current": 1,
  "slots": [
    {"slot": 1, "label": "Slot 1", "material": "PLA", "color": "FFFFFF", "loaded": true}
  ]
}}
```

A 404 from Moonraker means kiln_nfc is not installed, is older than this
API, or Klipper is not running.

## POST /printer/kiln_nfc/assign

Answer the open prompt from the reader: `{"slot": 2}`. Fails with
`NFC: no scanned tag is waiting for a slot` when the prompt was closed or
timed out; send the scan again first.

```json
{"result": {"message": "NFC: Slot 2 is now Spoolman spool 12 (Bambu Lab PLA Matte)", "scan": {}}}
```

## POST /printer/kiln_nfc/cancel

Close the prompt. Body `{}`.
