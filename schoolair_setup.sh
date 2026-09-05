#!/usr/bin/env bash
# SchoolAir – Device setup script
#
# Run on a fresh Raspberry Pi OS Lite (Bookworm or later):
#
#   curl -sSL https://raw.githubusercontent.com/SchoolAir/schoolair-ex-RMIT-pi/main/schoolair_setup.sh | sudo bash
#
# To override the Pi username (default: admin):
#   curl ... | sudo ADMIN_USER=pi bash
#
# What this script does:
#   0.  Pre-flight checks
#   1.  Hostname  →  schoolair-YYMDD-XXXX  (skipped if already set; M = hex month)
#   2.  System packages
#   3.  Clone SchoolAir repo  →  ~/schoolair/  (preserves .env on re-runs)
#   4.  Python dependencies
#   5.  Device utility scripts  →  ~/  (first_boot.sh, set_hostname.sh, add_wifi.sh, schoolair)
#   6.  Registration wizard TLS certificate
#   7.  Build sen6x binaries
#   8.  I2C enable + 100 kHz baudrate
#   9.  NetworkManager Wi-Fi hotspot (SchoolAir_Setup, open)
#   10. Captive-portal DNS hijacking via NM dnsmasq plugin
#   11. Avahi  →  schoolair-register.local
#   12. dhcpcd conflict prevention (Bullseye only)
#   13. nginx  →  port 80 (HTTP) + 443 (HTTPS, self-signed) → telemetry :8080 (disabled until registered)
#   14. Sudoers rule for telemetry to start wizard
#   15. systemd services
#   16. Verification + summary
#
# Port-80 lifecycle:
#   Unregistered:  wizard (Microdot) holds port 80
#   Registered:    wizard exits, nginx activates → proxies port 80 to telemetry :8080
#
# Idempotent — safe to re-run.  Hostname and .env are preserved once set.

set -euo pipefail

# ── Mode ───────────────────────────────────────────────────────────────────────
# Pass "--update" as the first argument to run only the code-update steps,
# skipping host-config steps (hostname, apt, I2C, networking) that never change
# after initial setup.  The default (no argument) runs the full first-time setup.
MODE="${1:-setup}"
[[ "$MODE" == "setup" || "$MODE" == "--update" ]] \
    || { echo "Usage: $0 [--update]"; exit 1; }

# ── Configuration ──────────────────────────────────────────────────────────────
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_HOME="/home/${ADMIN_USER}"

REPO_URL="https://github.com/SchoolAir/schoolair-ex-RMIT-pi.git"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_DIR="/tmp/schoolair-repo"

SCHOOLAIR_DIR="${ADMIN_HOME}/schoolair"
WIZARD_DIR="${SCHOOLAIR_DIR}/registration_wizard"
I2C_DIR="${ADMIN_HOME}/i2c"

AP_IFACE="wlan0"
AP_CONN="SchoolAir_AP"
AP_SSID="SchoolAir_Setup"
AP_IP="192.168.4.1"

TELEMETRY_PORT=8080

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE="/var/log/schoolair-setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "━━━ SchoolAir setup started: $(date) ━━━"

