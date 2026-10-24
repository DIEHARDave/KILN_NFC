"""The Klipper module against a minimal fake Klipper."""

import os
import tempfile
import unittest

from helpers import text_record, type2_tag

from kilnnfc import klipper_plugin


class CommandError(Exception):
    pass


class ConfigError(Exception):
    pass


class FakeReactor(object):
    NOW = 0.
    NEVER = 9999999999.

    def __init__(self):
        self.time = 100.
        self.timers = []

    def monotonic(self):
        return self.time

    def register_timer(self, callback, waketime=None):
        self.timers.append(callback)
        return callback

    def update_timer(self, timer, waketime):
        pass

    def unregister_timer(self, timer):
        pass

    def register_async_callback(self, callback):
        callback(self.time)


class FakeGcode(object):
    def __init__(self):
        self.commands = {}
        self.raw = []
        self.info = []
        self.scripts = []
        self.in_command = []

    @property
    def ready_gcode_handlers(self):
        return self.commands

    def register_command(self, name, fn, desc=None):
        if fn is None:
            return self.commands.pop(name, None)
        self.commands[name] = fn

    def respond_raw(self, msg):
        self.raw.append(msg)

    def respond_info(self, msg):
        self.info.append(msg)

    def run_script(self, script):
        self.scripts.append(script)

    def run_script_from_command(self, script):
        self.in_command.append(script)


class FakeGcmd(object):
    def __init__(self, gcode, **params):
        self.gcode = gcode
        self.params = {k.upper(): str(v) for k, v in params.items()}

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None):
        return int(self.params.get(name, default))

    def get_float(self, name, default=None, minval=None, maxval=None):
        return float(self.params.get(name, default))

    def respond_info(self, msg):
        self.gcode.info.append(msg)

    def error(self, msg):
        return CommandError(msg)


class FakeIfs(object):
    """printer.ifs: slot 1 loaded and holding filament."""

    def __init__(self):
        self.status = {"connected": True, "channel": 1, "channel_count": 4,
                       "loaded_channels": [1]}

    def get_status(self, eventtime):
        return self.status


class FakeIfsMaterials(object):
    """printer.ifs_materials: slot 1 is PLA, the others unset."""

    def __init__(self, slots=4):
        self.status = {
            "available": True, "channel_count": slots,
            "slots": {str(n): {"type": "PLA" if n == 1 else None,
                               "color": "#FFFFFF" if n == 1 else "", "temp": None}
                      for n in range(1, slots + 1)},
            "temperatures": {"PLA": 220.0, "PETG": 250.0, "ABS": 250.0},
        }

    def get_status(self, eventtime):
        return self.status

class FakePrinter(object):
    command_error = CommandError

    def __init__(self, objects):
        self.objects = objects
        self.reactor = FakeReactor()
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, event, fn):
        self.handlers[event] = fn


class FakeWebhooks(object):
    def __init__(self):
        self._remote_methods = {}
        self.calls = []
        self.endpoints = {}

    def register_endpoint(self, path, callback):
        self.endpoints[path] = callback

    def call_remote_method(self, method, **kwargs):
        self.calls.append((method, kwargs))


class FakeConfig(object):
    error = ConfigError

    def __init__(self, printer, options):
        self.printer = printer
        self.options = options

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getint(self, name, default=None, **kw):
        return int(self.options.get(name, default))

    def getfloat(self, name, default=None, **kw):
        return float(self.options.get(name, default))

    def getboolean(self, name, default=None):
        value = self.options.get(name, default)
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")

    def getchoice(self, name, choices, default=None):
        return choices[self.options.get(name, default)]

    def get_prefix_sections(self, prefix):
        return []


def make_plugin(options=None, spoolman_slots=False, slots=4):
    gcode = FakeGcode()
    objects = {"gcode": gcode, "webhooks": FakeWebhooks(), "ifs": FakeIfs(),
               "ifs_materials": FakeIfsMaterials(slots)}
    if spoolman_slots:
        objects["spoolman_slots"] = object()
    printer = FakePrinter(objects)
    plugin = klipper_plugin.KilnNfc(FakeConfig(printer, options or {}))
    plugin.ifs = objects["ifs"]
    plugin.ifs_materials = objects["ifs_materials"]
    plugin.spoolman_slots = objects.get("spoolman_slots")
    return plugin, gcode, objects


