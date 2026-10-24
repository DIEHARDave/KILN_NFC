"""Vendor tag formats: Bambu Lab, Creality, Snapmaker, Qidi, Anycubic,
Elegoo and TigerTag.

Adapted from OpenRFID (https://github.com/suchmememanyskill/OpenRFID,
GPL-3.0), which builds on the Bambu Research Group RFID guide, DnG-Crafts
ACE-RFID and the TigerTag project. Ported to Python 3.8 and to the pure
Python crypto in crypto.py.

The Bambu, Creality and Snapmaker keys are built in, from their public
write-ups (Bambu Research Group RFID guide, the Creality CFS RFID research,
DnG-Crafts U1-RFID). Each matches the SHA-256 below; the [kiln_nfc] options
can override them."""

import datetime
import json
import os
import struct

from ..tag import Filament, KIND_MIFARE_CLASSIC, KIND_TYPE2
from ..readers.base import KeySet
from .crypto import aes128_ecb, hkdf_sha256
from .materials import normalize

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FACTORY_KEY = b"\xff" * 6

# SHA-256 of each vendor key, to validate keys pasted into the config.
BAMBU_KEY_SHA256 = "19cc3c63cb8802668800c3b3bf3fee05b3c59bf59fc5fd256b68e868084ec304"
CREALITY_KEY_SHA256 = "e544d94feb16159bbd7bc227df1e283eca1f38f2bb2015dfcc6161b74473b5c2"
CREALITY_AES_KEY_SHA256 = "acec2106007458579ba522b25610b2cf509ae59d7879cb975f65c45228e5c9a1"
SNAPMAKER_KEY_SHA256 = "19aee31a8bcadb0becc08bbaa9197ab403f9c7a2097197d127a2ab7010a7867a"


def _ascii(data):
    return data.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()


def _opened_with(tag, name, sector=1):
    return tag.kind == KIND_MIFARE_CLASSIC and tag.sector_keys.get(sector) == name


def _weight_from_length(meters, table):
    return table.get(meters, 1000)


# --- Bambu Lab -------------------------------------------------------------

BAMBU_DEFAULT_KEY = bytes.fromhex("9a759cf2c4f7caff222cb9769b41bc96")
CREALITY_DEFAULT_KEY = b"q3bu^t1nqfZ(pf$1"
CREALITY_DEFAULT_AES_KEY = b"H@CFkRnz@KAtBJp2"


def bambu_keysets(uid, settings):
    key = settings.get("bambu_key") or BAMBU_DEFAULT_KEY
    okm = hkdf_sha256(uid[:4], key, b"RFID-A\x00", 6 * 16)
    return [KeySet("Bambu", [okm[i * 6:(i + 1) * 6] for i in range(16)])]


def decode_bambu(tag, records, settings):
    if not _opened_with(tag, "Bambu"):
        return None
    d = tag.memory
    material = _ascii(d[32:48])
    detailed = _ascii(d[64:80])
    modifier = detailed[len(material):].strip() if detailed.startswith(material) else detailed
    base, modifiers = normalize(material, [modifier] if modifier and modifier != material else [])
    extra = []
    if struct.unpack_from("<H", d, 256)[0] == 0x0002 and struct.unpack_from("<H", d, 258)[0] > 1:
        abgr = d[260:264]
        extra.append(bytes([abgr[3], abgr[2], abgr[1]]).hex())
    made = _ascii(d[192:208]).split("_")
    manufactured = None
    if len(made) >= 3:
        manufactured = "%s-%s-%s" % (made[0], made[1].zfill(2), made[2].zfill(2))
    return Filament(
        "Bambu",
        material=base, modifiers=modifiers, brand="Bambu Lab",
        color=d[80:83].hex(), alpha=d[83], extra_colors=extra,
        weight=struct.unpack_from("<H", d, 84)[0] or None,
        diameter=round(struct.unpack_from("<f", d, 88)[0], 2) or 1.75,
        dry_temp=struct.unpack_from("<H", d, 96)[0] or None,
        dry_time=struct.unpack_from("<H", d, 98)[0] or None,
        bed_temp=struct.unpack_from("<H", d, 102)[0] or None,
        nozzle_max=struct.unpack_from("<H", d, 104)[0] or None,
        nozzle_min=struct.unpack_from("<H", d, 106)[0] or None,
        external_id=_ascii(d[24:32]) or None,      # Bambu filament ID, e.g. GFA00
        manufactured=manufactured,
    )