# ── Helpers ────────────────────────────────────────────────────────────────────
BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()  { echo; echo -e "${BOLD}── $* ──${NC}"; }
ok()    { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()  { echo -e "  ${YELLOW}⚠${NC}   $*"; }
skip()  { echo -e "  –   $* (skipped)"; }
die()   { echo -e "${RED}${BOLD}FATAL: $*${NC}"; exit 1; }

# ── 0. Pre-flight ──────────────────────────────────────────────────────────────
step "0 / Pre-flight"
[[ $EUID -eq 0 ]] \
    || die "Must run as root.  Try:  sudo bash $0"
id -u "$ADMIN_USER" >/dev/null 2>&1 \
    || die "User '${ADMIN_USER}' not found.  Set ADMIN_USER=<name> and re-run."
command -v nmcli   >/dev/null 2>&1 || die "nmcli not found — is NetworkManager installed?"
command -v python3 >/dev/null 2>&1 || die "python3 not found."
ok "Root, user='${ADMIN_USER}', home='${ADMIN_HOME}'"

# ── 1. Hostname ────────────────────────────────────────────────────────────────
if [[ "$MODE" == "setup" ]]; then
    step "1 / Hostname"
    # Fetch set_hostname.sh just before it's needed — skip in update mode.
    _RAW_BASE="$(echo "$REPO_URL" | sed 's|github\.com|raw.githubusercontent.com|; s|\.git$||')"
    _SET_HN_TMP="/tmp/schoolair-set_hostname.sh"
    curl -fsSL "${_RAW_BASE}/${REPO_BRANCH}/set_hostname.sh" \
        -o "$_SET_HN_TMP" \
        || die "Cannot fetch set_hostname.sh from GitHub — check connectivity."
    chmod +x "$_SET_HN_TMP"
    ok "set_hostname.sh fetched from GitHub"
    CURRENT_HN=$(hostname)
    if [[ "$CURRENT_HN" == schoolair-[0-9]* ]]; then
        ok "Hostname already set: ${CURRENT_HN}  (not regenerated)"
        _HN_FILE=$(cat /etc/hostname 2>/dev/null | tr -d '[:space:]')
        if [ "$_HN_FILE" != "$CURRENT_HN" ]; then
            bash "$_SET_HN_TMP" "$CURRENT_HN" > /dev/null
            ok "Hostname locations synced to ${CURRENT_HN}"
        fi
    else
        NEW_HN=$(bash "$_SET_HN_TMP")
        ok "Hostname set to ${NEW_HN}"
    fi
else
    skip "1 / Hostname  (update mode — hostname already set)"
fi

# ── 2. System packages ─────────────────────────────────────────────────────────
if [[ "$MODE" == "setup" ]]; then
    step "2 / System packages"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        git python3-pip i2c-tools nginx avahi-daemon gcc make unattended-upgrades
    ok "git python3-pip i2c-tools nginx avahi-daemon gcc make unattended-upgrades"

    # Security-only automatic updates — reboot at 03:00 if needed (outside school hours)
    cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

    cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'EOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
    "origin=Raspbian,codename=${distro_codename},label=Raspbian";
    "origin=Raspberry Pi Foundation,codename=${distro_codename},label=Raspberry Pi Foundation";
};
Unattended-Upgrade::Package-Blacklist {};
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
EOF
    ok "unattended-upgrades: security-only, auto-reboot at 03:00"

    systemctl disable nginx 2>/dev/null || true
    systemctl stop    nginx 2>/dev/null || true
else
    skip "2 / System packages  (update mode — already installed)"
fi

# ── 2b. SD card longevity ─────────────────────────────────────────────────────
# Runs in both setup and update mode — changes are idempotent.
step "2b / SD card longevity"

# journald volatile: journal lives in /run (already tmpfs) — never writes to SD
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/00-schoolair.conf << 'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=32M
EOF
systemctl restart systemd-journald 2>/dev/null || true
ok "journald: volatile storage, 32 MB RAM cap"

# Disable swap — Pi Zero 512 MB is sufficient; SD swap is the #1 card killer
systemctl disable dphys-swapfile 2>/dev/null || true
systemctl stop    dphys-swapfile 2>/dev/null || true
dphys-swapfile swapoff           2>/dev/null || true
ok "swap: disabled"

# ── 3. Clone / update SchoolAir app ───────────────────────────────────────────
step "3 / Clone SchoolAir app  →  ${SCHOOLAIR_DIR}"
rm -rf "$REPO_DIR"
git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR" \
    || die "git clone failed — check connectivity and repo URL."
ok "Cloned from ${REPO_URL}"

mkdir -p "$SCHOOLAIR_DIR"
if [ -f "${SCHOOLAIR_DIR}/.env" ]; then
    cp "${SCHOOLAIR_DIR}/.env" /tmp/schoolair-env.bak
    cp -r "${REPO_DIR}/." "${SCHOOLAIR_DIR}/"
    mv /tmp/schoolair-env.bak "${SCHOOLAIR_DIR}/.env"
    ok "App deployed  (existing .env preserved)"
else
    cp -r "${REPO_DIR}/." "${SCHOOLAIR_DIR}/"
    cp "${SCHOOLAIR_DIR}/.env.example" "${SCHOOLAIR_DIR}/.env"
    ok "App deployed + .env created from .env.example"