class PluginTest(unittest.TestCase):
    def scan(self, plugin, text):
        scan = plugin._process(type2_tag(text_record(text)))
        plugin._handle_scan(scan)
        return scan

    def test_prompt_and_assign(self):
        plugin, gcode, _ = make_plugin()
        self.scan(plugin, "PETG:00FF00")
        self.assertTrue(gcode.raw[0].startswith("// action:prompt_begin NFC: PETG"))
        buttons = [r for r in gcode.raw if "prompt_button " in r]
        self.assertEqual(len(buttons), 4)
        self.assertIn("Slot 1: PLA|NFC_ASSIGN SLOT=1|secondary", buttons[0])
        self.assertIn("Slot 2|NFC_ASSIGN SLOT=2|primary", buttons[1])
        self.assertEqual(gcode.raw[-1], "// action:prompt_show")
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=2))
        self.assertEqual(gcode.in_command, ["IFS_SET_MATERIAL SLOT=2 TYPE=PETG COLOR=00FF00"])
        self.assertIn("// action:prompt_end", gcode.raw)
        self.assertIsNone(plugin.pending)

    def test_assign_on_insert(self):
        plugin, gcode, objects = make_plugin()
        self.scan(plugin, "PLA:112233")
        objects["ifs"].status["loaded_channels"] = [1, 3]
        plugin._tick(plugin.reactor.monotonic())
        self.assertEqual(gcode.scripts, ["IFS_SET_MATERIAL SLOT=3 TYPE=PLA COLOR=112233"])
        self.assertIsNone(plugin.pending)

    def test_no_insert_detection_without_the_ifs_link(self):
        plugin, gcode, objects = make_plugin()
        objects["ifs"].status["connected"] = False
        self.scan(plugin, "PLA:112233")
        self.assertFalse(any("insert the filament" in r for r in gcode.raw))
        objects["ifs"].status["loaded_channels"] = [1, 3]
        plugin._tick(plugin.reactor.monotonic())
        self.assertEqual(gcode.scripts, [])

    def test_prompt_timeout(self):
        plugin, gcode, _ = make_plugin({"prompt_timeout": 5})
        self.scan(plugin, "PLA:112233")
        plugin._tick(plugin.reactor.monotonic() + 10)
        self.assertIsNone(plugin.pending)
        self.assertIn("// action:prompt_end", gcode.raw)

    def test_temperature_from_the_tag(self):
        plugin, gcode, _ = make_plugin()
        payload = (b'{"protocol":"openspool","type":"PETG","color_hex":"0022FF",'
                   b'"min_temp":230,"max_temp":250}')
        from helpers import mime_record
        plugin._handle_scan(plugin._process(type2_tag(mime_record("application/json", payload))))
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=4))
        cmd = gcode.in_command[0]
        self.assertTrue(cmd.startswith("IFS_SET_MATERIAL SLOT=4 TYPE=PETG COLOR=0022FF TEMP="), cmd)
        self.assertTrue(230 <= int(cmd.split("TEMP=")[1]) <= 250)

    def test_set_temperature_off(self):
        plugin, gcode, _ = make_plugin({"set_temperature": False})
        scan = plugin._process(type2_tag(text_record("PLA:112233")))
        scan.spool = {"id": 5, "extra": {"card_uids": '"%s"' % scan.uid},
                      "filament": {"material": "PLA", "color_hex": "112233",
                                   "settings_extruder_temp": 215}}
        plugin._handle_scan(scan)
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=2))
        self.assertEqual(gcode.in_command[0], "IFS_SET_MATERIAL SLOT=2 TYPE=PLA COLOR=112233")

    def test_kiln_spoolman_plugin_gets_the_spool(self):
        plugin, gcode, objects = make_plugin(spoolman_slots=True)
        plugin.bind_card_uid = False
        scan = plugin._process(type2_tag(text_record("PLA:112233")))
        scan.spool = {"id": 5, "extra": {},
                      "filament": {"material": "PLA", "color_hex": "112233",
                                   "settings_extruder_temp": 215}}
        plugin._handle_scan(scan)
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=2))
        self.assertEqual(gcode.in_command, [
            "IFS_SET_MATERIAL SLOT=2 TYPE=PLA COLOR=112233 TEMP=215",
            "SPOOLMAN_SET_SLOT SLOT=2 SPOOL=5"])
        # The spoolman plugin sets the active spool when the slot loads.
        self.assertEqual(objects["webhooks"].calls, [])

    def test_spool_without_the_spoolman_plugin(self):
        plugin, gcode, objects = make_plugin()
        plugin.bind_card_uid = False
        scan = plugin._process(type2_tag(text_record("PLA:112233")))
        scan.spool = {"id": 8, "filament": {"material": "PETG", "color_hex": "445566"},
                      "extra": {}}
        plugin._handle_scan(scan)
        # Slot 1 is the loaded one: its spool becomes the active spool.
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=1))
        self.assertEqual(gcode.in_command, ["IFS_SET_MATERIAL SLOT=1 TYPE=PETG COLOR=445566"])
        self.assertEqual(objects["webhooks"].calls, [("spoolman_set_active_spool", {"spool_id": 8})])
        self.assertIn("INSTALL_PLUGIN NAME=spoolman", gcode.info[-1])

    def test_spool_in_another_slot_is_not_made_active(self):
        plugin, gcode, objects = make_plugin()
        plugin.bind_card_uid = False
        scan = plugin._process(type2_tag(text_record("PLA:112233")))
        scan.spool = {"id": 8, "filament": {"material": "PLA"}, "extra": {}}
        plugin._handle_scan(scan)
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=3))
        self.assertEqual(objects["webhooks"].calls, [])

    def test_material_names(self):
        self.assertEqual(klipper_plugin.kiln_material("asa"), "ASA")
        self.assertEqual(klipper_plugin.kiln_material("PETG-CF"), "PETG-CF")
        self.assertEqual(klipper_plugin.kiln_material("PLA Silk"), "PLA-SILK")
        self.assertEqual(klipper_plugin.kiln_material("PLA+"), "PLA+")
        self.assertIsNone(klipper_plugin.kiln_material("?"))
        self.assertIsNone(klipper_plugin.kiln_material(""))
        self.assertEqual(len(klipper_plugin.kiln_material("X" * 40)), 16)

    def test_no_material_keeps_the_slots(self):
        plugin, gcode, _ = make_plugin()
        scan = plugin._process(type2_tag(text_record("PLA:112233")))
        scan.filament.material = "!!"
        plugin._handle_scan(scan)
        gcode.commands["NFC_ASSIGN"](FakeGcmd(gcode, SLOT=1))
        self.assertEqual(gcode.in_command, ["IFS_SET_MATERIAL SLOT=1 COLOR=112233"])
        self.assertIn("keeps PLA", gcode.info[-2])

    def test_without_an_ifs(self):
        plugin, gcode, _ = make_plugin()
        plugin.ifs = plugin.ifs_materials = None
        plugin._wait_background = lambda fn, *args: fn(*args)
        gcode.commands["NFC_TEST_SCAN"](FakeGcmd(gcode, TEXT="PLA:112233"))
        self.assertIsNone(plugin.pending)
        self.assertEqual(gcode.in_command, [])
        self.assertIn("Slot 1 is now PLA #112233", gcode.info[-1])

    def test_ignored_while_printing(self):
        plugin, gcode, objects = make_plugin()

        class Stats(object):
            def get_status(self, eventtime):
                return {"state": "printing"}

        objects["print_stats"] = Stats()
        self.scan(plugin, "PLA:112233")
        self.assertIsNone(plugin.pending)
        self.assertIn("ignored while printing", gcode.info[-1])

    def test_test_scan_command(self):
        plugin, gcode, _ = make_plugin()
        plugin._wait_background = lambda fn, *args: fn(*args)
        gcode.commands["NFC_TEST_SCAN"](FakeGcmd(gcode, TEXT="ABS:010203"))
        self.assertEqual(plugin.pending.material(), "ABS")
        gcode.commands["NFC_DUMP"](FakeGcmd(gcode, RAW=1))
        self.assertIn("NDEF text: ABS:010203", gcode.info[-1])

    def test_single_slot_test_scan_stays_in_command_context(self):
        plugin, gcode, objects = make_plugin(slots=1)
        plugin._wait_background = lambda fn, *args: fn(*args)
        gcode.commands["NFC_TEST_SCAN"](FakeGcmd(gcode, TEXT="PLA:112233"))
        # run_script would deadlock inside a G-code command.
        self.assertEqual(gcode.scripts, [])
        self.assertEqual(gcode.in_command, ["IFS_SET_MATERIAL SLOT=1 TYPE=PLA COLOR=112233"])

    def test_foreign_spool_id_falls_back_to_uid(self):
        plugin, gcode, objects = make_plugin()
        objects["webhooks"]._remote_methods["spoolman_set_active_spool"] = True
        looked_up = []

        class FakeSpoolman(object):
            def get_spool(self, spool_id):
                looked_up.append(spool_id)
                raise klipper_plugin.SpoolmanError("no spool")

            def find_spool_by_uid(self, uid):
                return {"id": 12, "filament": {"material": "PLA"}, "extra": {}}

        plugin.spoolman = FakeSpoolman()
        payload = b'{"protocol":"openspool","type":"PLA","color_hex":"0022FF","spool_id":1776053727949617}'
        from helpers import mime_record
        scan = plugin._process(type2_tag(mime_record("application/json", payload)))
        self.assertEqual(looked_up, [])          # implausible ID not even tried
        self.assertEqual(scan.spool["id"], 12)

    def _spoolman_by_filament(self, spools):
        plugin, gcode, objects = make_plugin()
        objects["webhooks"]._remote_methods["spoolman_set_active_spool"] = True

        class FakeSpoolman(object):
            def find_spool_by_uid(self, uid):
                return None

            def request(self, method, path, query=None, body=None):
                return spools

        fake = FakeSpoolman()
        from kilnnfc.spoolman import Spoolman
        fake.find_spools_by_filament = lambda f: Spoolman.find_spools_by_filament(fake, f)
        plugin.spoolman = fake
        payload = b'{"protocol":"openspool","brand":"Snapmaker","type":"PLA","color_hex":"6600CC"}'
        from helpers import mime_record
        return plugin._process(type2_tag(mime_record("application/json", payload)))

    def test_unbound_tag_matches_spool_by_filament(self):
        violet = {"id": 19, "extra": {}, "filament": {
            "vendor": {"name": "Snapmaker"}, "material": "PLA", "color_hex": "6600cc"}}
        red = {"id": 6, "extra": {}, "filament": {
            "vendor": {"name": "Snapmaker"}, "material": "PLA", "color_hex": "E36868"}}
        scan = self._spoolman_by_filament([red, violet])
        self.assertEqual(scan.spool["id"], 19)

    def test_several_filament_matches_are_not_guessed(self):
        spool = lambda n: {"id": n, "extra": {}, "filament": {
            "vendor": {"name": "Snapmaker"}, "material": "PLA", "color_hex": "6600CC"}}
        scan = self._spoolman_by_filament([spool(19), spool(30)])
        self.assertIsNone(scan.spool)
        self.assertIn("2 spools match", scan.spoolman_note)

    def test_spool_title_drops_repeated_words(self):
        from kilnnfc.spoolman import spool_title
        spool = {"id": 15, "filament": {"vendor": {"name": "Snapmaker"}, "material": "PLA",
                                        "name": "PLA SnapSpeed"}}
        self.assertEqual(spool_title(spool), "Snapmaker PLA SnapSpeed")

    def test_bad_vendor_key(self):
        with self.assertRaises(ConfigError):
            make_plugin({"bambu_key": "00" * 16})

    def test_no_filament_data(self):
        plugin, gcode, _ = make_plugin()
        plugin._handle_scan(plugin._process(type2_tag(b"")))
        self.assertIsNone(plugin.pending)
        self.assertIn("has no filament data", gcode.info[-1])


class HookTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.hook = os.path.join(self.dir, "nfc-hook.sh")
        self.proc = os.path.join(self.dir, "proc")
        os.mkdir(self.proc)

    def write(self, path, text, mode="w"):
        with open(path, mode) as handle:
            handle.write(text)

    def watcher(self, pid, args):
        os.mkdir(os.path.join(self.proc, pid))
        self.write(os.path.join(self.proc, pid, "cmdline"), b"\0".join(args) + b"\0", "wb")

    def test_missing(self):
        problems = klipper_plugin.hook_problems(self.hook, self.proc)
        self.assertEqual(len(problems), 1)
        self.assertIn("install.sh as root", problems[0])

    def test_outdated(self):
        self.write(self.hook, "NFC_HOOK_VERSION=0\n")
        self.assertIn("outdated", klipper_plugin.hook_problems(self.hook, self.proc)[0])

    def test_watcher(self):
        self.write(self.hook, "x\nNFC_HOOK_VERSION=%d\n" % klipper_plugin.HOOK_VERSION)
        self.assertIn("not running", klipper_plugin.hook_problems(self.hook, self.proc)[0])
        self.watcher("12", [b"/bin/sh", b"/data/ad5x/nfc-hook.sh", b"watch"])
        self.assertEqual(klipper_plugin.hook_problems(self.hook, self.proc), [])

    def test_shipped_hook_version_matches(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "root", "nfc-hook.sh")) as handle:
            text = handle.read()
        self.assertIn("NFC_HOOK_VERSION=%d\n" % klipper_plugin.HOOK_VERSION, text)


if __name__ == "__main__":
    unittest.main()
