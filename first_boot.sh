#!/usr/bin/env bash
# SchoolAir First-Boot Hostname Assignment + Unit-Type Detection
#
# Runs on every boot via schoolair-first-boot.service.
# Acts only when the hostname is exactly "schoolair-template" (freshly flashed clone).

set -euo pipefail

# Regenerate SSH host keys if missing (wiped by prepare_image.sh on the source device).
# Must run before sshd starts — enforced via Before=ssh.service in the unit file.
if ! ls /etc/ssh/ssh_host_*_key &>/dev/null 2>&1; then
    echo "[schoolair-first-boot] SSH host keys missing — regenerating…"
    ssh-keygen -A
    echo "[schoolair-first-boot] SSH host keys generated"
fi

[[ "$(hostname)" == "schoolair-template" ]] || exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[schoolair-first-boot] Template hostname detected — assigning unique hostname…"
NEW_HN=$(bash "${SCRIPT_DIR}/set_hostname.sh")
echo "[schoolair-first-boot] Hostname is now: ${NEW_HN}"

# ── Unit-type detection: indoor (SEN63C) units get a flower actuator on
# GPIO14, outdoor (SEN65) units don't. Same detect-by-getProductName()
# technique sen6x_read.c already uses internally — we just read its JSON
# output rather than duplicating the I2C probe. Golden-image clones inherit
# I2C already enabled and sen6x_read already built from the source device,
# so this works on true first boot without waiting on a prior reboot.
#
# Only does fast, offline-safe work (I2C probe + a local cmdline.txt edit).
# schoolair-first-boot.service runs *before* schoolair-launcher.service (the
# thing that brings up the AP hotspot) — there is deliberately no
# network-dependent work here, since anything that blocks on connectivity
# at this point would delay the AP from ever appearing on a truly fresh
# clone (no WiFi creds exist yet, and the AP isn't up yet either — nothing
# provides a network at this stage). Installing pigpiod needs the network,
# so that's handled by schoolair-pigpio-setup.service instead, gated on the
# marker file this writes and on network-online.target actually being met
# — whenever that happens, registration-time or later, without blocking
# anything else in the boot sequence.
configure_unit_type() {
    local sen6x_read="/home/admin/i2c/sen6x/sen6x_read"
    if [ ! -x "$sen6x_read" ]; then
        echo "[schoolair-first-boot] sen6x_read not found — skipping unit-type detection"
        return
    fi

    echo "[schoolair-first-boot] Detecting sensor type…"
    local reading=""
    reading=$("$sen6x_read" --init 2>&1) || true

    if echo "$reading" | grep -q '"co2"'; then
        echo "[schoolair-first-boot] SEN63C detected — indoor unit"
        echo "indoor" > /etc/schoolair-unit-type

        # Free GPIO14 from the serial console (local edit, works offline;
        # takes effect after the reboot that normally follows first boot
        # anyway — see schoolair_setup.sh's own "after rebooting" notes).
        if grep -q "console=serial0" /boot/firmware/cmdline.txt 2>/dev/null; then
            sed -i 's/console=serial0,[0-9]* //' /boot/firmware/cmdline.txt
            echo "[schoolair-first-boot] Serial console disabled (GPIO14 freed for flower actuator)"
        fi
        echo "[schoolair-first-boot] pigpiod install deferred to schoolair-pigpio-setup.service (needs network)"
    elif echo "$reading" | grep -q '"voc"'; then
        echo "[schoolair-first-boot] SEN65 detected — outdoor unit, no actuator"
        echo "outdoor" > /etc/schoolair-unit-type
    else
        echo "[schoolair-first-boot] WARNING: could not identify sensor type — leaving defaults"
    fi
}

configure_unit_type
