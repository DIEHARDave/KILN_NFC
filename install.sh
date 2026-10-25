#!/bin/sh
# KILN_NFC: the root part (a plugin runs as user klipper and can't).
# Only needed for USB readers; LaneTag and other network readers work
# without it. Run once as root after cloning, and again when NFC_STATUS
# says the hook is outdated:
#   sh /data/ad5x/plugins/nfc/install.sh
# Copies root/nfc-hook.sh to /data/ad5x/nfc-hook.sh (root-owned, so the
# clone that Moonraker updates as klipper is never run as root), adds its
# line to /data/ad5x/screen.conf (sourced as root at every boot by Kiln's
# screen service) and starts it. A stopgap: Kiln has no boot hook for
# plugins, and since v0.0.24 camera.conf (used before) is no longer run.
set -e
HOOK=/data/ad5x/nfc-hook.sh
CONF=/data/ad5x/screen.conf
OLD_CONF=/data/ad5x/camera.conf
WATCH_PID=/run/kiln-nfc-usb.pid
SERVICE=/etc/init.d/S65ad5x-screen
LINE=". $HOOK"
D=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "install.sh: run as root"; exit 1; }
grep -qs '^[[:space:]]*\[ -f \$CONF \] && \. \$CONF' $SERVICE &&
	grep -qs "^CONF=$CONF\$" $SERVICE || {
	echo "install.sh: $SERVICE missing or doesn't source $CONF;"
	echo "this Kiln OS has no hook left to use (see README)"
	exit 1
}

cp "$D/root/nfc-hook.sh" $HOOK.new
chown root:root $HOOK.new
chmod 755 $HOOK.new
mv $HOOK.new $HOOK
echo "installed $HOOK (version $(sed -n 's/^NFC_HOOK_VERSION=//p' $HOOK))"

# Hook version 1 used camera.conf, which Kiln now reads as settings only
# (and logs our line as skipped)
if grep -qxF "$LINE" $OLD_CONF 2>/dev/null; then
	sed -i -e '/^# KILN_NFC plugin/d' -e "\\|^\\. $HOOK\$|d" $OLD_CONF
	grep -qv '^[[:space:]]*\(#.*\)\{0,1\}$' $OLD_CONF || rm -f $OLD_CONF
	echo "removed the old hook line from $OLD_CONF"
fi

if ! grep -qxF "$LINE" $CONF 2>/dev/null; then
	[ -f $CONF ] && cp -p $CONF $CONF.before-kiln-nfc
	printf '# KILN_NFC plugin (uninstall.sh removes these two lines)\n%s\n' \
		"$LINE" >> $CONF
	echo "added the hook to $CONF"
fi
chown root:root $CONF
chmod 644 $CONF

# Restart the watcher with the new code (the screen stream keeps running)
start-stop-daemon -K -q -p $WATCH_PID 2>/dev/null || true
rm -f $WATCH_PID
start-stop-daemon -S -b -m -p $WATCH_PID -x /bin/sh -- $HOOK watch
echo "USB watcher started: plug the reader in (or replug it) and run NFC_STATUS"