fi
chown -R "${ADMIN_USER}:${ADMIN_USER}" "$SCHOOLAIR_DIR"
rm -rf "$REPO_DIR"

# Don't rely on git-tracked file modes surviving — explicitly re-assert the
# executable bit on scripts systemd invokes directly. netwatch.sh was once
# committed non-executable and every OTA silently reproduced the crash-loop
# until this was added.
chmod +x "${WIZARD_DIR}/launcher.sh" "${WIZARD_DIR}/netwatch.sh" 2>/dev/null || true

# ── 4. Python dependencies ─────────────────────────────────────────────────────
step "4 / Python dependencies"
pip3 install --quiet --break-system-packages --root-user-action=ignore \
    "microdot>=2.0.0" simple-websocket httpx python-dotenv questionary netifaces
python3 -c "import microdot" 2>/dev/null \
    || die "microdot failed to import after install."
ok "microdot, simple-websocket, httpx, python-dotenv, questionary, netifaces installed"

# ── 5. Device utility scripts ─────────────────────────────────────────────────
step "5 / Device utility scripts  →  ${ADMIN_HOME}/"
for _f in first_boot.sh set_hostname.sh version_check.py add_wifi.sh; do
    if [ -f "${SCHOOLAIR_DIR}/${_f}" ]; then
        cp "${SCHOOLAIR_DIR}/${_f}" "${ADMIN_HOME}/${_f}"
        chmod +x "${ADMIN_HOME}/${_f}"
        chown "${ADMIN_USER}:${ADMIN_USER}" "${ADMIN_HOME}/${_f}"
        ok "${_f}  →  ${ADMIN_HOME}/"
    else
        warn "${_f} not found in app dir — skipped"
    fi
done
ln -sf "${ADMIN_HOME}/version_check.py" /usr/local/bin/schoolair
ok "schoolair command  →  /usr/local/bin/schoolair"

if [ -f "${SCHOOLAIR_DIR}/schoolair-update" ]; then
    cp "${SCHOOLAIR_DIR}/schoolair-update" /usr/local/bin/schoolair-update
    chmod 755 /usr/local/bin/schoolair-update
    chown root:root /usr/local/bin/schoolair-update
    ok "schoolair-update  →  /usr/local/bin/  (OTA entry point, root-owned)"
else
    warn "schoolair-update not found in app dir — OTA updates unavailable"
fi

# ── 6. Registration wizard TLS certificate ────────────────────────────────────
step "6 / Registration wizard TLS certificate"
if [ ! -f "${WIZARD_DIR}/cert.pem" ] || [ ! -f "${WIZARD_DIR}/key.pem" ]; then
    openssl req -x509 -newkey rsa:2048 \
        -keyout "${WIZARD_DIR}/key.pem" \
        -out    "${WIZARD_DIR}/cert.pem" \
        -days 3650 -nodes \
        -subj "/CN=schoolair-setup" \
        2>/dev/null
    chmod 640 "${WIZARD_DIR}/key.pem"
    chown "${ADMIN_USER}:${ADMIN_USER}" "${WIZARD_DIR}/cert.pem" "${WIZARD_DIR}/key.pem"
    ok "TLS certificate generated (self-signed, 10 yr)"
else
    skip "TLS certificate already present — not regenerated"
fi

# ── 7. Build sen6x binaries ────────────────────────────────────────────────────
step "7 / Build sen6x binaries"
MAKEFILE="${SCHOOLAIR_DIR}/i2c/sen6x/Makefile.daemon"
if [ ! -f "$MAKEFILE" ]; then
    skip "i2c/sen6x/Makefile.daemon not found in app"
else
    systemctl stop sen6x 2>/dev/null || true
    if make -C "${SCHOOLAIR_DIR}/i2c/sen6x" -f Makefile.daemon; then
        mkdir -p "${I2C_DIR}/sen6x"
        cp "${SCHOOLAIR_DIR}/i2c/sen6x/sen6x_d"    "${I2C_DIR}/sen6x/sen6x_d"
        cp "${SCHOOLAIR_DIR}/i2c/sen6x/sen6x_read"  "${I2C_DIR}/sen6x/sen6x_read"
        chmod +x "${I2C_DIR}/sen6x/sen6x_d" "${I2C_DIR}/sen6x/sen6x_read"
        chown -R "${ADMIN_USER}:${ADMIN_USER}" "$I2C_DIR"
        ok "sen6x binaries compiled  →  ${I2C_DIR}/sen6x/"
    else
        warn "sen6x make failed — check gcc output above (non-fatal)"
    fi
