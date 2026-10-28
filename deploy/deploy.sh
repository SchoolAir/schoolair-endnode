#!/usr/bin/env bash
# deploy.sh — run on the Pi as admin to complete the schoolair installation.
# Assumes code has already been rsynced to /home/admin/schoolair/.
#
# Usage: bash /home/admin/schoolair/deploy/deploy.sh

set -euo pipefail

SCHOOLAIR_DIR="/home/admin/schoolair"
SYSTEMD_DIR="/etc/systemd/system"

log()  { echo "[deploy] $*"; }
ok()   { echo "[deploy] ✓  $*"; }
die()  { echo "[deploy] ✗  $*" >&2; exit 1; }

log "=== SchoolAir deployment starting ==="

# ── 1. Python dependencies ────────────────────────────────────────────────────
log "Installing Python dependencies…"
pip3 install --quiet --break-system-packages --root-user-action=ignore \
    httpx python-dotenv questionary netifaces
ok "Python dependencies installed"

# ── 2. Create .env if it doesn't exist ───────────────────────────────────────
if [ ! -f "$SCHOOLAIR_DIR/.env" ]; then
    cp "$SCHOOLAIR_DIR/.env.example" "$SCHOOLAIR_DIR/.env"
    log "Created .env from .env.example"
else
    ok ".env already exists — skipping"
fi

# ── 3. Migrate existing auth token ───────────────────────────────────────────
log "Migrating existing auth token…"
if python3 "$SCHOOLAIR_DIR/migrate_token.py"; then
    ok "Token migrated to .env"
else
    log "No token found — device will need wizard registration on first boot"
fi

# ── 4. Build sen6x binaries ───────────────────────────────────────────────────
log "Building sen6x binaries…"
DAEMON_SRC="$SCHOOLAIR_DIR/i2c/sen6x"
DAEMON_DST="/home/admin/i2c/sen6x"

if ! command -v gcc &>/dev/null; then
    die "gcc not found — install build-essential: sudo apt install build-essential"
fi

(cd "$DAEMON_SRC" && make -f Makefile.daemon -B --silent) || die "Build failed"

mkdir -p "$DAEMON_DST"
cp "$DAEMON_SRC/sen6x_d"    "$DAEMON_DST/sen6x_d"
cp "$DAEMON_SRC/sen6x_read" "$DAEMON_DST/sen6x_read"
chmod +x "$DAEMON_DST/sen6x_d" "$DAEMON_DST/sen6x_read"
ok "sen6x binaries built and installed to $DAEMON_DST"

# ── 5. Install systemd service files ─────────────────────────────────────────
log "Installing service files…"
sudo cp "$SCHOOLAIR_DIR/deploy/sen6x.service"                "$SYSTEMD_DIR/sen6x.service"
sudo cp "$SCHOOLAIR_DIR/deploy/schoolair.service"            "$SYSTEMD_DIR/schoolair.service"
sudo cp "$SCHOOLAIR_DIR/deploy/schoolair-wizard.service"     "$SYSTEMD_DIR/schoolair-wizard.service"
sudo cp "$SCHOOLAIR_DIR/deploy/schoolair-ble.service"        "$SYSTEMD_DIR/schoolair-ble.service"
sudo cp "$SCHOOLAIR_DIR/deploy/schoolair-launcher.service"   "$SYSTEMD_DIR/schoolair-launcher.service"
sudo cp "$SCHOOLAIR_DIR/deploy/schoolair-netwatch.service"   "$SYSTEMD_DIR/schoolair-netwatch.service"
chmod +x "$SCHOOLAIR_DIR/registration_wizard/launcher.sh"
chmod +x "$SCHOOLAIR_DIR/registration_wizard/netwatch.sh"
ok "Service files installed"

# ── 6. Add sudoers rule (allows telemetry service to start wizard) ────────────
SUDOERS_FILE="/etc/sudoers.d/schoolair-wizard"
if [ ! -f "$SUDOERS_FILE" ]; then
    echo 'admin ALL=NOPASSWD: /bin/systemctl start schoolair-wizard' \
        | sudo tee "$SUDOERS_FILE" > /dev/null
    sudo chmod 0440 "$SUDOERS_FILE"
    ok "sudoers rule added"
else
    ok "sudoers rule already present"
fi

# ── 7. Reload systemd (must happen before any enable/start) ──────────────────
sudo systemctl daemon-reload
ok "systemd daemon reloaded"

# ── 8. Stop and disable old services ─────────────────────────────────────────
log "Stopping old services…"
sudo systemctl stop    schoolair-telemetry 2>/dev/null && log "stopped schoolair-telemetry" || true
sudo systemctl disable schoolair-telemetry 2>/dev/null && log "disabled schoolair-telemetry" || true
sudo systemctl stop schoolair-wizard 2>/dev/null || true
ok "Old services stopped"

# ── 9. Enable services ───────────────────────────────────────────────────────
sudo systemctl enable sen6x.service
sudo systemctl enable schoolair.service
sudo systemctl enable schoolair-launcher.service
sudo systemctl enable schoolair-netwatch.service
# schoolair-wizard.service is NOT enabled — the launcher / netwatch start it on demand.
ok "Services enabled"

# ── 10. Run sen6x initialisation (schoolair.service depends on it) ───────────
if ! sudo systemctl is-active --quiet sen6x.service; then
    sudo systemctl start sen6x.service
    sleep 2
fi
ok "sen6x.service initialisation complete"

# ── 11. Start the telemetry service ──────────────────────────────────────────
log "Starting schoolair.service…"
sudo systemctl start schoolair.service
sleep 2

if sudo systemctl is-active --quiet schoolair.service; then
    ok "schoolair.service is running"
else
    echo ""
    echo "schoolair.service failed to start. Check logs with:"
    echo "  journalctl -u schoolair.service -n 50"
    die "Deployment failed at service start"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "[deploy] === Deployment complete ==="
echo ""
echo "Dashboard:  http://$(hostname -I | awk '{print $1}'):8080"
echo "Logs:       journalctl -u schoolair.service -f"
echo ""
echo "If the dashboard shows 'not registered', go to:"
echo "  http://$(hostname -I | awk '{print $1}'):8080  → Re-register"