# --- Creality --------------------------------------------------------------

# Creality material code -> (material, modifiers, nozzle min, max, bed,
# dry temp, dry hours)
CREALITY_MATERIALS = {
    "01001": ("PLA", ["Hyper"], 190, 240, 50, 50, 8),
    "02001": ("PLA-CF", ["Hyper"], 190, 240, 50, 50, 8),
    "06002": ("PETG", ["Hyper"], 220, 270, 70, 60, 8),
    "03001": ("ABS", ["Hyper"], 240, 280, 80, 80, 8),
    "09002": ("PLA", ["Ender", "Fast"], 190, 240, 50, 50, 6),
    "04001": ("PLA", ["CR"], 190, 240, 50, 50, 8),
    "05001": ("PLA", ["CR", "Silk"], 190, 240, 50, 50, 8),
    "06001": ("PETG", ["CR"], 220, 270, 70, 60, 8),
    "07001": ("ABS", ["CR"], 240, 280, 100, 80, 8),
    "08001": ("PLA", ["Ender"], 190, 240, 50, 50, 8),
    "09001": ("PLA", ["EN", "+"], 190, 240, 50, 50, 8),
    "10001": ("TPU", ["HP"], 190, 240, 40, 65, 8),
    "11001": ("PA", ["CR", "Nylon"], 250, 270, 50, 80, 8),
    "13001": ("PLA", ["CR", "Carbon"], 190, 240, 50, 50, 8),
    "14001": ("PLA", ["CR", "Matte"], 190, 240, 50, 50, 8),
    "15001": ("PLA", ["CR", "Fluo"], 190, 240, 50, 50, 8),
    "16001": ("TPU", ["CR"], 210, 240, 40, 65, 8),
    "17001": ("PLA", ["CR", "Wood"], 190, 240, 50, 50, 8),
    "18001": ("PLA", ["HP", "Ultra"], 190, 240, 50, 50, 8),
    "19001": ("ASA", ["HP"], 240, 280, 90, 80, 8),
    "12003": ("PA-CF", ["Hyper", "PAHT"], 280, 320, 90, 80, 10),
    "12002": ("PA-CF", ["Hyper", "PPA"], 280, 320, 100, 100, 8),
    "07002": ("PC", ["Hyper"], 250, 270, 110, 80, 8),
    "01601": ("PLA", ["Soleyin", "Ultra"], 190, 240, 50, 50, 8),
}


def creality_keysets(uid, settings):
    key = settings.get("creality_key") or CREALITY_DEFAULT_KEY
    if len(uid) < 4:
        return []
    derived = aes128_ecb(key, uid[:4] * 4)[:6]
    keys_a = [FACTORY_KEY] * 16
    keys_a[1] = derived
    return [KeySet("Creality", keys_a, [FACTORY_KEY] * 16)]


def decode_creality(tag, records, settings):
    if not _opened_with(tag, "Creality"):
        return None
    data = tag.memory[64:112]
    plain = data[3] == 0x32 and data[17] in (0x30, 0x23)
    if not plain:
        aes_key = settings.get("creality_aes_key") or CREALITY_DEFAULT_AES_KEY
        data = aes128_ecb(aes_key, data, decrypt=True)
    text = data.decode("ascii", "ignore")
    try:
        year, month, day = 2000 + int(text[3:5]), int(text[5], 16), int(text[6:8])
        color = text[18:24]
        length = int(text[24:28])
    except ValueError:
        return None
    code = text[12:17]
    material, modifiers, nmin, nmax, bed, dtemp, dtime = CREALITY_MATERIALS.get(
        code, (None, [], None, None, None, None, None))
    return Filament(
        "Creality",
        material=material, modifiers=list(modifiers), brand="Creality",
        color=color, diameter=1.75,
        weight=_weight_from_length(length, {330: 1000, 165: 500, 80: 250}),
        nozzle_min=nmin, nozzle_max=nmax, bed_temp=bed, dry_temp=dtemp, dry_time=dtime,
        external_id="creality:" + code,
        manufactured="%04d-%02d-%02d" % (year, month, day) if 1 <= month <= 12 else None,
    )


