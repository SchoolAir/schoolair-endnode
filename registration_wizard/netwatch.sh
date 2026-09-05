#!/usr/bin/env bash
# schoolair-netwatch.sh — persistent network watchdog
#
# Runs as a systemd service after schoolair-launcher.service.
#
# State machine:
#   online  — non-AP WiFi uplink is present; monitors for loss
#   grace   — uplink gone but within the grace window; recovers to online if
#             the blip is transient
#   ap      — AP hotspot is up + wizard is running; periodically probes for a
#             saved network by briefly closing the AP; restores AP on failure
#
# Environment overrides (for testing):
#   NETWATCH_POLL       seconds between connectivity checks  (default 30)
#   NETWATCH_GRACE      seconds of loss before entering ap   (default 120)
#   NETWATCH_RECONNECT  seconds between reconnect probes     (default 300)

set -euo pipefail

AP_CONN="SchoolAir_AP"
WIZARD_SERVICE="schoolair-wizard"
TELEMETRY_SERVICE="schoolair"

POLL_INTERVAL="${NETWATCH_POLL:-30}"
GRACE_SECS="${NETWATCH_GRACE:-120}"
RECONNECT_INTERVAL="${NETWATCH_RECONNECT:-300}"

log() { echo "[schoolair-netwatch] $*"; }

# ── helpers ────────────────────────────────────────────────────────────────────

has_uplink() {
    nmcli -t -f NAME,TYPE,STATE con show --active 2>/dev/null \
        | awk -F: '$2=="802-11-wireless" && $3=="activated" && $1!="'"$AP_CONN"'" \
                  {found=1} END {exit !found}'
}

ap_is_up() {
    nmcli -t -f NAME,STATE con show --active 2>/dev/null \
        | grep -q "^${AP_CONN}:activated"
}

saved_sta_profiles() {
    nmcli -t -f NAME,TYPE con show 2>/dev/null \
        | awk -F: '$2=="802-11-wireless" && $1!="'"$AP_CONN"'" {print $1}'
}

ap_has_clients() {
    iw dev wlan0 station dump 2>/dev/null | grep -q '^Station'
}

# ── AP control ─────────────────────────────────────────────────────────────────

_install_captive_portal() {
    iptables -t nat -A PREROUTING -i wlan0 -p tcp --dport 80  -j REDIRECT --to-port 80  2>/dev/null || true
    iptables -t nat -A PREROUTING -i wlan0 -p tcp --dport 443 -j REDIRECT --to-port 443 2>/dev/null || true
}

_remove_captive_portal() {
    iptables -t nat -D PREROUTING -i wlan0 -p tcp --dport 80  -j REDIRECT --to-port 80  2>/dev/null || true
    iptables -t nat -D PREROUTING -i wlan0 -p tcp --dport 443 -j REDIRECT --to-port 443 2>/dev/null || true
}

bring_up_ap() {
    log "Bringing up AP '${AP_CONN}'"
    if nmcli con up "$AP_CONN" 2>/dev/null; then
        log "AP is up"
    else
        log "WARNING: nmcli could not bring up '${AP_CONN}' — trying hostapd fallback"
        systemctl start hostapd 2>/dev/null || log "WARNING: hostapd also unavailable"
    fi
    sleep 3
    _install_captive_portal
    log "Captive-portal iptables rules active"
    systemctl start "$WIZARD_SERVICE" 2>/dev/null || true
}

take_down_ap() {
    log "Closing AP"
    _remove_captive_portal
    nmcli con down "$AP_CONN" 2>/dev/null || true
    systemctl stop "$WIZARD_SERVICE" 2>/dev/null || true
}

# ── Reconnect probe ────────────────────────────────────────────────────────────
# Briefly closes the AP to attempt reconnection to a saved client network.
# Returns 0 if reconnected (AP left down); 1 if all failed (AP restored).
try_reconnect() {
    local profiles
    profiles=$(saved_sta_profiles)
    if [ -z "$profiles" ]; then
        log "Reconnect probe skipped — no saved client profiles"
        return 1
    fi

    if ap_has_clients; then
        log "Reconnect probe deferred — client currently connected to AP"
        return 1
    fi

    log "Reconnect probe: closing AP to scan for saved networks"
    systemctl stop "$WIZARD_SERVICE" 2>/dev/null || true
    nmcli con down "$AP_CONN" 2>/dev/null || true
    _remove_captive_portal

    # Give the interface time to switch from AP to station mode
    sleep 5
    nmcli dev wifi rescan ifname wlan0 2>/dev/null || true
    sleep 3

    local connected=false
    while IFS= read -r profile; do
        log "Trying profile '${profile}'…"
        if nmcli con up "$profile" 2>/dev/null; then
            log "Connected to '${profile}'"
            connected=true
            break
        fi
    done <<< "$profiles"

    if $connected; then
        return 0
    fi

    log "Reconnect probe failed — restoring AP"
    nmcli con up "$AP_CONN" 2>/dev/null || true
    sleep 3
    _install_captive_portal
    systemctl start "$WIZARD_SERVICE" 2>/dev/null || true
    return 1
}

# ── Main loop ──────────────────────────────────────────────────────────────────

log "Started (poll=${POLL_INTERVAL}s  grace=${GRACE_SECS}s  reconnect=${RECONNECT_INTERVAL}s)"

# Initialise state from current conditions so we don't conflict with launcher
state="online"
grace_start=0
last_reconnect=0

if ap_is_up; then
    state="ap"
    last_reconnect=$(date +%s)
    log "Initial state: ap (AP already up from launcher)"
elif has_uplink; then
    log "Initial state: online"
else
    state="grace"
    grace_start=$(date +%s)
    log "Initial state: grace (no uplink at start)"
fi

while true; do
    sleep "$POLL_INTERVAL"

    case "$state" in

        online)
            if ! has_uplink; then
                log "Uplink lost — starting ${GRACE_SECS}s grace period"
                state="grace"
                grace_start=$(date +%s)
            fi
            ;;

        grace)
            if has_uplink; then
                log "Uplink restored during grace period"
                state="online"
            elif [ $(( $(date +%s) - grace_start )) -ge "$GRACE_SECS" ]; then
                bring_up_ap
                state="ap"
                last_reconnect=$(date +%s)
            fi
            ;;

        ap)
            if has_uplink; then
                # Uplink appeared — either the user configured WiFi via wizard,
                # or a previously-known network came back on its own.
                log "Uplink detected while in AP mode — closing AP"
                take_down_ap
                systemctl restart "$TELEMETRY_SERVICE" 2>/dev/null || true
                state="online"
            elif [ $(( $(date +%s) - last_reconnect )) -ge "$RECONNECT_INTERVAL" ]; then
                if try_reconnect; then
                    # AP already cleaned up inside try_reconnect
                    systemctl restart "$TELEMETRY_SERVICE" 2>/dev/null || true
                    state="online"
                else
                    last_reconnect=$(date +%s)
                fi
            fi
            ;;

    esac
done
