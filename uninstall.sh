#!/bin/sh
# KILN_NFC: remove the root part (install.sh), as root:
#   sh /data/ad5x/plugins/nfc/uninstall.sh
# Reader nodes go back to root-only at their next replug or reboot.
# Then DISABLE_PLUGIN NAME=nfc and delete /data/ad5x/plugins/nfc.
HOOK=/data/ad5x/nfc-hook.sh
WATCH_PID=/run/kiln-nfc-usb.pid

[ "$(id -u)" = 0 ] || { echo "uninstall.sh: run as root"; exit 1; }
start-stop-daemon -K -q -p $WATCH_PID 2>/dev/null
rm -f $WATCH_PID
# screen.conf now, camera.conf from hook version 1
for CONF in /data/ad5x/screen.conf /data/ad5x/camera.conf; do
	[ -f $CONF ] || continue
	sed -i -e '/^# KILN_NFC plugin/d' -e "\\|^\\. $HOOK\$|d" $CONF
	# nothing left but comments and blank lines: it was ours
	grep -qv '^[[:space:]]*\(#.*\)\{0,1\}$' $CONF || rm -f $CONF
done
rm -f $HOOK
echo "KILN_NFC root part removed"