# --- Snapmaker -------------------------------------------------------------

SNAPMAKER_TYPES = {1: "PLA", 2: "PETG", 3: "ABS", 4: "TPU", 5: "PVA"}
SNAPMAKER_SUBTYPES = {1: "Basic", 2: "Matte", 3: "SnapSpeed", 4: "Silk",
                      5: "Support", 6: "HF", 7: "95A", 8: "95A HF"}


# Snapmaker U1 salts: key B uses all 32 bytes, key A the first 25.
SNAPMAKER_DEFAULT_KEY = b"Snapmaker_qwertyuiop[,.;]_1q2w3e"


def snapmaker_keysets(uid, settings):
    key = settings.get("snapmaker_key") or SNAPMAKER_DEFAULT_KEY

    def derive(salt, kind):
        if salt.endswith(b"\x00"):
            salt = salt[:-1]
        return [hkdf_sha256(uid[:4], salt, ("key_%s_%d" % (kind, i)).encode(), 6)
                for i in range(16)]

    return [KeySet("Snapmaker", derive(key[:25], "a"), derive(key, "b"))]


def decode_snapmaker(tag, records, settings):
    """The RSA signature over the data is not verified: a tag that opens
    with the derived keys is decoded."""
    if not _opened_with(tag, "Snapmaker"):
        return None
    d = tag.memory
    le = lambda pos, n: int.from_bytes(d[pos:pos + n], "little")
    main = SNAPMAKER_TYPES.get(le(66, 2))
    if main is None:
        return None
    sub = SNAPMAKER_SUBTYPES.get(le(68, 2))
    count = min(d[72], 5) or 1
    colors = [d[80 + i * 3:83 + i * 3].hex() for i in range(count)]
    return Filament(
        "Snapmaker",
        material=main, modifiers=[sub] if sub else [],
        brand=_ascii(d[16:32]) or "Snapmaker",
        color=colors[0], alpha=0xFF - d[73], extra_colors=colors[1:],
        diameter=le(128, 2) / 100. or 1.75,
        weight=le(130, 2) or None,
        dry_temp=le(144, 2) or None, dry_time=le(146, 2) or None,
        nozzle_max=le(148, 2) or None, nozzle_min=le(150, 2) or None,
        bed_temp=le(154, 2) or None,
        external_id="snapmaker:%d" % le(96, 4),
        manufactured=_snapmaker_date(_ascii(d[160:168])),
    )


def _snapmaker_date(text):
    """ISO date from the tag's "YYYYMMDD", or None."""
    if len(text) == 8 and text.isdigit() and text != "19700101":
        return "%s-%s-%s" % (text[:4], text[4:6], text[6:])
    return None


# --- Qidi ------------------------------------------------------------------

QIDI_COLORS = {
    0x01: "FAFAFA", 0x02: "060606", 0x03: "D9E3ED", 0x04: "5CF30F",
    0x05: "63E492", 0x06: "2850FF", 0x07: "FE98FE", 0x08: "DFD628",
    0x09: "228332", 0x0A: "99DEFF", 0x0B: "1714B0", 0x0C: "CEC0FE",
    0x0D: "CADE4B", 0x0E: "1353AB", 0x0F: "5EA9FD", 0x10: "A878FF",
    0x11: "FE717A", 0x12: "FF362D", 0x13: "E2DFCD", 0x14: "898F9B",
    0x15: "6E3812", 0x16: "CAC59F", 0x17: "F28636", 0x18: "B87F2B",
}
QIDI_MATERIALS = {
    0x01: ("PLA", []), 0x02: ("PLA", ["Matte"]), 0x03: ("PLA", ["Metal"]),
    0x04: ("PLA", ["Silk"]), 0x05: ("PLA-CF", []), 0x06: ("PLA", ["Wood"]),
    0x07: ("PLA", ["Basic"]), 0x08: ("PLA", ["Matte", "Basic"]),
    0x0B: ("ABS", []), 0x0C: ("ABS-GF", []), 0x0D: ("ABS", ["Metal"]),
    0x0E: ("ABS", ["Odorless"]), 0x12: ("ASA", []), 0x13: ("ASA", ["Aero"]),
    0x18: ("PA", ["Ultra"]), 0x19: ("PA12-CF", []), 0x1A: ("PA-CF", ["Ultra", "CF25"]),
    0x1E: ("PAHT-CF", []), 0x1F: ("PAHT-GF", []), 0x20: ("BVOH", ["For PAHT"]),
    0x21: ("BVOH", ["For PET/PA"]), 0x22: ("PC-ABS", ["FR"]), 0x25: ("PET-CF", []),
    0x26: ("PET-GF", []), 0x27: ("PETG", ["Basic"]), 0x28: ("PETG", ["Tough"]),
    0x29: ("PETG", []), 0x2C: ("PPS-CF", []), 0x2D: ("PETG", ["Translucent"]),
    0x2F: ("PVA", []), 0x31: ("TPU", ["Aero"]), 0x32: ("TPU", []),
}


