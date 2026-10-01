#!/usr/bin/env bash
# detect_unit_type.sh — indoor (SEN63C) or outdoor (SEN65) unit?
#
# Writes /etc/schoolair-unit-type, which gates everything indoor-only: pigpiod
# (schoolair-pigpio-setup.service), the status LED and the wilting flower.
# Indoor units may carry a flower (stepper on GPIO17/27/22/23; see
# detect_flower.sh), outdoor units don't. Same detect-by-getProductName()
# technique sen6x_read.c already uses internally — we just read its JSON
# output rather than duplicating the I2C probe. Needs I2C enabled and
# sen6x_read built.
#
# Run as root by:
#  - first_boot.sh, at a golden-image clone's first boot (the clone inherits
#    I2C and sen6x_read from the source device);
#  - schoolair_setup.sh, on a fresh install, right after it enables I2C.
#
# Only does fast, offline-safe work (I2C probe + a local cmdline.txt edit):
# first_boot.sh runs before the AP hotspot can come up. Installing pigpiod needs
# the network, so that is schoolair-pigpio-setup.service's job.
# Exit status: 0 type written, 1 sensor not identified.
set -u

SEN6X_READ="${SEN6X_READ:-/home/admin/i2c/sen6x/sen6x_read}"
UNIT_TYPE_FILE="${UNIT_TYPE_FILE:-/etc/schoolair-unit-type}"
log() { echo "[detect_unit_type] $*"; }

if [ ! -x "$SEN6X_READ" ]; then
    log "sen6x_read not found — skipping unit-type detection"
    exit 1
fi

log "Detecting sensor type…"
reading="$("$SEN6X_READ" --init 2>&1)" || true

if echo "$reading" | grep -q '"co2"'; then
    log "SEN63C detected — indoor unit"
    echo "indoor" > "$UNIT_TYPE_FILE"
    # Free GPIO14 from the serial console (local edit, works offline; takes
    # effect after the next reboot). GPIO14 was for the flower's servo, retired
    # in Rev H; the stepper doesn't need it. Kept because it is harmless and
    # changing the boot console on units in the field is not worth the risk.
    if grep -q "console=serial0" /boot/firmware/cmdline.txt 2>/dev/null; then
        sed -i 's/console=serial0,[0-9]* //' /boot/firmware/cmdline.txt
        log "Serial console disabled (GPIO14 free)"
    fi
    log "pigpiod install deferred to schoolair-pigpio-setup.service (needs network)"
elif echo "$reading" | grep -q '"voc"'; then
    log "SEN65 detected — outdoor unit, no actuator"
    echo "outdoor" > "$UNIT_TYPE_FILE"
else
    log "WARNING: could not identify sensor type — leaving defaults"
    exit 1
fi
