#!/usr/bin/env bash
# detect_flower.sh — is a wilting flower fitted to this unit?
#
# The flower's stepper has no sensor, and plain indoor kits have the same
# SEN63C, so the unit can't tell from what it measures. Instead the AQU dock's
# wiring harness ties one GPIO to ground: FLOWER_STRAP_GPIO, default GPIO26
# (physical pin 37, right next to ground on pin 39). With the Pi's internal
# pull-up switched on, that pin reads "lo" only when the strap is there.
#
# Strap found → creates FLOWER_MARKER (/etc/schoolair-flower-fitted), which the
# schoolair-flower.service drop-in requires (deploy/schoolair-flower-fitted.conf).
# Never removes the marker: a bench unit wired without a strap can be marked by
# hand (sudo touch /etc/schoolair-flower-fitted). prepare_image.sh removes it
# from golden images so every clone decides for itself.
#
# Run as root (first_boot.sh at a clone's first boot, schoolair_setup.sh on
# every setup/update). Exit status: 0 flower fitted, 1 not fitted, 2 unknown.
set -u

FLOWER_STRAP_GPIO="${FLOWER_STRAP_GPIO:-26}"
FLOWER_MARKER="${FLOWER_MARKER:-/etc/schoolair-flower-fitted}"
PINCTRL="${PINCTRL:-pinctrl}"

if ! command -v "$PINCTRL" >/dev/null 2>&1; then
    echo "[detect_flower] pinctrl not found — cannot check the strap"
    [ -e "$FLOWER_MARKER" ] && exit 0 || exit 2
fi

"$PINCTRL" set "$FLOWER_STRAP_GPIO" ip pu    # input, pull-up: floats high without the strap
sleep 0.1
# `pinctrl get 26` prints e.g. "26: ip    pu | lo // GPIO26 = input"
level="$("$PINCTRL" get "$FLOWER_STRAP_GPIO" | sed -nE 's/.*\| *(lo|hi).*/\1/p')"

case "$level" in
    lo)
        touch "$FLOWER_MARKER"
        echo "[detect_flower] strap on GPIO${FLOWER_STRAP_GPIO} found — flower fitted"
        exit 0 ;;
    hi)
        if [ -e "$FLOWER_MARKER" ]; then
            echo "[detect_flower] no strap on GPIO${FLOWER_STRAP_GPIO}, but ${FLOWER_MARKER} was set by hand — keeping it"
            exit 0
        fi
        echo "[detect_flower] no strap on GPIO${FLOWER_STRAP_GPIO} — no flower"
        exit 1 ;;
    *)
        echo "[detect_flower] could not read GPIO${FLOWER_STRAP_GPIO}"
        [ -e "$FLOWER_MARKER" ] && exit 0 || exit 2 ;;
esac
