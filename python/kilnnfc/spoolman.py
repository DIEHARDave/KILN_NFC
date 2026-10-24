"""Spoolman through Moonraker's [spoolman] proxy, so Spoolman's address is
configured once, in Moonraker. Blocking calls; run them off the reactor.

Tags are matched to spools by UID in the spool's "card_uids" extra field
(comma-separated hex, the convention zmod_afclite and the Snapmaker U1
SpoolLink apps use) or nfc2klipper's "nfc_id" field."""

import json
import urllib.error
import urllib.parse
import urllib.request

CARD_UIDS_FIELD = "card_uids"
NFC_ID_FIELD = "nfc_id"
CARD_UIDS_FIELD_DEFINITION = {
    "name": "Card UIDs",
    "field_type": "text",
    "order": 1,
    "default_value": json.dumps(""),
}
TIMEOUT = 30.0

DENSITY = {
    "PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21,
    "PA": 1.14, "PC": 1.20, "PCTG": 1.23, "PVA": 1.23, "HIPS": 1.04,
    "PP": 0.90,
}


class SpoolmanError(IOError):
    pass


def normalize_uid(uid):
    text = "".join(ch for ch in str(uid) if ch not in ": -").upper()
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


def _extra_text(spool, field):
    raw = (spool.get("extra") or {}).get(field) or ""
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, str):
            raw = decoded
    except (TypeError, ValueError):
        pass
    return str(raw)


def spool_uids(spool):
    uids = [normalize_uid(u) for u in _extra_text(spool, CARD_UIDS_FIELD).split(",")]
    uids.append(normalize_uid(_extra_text(spool, NFC_ID_FIELD)))
    return [u for u in uids if u]


def spool_title(spool):
    filament = spool.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name")
    # Filament names often repeat the vendor or material ("PLA SnapSpeed"),
    # so words already in the title are dropped.
    words = []
    for part in (vendor, filament.get("material"), filament.get("name")):
        for word in str(part or "").split():
            if word.lower() not in (w.lower() for w in words):
                words.append(word)
    return " ".join(words) or "spool %s" % spool.get("id")


def spool_color(spool):
    filament = spool.get("filament") or {}
    colors = [filament.get("color_hex") or ""]
    colors += str(filament.get("multi_color_hexes") or "").split(",")
    for color in colors:
        color = str(color).replace("#", "").strip().upper()[:6]
        if len(color) == 6:
            try:
                int(color, 16)
                return color
            except ValueError:
                pass
    return None


