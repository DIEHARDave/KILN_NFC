#!/bin/sh
# KILN_NFC: the root part (a plugin runs as user klipper and can't).
# Only needed for USB readers; LaneTag and other network readers work
# without it. Run once as root after cloning, and again when NFC_STATUS
# says the hook is outdated:
#   sh /data/ad5x/plugins/nfc/install.sh
# Copies root/nfc-hook.sh to /data/ad5x/nfc-hook.sh (root-owned, so the
# clone that Moonraker updates as klipper is never run as root), adds its
# line to /data/ad5x/camera.conf (read at every boot) and starts it.
set -e
HOOK=/data/ad5x/nfc-hook.sh
CONF=/data/ad5x/camera.conf
WATCH_PID=/run/kiln-nfc-usb.pid
SERVICE=/etc/init.d/S64ad5x-camera
LINE=". $HOOK"
D=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "install.sh: run as root"; exit 1; }
grep -qs "$CONF" $SERVICE || {
	echo "install.sh: $SERVICE missing or doesn't read $CONF (not Kiln OS?)"
	exit 1
}

cp "$D/root/nfc-hook.sh" $HOOK.new
chown root:root $HOOK.new
chmod 755 $HOOK.new
mv $HOOK.new $HOOK
echo "installed $HOOK (version $(sed -n 's/^NFC_HOOK_VERSION=//p' $HOOK))"

if ! grep -qxF "$LINE" $CONF 2>/dev/null; then
	[ -f $CONF ] && cp -p $CONF $CONF.before-kiln-nfc
	printf '# KILN_NFC plugin (uninstall.sh removes these two lines)\n%s\n' \
		"$LINE" >> $CONF
	echo "added the hook to $CONF"
fi
# Kiln runs with umask 077: let Klipper read it
chmod 644 $CONF

# Restart the watcher with the new code (the cameras keep running)
start-stop-daemon -K -q -p $WATCH_PID 2>/dev/null || true
rm -f $WATCH_PID
start-stop-daemon -S -b -m -p $WATCH_PID -x /bin/sh -- $HOOK watch
echo "USB watcher started: plug the reader in (or replug it) and run NFC_STATUS"