fi

if [[ "$MODE" == "setup" ]]; then

# ── 8. I2C + baudrate ────────────────────────────────────────────────────────
step "8 / I2C enable + baudrate"
raspi-config nonint do_i2c 0
ok "I2C enabled (takes effect after reboot)"

if   [ -f /boot/firmware/config.txt ]; then CFG=/boot/firmware/config.txt
elif [ -f /boot/config.txt ];           then CFG=/boot/config.txt
else die "Cannot find config.txt — is this a Raspberry Pi?"; fi
ok "config.txt → ${CFG}"

sed -i '/dtparam=i2c_arm_baudrate/d' "$CFG"
echo "dtparam=i2c_arm_baudrate=100000" >> "$CFG"
ok "I2C baudrate → 100 kHz"

# ── 9. NM hotspot ────────────────────────────────────────────────────────────
step "9 / NetworkManager hotspot  (${AP_SSID})"
if nmcli con show "$AP_CONN" &>/dev/null; then
    nmcli con delete "$AP_CONN" >/dev/null
fi
nmcli con add           \
    type wifi           \
    ifname "$AP_IFACE"  \
    con-name "$AP_CONN" \
    wifi.mode ap        \
    ssid "$AP_SSID"     \
    ipv4.method shared  \
    ipv4.addresses "${AP_IP}/24" \
    connection.autoconnect no   \
    >/dev/null
ok "Open hotspot on ${AP_IP}  (autoconnect disabled — launcher controls it)"

# ── 10. Captive-portal DNS ────────────────────────────────────────────────────
step "10 / Captive-portal DNS hijacking"
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/schoolair-captive.conf << EOF
address=/#/${AP_IP}
address=/schoolair-register.local/${AP_IP}
address=/schoolair.local/${AP_IP}
address=/regiwiz.local/${AP_IP}
address=/schoolair/${AP_IP}
address=/regiwiz/${AP_IP}
EOF
systemctl reload NetworkManager 2>/dev/null || systemctl restart NetworkManager
ok "Captive-portal DNS config written"

# ── 11. Avahi ─────────────────────────────────────────────────────────────────
step "11 / Avahi  →  schoolair.local + regiwiz.local"
mkdir -p /etc/avahi/services

# Default device service — uses the device's own hostname (schoolair-XXXX.local)
cat > /etc/avahi/services/schoolair.service << 'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>SchoolAir Device</name>
  <service><type>_http._tcp</type><port>80</port></service>
</service-group>
EOF

# Friendly aliases — schoolair.local → dashboard, regiwiz.local → wizard.
# Avahi publishes an A record for each host-name so browsers can reach the
# device without knowing its unique hostname.  On a multi-device network,
# Avahi resolves conflicts by appending -2, -3, etc.
cat > /etc/avahi/services/schoolair-aliases.service << 'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>SchoolAir (schoolair.local)</name>
  <host-name>schoolair.local</host-name>
  <service><type>_http._tcp</type><port>80</port></service>
</service-group>
EOF

cat > /etc/avahi/services/regiwiz-alias.service << 'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>SchoolAir Wizard (regiwiz.local)</name>
  <host-name>regiwiz.local</host-name>
  <service><type>_http._tcp</type><port>80</port></service>
</service-group>
EOF

systemctl enable --quiet avahi-daemon
systemctl restart avahi-daemon
ok "Avahi configured (schoolair.local + regiwiz.local)"

# ── 12. dhcpcd (Bullseye only) ────────────────────────────────────────────────
if [ -f /etc/dhcpcd.conf ]; then
    step "12 / dhcpcd conflict prevention  (Bullseye)"
    if grep -q "denyinterfaces ${AP_IFACE}" /etc/dhcpcd.conf; then
        ok "Already configured"
    else
        printf '\n# SchoolAir — NetworkManager manages %s\ndenyinterfaces %s\n' \
            "$AP_IFACE" "$AP_IFACE" >> /etc/dhcpcd.conf
        ok "Added denyinterfaces ${AP_IFACE}"
    fi
