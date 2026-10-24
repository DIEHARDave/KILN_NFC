"""Moonraker lane_data: what HelixScreen and OrcaSlicer read per slot.

Kiln's kiln_lanes component mirrors the IFS slots into lane_data, but its
records carry a "vendor" key, which HelixScreen takes for one of its own
legacy records. HelixScreen then ignores them and keeps a color the user
once picked on its screen over the newly scanned spool's.

So after an assignment the slot's record is written once more, as a
record of another tool: no helix_ keys, the shared spellings ("name",
"vendor_name"), and scan_time set to now. HelixScreen re-reads lane_data
2 s after it sees the slot change and takes a newer record of another
tool as the slot's latest edit. The write waits until kiln_lanes has
written the new color (or WAIT_MAX), so that it lands between the two.

Entries are found by their "lane" field ("0" for the first slot)."""

import datetime
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LOGGER = logging.getLogger("kiln_nfc")

NAMESPACE = "lane_data"
TIMEOUT = 5.0
POLL = 0.2
# Before HelixScreen's re-read, 2 s after it sees the change.
WAIT_MAX = 1.4

# What describes the spool rather than the slot; another spool's goes.
SPOOL_KEYS = ("name", "spool_name", "vendor", "vendor_name", "color_name",
              "spoolman_vendor_id", "spool_id", "remaining_weight_g", "total_weight_g")


def entry_key(entries, slot):
    """Key of the lane_data entry for a 1-based slot."""
    lane = str(slot - 1)
    for key, value in sorted(entries.items()):
        if isinstance(value, dict) and str(value.get("lane")) == lane:
            return key
    return "lane%d" % slot


def lane_value(existing, slot, info):
    """The new entry: other keys of the existing one kept, HelixScreen's and
    the previous spool's dropped, the scanned spool's set. info has material,
    color (RRGGBB), scan_time and optionally spool_id, vendor, name,
    nozzle_temp, bed_temp, uid."""
    value = dict(existing) if isinstance(existing, dict) else {}
    for key in list(value):
        if key.startswith("helix_") or key in SPOOL_KEYS:
            del value[key]
    value["lane"] = str(slot - 1)
    if info.get("material"):
        value["material"] = info["material"]
    if info.get("color"):
        value["color"] = "#" + info["color"].lstrip("#").upper()
    for key in ("nozzle_temp", "bed_temp"):
        if info.get(key):
            value[key] = int(info[key])
    if info.get("spool_id"):
        value["spool_id"] = info["spool_id"]
    if info.get("name"):
        value["name"] = info["name"]
    if info.get("vendor"):
        value["vendor_name"] = info["vendor"]
    value["kiln_nfc_uid"] = info.get("uid") or ""
    value["scan_time"] = info["scan_time"]
    return value


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LaneData(object):
    def __init__(self, moonraker_url):
        self.url = moonraker_url.rstrip("/") + "/server/database/item"

    def _request(self, method, query=None, body=None):
        url = self.url + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)

    def entries(self):
        try:
            reply = self._request("GET", {"namespace": NAMESPACE})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:            # the namespace does not exist yet
                return {}
            raise
        return (reply.get("result") or {}).get("value") or {}

    def write(self, slot, info, wait=WAIT_MAX, sleep=time.sleep):
        """Write the slot's entry once kiln_lanes shows the new color, or
        after wait seconds. Blocking; returns the key."""
        want = "#" + info["color"].lstrip("#").upper() if info.get("color") else None
        deadline = time.monotonic() + wait
        while True:
            entries = self.entries()
            key = entry_key(entries, slot)
            current = entries.get(key) or {}
            if want is None or str(current.get("color", "")).upper() == want \
                    or time.monotonic() >= deadline:
                break
            sleep(POLL)
        self._request("POST", body={"namespace": NAMESPACE, "key": key,
                                    "value": lane_value(current, slot, info)})
        return key

    def write_later(self, slot, info):
        """write() on a thread; failures are only logged."""
        info = dict(info, scan_time=info.get("scan_time") or utc_now())

        def run():
            try:
                key = self.write(slot, info)
                LOGGER.info("kiln_nfc: lane_data %s set for slot %d", key, slot)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                LOGGER.warning("kiln_nfc: could not write lane_data for slot %d: %s", slot, exc)
        thread = threading.Thread(target=run, name="kiln_nfc lane_data")
        thread.daemon = True
        thread.start()
        return thread
