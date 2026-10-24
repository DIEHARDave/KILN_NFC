# Klipper extras entry point for KILN_NFC ([kiln_nfc]).
#
# Kiln OS loads a plugin's extras/*.py while the plugin is enabled, and
# puts its python/ directory (the kilnnfc package) on sys.path.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys


def load_config(config):
    # Klipper keeps imported modules across RESTART. Drop the package each
    # time so a plugin update takes effect on the next RESTART.
    for name in list(sys.modules):
        if name == "kilnnfc" or name.startswith("kilnnfc."):
            del sys.modules[name]
    from kilnnfc import klipper_plugin
    return klipper_plugin.load_config(config)