def decode_qidi(tag, records, settings):
    if tag.kind != KIND_MIFARE_CLASSIC or 1 not in tag.sectors_read:
        return None
    if tag.sector_keys.get(1) != "default":
        return None
    sector = tag.memory[64:112]
    material_code, color_code, maker = sector[0], sector[1], sector[2]
    if any(sector[3:]) or not (material_code and color_code and maker):
        return None
    if material_code not in QIDI_MATERIALS or color_code not in QIDI_COLORS:
        return None
    material, modifiers = QIDI_MATERIALS[material_code]
    return Filament("Qidi", material=material, modifiers=list(modifiers), brand="QIDI",
                    color=QIDI_COLORS[color_code], diameter=1.75, weight=1000)


# --- Anycubic --------------------------------------------------------------

def decode_anycubic(tag, records, settings):
    d = tag.memory
    if tag.kind != KIND_TYPE2 or len(d) < 0x80 or d[0x10:0x14] != b"\x7b\x00\x65\x00":
        return None
    material, modifiers = normalize(_ascii(d[0x3C:0x4C]).replace("-", " "))
    length = struct.unpack_from("<H", d, 0x7A)[0]
    return Filament(
        "Anycubic",
        material=material or "PLA", modifiers=modifiers,
        brand=_ascii(d[0x28:0x38]) or "Anycubic",
        color=bytes([d[0x53], d[0x52], d[0x51]]).hex(), alpha=d[0x50],
        nozzle_min=struct.unpack_from("<H", d, 0x60)[0] or None,
        nozzle_max=struct.unpack_from("<H", d, 0x62)[0] or None,
        bed_temp=struct.unpack_from("<H", d, 0x76)[0] or None,
        diameter=struct.unpack_from("<H", d, 0x78)[0] / 100. or 1.75,
        weight=_weight_from_length(length, {330: 1000, 247: 750, 198: 600, 165: 500, 82: 250}),
        external_id="anycubic:" + _ascii(d[0x14:0x24]),
    )


# --- Elegoo ----------------------------------------------------------------

_ELEGOO_BASE = ["PLA", "PETG", "ABS", "TPU", "PA", "CPE", "PC", "PVA", "ASA",
                "BVOH", "EVA", "HIPS", "PP", "PPA", "PPS"]
_ELEGOO_MODIFIERS = {
    "PLA": {0: [], 1: ["+"], 2: ["Pro"], 3: ["Silk"], 4: ["CF"], 5: ["Carbon"],
            6: ["Matte"], 7: ["Fluo"], 8: ["Wood"], 9: ["Basic"], 10: ["Rapid", "+"],
            11: ["Marble"], 12: ["Galaxy"], 13: ["Red", "Copper"], 14: ["Sparkle"]},
    "PETG": {0: [], 1: ["CF"], 2: ["GF"], 3: ["Pro"], 4: ["Translucent"], 5: ["Rapid"]},
    "ABS": {0: [], 1: ["GF"]},
    "TPU": {0: [], 1: ["95A"], 2: ["Rapid", "95A"]},
    "PA": {0: [], 1: ["CF"], 3: ["HT", "CF"], 4: ["6"], 5: ["6", "CF"], 6: ["12"], 7: ["12", "CF"]},
    "PC": {0: [], 1: ["TG"], 2: ["FR"]},
    "PP": {0: [], 1: ["CF"], 2: ["GF"]},
    "PPA": {0: [], 1: ["CF"], 2: ["GF"]},
    "PPS": {0: [], 2: ["CF"]},
}


