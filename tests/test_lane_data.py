"""lane_data records for HelixScreen and OrcaSlicer."""

import unittest

import helpers  # noqa: F401  (sets sys.path)

from kilnnfc import lane_data

HELIX = {"bed_temp": 60, "color": "#43A047", "helix_declared": ["color_rgb"],
         "helix_locked_color": True, "helix_material": "PLA", "lane": "0",
         "material": "PLA", "nozzle_temp": 205, "scan_time": "2026-10-24T16:05:14Z"}
KILN = {"lane": "0", "material": "PLA", "color": "#FF7F37", "nozzle_temp": 208,
        "bed_temp": 0, "name": "", "vendor": "", "spool_id": None, "scan_time": ""}
INFO = {"material": "PLA", "color": "FF7F37", "nozzle_temp": 208, "name": "Creality PLA",
        "vendor": "Creality", "uid": "04AA", "scan_time": "2026-10-24T17:00:00Z"}


class LaneValueTest(unittest.TestCase):
    def test_entry_key_follows_lane_field(self):
        entries = {"lane1": {"lane": "0"}, "x": {"lane": "1"}}
        self.assertEqual(lane_data.entry_key(entries, 2), "x")
        self.assertEqual(lane_data.entry_key(entries, 3), "lane3")

    def test_helixscreen_keys_go(self):
        value = lane_data.lane_value(HELIX, 1, INFO)
        self.assertFalse([k for k in value if k.startswith("helix_")])
        self.assertEqual(value["color"], "#FF7F37")
        self.assertEqual(value["scan_time"], INFO["scan_time"])
        self.assertEqual(value["bed_temp"], 60)       # kept: the tag had none

    def test_kiln_lanes_vendor_key_goes(self):
        # HelixScreen takes a record with "vendor" for one of its own.
        value = lane_data.lane_value(KILN, 1, INFO)
        self.assertNotIn("vendor", value)
        self.assertEqual(value["vendor_name"], "Creality")
        self.assertEqual(value["name"], "Creality PLA")
        self.assertNotIn("spool_id", value)

    def test_spool_id(self):
        value = lane_data.lane_value(KILN, 1, dict(INFO, spool_id=12))
        self.assertEqual(value["spool_id"], 12)


class FakeMoonraker(lane_data.LaneData):
    def __init__(self, reads):
        lane_data.LaneData.__init__(self, "http://x")
        self.reads = list(reads)
        self.posts = []

    def _request(self, method, query=None, body=None):
        if method == "GET":
            entries = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
            return {"result": {"value": entries}}
        self.posts.append(body)
        return {}


class WriteTest(unittest.TestCase):
    def test_waits_for_kiln_lanes(self):
        moonraker = FakeMoonraker([{"lane1": HELIX}, {"lane1": HELIX}, {"lane1": KILN}])
        slept = []
        key = moonraker.write(1, INFO, wait=10, sleep=slept.append)
        self.assertEqual(key, "lane1")
        self.assertEqual(len(slept), 2)
        self.assertEqual(moonraker.posts[0]["value"]["color"], "#FF7F37")
        self.assertNotIn("vendor", moonraker.posts[0]["value"])

    def test_gives_up_waiting(self):
        moonraker = FakeMoonraker([{"lane1": HELIX}])
        moonraker.write(1, INFO, wait=0, sleep=lambda s: None)
        self.assertEqual(len(moonraker.posts), 1)


if __name__ == "__main__":
    unittest.main()
