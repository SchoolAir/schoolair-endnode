#!/usr/bin/env bash
# deploy/canary_ota_test.sh — validate the OTA update path on real device(s)
# BEFORE raising SCHOOLAIR_MIN_VERSION on the production server.
#
# SCHOOLAIR_MIN_VERSION is a single global setting: bumping it signals every
# device in the fleet to self-update, not just the one you meant to test.
# This script runs the exact command the fleet runs — `sudo schoolair-update`
# — directly against one or more canary devices you already have access to,
# then verifies they come back up healthy. It exercises the real update
# script (git clone of `main`, dependency install, sen6x binary rebuild,
# systemd unit reinstall, service restart) without touching the shared
# min_version env var, so a broken release never reaches devices you don't
# have direct access to.
#
# This doesn't require the canary to be behind any particular version —
# schoolair-update always pulls whatever's on `main` right now, so running
# it validates "does the update script work today" regardless of the
# canary's starting point. Point it at whichever device(s) you can reach.
#
# Usage:
#   ./deploy/canary_ota_test.sh <canary-ip> [<canary-ip> ...]
#   ADMIN_USER=admin ./deploy/canary_ota_test.sh 192.168.1.169
#
# Password is read from $SCHOOLAIR_SSH_PASSWORD if set, otherwise prompted
# for (hidden input, one prompt, reused for every host given).

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <canary-ip> [<canary-ip> ...]" >&2
    exit 2
fi

ADMIN_USER="${ADMIN_USER:-admin}"

if [ -z "${SCHOOLAIR_SSH_PASSWORD:-}" ]; then
    read -rsp "SSH password for ${ADMIN_USER}@<canary>: " SCHOOLAIR_SSH_PASSWORD
    echo
fi

ASKPASS_FILE="$(mktemp)"
cleanup() { rm -f "$ASKPASS_FILE"; }
trap cleanup EXIT

cat > "$ASKPASS_FILE" <<EOF
#!/bin/sh
echo '${SCHOOLAIR_SSH_PASSWORD}'
EOF
chmod 700 "$ASKPASS_FILE"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o PreferredAuthentications=password
          -o PubkeyAuthentication=no -o ConnectTimeout=10)

ssh_get() {
    # $1 = host, $2 = remote command (no sudo needed)
    SSH_ASKPASS="$ASKPASS_FILE" SSH_ASKPASS_REQUIRE=force \
        ssh "${SSH_OPTS[@]}" "${ADMIN_USER}@$1" "$2" < /dev/null
}

read_version() {
    # $1 = host — prints the device's currently-running app version, or "?"
    ssh_get "$1" 'grep -m1 "^VERSION" /home/admin/schoolair/jobs/ingest.py 2>/dev/null' \
        | sed -nE 's/.*"([^"]+)".*/\1/p'
}

test_one_canary() {
    local host="$1"
    echo
    echo "━━━ Canary OTA test: ${host} ━━━"

    if ! ssh_get "$host" 'true' 2>/dev/null; then
        echo "✗ Cannot SSH to ${host} — skipping"
        return 1
    fi

    local before
    before="$(read_version "$host")"
    echo "Before: v${before:-unknown}"

    echo "Running 'sudo schoolair-update' on ${host}…"
    local update_log
    update_log="$(mktemp)"
    local rc=0
    printf '%s\n' "$SCHOOLAIR_SSH_PASSWORD" \
        | SSH_ASKPASS="$ASKPASS_FILE" SSH_ASKPASS_REQUIRE=force \
          ssh "${SSH_OPTS[@]}" "${ADMIN_USER}@${host}" \
          'sudo -S -p "" /usr/local/bin/schoolair-update' \
          > "$update_log" 2>&1 || rc=$?

    tail -n 40 "$update_log"
    rm -f "$update_log"

    if [ "$rc" -ne 0 ]; then
        echo "✗ schoolair-update exited ${rc}"
        return 1
    fi

    echo "Waiting for services to settle…"
    sleep 20

    local fail=0
    local svc
    for svc in sen6x.service schoolair.service schoolair-netwatch.service; do
        local state
        state="$(ssh_get "$host" "systemctl is-active ${svc}" 2>/dev/null || echo unreachable)"
        if [ "$state" = "active" ]; then
            echo "✓ ${svc}: active"
        else
            echo "✗ ${svc}: ${state}"
            fail=1
        fi
    done

    # A service that immediately crash-loops still reports "active" moments
    # after a restart — give it another beat and recheck restart counts.
    sleep 15
    for svc in sen6x.service schoolair.service schoolair-netwatch.service; do
        local restarts
        restarts="$(ssh_get "$host" "systemctl show ${svc} -p NRestarts --value" 2>/dev/null || echo '?')"
        echo "  ${svc} restarts since boot: ${restarts}"
    done

    local after
    after="$(read_version "$host")"
    echo "After: v${after:-unknown}"

    if [ "$fail" -ne 0 ]; then
        echo "━━━ RESULT (${host}): FAIL ━━━"
        return 1
    fi
    echo "━━━ RESULT (${host}): PASS — v${before:-?} → v${after:-?} ━━━"
    return 0
}

OVERALL=0
for host in "$@"; do
    test_one_canary "$host" || OVERALL=1
done

echo
if [ "$OVERALL" -eq 0 ]; then
    echo "All canaries passed — safe to raise SCHOOLAIR_MIN_VERSION."
else
    echo "One or more canaries FAILED — do not raise SCHOOLAIR_MIN_VERSION until this is fixed."
fi
exit "$OVERALL"