fi

else
    skip "8–12 / I2C + networking config  (update mode — unchanged since setup)"
fi

# ── 13. nginx ─────────────────────────────────────────────────────────────────
step "13 / nginx  (configured, disabled until registration)"
# nginx proxies port 80 (HTTP) and port 443 (HTTPS, self-signed cert) to
# telemetry :8080. Stays disabled here — wizard.py enables it on registration.
# Port 443 uses the same self-signed cert generated for the wizard in step 6.
# Browsers that auto-upgrade to https:// get a clickable cert warning rather
# than a hard SSL_ERROR_RX_RECORD_TOO_LONG failure.
CERT_FILE="${SCHOOLAIR_DIR}/registration_wizard/cert.pem"
KEY_FILE="${SCHOOLAIR_DIR}/registration_wizard/key.pem"
cat > /etc/nginx/sites-available/default << NGINXEOF
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:${TELEMETRY_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 300s;
    }
}
server {
    listen 443 ssl;
    server_name _;
    ssl_certificate     ${CERT_FILE};
    ssl_certificate_key ${KEY_FILE};
    location / {
        proxy_pass http://127.0.0.1:${TELEMETRY_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 300s;
    }
}
NGINXEOF
ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
systemctl disable nginx 2>/dev/null || true
systemctl stop    nginx 2>/dev/null || true
ok "nginx config written  (proxies to :${TELEMETRY_PORT}, service disabled)"

# ── 14. Sudoers rule ──────────────────────────────────────────────────────────
step "14 / Sudoers rule for telemetry → wizard / OTA update"
SUDOERS_FILE="/etc/sudoers.d/schoolair-wizard"
cat > "$SUDOERS_FILE" << EOF
# Allow the telemetry server (runs as ${ADMIN_USER}) to start the wizard service
${ADMIN_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start schoolair-wizard
# Allow the telemetry server to trigger an OTA update (fixed, root-owned script)
${ADMIN_USER} ALL=(ALL) NOPASSWD: /usr/local/bin/schoolair-update
EOF
chmod 440 "$SUDOERS_FILE"
ok "sudoers: ${ADMIN_USER} may start schoolair-wizard / run schoolair-update without password"

# ── 15. systemd services ──────────────────────────────────────────────────────
step "15 / systemd services"
DEPLOY_DIR="${SCHOOLAIR_DIR}/deploy"

for svc in sen6x.service schoolair.service schoolair-wizard.service schoolair-launcher.service; do
    if [ -f "${DEPLOY_DIR}/${svc}" ]; then
        cp "${DEPLOY_DIR}/${svc}" /etc/systemd/system/
        ok "${svc} installed"
    else
        warn "${svc} not found in deploy/ — skipped"
    fi
done

if [ -f "${DEPLOY_DIR}/schoolair-first-boot.service" ]; then
    cp "${DEPLOY_DIR}/schoolair-first-boot.service" /etc/systemd/system/
    ok "schoolair-first-boot.service installed"
fi

systemctl daemon-reload
systemctl enable schoolair-launcher.service
systemctl enable schoolair.service
systemctl enable sen6x.service
systemctl enable schoolair-first-boot.service 2>/dev/null || true
ok "Services enabled"

if [[ "$MODE" == "--update" ]]; then
    step "15b / Migrate .env + restart updated services"
    # Rewrite NEW_SERVER_URL if it still points to the old bare-IP endpoint.
    # The .env is preserved across OTA runs, so this is the only way to move
    # already-registered devices to the new domain without touching them manually.
    _ENV="${SCHOOLAIR_DIR}/.env"
    _OLD_URL="http://54.252.165.86:3000"
    _NEW_URL="https://dashboard.schoolair.org"
    if grep -qF "NEW_SERVER_URL=${_OLD_URL}" "$_ENV" 2>/dev/null; then
        sed -i "s|NEW_SERVER_URL=${_OLD_URL}|NEW_SERVER_URL=${_NEW_URL}|" "$_ENV"
        ok "NEW_SERVER_URL migrated → ${_NEW_URL}"
    else
        ok "NEW_SERVER_URL already up to date (no migration needed)"
    fi

    if systemctl is-active --quiet nginx; then systemctl restart nginx; fi
    systemctl restart sen6x.service     || warn "sen6x.service restart failed"
    systemctl restart schoolair.service || warn "schoolair.service restart failed"
    ok "Services restarted with updated code"
fi

# ── 16. Verification ───────────────────────────────────────────────────────────
step "16 / Verification"
ERRORS=0
chk() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then ok "$label"
    else warn "$label"; ERRORS=$((ERRORS+1)); fi
}

