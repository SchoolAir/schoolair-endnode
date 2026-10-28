#!/usr/bin/env bash
# detect_flower.sh — is a wilting flower fitted to this unit?
#
# Detects the flower's ULN2003 driver board on the stepper pins themselves
# (FLOWER_PINS, default GPIO17/27/22/23 = IN1..IN4, physical 11/13/15/16), no
# jumper needed. Each ULN2003 input goes to ground through its built-in
# resistors (2.7k + 7.2k + 3k, ~13k), far below the Pi's ~50k pull-up: with the
# pull-up on, a pin wired to the board reads "lo" (~0.7 V, too little to switch
# the Darlington, so the motor never moves), and an open pin reads "hi".
# What it can't see is the motor itself behind the board's socket.
# The other parts (sensor type, the status LED, which is assumed rather than
# detected): README.md, "Hardware detection".
#
#   all four lo → board fitted: creates FLOWER_MARKER (/etc/schoolair-flower-fitted),
#                 which the schoolair-flower.service drop-in requires
#   all four hi → nothing there: removes the marker
#   mixed       → a loose or missing jumper wire: leaves the marker alone and
#                 says which pins
#
# Never probes pins in use: while schoolair-flower.service runs it holds them
# as outputs, and turning one into an input mid-move would lose the position.
# Then the answer is the existing marker. schoolair-redetect stops the flower
# first when a check must happen anyway.
#
# Pins are left as inputs with the pull-down, their power-on state, which also
# holds the ULN2003 inputs firmly off until the flower takes them.
#
# Run as root: at every boot (schoolair-detect-hardware.service, before the
# flower starts), at a clone's first boot (first_boot.sh), on every setup/update
# (schoolair_setup.sh) and on request (schoolair-redetect).
# Exit status: 0 flower fitted, 1 not fitted, 2 unknown.
set -u

FLOWER_PINS="${FLOWER_PINS:-17 27 22 23}"
FLOWER_MARKER="${FLOWER_MARKER:-/etc/schoolair-flower-fitted}"
PINCTRL="${PINCTRL:-pinctrl}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
log() { echo "[detect_flower] $*"; }

keep_marker() {
    [ -e "$FLOWER_MARKER" ] && exit 0 || exit 2
}

if ! command -v "$PINCTRL" >/dev/null 2>&1; then
    log "pinctrl not found — cannot check the driver board"
    keep_marker
fi

if "$SYSTEMCTL" is-active --quiet schoolair-flower.service 2>/dev/null; then
    log "schoolair-flower.service is running and owns the pins — not probing"
    keep_marker
fi
for p in $FLOWER_PINS; do
    # `pinctrl get 17` prints e.g. "17: op dl pd | lo // GPIO17 = output"
    if "$PINCTRL" get "$p" | grep -q '= output'; then
        log "GPIO${p} is an output (something drives it) — not probing"
        keep_marker
    fi
done

lo=""; hi=""
for p in $FLOWER_PINS; do
    "$PINCTRL" set "$p" ip pu    # input, pull-up: floats high without the board
    sleep 0.05
    level="$("$PINCTRL" get "$p" | sed -nE 's/.*\| *(lo|hi).*/\1/p')"
    "$PINCTRL" set "$p" ip pd    # back to the power-on state
    case "$level" in
        lo) lo="$lo $p" ;;
        hi) hi="$hi $p" ;;
        *)  log "could not read GPIO${p}"; keep_marker ;;
    esac
done

if [ -z "$hi" ]; then
    touch "$FLOWER_MARKER"
    log "ULN2003 driver found on GPIO${lo# } — flower fitted"
    exit 0
elif [ -z "$lo" ]; then
    rm -f "$FLOWER_MARKER"
    log "no driver board on GPIO${hi# } — no flower"
    exit 1
else
    log "WARNING: driver board on GPIO${lo# } but not on GPIO${hi# } — check the jumper wires; flower marker left as it was"
    keep_marker
fi