def decode_elegoo(tag, records, settings):
    d = tag.memory
    if tag.kind != KIND_TYPE2 or len(d) < 0x69:
        return None
    f = d[0x40:0x69]
    if f[1:5] != b"\xee\xee\xee\xee":
        return None
    base_id, mod_id = f[0x0C], f[0x0D]
    if base_id >= len(_ELEGOO_BASE):
        return None
    material = _ELEGOO_BASE[base_id]
    mods = _ELEGOO_MODIFIERS.get(material, {0: []}).get(mod_id)
    if mods is None:
        return None
    mods = list(mods)
    for filler in ("CF", "GF"):
        if filler in mods:
            mods.remove(filler)
            material += "-" + filler
    for size in ("6", "12"):
        if size in mods:
            mods.remove(size)
            material = material.replace("PA", "PA" + size, 1)
    return Filament(
        "Elegoo",
        material=material, modifiers=mods, brand="Elegoo",
        color=f[0x10:0x13].hex(), alpha=f[0x13],
        nozzle_min=struct.unpack_from(">H", f, 0x14)[0] or None,
        nozzle_max=struct.unpack_from(">H", f, 0x16)[0] or None,
        diameter=struct.unpack_from(">H", f, 0x1C)[0] / 100. or 1.75,
        weight=struct.unpack_from(">H", f, 0x1E)[0] or None,
    )


# --- TigerTag --------------------------------------------------------------

TIGERTAG_IDS = (0x5BF59264, 0xBC0FCB97)
TIGERTAG_EPOCH = 946684800      # 2000-01-01
_TIGERTAG_DB = None


def _tigertag_db():
    """Offline copies of TigerTag's ID tables, loaded on first use."""
    global _TIGERTAG_DB
    if _TIGERTAG_DB is not None:
        return _TIGERTAG_DB
    db = {}
    for name in ("material", "brand", "aspect", "diameter"):
        path = os.path.join(DATA_DIR, "tigertag", "id_%s.json" % name)
        try:
            with open(path, encoding="utf-8") as handle:
                db[name] = {int(e["id"]): e for e in json.load(handle) if "id" in e}
        except (IOError, OSError, ValueError, KeyError):
            db[name] = {}
    _TIGERTAG_DB = db
    return db


def decode_tigertag(tag, records, settings):
    if tag.kind != KIND_TYPE2 or len(tag.memory) < 16 + 36:
        return None
    u = tag.memory[16:]
    if struct.unpack_from(">I", u, 0)[0] not in TIGERTAG_IDS:
        return None
    db = _tigertag_db()
    material_id = struct.unpack_from(">H", u, 8)[0]
    entry = db["material"].get(material_id, {})
    material = entry.get("material_type") or entry.get("label") or "PLA"
    if entry.get("filled_type"):
        material = "%s-%s" % (material, entry["filled_type"])
    modifiers = []
    for aspect_id in (u[10], u[11]):
        label = db["aspect"].get(aspect_id, {}).get("label")
        if label and label not in ("None", "-"):
            modifiers.append(label)
    brand = db["brand"].get(struct.unpack_from(">H", u, 14)[0], {}).get("name")
    diameter = db["diameter"].get(u[13], {}).get("label")
    measure = (u[20] << 16) | (u[21] << 8) | u[22]
    unit = u[23]
    weight = measure * 1000 if unit in (2, 35) else measure / 1000. if unit == 10 else measure
    recommended = entry.get("recommended") or {}
    stamp = struct.unpack_from(">I", u, 32)[0]
    manufactured = None
    if stamp:
        try:
            manufactured = datetime.datetime.fromtimestamp(stamp + TIGERTAG_EPOCH, datetime.timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return Filament(
        "TigerTag",
        material=material, modifiers=modifiers, brand=brand,
        color=u[16:19].hex(), alpha=u[19],
        diameter=float(diameter) if diameter else 1.75,
        weight=int(weight) if weight else None,
        nozzle_min=struct.unpack_from(">H", u, 24)[0] or None,
        nozzle_max=struct.unpack_from(">H", u, 26)[0] or None,
        bed_temp=u[31] or u[30] or recommended.get("bedTempMax"),
        dry_temp=u[28] or None, dry_time=u[29] or None,
        external_id="tigertag:%d" % struct.unpack_from(">I", u, 4)[0],
        manufactured=manufactured,
    )
