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

# rpi-resize.service (grow + fstrim the root fs) is ConditionFirstBoot=yes and only
# disables itself when it actually runs. If systemd did not consider this a first
# boot (e.g. /etc/machine-id was already populated) it is skipped every boot —
# yet its Wants= still pulls in fstrim.service, a full-card TRIM that stalls the
# SD card for ~30s on a Pi Zero W (starving NetworkManager and everything
# after it) on EVERY boot. Drop the enable once it has been skipped.
if [ "$(systemctl show rpi-resize.service -p ConditionResult --value 2>/dev/null)" = "no" ]; then
    systemctl disable rpi-resize.service &>/dev/null || true
    echo "[schoolair-first-boot] rpi-resize.service was skipped (not a first boot) — disabled so it stops triggering fstrim"
fi

[[ "$(hostname)" == "schoolair-template" ]] || exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[schoolair-first-boot] Template hostname detected — assigning unique hostname…"
NEW_HN=$(bash "${SCRIPT_DIR}/set_hostname.sh")
echo "[schoolair-first-boot] Hostname is now: ${NEW_HN}"

# ── Unit-type detection (indoor/outdoor): see detect_unit_type.sh. Offline
# and fast, like the rest of this script.
/home/admin/schoolair/detect_unit_type.sh || true

# Indoor units: is a wilting flower fitted? detect_flower.sh reads the dock's
# strap and sets /etc/schoolair-flower-fitted; offline and instant, like the
# rest of this script. The flower itself starts once pigpiod is installed.
if grep -qs indoor /etc/schoolair-unit-type && [ -x /home/admin/schoolair/detect_flower.sh ]; then
    /home/admin/schoolair/detect_flower.sh || true
fi
