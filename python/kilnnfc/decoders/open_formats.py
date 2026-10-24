"""Open and DIY tag formats: OpenSpool, OpenTag3D, OpenPrintTag, SpoolEase,
Spoolman / nfc2klipper spool references and plain text.

OpenSpool, OpenTag3D and SpoolEase are adapted from OpenRFID
(https://github.com/suchmememanyskill/OpenRFID, GPL-3.0). OpenPrintTag
follows Prusa's specification (https://specs.openprinttag.org)."""

import datetime
import json
import os
import re
import urllib.parse

from ..tag import Filament, KIND_TYPE5, normalize_color
from . import cbor
from .materials import normalize

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Bed/drying defaults by material, for formats that only carry nozzle
# temperatures (from OpenRFID's SpoolEase table).
MATERIAL_DEFAULTS = {
    "PLA": (55, 50, 8), "PLA-CF": (60, 55, 8), "PETG": (70, 65, 8),
    "PETG-CF": (70, 65, 8), "ABS": (95, 80, 8), "ASA": (95, 80, 8),
    "TPU": (38, 70, 8), "PA": (110, 80, 10), "PA-CF": (110, 80, 10),
    "PC": (100, 80, 8), "PCTG": (80, 65, 6), "PVA": (45, 60, 8),
    "HIPS": (100, 70, 6), "PP": (90, 70, 12), "BVOH": (60, 60, 6),
}


def _int(value, default=None):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _with_defaults(filament):
    defaults = MATERIAL_DEFAULTS.get(filament.material or "")
    if defaults:
        bed, dry_temp, dry_time = defaults
        filament.bed_temp = filament.bed_temp or bed
        filament.dry_temp = filament.dry_temp or dry_temp
        filament.dry_time = filament.dry_time or dry_time
    return filament


# --- OpenSpool -------------------------------------------------------------