class Spoolman(object):
    def __init__(self, moonraker_url):
        self.url = moonraker_url.rstrip("/") + "/server/spoolman/proxy"

    def request(self, method, path, query=None, body=None):
        payload = {"request_method": method, "path": path, "use_v2_response": True}
        if query:
            payload["query"] = urllib.parse.urlencode(query)
        if body is not None:
            payload["body"] = body
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                reply = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise SpoolmanError("Moonraker has no [spoolman] section")
            raise SpoolmanError("Moonraker answered HTTP %d" % exc.code)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SpoolmanError("Moonraker unreachable: %s" % exc)
        result = reply.get("result") or {}
        error = result.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else error
            raise SpoolmanError("Spoolman: %s" % message)
        return result.get("response")

    def get_spool(self, spool_id):
        spool = self.request("GET", "/v1/spool/%d" % int(spool_id))
        if not isinstance(spool, dict) or "id" not in spool:
            raise SpoolmanError("Spoolman has no spool %s" % spool_id)
        return spool

    def find_spool_by_uid(self, uid):
        uid = normalize_uid(uid)
        spools = self.request("GET", "/v1/spool", {"allow_archived": "false"}) or []
        for spool in spools:
            if uid in spool_uids(spool):
                return spool
        return None

    def find_spools_by_filament(self, filament):
        """Spools whose filament has the tag's vendor, material and color:
        how a spool's second tag (Snapmaker spools have one on each side)
        or a tag never bound to its spool is still found."""
        if not (filament.brand and filament.material and filament.color):
            return []
        brand = filament.brand.lower()
        material = filament.material.upper()
        color = filament.color.replace("#", "").upper()[:6]
        found = []
        for spool in self.request("GET", "/v1/spool", {"allow_archived": "false"}) or []:
            spool_filament = spool.get("filament") or {}
            vendor = str((spool_filament.get("vendor") or {}).get("name") or "").lower()
            if vendor == brand and str(spool_filament.get("material") or "").upper() == material                     and spool_color(spool) == color:
                found.append(spool)
        return found

    def ensure_card_field(self):
        fields = self.request("GET", "/v1/field/spool") or []
        if not any(f.get("key") == CARD_UIDS_FIELD for f in fields):
            self.request("POST", "/v1/field/spool/" + CARD_UIDS_FIELD,
                         body=CARD_UIDS_FIELD_DEFINITION)

    def bind_uid(self, spool, uid):
        """Add uid to the spool's card_uids, removing it from other spools."""
        uid = normalize_uid(uid)
        if not uid or uid in spool_uids(spool):
            return spool
        self.ensure_card_field()
        for other in self.request("GET", "/v1/spool", {"allow_archived": "true"}) or []:
            if other.get("id") != spool["id"] and uid in spool_uids(other):
                remaining = [u for u in _extra_text(other, CARD_UIDS_FIELD).split(",")
                             if normalize_uid(u) and normalize_uid(u) != uid]
                self.request("PATCH", "/v1/spool/%d" % other["id"], body={
                    "extra": {CARD_UIDS_FIELD: json.dumps(",".join(remaining))}})
        uids = [u for u in _extra_text(spool, CARD_UIDS_FIELD).split(",") if normalize_uid(u)]
        uids.append(uid)
        return self.request("PATCH", "/v1/spool/%d" % spool["id"], body={
            "extra": {CARD_UIDS_FIELD: json.dumps(",".join(uids))}})

    def create_spool(self, filament, uid):
        """Vendor, filament and spool for a decoded tag, reusing an existing
        vendor and filament with the same name, material and color."""
        vendor_id = None
        if filament.brand:
            vendors = self.request("GET", "/v1/vendor", {"name": filament.brand}) or []
            match = [v for v in vendors if str(v.get("name", "")).lower() == filament.brand.lower()]
            vendor = match[0] if match else self.request(
                "POST", "/v1/vendor", body={"name": filament.brand})
            vendor_id = vendor["id"]
        name = filament.name or " ".join([filament.material or ""] + list(filament.modifiers)).strip()
        query = {"material": filament.material or ""}
        if vendor_id is not None:
            query["vendor.id"] = vendor_id
        if filament.color:
            query["color_hex"] = filament.color
        existing = [f for f in self.request("GET", "/v1/filament", query) or []
                    if (f.get("name") or "") == name]
        if existing:
            spool_filament = existing[0]
        else:
            body = {
                "name": name or "Unnamed",
                "material": filament.material,
                "density": DENSITY.get((filament.material or "").split("-")[0], 1.24),
                "diameter": filament.diameter or 1.75,
            }
            if vendor_id is not None:
                body["vendor_id"] = vendor_id
            if filament.color:
                body["color_hex"] = filament.color
            if filament.extra_colors:
                body["multi_color_hexes"] = ",".join([filament.color] + filament.extra_colors)
                body["multi_color_direction"] = "coaxial"
                body.pop("color_hex", None)
            if filament.weight:
                body["weight"] = filament.weight
            if filament.nozzle_temp:
                body["settings_extruder_temp"] = filament.nozzle_temp
            if filament.bed_temp:
                body["settings_bed_temp"] = int(filament.bed_temp)
            if filament.external_id:
                body["external_id"] = str(filament.external_id)
            spool_filament = self.request("POST", "/v1/filament", body=body)
        self.ensure_card_field()
        spool_body = {
            "filament_id": spool_filament["id"],
            "extra": {CARD_UIDS_FIELD: json.dumps(normalize_uid(uid) or "")},
        }
        if filament.weight:
            spool_body["initial_weight"] = filament.weight
        return self.request("POST", "/v1/spool", body=spool_body)
