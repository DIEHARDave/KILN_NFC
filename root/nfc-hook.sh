#!/bin/sh
#####################################################################
# KILN_NFC root hook: USB access for NFC readers on Kiln OS (AD5X)
#
# Klipper runs as user klipper, and Kiln's /dev/bus/usb nodes are
# root-only (mdev.conf is on the read-only system). Kiln's stock 5.10
# kernel also has no USB serial drivers, so the plugin drives the
# readers through those nodes itself (usbfs).
#
# install.sh copies this to /data/ad5x/nfc-hook.sh (root) and adds
#   . /data/ad5x/nfc-hook.sh
# to /data/ad5x/screen.conf, which Kiln's /etc/init.d/S65ad5x-screen
# (root) sources at every boot (a stopgap: Kiln has no plugin boot hook,
# and camera.conf, used up to version 1, is no longer run). When
# sourced, this hook starts the
# watcher once (pid in /run, so once per boot). The watcher
# ("nfc-hook.sh watch") gives group klipper read/write on the nodes of
# USB devices that can be NFC readers - CCID class (ACR122U, ACR1552U),
# and the serial chips PN532 boards and reader bridges use - whenever
# USB devices come or go. Nothing else is touched.
#####################################################################

# Bump when this file changes: the plugin's NFC_STATUS compares it with
# the installed copy and asks for install.sh again
NFC_HOOK_VERSION=2

NFC_HOOK=/data/ad5x/nfc-hook.sh
NFC_WATCH_PID=/run/kiln-nfc-usb.pid

# Vendors of the USB serial chips and boards the plugin probes (keep in
# sync with readers/detect.py PROBE_VIDS), and ACS (CCID readers)
NFC_VIDS="1a86 4348 10c4 067b 0403 2e8a 303a 072f"

# nfc_reader DIR: true when the USB device at sysfs DIR may be a reader
nfc_reader() {
	nfc_vid=$(cat "$1/idVendor" 2>/dev/null)
	for v in $NFC_VIDS; do
		[ "$nfc_vid" = "$v" ] && return 0
	done
	for c in "$1"/*:*/bInterfaceClass; do
		[ "$(cat "$c" 2>/dev/null)" = 0b ] && return 0
	done
	return 1
}

nfc_grant() {
	for d in /sys/bus/usb/devices/*; do
		[ -f "$d/busnum" ] && [ -f "$d/devnum" ] || continue
		nfc_reader "$d" || continue
		node=$(printf '/dev/bus/usb/%03d/%03d' "$(cat $d/busnum)" "$(cat $d/devnum)")
		[ -c "$node" ] || continue
		chgrp klipper "$node" && chmod 660 "$node"
	done
}

nfc_watch() {
	last=
	while :; do
		now=$(ls /sys/bus/usb/devices 2>/dev/null | tr '\n' ' ')
		if [ "$now" != "$last" ]; then
			# mdev creates the node a moment after sysfs shows the device
			sleep 1
			nfc_grant
			last=$now
		fi
		sleep 2
	done
}

if [ "$1" = watch ]; then
	nfc_watch
	exit 0
fi

if ! { [ -f $NFC_WATCH_PID ] && kill -0 "$(cat $NFC_WATCH_PID)" 2>/dev/null; }; then
	start-stop-daemon -S -b -m -p $NFC_WATCH_PID -x /bin/sh -- $NFC_HOOK watch
fi
