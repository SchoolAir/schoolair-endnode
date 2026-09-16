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

        # pigpiod needs the network, which may not be up yet this early in
        # boot — retry apt-get update for up to a minute before giving up.
        echo "[schoolair-first-boot] Waiting for network to install pigpiod…"
        local attempt=0
        until apt-get update -qq 2>/dev/null; do
            attempt=$((attempt + 1))
            if [ "$attempt" -ge 12 ]; then
                echo "[schoolair-first-boot] WARNING: no network after 60s — pigpiod not installed, install manually later"
                return
            fi
            sleep 5
        done
        if apt-get install -y -qq pigpio python3-pigpio; then
            systemctl enable --now pigpiod
            echo "[schoolair-first-boot] pigpiod installed and running"
        else
            echo "[schoolair-first-boot] WARNING: pigpiod install failed"
        fi
    elif echo "$reading" | grep -q '"voc"'; then
        echo "[schoolair-first-boot] SEN65 detected — outdoor unit, no actuator"
        echo "outdoor" > /etc/schoolair-unit-type
    else
        echo "[schoolair-first-boot] WARNING: could not identify sensor type — leaving defaults"
    fi
}

configure_unit_type
