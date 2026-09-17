#!/usr/bin/env bash
# SchoolAir Golden Image Preparation Script
#
# Run as the admin user before shutting down to image the SD card.
# Removes all device-unique state so every clone boots fresh.
#
#   bash prepare_image.sh
#   sudo shutdown -h now   ← after it completes
#
# Usage:  bash prepare_image.sh

set -euo pipefail

ADMIN_USER="${SUDO_USER:-admin}"
ADMIN_HOME="/home/${ADMIN_USER}"
SCHOOLAIR_DIR="${ADMIN_HOME}/schoolair"

BOLD='\033[1m'; GREEN='\033[0;32m'; NC='\033[0m'
step() { echo; echo -e "${BOLD}▶ $*${NC}"; }
ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }

echo -e "${BOLD}━━━ SchoolAir Golden Image Preparation ━━━${NC}"
echo    "    User: ${ADMIN_USER}  |  Home: ${ADMIN_HOME}"

# ── 1. Stop services ──────────────────────────────────────────────────────────
step "Stopping services"
for svc in schoolair-wizard schoolair-launcher schoolair schoolair-netwatch nodered sen6x nginx; do
    sudo systemctl stop "$svc" 2>/dev/null && ok "Stopped $svc" || true
done

# ── 2. Device identity & registration state ───────────────────────────────────
step "Removing device identity"
# Reset .env: copy from .env.example (drops any appended device-specific keys
# like NEW_AUTH_TOKEN and NEW_SERVER_URL that the wizard appends).
# Fallback sed path clears all three auth keys individually.
if [ -f "${SCHOOLAIR_DIR}/.env.example" ]; then
    cp "${SCHOOLAIR_DIR}/.env.example" "${SCHOOLAIR_DIR}/.env"
    ok ".env reset to defaults from .env.example"
elif [ -f "${SCHOOLAIR_DIR}/.env" ]; then
    sed -i \
        -e 's/^AUTH_TOKEN=.*/AUTH_TOKEN=/' \
        -e '/^NEW_AUTH_TOKEN=/d' \
        -e '/^NEW_SERVER_URL=/d' \
        "${SCHOOLAIR_DIR}/.env"
    ok "AUTH_TOKEN / NEW_AUTH_TOKEN / NEW_SERVER_URL cleared from .env"
fi
# Old-style wizard state (backwards-compatible with install_files-based deployments)
rm -f "${ADMIN_HOME}/.device_token"
rm -f "${ADMIN_HOME}/.config/schoolair/status.json"
rm -f "${ADMIN_HOME}/.config/schoolair/staging.json"
rm -f "${ADMIN_HOME}/.config/schoolair/last_error.txt"
ok "Device token and wizard state cleared"

# ── 3. Clear runtime database and per-device config ───────────────────────────
step "Clearing runtime database and per-device state"
rm -f "${SCHOOLAIR_DIR}/queue.db" \
      "${SCHOOLAIR_DIR}/queue.db-shm" \
      "${SCHOOLAIR_DIR}/queue.db-wal"
rm -f "${SCHOOLAIR_DIR}/spike_state.json"
ok "SQLite queue cleared"
# settings.json holds drain_jitter_seconds — a per-device random offset that
# must be unique per clone. Delete it so each clone generates its own.
rm -f "${SCHOOLAIR_DIR}/config/settings.json"
ok "config/settings.json removed (clone will generate unique drain jitter)"
# criteria.json is pushed by the server on first ingest; don't bake in stale thresholds.
rm -f "${SCHOOLAIR_DIR}/config/criteria.json"
ok "config/criteria.json removed (will be fetched from server after registration)"
# wifi_state.json tracks WiFi push credential_ids and pending acks.
rm -f "${SCHOOLAIR_DIR}/config/wifi_state.json"
ok "config/wifi_state.json removed (WiFi push state cleared)"

# ── 4. nginx: ensure disabled so wizard owns port 80 on first boot ────────────
step "Resetting nginx to disabled"
sudo systemctl disable nginx 2>/dev/null || true
ok "nginx disabled"

# ── 5. Node-RED cleanup (skipped gracefully if not installed) ─────────────────
if [ -d "${ADMIN_HOME}/.node-red" ]; then
    step "Cleaning Node-RED"
    rm -f  "${ADMIN_HOME}/.node-red/.config.json.backup"
    rm -f  "${ADMIN_HOME}/.node-red/flows_"*.json.backup 2>/dev/null || true
    rm -rf "${ADMIN_HOME}/.node-red/context/"
    ok "Node-RED backups and runtime context cleared"
fi

# ── 6. APT cache ──────────────────────────────────────────────────────────────
step "Cleaning APT cache"
sudo apt-get autoremove -y -qq
sudo apt-get clean
ok "APT cache cleared"

