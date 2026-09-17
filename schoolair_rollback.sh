#!/usr/bin/env bash
# schoolair_rollback.sh — offline-only restore for a broken OTA update.
#
# Deployed to ~/schoolair_rollback.sh — deliberately OUTSIDE ~/schoolair/,
# since it has to survive even when the app directory itself is what's
# broken. Uses only local file operations (the same backup store
# schoolair_setup.sh's install_with_backup()/install_dir_with_backup()
# write to): no network, no GitHub, nothing that a broken update could
# have taken down along with everything else.
#
# Two ways it runs:
#
#   sudo schoolair_rollback.sh --restore-now
#       Immediate, unconditional restore. Called by schoolair_setup.sh's
#       own failure trap when an --update run dies partway through.
#
#   schoolair_rollback.sh --check   (via schoolair-update-watchdog.timer,
#                                     runs every 5 min as root)
#       Checks whether a pending update was ever confirmed by a real
#       successful upload (see jobs/ingest.py's _confirm_update_if_pending).
#       If its deadline has passed with no confirmation, restores
#       automatically — no human needed. This is the case that matters
#       most: an update that "succeeds" (exits 0, services start) but
#       silently breaks the device's own ability to signal it's broken,
#       which would otherwise mean no future fix could ever reach it
#       either, since OTA triggering itself is piggybacked on a
#       successful upload.

set -uo pipefail   # not -e: a failed restore of one path shouldn't abort the rest

# Overridable via env for tests/test_ota_rollback.py — production always
# uses the defaults, nothing here changes real behavior on a device.
BACKUP_ROOT="${SCHOOLAIR_BACKUP_ROOT:-/var/backups/schoolair-update}"
BACKUP_MANIFEST="${SCHOOLAIR_BACKUP_MANIFEST:-${BACKUP_ROOT}.manifest}"
PENDING_FILE="${SCHOOLAIR_PENDING_FILE:-/var/lib/schoolair/update-pending.json}"
LOG_TAG="[schoolair-rollback]"

log() { echo "${LOG_TAG} $*"; }

restore_from_backup() {
    if [ ! -s "$BACKUP_MANIFEST" ]; then
        log "No backup manifest at ${BACKUP_MANIFEST} — nothing to restore."
        return 0
    fi
    local real_path
    while IFS= read -r real_path; do
        [ -n "$real_path" ] || continue
        if [ -e "${BACKUP_ROOT}${real_path}" ]; then
            log "Restoring ${real_path}"
            rm -rf "$real_path"
            cp -a "${BACKUP_ROOT}${real_path}" "$real_path"
        else
            log "WARNING: no backup found for ${real_path} — leaving as-is"
        fi
    done < "$BACKUP_MANIFEST"
}

restart_services() {
    systemctl daemon-reload 2>/dev/null || true
    for svc in sen6x.service schoolair.service schoolair-netwatch.service schoolair-led.service; do
        systemctl restart "$svc" 2>/dev/null || log "WARNING: could not restart ${svc}"
    done
}

do_restore_now() {
    log "Restoring from backup (immediate)…"
    restore_from_backup
    restart_services
    rm -f "$PENDING_FILE"
    log "Restore complete."
}

do_check() {
    if [ ! -f "$PENDING_FILE" ]; then
        return 0   # no update pending confirmation — nothing to do
    fi

    # Active health check, every tick — not just at deadline. Catches a
    # service that crashes later (after the initial post-restart check in
    # schoolair_setup.sh passed), instead of waiting up to the full
    # deadline for a problem that's already visible right now.
    #
    # is-active alone isn't enough — proven live: Type=simple marks a
    # service "active" the instant its process is spawned, even if it
    # crashes moments later, so a genuinely crash-looping service can
    # still read as "active" at any single sampled instant. Compare each
    # service's current restart count against the baseline schoolair_setup.sh
    # recorded in the pending file at update time — any increase since then
    # means it crashed at least once after the update, regardless of
    # whether it happens to be up again right this moment.
    local svc unhealthy="" baseline now_restarts
    for svc in sen6x.service schoolair.service schoolair-netwatch.service; do
        systemctl is-active --quiet "$svc" || unhealthy="${unhealthy} ${svc}(inactive)"
        baseline="$(python3 -c "import json; print(json.load(open('${PENDING_FILE}')).get('restart_baseline', {}).get('${svc}', 0))" 2>/dev/null || echo 0)"
        now_restarts="$(systemctl show "$svc" -p NRestarts --value 2>/dev/null || echo 0)"
        if [ "${now_restarts:-0}" -gt "${baseline:-0}" ] 2>/dev/null; then
            unhealthy="${unhealthy} ${svc}(crash-looping)"
        fi
    done
    if [ -n "$unhealthy" ]; then
        log "Service(s) unhealthy:${unhealthy} — rolling back now, not waiting for the deadline."
        do_restore_now
        return 0
    fi

    local deadline
    deadline="$(python3 -c "import json; print(json.load(open('${PENDING_FILE}')).get('deadline', 0))" 2>/dev/null || echo 0)"
    local now
    now="$(date +%s)"
    if [ "${deadline%.*}" -gt "$now" ] 2>/dev/null; then
        return 0   # services look healthy and still within the confirmation window
    fi
    log "Update was never confirmed by a successful upload within its deadline — rolling back."
    do_restore_now
}

case "${1:-}" in
    --restore-now) do_restore_now ;;
    --check)       do_check ;;
    *) echo "Usage: $0 --restore-now | --check" >&2; exit 2 ;;
esac
