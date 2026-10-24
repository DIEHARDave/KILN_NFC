"""Network readers: request parsing and the Moonraker-published endpoints."""

import base64
import json
import time
import unittest

from helpers import mime_record, text_record, type2_tag
from test_plugin import CommandError, FakeGcmd, make_plugin

from kilnnfc.remote import RemoteError, tag_from_json


def scan_request(tag, **extra):
    body = {
        "reader": "ESP32 PN532",
        "uid": tag.uid_hex,
        "kind": tag.kind,
        "sak": tag.sak,
        "atqa": tag.atqa.hex(),
        "memory": base64.b64encode(tag.memory).decode(),
    }
    body.update(extra)
    return body


class FakeWebRequest(object):
    """Klipper's WebRequest as the endpoint handlers use it."""

    def __init__(self, method, params):
        self.method = method
        self.params = params
        self.response = None

    def get_method(self):
        return self.method

    def get_int(self, item, default=None):
        value = self.params.get(item, default)
        if not isinstance(value, int):
            raise CommandError("Invalid Argument Type [%s]" % item)
        return value

    def send(self, data):
        self.response = data


class TagFromJsonTest(unittest.TestCase):
    def test_type2_round_trip(self):
        tag = type2_tag(text_record("PLA:FF0000"))
        parsed = tag_from_json(scan_request(tag))
        self.assertEqual(parsed.uid, tag.uid)
        self.assertEqual(parsed.memory, tag.memory)
        self.assertEqual(parsed.kind, "type2")
        self.assertEqual(parsed.reader, "ESP32 PN532")
        self.assertFalse(parsed.incomplete)

    def test_mifare_sector_keys(self):
        body = {"uid": "A1B2C3D4", "kind": "mifare_classic", "sak": 8,
                "memory": base64.b64encode(bytes(1024)).decode(),
                "sector_keys": {"1": "Bambu", "2": "default"}}
        tag = tag_from_json(body)
        self.assertEqual(tag.sector_keys, {1: "Bambu", 2: "default"})
        self.assertEqual(tag.sectors_read, {1, 2})

    def test_rejects_bad_requests(self):
        good = {"uid": "04112233445566", "kind": "type2", "memory": ""}
        for change in ({"kind": "floppy"}, {"uid": "zz"}, {"uid": "0102"},
                       {"memory": "not base64!"}, {"sak": 300},
                       {"kind": "mifare_classic", "memory": base64.b64encode(b"x" * 15).decode()},
                       {"kind": "mifare_classic", "sector_keys": {"50": "x"}}):
            with self.assertRaises(RemoteError, msg=change):
                tag_from_json(dict(good, **change))
        with self.assertRaises(RemoteError):
            tag_from_json([1, 2])


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.plugin, self.gcode, objects = make_plugin()
        self.endpoints = objects["webhooks"].endpoints

    def call(self, name, **params):
        request = FakeWebRequest("kiln_nfc/" + name, params)
        self.endpoints["kiln_nfc/" + name](request)
        return request.response

    def scan(self, tag, **extra):
        reply = self.call("scan", **scan_request(tag, **extra))
        if not reply["accepted"]:
            return reply
        deadline = time.time() + 5
        while (self.plugin.remote_result or {}).get("id") != reply["id"]:
            self.assertLess(time.time(), deadline, "the scan never finished")
            time.sleep(0.01)
        return self.plugin.remote_result

    def test_endpoints_are_registered(self):
        self.assertEqual(sorted(self.endpoints), ["kiln_nfc/assign", "kiln_nfc/cancel",
                                                  "kiln_nfc/info", "kiln_nfc/scan"])

    def test_scan_prompts_like_a_local_reader(self):
        result = self.scan(type2_tag(text_record("PETG:00FF00")))
        self.assertEqual(result["outcome"], "prompt")
        self.assertEqual(result["scan"]["material"], "PETG")
        self.assertEqual(self.plugin.get_status(0)["remote"], result)
        self.assertTrue(self.gcode.raw[0].startswith("// action:prompt_begin NFC: PETG"))
        reply = self.call("assign", slot=3)
        self.assertIn("Slot 3", reply["message"])
        self.assertEqual(self.gcode.scripts, ["IFS_SET_MATERIAL SLOT=3 TYPE=PETG COLOR=00FF00"])
        self.assertIn("// action:prompt_end", self.gcode.raw)
        with self.assertRaises(CommandError):
            self.call("assign", slot=3)

    def test_reader_fixed_to_a_slot_skips_the_prompt(self):
        result = self.scan(type2_tag(text_record("PETG:00FF00")), slot=2)
        self.assertEqual(result["outcome"], "assigned")
        self.assertEqual(result["slot"], 2)
        self.assertIn("Slot 2 is now PETG", result["message"])
        self.assertEqual(self.gcode.scripts, ["IFS_SET_MATERIAL SLOT=2 TYPE=PETG COLOR=00FF00"])
        self.assertFalse([r for r in self.gcode.raw if "prompt_begin" in r])
        self.assertIsNone(self.plugin.pending)

    def test_fixed_slot_errors(self):
        tag = type2_tag(text_record("PLA:FF0000"))
        self.assertEqual(self.scan(tag, slot=9)["outcome"], "no_slot")
        self.assertEqual(self.gcode.scripts, [])
        with self.assertRaises(CommandError):
            self.call("scan", **scan_request(tag, slot="two"))
        # slot 0 means "ask", as when the field is missing.
        self.assertEqual(self.scan(tag, slot=0)["outcome"], "prompt")

    def test_openspool_json_is_decoded_on_the_printer(self):
        payload = json.dumps({"protocol": "openspool", "version": "1.0", "type": "PLA",
                              "color_hex": "FFAA00", "brand": "Sunlu",
                              "min_temp": 200, "max_temp": 220}).encode()
        result = self.scan(type2_tag(mime_record("application/json", payload)))
        self.assertEqual(result["scan"]["format"], "OpenSpool")
        self.assertEqual(result["scan"]["filament"]["brand"], "Sunlu")

    def test_incomplete_disabled_and_bad_requests(self):
        tag = type2_tag(text_record("PLA:FF0000"))
        self.assertEqual(self.scan(tag, incomplete=True)["outcome"], "incomplete")
        self.gcode.commands["NFC_DISABLE"](FakeGcmd(self.gcode))
        self.assertEqual(self.scan(tag)["outcome"], "disabled")
        # Errors must reach Klipper as command errors; anything else shuts it down.
        with self.assertRaises(CommandError):
            self.call("scan", kind="type2", uid="xx")
        with self.assertRaises(CommandError):
            self.call("assign", slot="two")

    def test_info_and_cancel(self):
        info = self.call("info")
        self.assertEqual(info["plugin"], "kiln_nfc")
        self.assertEqual([s["label"] for s in info["slots"]], ["Slot 1", "Slot 2", "Slot 3", "Slot 4"])
        self.scan(type2_tag(text_record("ABS:112233")))
        self.assertIsNotNone(self.call("info")["pending"])
        self.call("cancel")
        self.assertIsNone(self.plugin.pending)


if __name__ == "__main__":
    unittest.main()