# ── 7. Reset cloud-init ───────────────────────────────────────────────────────
step "Resetting cloud-init"
sudo cloud-init clean --logs
sudo rm -rf /var/lib/cloud/instances/*
# Pi Imager writes WiFi credentials to /boot/firmware/network-config.
# cloud-init clean causes cloud-init to re-run on next boot, which would
# re-create the home WiFi NM profile from that file. Replace the entire file
# with a minimal ethernet-only config so clones don't inherit the original
# owner's home network credentials.
if [ -f /boot/firmware/network-config ]; then
    sudo tee /boot/firmware/network-config > /dev/null <<'NETCFG'
version: 2
ethernets:
  eth0:
    dhcp4: true
    optional: true
NETCFG
    ok "cloud-init network-config: replaced with ethernet-only config (WiFi credentials removed)"
fi
ok "cloud-init reset"

# ── 8. Reset hostname ─────────────────────────────────────────────────────────
step "Ensuring first-boot service is enabled for clones"
sudo systemctl enable schoolair-first-boot.service 2>/dev/null \
    && ok "schoolair-first-boot.service enabled" \
    || ok "schoolair-first-boot.service not found (run schoolair_setup.sh first)"

step "Resetting hostname"
sudo bash "${ADMIN_HOME}/set_hostname.sh" "schoolair-template" > /dev/null
ok "Hostname → schoolair-template  (Pi Imager can override per-device when flashing)"

# ── 9. SSH host keys ──────────────────────────────────────────────────────────
step "Removing SSH host keys"
sudo rm -f /etc/ssh/ssh_host_*
# The regenerate_ssh_host_keys service disables itself after its first run.
# Re-enable it so the next boot (of a clone) regenerates fresh keys.
sudo systemctl enable regenerate_ssh_host_keys.service 2>/dev/null || true
ok "Keys removed — regenerate_ssh_host_keys.service re-enabled for next boot"

# ── 9b. Machine ID + filesystem-grow first-boot unit ───────────────────────────
# systemd's ConditionFirstBoot= (used by regenerate_ssh_host_keys.service above,
# AND by rpi-resize.service — the unit that grows the root filesystem to fill
# whatever card a clone gets flashed onto) is derived from /etc/machine-id being
# empty. Leaving a real machine-id in the golden image means systemd never
# considers a clone's boot a "first boot", every clone silently shares the same
# machine-id (breaks DHCP client-id/journald per-device identity), AND the
# filesystem-grow step never fires. rpi-resize.service also self-disables after
# its first run (same pattern as the SSH key service above) — it already fired
# once during this device's own original setup, so it must be re-enabled here
# too, not just have its condition fixed.
step "Resetting machine-id and re-enabling filesystem-grow for clones"
sudo truncate -s 0 /etc/machine-id
sudo systemctl enable rpi-resize.service 2>/dev/null || true
ok "machine-id cleared, rpi-resize.service re-enabled — clones will grow to fill their card on first boot"

# ── 10. Logs, journald, and shell history ─────────────────────────────────────
step "Wiping logs and shell history"
sudo find /var/log -type f -exec truncate -s 0 {} \; 2>/dev/null || true
sudo truncate -s 0 /var/log/schoolair-setup.log 2>/dev/null || true
# Vacuum journald so systemd unit logs from this device don't appear on clones.
sudo journalctl --vacuum-time=1s 2>/dev/null || true
ok "Flat logs and journald vacuumed"
# Clear SSH known_hosts so clones don't carry this device's server fingerprints.
rm -f "${ADMIN_HOME}/.ssh/known_hosts"
ok "SSH known_hosts cleared"
cat /dev/null > "${ADMIN_HOME}/.bash_history"
history -c 2>/dev/null || true
ok "Shell history cleared"

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "  ${GREEN}${BOLD}Image preparation complete.${NC}"
echo
echo "  Next steps:"
echo "  1. Shut down the Pi cleanly:"
echo "       sudo shutdown -h now"
echo "  2. Attach the SD card to your laptop:"
echo "       lsblk   (confirm device, e.g. /dev/sdX)"
echo "  3. Create the image:"
echo "       sudo dd if=/dev/sdX of=schoolair-golden-$(date +%Y%m%d).img bs=4M status=progress"
echo "  4. Optionally shrink with pishrink:"
echo "       pishrink.sh schoolair-golden-$(date +%Y%m%d).img"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# ── Last step: drop WiFi client connections ───────────────────────────────────
# Done last so SSH stays alive for the entire script. Losing the connection
# here is the signal that everything completed — safe to shut down.
echo
step "Removing saved WiFi connections (preserving SchoolAir_AP)"
echo
echo -e "${BOLD}  The Pi will now forget its network credentials (saved WiFi's and passwords)."
echo -e "  SSH will disconnect. Run 'sudo shutdown -h now' to power off before imaging.${NC}"
echo
nmcli -t -f NAME,TYPE connection show \
    | awk -F: '$2=="802-11-wireless" && $1!="SchoolAir_AP" {print $1}' \
    | while IFS= read -r CON; do
        sudo nmcli connection delete "$CON" 2>/dev/null \
            && ok "Deleted WiFi profile: $CON" || true
    done