chk "hostname is schoolair-*"              bash -c '[[ "$(hostname)" == schoolair-* ]]'
chk "unattended-upgrades configured"      test -f /etc/apt/apt.conf.d/50unattended-upgrades
chk "journald volatile"                   grep -q "Storage=volatile" /etc/systemd/journald.conf.d/00-schoolair.conf
chk "swap disabled"                       bash -c "! systemctl is-enabled dphys-swapfile 2>/dev/null"
chk "microdot importable"                  python3 -c "import microdot"
chk "httpx importable"                     python3 -c "import httpx"
chk "launcher.sh executable"              test -x "${WIZARD_DIR}/launcher.sh"
chk "netwatch.sh executable"              test -x "${WIZARD_DIR}/netwatch.sh"
chk "main.py present"                      test -f "${SCHOOLAIR_DIR}/main.py"
chk "first_boot.sh executable"            test -x "${ADMIN_HOME}/first_boot.sh"
chk "schoolair command available"         test -L /usr/local/bin/schoolair
chk "schoolair-update installed"          test -x /usr/local/bin/schoolair-update
chk "NM hotspot '${AP_CONN}'"             nmcli con show "$AP_CONN"
chk "Captive-portal DNS config"           test -f /etc/NetworkManager/dnsmasq-shared.d/schoolair-captive.conf
chk "Avahi service file"                  test -f /etc/avahi/services/schoolair.service
chk "schoolair-launcher enabled"          systemctl is-enabled schoolair-launcher.service
chk "schoolair.service enabled"           systemctl is-enabled schoolair.service
chk "sen6x.service enabled"              systemctl is-enabled sen6x.service
chk "schoolair-first-boot enabled"        systemctl is-enabled schoolair-first-boot.service
chk "nginx proxies to ${TELEMETRY_PORT}"  grep -q "${TELEMETRY_PORT}" /etc/nginx/sites-available/default
if [[ "$MODE" == "setup" ]]; then
    chk "nginx disabled (correct pre-reg)"   bash -c "! systemctl is-enabled nginx >/dev/null 2>&1"
fi
chk "sudoers rule present"               test -f /etc/sudoers.d/schoolair-wizard
chk "wizard config uses new server URL"  grep -q "dashboard.schoolair.org" "${SCHOOLAIR_DIR}/registration_wizard/config.py"

# ── Summary ────────────────────────────────────────────────────────────────────
_LABEL="Setup"; [[ "$MODE" == "--update" ]] && _LABEL="Update"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ "$ERRORS" -eq 0 ]; then
    echo -e "  ${GREEN}${BOLD}${_LABEL} complete — all checks passed.${NC}"
else
    echo -e "  ${YELLOW}${BOLD}${_LABEL} complete with ${ERRORS} warning(s) — see above.${NC}"
fi
echo
echo "  This device hostname:  $(hostname)"
echo

if [[ "$MODE" == "setup" ]]; then
    echo "  After rebooting:"
    echo "  1. Join Wi-Fi:  SchoolAir_Setup  (open, no password)"
    echo "  2. Open:        http://${AP_IP}"
    echo "  3. Complete the registration form."
    echo "  4. On success the hotspot closes; nginx activates on port 80"
    echo "     and proxies to the telemetry server on :${TELEMETRY_PORT}."
    echo
fi

echo "  Logs:"
echo "    journalctl -u schoolair-launcher -u schoolair-wizard -u schoolair -f"
echo

if [[ "$MODE" == "setup" ]]; then
    echo "  Developer notes (re-run only):"
    echo -e "  ${YELLOW}➜${NC}  sen6x binaries were replaced."
    echo "     Re-run initialisation without rebooting:  sudo systemctl restart sen6x"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