def decode_openspool(tag, records, settings):
    """NDEF application/json with "protocol": "openspool"."""
    for record in records:
        if record.mime != "application/json":
            continue
        try:
            data = json.loads(record.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or str(data.get("protocol", "")).lower() != "openspool":
            continue
        material, modifiers = normalize(data.get("type") or "PLA",
                                        [data["subtype"]] if data.get("subtype") else [])
        alpha = _int(data.get("alpha"), None)
        if isinstance(data.get("alpha"), str):
            try:
                alpha = int(data["alpha"], 16)
            except ValueError:
                alpha = None
        return _with_defaults(Filament(
            "OpenSpool",
            material=material, modifiers=modifiers,
            brand=data.get("brand") or None,
            color=data.get("color_hex"),
            alpha=0xFF if alpha is None else max(0, min(0xFF, alpha)),
            extra_colors=list(data.get("additional_color_hexes") or []),
            diameter=_float(data.get("diameter"), 1.75),
            weight=_int(data.get("weight")),
            nozzle_min=_int(data.get("min_temp")),
            nozzle_max=_int(data.get("max_temp")),
            bed_temp=_int(data.get("bed_max_temp")) or _int(data.get("bed_min_temp")),
            spool_id=_int(data.get("spool_id")),
        ))
    return None


# --- OpenTag3D -------------------------------------------------------------

OPENTAG3D_MIME = "application/opentag3d"
_OPENTAG3D_SCHEMA = None


def _opentag3d_schema():
    global _OPENTAG3D_SCHEMA
    if _OPENTAG3D_SCHEMA is None:
        with open(os.path.join(DATA_DIR, "opentag3d_v2.json"), encoding="utf-8") as handle:
            _OPENTAG3D_SCHEMA = json.load(handle)
    return _OPENTAG3D_SCHEMA


def _opentag3d_fields(payload, schema):
    values = {}
    for field in schema["core"]["fields"]:
        start = int(field["start"], 16)
        length = field["length"]
        raw = payload[start:start + length].ljust(length, b"\x00")
        kind = field["type"]
        if kind == "int":
            value = int.from_bytes(raw, "big") * field.get("scaling", 1)
        elif kind in ("utf8", "ascii"):
            value = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        elif kind == "rgba":
            value = bytes(raw)
        elif kind == "date":
            value = None
            if any(raw):
                try:
                    value = datetime.date(int.from_bytes(raw[:2], "big"), raw[2], raw[3]).isoformat()
                except ValueError:
                    pass
        else:
            value = None
        values[field["id"]] = value
    return values


def decode_opentag3d(tag, records, settings):
    for record in records:
        if record.mime != OPENTAG3D_MIME or len(record.payload) < 2:
            continue
        payload = record.payload
        version = int.from_bytes(payload[:2], "big")
        if version >= 2000:
            d = _opentag3d_fields(payload, _opentag3d_schema())
            colors = [d["color_%d" % i] for i in range(1, 5)]
            material, modifiers = normalize(d["material"], [d["material_mod"]] if d["material_mod"] else [])
            return Filament(
                "OpenTag3D",
                material=material, modifiers=modifiers,
                brand=d["manufacturer"] or None,
                name=d["color_name"] or None,
                color=colors[0][:3].hex(), alpha=colors[0][3],
                extra_colors=[c[:3].hex() for c in colors[1:] if any(c)],
                diameter=d["diameter"] or 1.75,
                weight=d["weight"] or None,
                nozzle_min=d["min_print_temp"] or d["print_temp"] or None,
                nozzle_max=d["max_print_temp"] or d["print_temp"] or None,
                bed_temp=d["bed_temp"] or None,
                dry_temp=d["max_dry_temp"] or None,
                dry_time=d["dry_time"] or None,
                external_id=d["serial"] or d["sku"] or None,
                manufactured=d["mfg_date"],
            )
        if version >= 0x000C and len(payload) >= 0x64:
            # Pre-2.0 layout (spec 0.012), as nfc2klipper reads it.
            text = lambda a, b: payload[a:b].split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
            material, modifiers = normalize(text(0x02, 0x07), [text(0x07, 0x0C)] if text(0x07, 0x0C) else [])
            return Filament(
                "OpenTag3D",
                material=material, modifiers=modifiers,
                brand=text(0x1B, 0x2B) or None, name=text(0x2B, 0x4B) or None,
                color=payload[0x4B:0x4E].hex(), alpha=payload[0x4E],
                diameter=(int.from_bytes(payload[0x5C:0x5E], "big") / 1000.) or 1.75,
                weight=int.from_bytes(payload[0x5E:0x60], "big") or None,
                nozzle_max=payload[0x60] * 5 or None,
                bed_temp=payload[0x61] * 5 or None,
            )
    return None


# --- OpenPrintTag (Prusa) --------------------------------------------------

OPENPRINTTAG_MIME = "application/vnd.openprinttag"

OPT_MATERIAL_TYPES = [
    "PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PCTG", "PP", "PA6", "PA11",
    "PA12", "PA66", "CPE", "TPE", "HIPS", "PHA", "PET", "PEI", "PBT", "PVB",
    "PVA", "PEKK", "PEEK", "BVOH", "TPC", "PPS", "PPSU", "PVC", "PEBA",
    "PVDF", "PPA", "PCL", "PES", "PMMA", "POM", "PPE", "PS", "PSU", "TPI",
    "SBS", "OBC", "EVA", "PA612",
]


def _opt_color(value):
    if isinstance(value, bytes) and len(value) >= 3:
        return value[:3].hex(), value[3] if len(value) > 3 else 0xFF
    return None, 0xFF


def decode_openprinttag(tag, records, settings):
    for record in records:
        if record.mime != OPENPRINTTAG_MIME:
            continue
        payload = record.payload
        try:
            meta, meta_end = cbor.decode(payload, 0)
            if not isinstance(meta, dict):
                continue
            main, _ = cbor.decode(payload, meta.get(0, meta_end))
            aux = {}
            if 2 in meta:
                try:
                    aux, _ = cbor.decode(payload, meta[2])
                except cbor.CborError:
                    aux = {}
        except cbor.CborError:
            continue
        if not isinstance(main, dict):
            continue
        material = main.get(52)
        material_type = main.get(9)
        if not material and isinstance(material_type, int) and material_type < len(OPT_MATERIAL_TYPES):
            material = OPT_MATERIAL_TYPES[material_type]
        material, modifiers = normalize(material)
        color, alpha = _opt_color(main.get(19))
        extra = [c for c in (_opt_color(main.get(k))[0] for k in range(20, 25)) if c]
        weight = main.get(17) or main.get(16)
        consumed = aux.get(0) if isinstance(aux, dict) else None
        if weight and isinstance(consumed, (int, float)):
            weight = max(0, weight - consumed)
        made = main.get(14)
        manufactured = None
        if isinstance(made, int) and made > 0:
            manufactured = datetime.datetime.fromtimestamp(made, datetime.timezone.utc).date().isoformat()
        instance = main.get(0)
        return _with_defaults(Filament(
            "OpenPrintTag",
            material=material, modifiers=modifiers,
            brand=main.get(11), name=main.get(10),
            color=color, alpha=alpha, extra_colors=extra,
            diameter=main.get(30) or 1.75,
            weight=int(weight) if weight else None,
            nozzle_min=main.get(34), nozzle_max=main.get(35),
            bed_temp=main.get(38) or main.get(37),
            dry_temp=main.get(57), dry_time=(main.get(58) / 60.) if main.get(58) else None,
            external_id=(instance.hex() if isinstance(instance, bytes) else main.get(5)),
            manufactured=manufactured,
        ))
    return None


# --- SpoolEase -------------------------------------------------------------

SPOOLEASE_ALIASES = {"ABS-S": "ABS", "PA-S": "PA", "PLA-S": "PLA", "TPU-AMS": "TPU"}


def decode_spoolease(tag, records, settings):
    """URI records https://tag.spoolease.io/S1?M=PLA&CC=FF0000FF&..."""
    for record in records:
        if not record.is_uri():
            continue
        parsed = urllib.parse.urlparse(record.uri())
        if parsed.netloc.lower() != "tag.spoolease.io" or not parsed.path.startswith("/S1"):
            continue
        # Split on "&" only: colors are ";"-separated, and Python before
        # 3.8.8 (Klipper's 3.8.2 on the AD5X) also splits parse_qs on ";".
        query = {}
        for part in parsed.query.split("&"):
            key, _, value = part.partition("=")
            query.setdefault(urllib.parse.unquote_plus(key), urllib.parse.unquote_plus(value))
        get = lambda key, default=None: query.get(key) or default
        raw = str(get("M", "PLA")).upper()
        material, modifiers = normalize(SPOOLEASE_ALIASES.get(raw, raw),
                                        [get("MS")] if get("MS") else [])
        colors = [c for c in str(get("CC", "FFFFFFFF")).split(";") if c.strip()]
        alpha = 0xFF
        if colors and len(colors[0].strip().lstrip("#")) == 8:
            alpha = int(colors[0].strip().lstrip("#")[6:], 16)
        return _with_defaults(Filament(
            "SpoolEase",
            material=material, modifiers=modifiers, brand=get("B"),
            color=colors[0] if colors else None, alpha=alpha,
            extra_colors=colors[1:5],
            weight=_int(get("WL")), nozzle_min=_int(get("NN")), nozzle_max=_int(get("NX")),
        ))
    return None


# --- Spoolman / nfc2klipper references --------------------------------------

_SPOOLMAN_URL = re.compile(r"(?:web\+spoolman:s-|/spool/show/)(\d+)", re.I)
_SPOOLMAN_FILAMENT_URL = re.compile(r"(?:web\+spoolman:f-|/filament/show/)(\d+)", re.I)


def decode_spoolman(tag, records, settings):
    """Tags that name a Spoolman spool:
      - nfc2klipper text records "SPOOL:23\\nFILAMENT:14"
      - Spoolman QR/label links "web+spoolman:s-23" or ".../spool/show/23"
    """
    spool = filament = None
    for record in records:
        if record.is_text():
            for line in record.text().splitlines():
                key, _, value = line.partition(":")
                key = key.strip().upper()
                if key == "SPOOL" and value.strip().isdigit():
                    spool = int(value)
                elif key == "FILAMENT" and value.strip().isdigit():
                    filament = int(value)
        elif record.is_uri():
            uri = record.uri()
            match = _SPOOLMAN_URL.search(uri)
            if match:
                spool = int(match.group(1))
            match = _SPOOLMAN_FILAMENT_URL.search(uri)
            if match:
                filament = int(match.group(1))
    if spool is None and filament is None:
        return None
    return Filament("Spoolman", spool_id=spool, filament_id=filament)


# --- Plain text ------------------------------------------------------------

# "PLA:FF0000", "PETG #00FF00", "PLA;FF0000;Brand".
_TEXT_FORMAT = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9+\- ]{1,15}?)\s*[:;,# ]\s*#?([0-9A-Fa-f]{6})\b"
    r"(?:\s*[:;,]\s*(.+))?$")


def decode_text(tag, records, settings):
    """Simple text tags: a material and a color, as quick DIY tags carry
    them; also a JSON object with material/type and color/color_hex keys."""
    for record in records:
        if not (record.is_text() or record.mime in ("text/plain", "application/json")):
            continue
        text = record.text().strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict) and str(data.get("protocol", "")).lower() != "openspool":
                material = data.get("material") or data.get("type")
                color = data.get("color_hex") or data.get("color")
                if material and normalize_color(color):
                    material, modifiers = normalize(material)
                    return _with_defaults(Filament(
                        "Text", material=material, modifiers=modifiers,
                        color=color, brand=data.get("brand") or data.get("vendor"),
                        spool_id=_int(data.get("spool_id"))))
            continue
        match = _TEXT_FORMAT.match(text)
        if match:
            material, modifiers = normalize(match.group(1))
            return _with_defaults(Filament(
                "Text", material=material, modifiers=modifiers,
                color=match.group(2), brand=(match.group(3) or "").strip() or None))
    return None
