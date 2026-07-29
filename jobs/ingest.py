"""jobs/ingest.py

Two-speed data pipeline:

  Read loop  — collects a sensor reading every READ_ACTIVE_SECONDS (active
               window) or READ_IDLE_SECONDS (outside window) and holds it in
               an in-memory buffer.  Nothing is written to disk here.

  Drain loop — event-driven: the read loop triggers a drain after each standard
               read once DRAIN_ACTIVE_SECONDS / DRAIN_IDLE_SECONDS have elapsed
               since the last drain.  The drain loop itself is a pure event
               consumer with a long fallback timeout as a safety net.

SQLite is written only when the buffer hits BUFFER_CAPACITY *and* the server is
unreachable — so during normal operation the SD card is never touched for
telemetry data.  If the device later reconnects, the next drain sends both the
SQLite backlog and the current in-memory buffer together.
"""

import asyncio
import json
import os
import random
from datetime import datetime, timezone, timedelta, time
from pathlib import Path
import httpx
from dotenv import load_dotenv
from services.sensor import read_sensor, extract_metric, probe_aux_sensors, read_aux_sensor
import db.queue as queue
import jobs.aggregate as aggregate
import state

load_dotenv()

VERSION            = "2.1.0"

# Primary (authoritative) server — AWS.  Buffer/SQLite retention is decided by
# whether this server accepts or rejects the drain.
_PRIMARY_SERVER_URL   = os.getenv("NEW_SERVER_URL", "").rstrip("/")
_PRIMARY_INGEST_URL   = os.getenv("NEW_INGEST_URL", f"{_PRIMARY_SERVER_URL}/aqc/v1/ingest") if _PRIMARY_SERVER_URL else ""

# Secondary (legacy) server — best-effort mirror.  Failures are logged but
# never affect buffering or retries.
_SECONDARY_SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
_SECONDARY_INGEST_URL = os.getenv("INGEST_URL", f"{_SECONDARY_SERVER_URL}/aqc/v1/ingest")
_SECONDARY_AUTH_TOKEN = os.getenv("AUTH_TOKEN", "").strip()
ALERT_NEAR_PCT     = float(os.getenv("ALERT_NEAR_PCT", 10))  # within N% of threshold = "near"
ALERT_COOLDOWN_HRS = float(os.getenv("ALERT_COOLDOWN_HOURS", 1))
BUFFER_CAPACITY    = int(os.getenv("BUFFER_CAPACITY", 500))

# Read and drain intervals are hardcoded, not user-configurable.
# Overridable via env var for development and testing only.
READ_ACTIVE_SECONDS  = int(os.getenv("READ_INTERVAL_ACTIVE",  300))   # 5 min
READ_IDLE_SECONDS    = int(os.getenv("READ_INTERVAL_IDLE",    900))   # 15 min
DRAIN_ACTIVE_SECONDS = int(os.getenv("DRAIN_INTERVAL_ACTIVE", 1800))  # 30 min
DRAIN_IDLE_SECONDS   = int(os.getenv("DRAIN_INTERVAL_IDLE",   7200))  # 2 h
DRAIN_JITTER_MAX     = int(os.getenv("DRAIN_JITTER_MAX",       120))  # 2 min spread

CRITERIA_PATH = Path("config/criteria.json")
SETTINGS_PATH = Path("config/settings.json")

DEFAULT_SETTINGS = {
    "active_window": {"start": "07:00", "end": "16:00"},
}

MAX_ACTIVE_HOURS = 9
BATCH_SIZE       = 500

# In-memory buffer: readings not yet sent to the server.
# Each entry: {"data": dict, "recorded_at": str}
_buffer: list[dict] = []
_update_in_progress = False

alert_cooldown:       dict[str, datetime] = {}
_verifying:           set[str]            = set()   # metrics currently in a verify routine
_alert_buffer:        list[dict]          = []      # alerts pending send (in-memory first)
ALERT_BUFFER_CAPACITY = int(os.getenv("ALERT_BUFFER_CAPACITY", 50))

# Set by trigger_drain() (SIGUSR2 handler or internal callers) to wake the
# drain loop.  Initialised to None until the event loop is running.
_drain_trigger:   asyncio.Event | None = None
_last_drained_at: datetime | None     = None


def trigger_drain() -> None:
    """Request an immediate out-of-schedule drain.

    Safe to call from asyncio signal handlers or from any coroutine.
    No-op if the drain loop hasn't started yet.
    """
    if _drain_trigger is not None:
        _drain_trigger.set()


# --------------------------- Settings ---------------------------


def _parse_hhmm(hhmm: str) -> time:
    h, m = map(int, hhmm.split(":"))
    return time(h, m)


def _window_hours(window: dict) -> float:
    start = _parse_hhmm(window["start"])
    end   = _parse_hhmm(window["end"])
    s = start.hour + start.minute / 60
    e = end.hour   + end.minute   / 60
    return (e - s) % 24


def _in_active_window(settings: dict, now: time | None = None) -> bool:
    if now is None:
        now = datetime.now().time()
    w = settings["active_window"]
    start, end = _parse_hhmm(w["start"]), _parse_hhmm(w["end"])
    if start <= end:
        return start <= now < end
    return (now >= start) or (now < end)


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        print("settings.json not found — using defaults")
        return dict(DEFAULT_SETTINGS)
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except json.JSONDecodeError:
        print("settings.json is malformed — using defaults")
        return dict(DEFAULT_SETTINGS)


def _ensure_drain_jitter(settings: dict) -> int:
    """Return the persisted drain jitter offset for this device.

    If `drain_jitter_seconds` is already in settings, that value is used
    unchanged — making it easy to migrate to server-assigned slots later by
    simply writing the value into settings.json.  If absent, a random offset
    in [0, DRAIN_JITTER_MAX] is generated, saved to settings.json, and returned.
    """
    if "drain_jitter_seconds" in settings:
        return int(settings["drain_jitter_seconds"])
    jitter = random.randint(0, DRAIN_JITTER_MAX)
    settings["drain_jitter_seconds"] = jitter
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    print(f"[drain] jitter slot assigned: {jitter}s — saved to {SETTINGS_PATH}")
    return jitter


_VALID_BOUNDARY_MINUTES = {0, 15, 30, 45}


def validate_settings(settings: dict):
    window = settings["active_window"]
    hours = _window_hours(window)
    if hours > MAX_ACTIVE_HOURS:
        raise SystemExit(
            f"Config error: active window is {hours:.1f}h, "
            f"max allowed is {MAX_ACTIVE_HOURS}h. Edit config/settings.json."
        )
    for key in ("start", "end"):
        t = _parse_hhmm(window[key])
        if t.minute not in _VALID_BOUNDARY_MINUTES:
            raise SystemExit(
                f"Config error: active_window.{key} ({window[key]}) must be on a "
                f"15-minute boundary (:00, :15, :30, or :45). Edit config/settings.json."
            )


def current_read_interval(settings: dict, now: time | None = None) -> int:
    return READ_ACTIVE_SECONDS if _in_active_window(settings, now) else READ_IDLE_SECONDS


def current_drain_interval(settings: dict, now: time | None = None) -> int:
    return DRAIN_ACTIVE_SECONDS if _in_active_window(settings, now) else DRAIN_IDLE_SECONDS


def _seconds_to_next_boundary(interval: int, now: datetime | None = None) -> float:
    """Seconds until the next wall-clock multiple of *interval* seconds.

    Uses the Unix epoch as reference — valid because 300 and 900 both divide
    evenly into 86400, so :00/:05/… and :00/:15/… boundaries align to whole
    minutes regardless of date.  Callers always land on the same grid, so
    accumulated per-read latency never carries forward.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    past = now.timestamp() % interval
    return float(interval - past)


# --------------------------- Criteria ---------------------------


def save_criteria(criteria: list[dict]):
    CRITERIA_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRITERIA_PATH.write_text(json.dumps(criteria, indent=4))


def load_criteria() -> list[dict]:
    if not CRITERIA_PATH.exists():
        return []
    try:
        return json.loads(CRITERIA_PATH.read_text())
    except json.JSONDecodeError:
        return []


# --------------------------- Alert verification ---------------------------


def _breached(value: float, threshold: float, condition: str) -> bool:
    if condition == "above":
        return value > threshold
    return value < threshold  # "below"


def _near_or_breached(value: float, threshold: float, condition: str) -> bool:
    """True if value is at or within ALERT_NEAR_PCT% of the threshold.

    'Near' prevents the verify routine from declaring an event safe when the
    reading has only just crept back below the line — e.g. PM2.5 = 24.8 with
    threshold 25 is still worth watching.
    """
    margin = threshold * ALERT_NEAR_PCT / 100
    if condition == "above":
        return value >= threshold - margin
    return value <= threshold + margin  # "below" — near means slightly above the floor


def _set_cooldown(breaching: list[tuple[str, dict]], now_dt: datetime | None = None) -> None:
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    for metric, _ in breaching:
        alert_cooldown[metric] = now_dt


def _buffer_alert(alert: dict):
    """Add alert to the in-memory buffer; flush to SQLite if it hits capacity.

    Mirrors the measurement buffer philosophy: SQLite is only written when both
    the buffer is full AND the server is unreachable, keeping SD card writes rare.
    With a 1h cooldown per metric, 50 slots covers days of outage.
    """
    _alert_buffer.append(alert)
    if len(_alert_buffer) >= ALERT_BUFFER_CAPACITY:
        for a in _alert_buffer:
            queue.enqueue_alert(a, a.get("recorded_at", datetime.now(timezone.utc).isoformat()))
        _alert_buffer.clear()
        print(f"Alert buffer at capacity ({ALERT_BUFFER_CAPACITY}) — flushed to SQLite")


def _log_queued_alert(alert: dict):
    delta       = alert.get("delta_seconds")
    delta_str   = f"{delta}s ago" if delta is not None else "unknown delay"
    persistence = "persistent" if alert.get("persistent") else "fleeting"
    stage_info  = ""
    if "stage2_avg" in alert:
        stage_info = f" | s1={alert['stage1_avg']} s2={alert['stage2_avg']}"
    elif "stage1_avg" in alert:
        stage_info = f" | s1={alert['stage1_avg']}"
    print(
        f"[ALERT] {alert['severity'].upper()} — "
        f"{alert['metric']} = {alert['value']} "
        f"({alert['condition']} {alert['threshold']}) "
        f"triggered {delta_str} | {persistence}{stage_info}"
    )


async def _send_or_queue_alert(
    metric: str,
    criterion: dict,
    entry: dict,
    now_dt: datetime,
    r1: dict,
    r2: dict,
    r3: dict,
    r4: dict | None,
) -> None:
    """Build and immediately send an alert for one metric; queue on failure."""
    threshold = float(criterion["threshold"])
    condition = criterion["condition"]

    mv1 = extract_metric(r1, metric)
    mv2 = extract_metric(r2, metric)
    mv3 = extract_metric(r3, metric)
    mv4 = extract_metric(r4, metric) if r4 is not None else None

    avg1 = round((mv1 + mv2) / 2, 2) if (mv1 is not None and mv2 is not None) else None
    avg2 = round((mv3 + mv4) / 2, 2) if (mv3 is not None and mv4 is not None) else None

    try:
        t = datetime.fromisoformat(entry["recorded_at"])
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        delta = int((now_dt - t).total_seconds())
    except ValueError:
        delta = None

    alert = {
        "metric":        metric,
        "value":         extract_metric(entry["data"], metric),
        "threshold":     threshold,
        "condition":     condition,
        "severity":      criterion["severity"],
        "recorded_at":   entry["recorded_at"],
        "delta_seconds": delta,
        "stage1_avg":    avg1,
        "stage2_avg":    avg2,
        "persistent":    True,
        "verified":      r4 is not None,
    }

    token = os.getenv("NEW_AUTH_TOKEN", "").strip()
    if token:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{_PRIMARY_SERVER_URL}/aqc/v1/alert",
                    headers=_auth_headers(),
                    json=alert,
                )
            print(f"[verify/{metric}] alert sent")
            return
        except (httpx.ConnectError, httpx.HTTPStatusError) as e:
            print(f"[verify/{metric}] alert send failed: {e} — queuing")
    else:
        print(f"[verify/{metric}] no token — queuing alert")

    _buffer_alert(alert)


async def _verify_all(breaching: list[tuple[str, dict]], entry: dict) -> None:
    """Two-stage verification for all metrics that breached at T.

    One sensor read per timing point, shared across all metrics.

    Severity scoring (integer, 0-7 range):
      +1  stage 1 launched (always, once any metric breaches)
      +1  T+10s: any breaching metric still near/above its threshold
      +1  T+30s: any breaching metric still near/above its threshold
      +2  T+1m:  any breaching metric still near/above its threshold
      +2  T+2m:  any breaching metric still near/above its threshold

    Outcomes:
      severity=1 (fluke)    — patch entry, drop all verification reads
      severity 2-3 (momentary) — patch entry, add T+1m to buffer
      severity>=4 (alert)   — patch entry, send per-metric alerts, drop reads
    """
    metrics_str = ", ".join(m for m, _ in breaching)

    def any_high(data: dict) -> bool:
        return any(
            (v := extract_metric(data, m)) is not None
            and _near_or_breached(v, float(c["threshold"]), c["condition"])
            for m, c in breaching
        )

    sev = 1  # baseline: stage 1 launched
    r3: dict | None = None
    r3_at: str = ""

    try:
        # ── Stage 1: T+10s ─────────────────────────────────────────────────────
        await asyncio.sleep(10)
        r1_at = datetime.now(timezone.utc).isoformat()
        try:
            r1 = read_sensor()
        except RuntimeError as e:
            print(f"[verify/{metrics_str}] T+10s read failed: {e} — aborting")
            entry["severity"] = sev
            return
        state.set(r1, r1_at)
        if any_high(r1):
            sev += 1

        # ── Stage 1: T+30s ─────────────────────────────────────────────────────
        await asyncio.sleep(20)
        r2_at = datetime.now(timezone.utc).isoformat()
        try:
            r2 = read_sensor()
        except RuntimeError as e:
            print(f"[verify/{metrics_str}] T+30s read failed: {e} — aborting")
            entry["severity"] = sev
            return
        state.set(r2, r2_at)
        if any_high(r2):
            sev += 1

        if sev == 1:
            entry["severity"] = sev
            print(f"[verify/{metrics_str}] stage 1: both reads low — fluke (severity=1)")
            return

        print(
            f"[verify/{metrics_str}] stage 1: {sev - 1} high read(s) "
            f"— advancing to stage 2 (severity so far={sev})"
        )

        # ── Stage 2: T+1m ──────────────────────────────────────────────────────
        await asyncio.sleep(30)
        r3_at = datetime.now(timezone.utc).isoformat()
        try:
            r3 = read_sensor()
        except RuntimeError as e:
            print(f"[verify/{metrics_str}] T+1m read failed: {e} — inconclusive")
            entry["severity"] = sev
            _set_cooldown(breaching)
            return
        state.set(r3, r3_at)
        if any_high(r3):
            sev += 2

        # ── Stage 2: T+2m ──────────────────────────────────────────────────────
        await asyncio.sleep(60)
        r4_at = datetime.now(timezone.utc).isoformat()
        r4: dict | None = None
        try:
            r4 = read_sensor()
        except RuntimeError as e:
            print(f"[verify/{metrics_str}] T+2m read failed: {e} — partial stage 2")
        if r4 is not None:
            state.set(r4, r4_at)
            if any_high(r4):
                sev += 2

        entry["severity"] = sev
        now_dt = datetime.now(timezone.utc)
        _set_cooldown(breaching, now_dt)

        if sev >= 4:
            print(f"[verify/{metrics_str}] stage 2: persistent breach (severity={sev})")
            for metric, criterion in breaching:
                await _send_or_queue_alert(metric, criterion, entry, now_dt, r1, r2, r3, r4)
            trigger_drain()
        else:
            print(
                f"[verify/{metrics_str}] stage 2: both reads low "
                f"— momentary event (severity={sev})"
            )
            _buffer.append({"data": r3, "recorded_at": r3_at})

    finally:
        for metric, _ in breaching:
            _verifying.discard(metric)


async def _drain_alerts():
    """Flush the in-memory alert buffer (and any SQLite overflow) to the server.

    Called after a successful measurement batch so we know we have connectivity.

    Buffer-first:   alerts live in _alert_buffer in memory.
    SQLite fallback: only written when the buffer hits ALERT_BUFFER_CAPACITY
                     while the server is unreachable — rare with a 1h cooldown.
    """
    # ── Send in-memory buffer ─────────────────────────────────────────────────
    if _alert_buffer:
        sent = 0
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for alert in list(_alert_buffer):
                    _log_queued_alert(alert)
                    try:
                        await client.post(
                            f"{_PRIMARY_SERVER_URL}/aqc/v1/alert",
                            headers=_auth_headers(),
                            json=alert,
                        )
                    except httpx.HTTPStatusError as e:
                        print(f"  Alert rejected ({e.response.status_code}) — will not retry")
                    sent += 1

            _alert_buffer.clear()
            if sent:
                print(f"  Sent {sent} buffered alert(s)")

        except httpx.ConnectError:
            # Keep unsent alerts in buffer; if now full, overflow to SQLite
            unsent = _alert_buffer[sent:]
            _alert_buffer.clear()
            _alert_buffer.extend(unsent)
            if sent:
                print(f"  Sent {sent} alert(s) before connection dropped")
            if len(_alert_buffer) >= ALERT_BUFFER_CAPACITY:
                for a in _alert_buffer:
                    queue.enqueue_alert(a, a.get("recorded_at", datetime.now(timezone.utc).isoformat()))
                _alert_buffer.clear()
                print(f"  Alert buffer full — flushed to SQLite")
            return  # connection is down; no point trying SQLite drain

    # ── Drain SQLite overflow (from previous capacity events) ─────────────────
    sqlite_rows = queue.get_pending_alerts()
    if not sqlite_rows:
        return

    all_ids  = [r["id"] for r in sqlite_rows]
    sent_ids = []
    queue.set_alert_status_many(all_ids, "sending")

    async with httpx.AsyncClient(timeout=10) as client:
        for row in sqlite_rows:
            alert = json.loads(row["data"])
            _log_queued_alert(alert)
            try:
                await client.post(
                    f"{_PRIMARY_SERVER_URL}/aqc/v1/alert",
                    headers=_auth_headers(),
                    json=alert,
                )
                sent_ids.append(row["id"])
            except httpx.ConnectError:
                print(f"  Lost connection — {len(sqlite_rows) - len(sent_ids)} SQLite alert(s) held")
                break
            except httpx.HTTPStatusError as e:
                print(f"  Alert rejected ({e.response.status_code}) — will not retry")
                sent_ids.append(row["id"])

    if sent_ids:
        queue.remove_alerts(sent_ids)
        print(f"  Drained {len(sent_ids)} SQLite alert(s)")

    unsent = [i for i in all_ids if i not in set(sent_ids)]
    if unsent:
        queue.set_alert_status_many(unsent, "pending")


# --------------------------- HTTP helpers ---------------------------


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.getenv('NEW_AUTH_TOKEN', '').strip()}",
        "Content-Type": "application/json",
        "X-Schoolair-Version": VERSION,
    }


async def _post_batch(client: httpx.AsyncClient, measurements: list[dict]) -> dict:
    wifi_state   = _load_wifi_state()
    pending_acks = wifi_state.get("pending_acks", [])
    body: dict   = {"measurements": measurements}
    if pending_acks:
        body["wifi_acks"] = pending_acks

    res = await client.post(
        _PRIMARY_INGEST_URL,
        headers=_auth_headers(),
        json=body,
    )
    if res.status_code == 401:
        print("Batch ingest failed: auth token rejected.")
        raise httpx.HTTPStatusError("Unauthorised", request=res.request, response=res)
    res.raise_for_status()
    result = res.json()

    if pending_acks:
        wifi_state["pending_acks"] = []
        _save_wifi_state(wifi_state)

    return result


async def _mirror_batch(measurements: list[dict]) -> None:
    """Best-effort POST to the legacy secondary server. Never raises."""
    if not _SECONDARY_INGEST_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                _SECONDARY_INGEST_URL,
                headers={"Authorization": f"Bearer {_SECONDARY_AUTH_TOKEN}", "Content-Type": "application/json"},
                json={"measurements": measurements},
            )
        if not res.is_success:
            print(f"[mirror] legacy server returned {res.status_code}")
        else:
            print(f"[mirror] legacy server received {len(measurements)} measurement(s)")
    except Exception as e:
        print(f"[mirror] legacy server unreachable: {e}")


# --------------------------- OTA update ---------------------------


async def _trigger_update():
    """Invoke the OTA update script as root via the pre-approved sudoers rule.

    The update script re-fetches schoolair_setup.sh from GitHub and runs it
    with --update, which re-deploys code, recompiles the C binary if needed,
    and restarts schoolair.service — killing this process.  systemd brings it
    back up immediately with the new code.  This function therefore may not
    return; that's expected and safe.
    """
    global _update_in_progress
    if _update_in_progress:
        print("[OTA] Update already in progress — skipping duplicate trigger")
        return
    _update_in_progress = True
    print(f"[OTA] Server signalled update available (running v{VERSION}) — starting update")
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "/usr/local/bin/schoolair-update",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode(errors="replace") if stdout else ""
        if proc.returncode == 0:
            print("[OTA] Update finished successfully")
        else:
            print(f"[OTA] Update exited with code {proc.returncode}")
            if output:
                print(output[-2000:])
    except Exception as e:
        print(f"[OTA] Update failed: {e}")
    finally:
        _update_in_progress = False


# --------------------------- WiFi push ---------------------------

_WIFI_STATE_FILE  = "config/wifi_state.json"
_WIFI_CONN_PREFIX = "schoolair-"
_WIFI_MAX_ENTRIES = 100
_WIFI_PRUNE_KEEP  = 50  # keep newest N when pruning


def _load_wifi_state() -> dict:
    try:
        return json.loads(Path(_WIFI_STATE_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {"pending_acks": [], "managed": []}


def _save_wifi_state(state: dict) -> None:
    Path(_WIFI_STATE_FILE).write_text(json.dumps(state, indent=2))


async def _nmcli_run(*args: str) -> bool:
    """Run nmcli with the given arguments. Returns True on success."""
    proc = await asyncio.create_subprocess_exec(
        "nmcli", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        print(f"[wifi-push] nmcli {' '.join(str(a) for a in args[:4])} "
              f"failed: {out.decode(errors='replace').strip()}")
    return proc.returncode == 0


async def _apply_wifi_credential(credential_id: int, ssid: str, password: str) -> bool:
    """Append a new NM connection for ssid. Never removes existing connections."""
    conn_name = f"{_WIFI_CONN_PREFIX}{credential_id}-{ssid[:30]}"
    args = ["connection", "add", "type", "wifi",
            "con-name", conn_name, "ssid", ssid]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    ok = await _nmcli_run(*args)
    if ok:
        print(f"[wifi-push] Added '{conn_name}' (SSID '{ssid}')")
    return ok


async def _prune_wifi_if_needed(state: dict) -> None:
    """Remove oldest SchoolAir-managed connections when count exceeds the cap."""
    managed: list = state.get("managed", [])
    if len(managed) <= _WIFI_MAX_ENTRIES:
        return
    managed.sort(key=lambda e: e.get("added_at", ""))
    to_prune, keep = managed[:-_WIFI_PRUNE_KEEP], managed[-_WIFI_PRUNE_KEEP:]
    for entry in to_prune:
        await _nmcli_run("connection", "delete", entry["conn_name"])
        print(f"[wifi-push] Pruned '{entry['conn_name']}'")
    state["managed"] = keep


async def _handle_wifi_push(push_list: list) -> None:
    """Process wifi_push entries from an ingest response."""
    if not push_list:
        return
    state   = _load_wifi_state()
    managed: list = state.setdefault("managed", [])
    pending: list = state.setdefault("pending_acks", [])
    known   = {e["credential_id"] for e in managed}

    for entry in push_list:
        cred_id  = entry.get("credential_id")
        ssid     = entry.get("ssid", "")
        password = entry.get("password", "")
        if cred_id is None or not ssid:
            continue
        if cred_id in known:
            continue  # already applied, ack was already sent
        success = await _apply_wifi_credential(cred_id, ssid, password)
        pending.append({"credential_id": cred_id, "success": success})
        if success:
            managed.append({
                "credential_id": cred_id,
                "ssid":          ssid,
                "conn_name":     f"{_WIFI_CONN_PREFIX}{cred_id}-{ssid[:30]}",
                "added_at":      datetime.now(timezone.utc).isoformat(),
            })
            known.add(cred_id)

    await _prune_wifi_if_needed(state)
    _save_wifi_state(state)


# --------------------------- Read step ---------------------------


def _should_drain(settings: dict) -> bool:
    """True if skipping this read would cause the drain deadline to be missed.

    Fires when the time remaining until the drain deadline is less than one read
    interval — i.e. the next scheduled read would arrive after the deadline.
    This guarantees drains happen *within* the configured interval (25–30 min
    active, 105–120 min idle).
    """
    if _last_drained_at is None:
        return False  # let the initial drain in _drain_loop handle startup
    elapsed = (datetime.now(timezone.utc) - _last_drained_at).total_seconds()
    return elapsed >= current_drain_interval(settings) - current_read_interval(settings)


async def _run_read(settings: dict, active_sensors: list):
    """Read one sensor sample, update buffer, and trigger verification on any breach."""
    recorded_at = datetime.now(timezone.utc).isoformat()
    try:
        data = read_sensor()
    except RuntimeError as e:
        print(f"Sensor read failed: {e}")
        return

    for sensor in active_sensors:
        reading = read_aux_sensor(sensor)
        if reading:
            data.update(reading)

    state.set(data, recorded_at)
    entry = {"data": data, "recorded_at": recorded_at, "severity": 0}
    _buffer.append(entry)

    criteria = load_criteria()
    if criteria:
        now = datetime.now(timezone.utc)
        breaching: list[tuple[str, dict]] = []
        for criterion in criteria:
            metric    = criterion["metric"]
            threshold = float(criterion["threshold"])
            condition = criterion["condition"]
            value     = extract_metric(data, metric)

            if value is None:
                continue
            if not _breached(float(value), threshold, condition):
                continue
            if metric in _verifying:
                continue

            last_alert = alert_cooldown.get(metric)
            if last_alert and (now - last_alert) < timedelta(hours=ALERT_COOLDOWN_HRS):
                continue

            print(f"[breach] {metric} = {value} ({condition} threshold {threshold})")
            breaching.append((metric, criterion))

        if breaching:
            for metric, _ in breaching:
                _verifying.add(metric)
            asyncio.create_task(_verify_all(breaching, entry))

    # Trigger drain on standard reads — not during alert verification.
    if not _verifying and _should_drain(settings):
        trigger_drain()


# --------------------------- Drain step ---------------------------


async def _run_drain(settings: dict):
    """Send the in-memory buffer plus any SQLite backlog to the server.

    SQLite is written only when both conditions are true:
      1. The buffer has reached BUFFER_CAPACITY.
      2. The server is unreachable.
    All other drain attempts go memory → server with no disk I/O.
    """
    global _buffer, _last_drained_at

    # Compact old SQLite rows into hourly means (no-op when SQLite is empty)
    try:
        summary = aggregate.run_aggregation()
        if summary["buckets"]:
            print(f"Aggregated {summary['rows_in']} old row(s) into {summary['buckets']} hourly row(s)")
    except Exception as e:
        print(f"Aggregation failed: {e}")

    # Hard cap: if SQLite has grown very large during an extended outage,
    # drop the oldest aggregated rows to keep it bounded.
    depth = queue.count_pending()
    if depth > aggregate.MAX_QUEUE_ROWS:
        dropped = queue.trim_aggregated(depth - aggregate.QUEUE_LOW_WATER)
        if dropped:
            print(f"SQLite queue over cap — discarded {dropped} oldest aggregated row(s)")

    _last_drained_at = datetime.now(timezone.utc)

    token = os.getenv("NEW_AUTH_TOKEN", "").strip()
    if not token:
        # No token yet — keep buffering; spill to SQLite only when buffer is full
        if len(_buffer) >= BUFFER_CAPACITY:
            for item in _buffer:
                queue.enqueue(item["data"], item["recorded_at"])
            _buffer.clear()
            print(f"[drain] no token — buffer full, flushed {BUFFER_CAPACITY} readings to SQLite")
        else:
            print(f"[drain] no token — {len(_buffer)} reading(s) held in memory, waiting for registration")
        return

    if not _buffer and not queue.count_pending():
        return

    # Build payload: fresh buffer readings first, then any SQLite backlog
    sqlite_rows = queue.get_pending(limit=BATCH_SIZE)
    sqlite_ids  = [r["id"] for r in sqlite_rows]

    payload = [
        {
            "recorded_at":   item["recorded_at"],
            "data":          item["data"],
            "is_aggregated": False,
            "severity":      item.get("severity", 0),
        }
        for item in _buffer
    ] + [
        {
            "recorded_at":   r["recorded_at"],
            "data":          json.loads(r["data"]),
            "is_aggregated": bool(r["is_aggregated"]),
        }
        for r in sqlite_rows
    ]

    print(
        f"Draining {len(payload)} measurement(s) "
        f"({len(_buffer)} buffered, {len(sqlite_rows)} from SQLite)…"
    )

    if sqlite_ids:
        queue.set_status_many(sqlite_ids, "sending")

    # Snapshot the buffer length before the async POST so any reading appended
    # by _run_read during the HTTP call is not accidentally discarded.
    n_buffered = len(_buffer)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await _post_batch(client, payload)

        del _buffer[:n_buffered]
        if sqlite_ids:
            queue.remove_many(sqlite_ids)

        if response.get("criteria"):
            save_criteria(response["criteria"])

        print(f"  Sent {response.get('count', len(payload))} measurement(s)")

        await _mirror_batch(payload)

        if response.get("update_available"):
            asyncio.create_task(_trigger_update())

        if response.get("wifi_push"):
            asyncio.create_task(_handle_wifi_push(response["wifi_push"]))

        # Flush alert buffer now that we know we have connectivity
        await _drain_alerts()

    except httpx.ConnectError:
        if sqlite_ids:
            queue.set_status_many(sqlite_ids, "pending")

        if len(_buffer) >= BUFFER_CAPACITY:
            print(
                f"Server unreachable and buffer full ({len(_buffer)}/{BUFFER_CAPACITY}) "
                f"— flushing buffer to SQLite"
            )
            for item in _buffer:
                queue.enqueue(item["data"], item["recorded_at"])
            _buffer.clear()
        else:
            remaining = BUFFER_CAPACITY - len(_buffer)
            print(
                f"Server unreachable — {len(_buffer)} reading(s) held in memory "
                f"({remaining} slot(s) before SQLite fallback)"
            )

    except httpx.HTTPStatusError:
        if sqlite_ids:
            queue.set_status_many(sqlite_ids, "pending")
        print("Server returned an error — will retry next drain cycle")

    except Exception as e:
        if sqlite_ids:
            queue.set_status_many(sqlite_ids, "pending")
        print(f"Drain failed unexpectedly: {e}")


# --------------------------- Loops ---------------------------


async def _read_loop(settings: dict, active_sensors: list):
    prev_active = _in_active_window(settings)
    while True:
        curr_active = _in_active_window(settings)

        if curr_active != prev_active:
            if prev_active and not curr_active:
                # active→idle: flush pending school-hours data before long cadence starts
                print("[transition] active→idle: triggering drain")
                trigger_drain()
            else:
                # idle→active: log (the immediate read below anchors us to the boundary)
                print("[transition] idle→active: immediate read")
            prev_active = curr_active

        await _run_read(settings, active_sensors)
        interval = current_read_interval(settings)
        delay = _seconds_to_next_boundary(interval)
        mode = "active" if curr_active else "idle"
        print(f"[read/{mode}] next in {delay:.0f}s")
        await asyncio.sleep(delay)


async def _drain_loop(settings: dict):
    global _drain_trigger
    _drain_trigger = asyncio.Event()

    jitter = _ensure_drain_jitter(settings)

    # Brief settle so the read loop collects at least one reading, and to clear
    # any SQLite backlog left from a previous outage.  No jitter here — devices
    # haven't converged to the same grid yet so there's no herd to scatter.
    await asyncio.sleep(15)
    await _run_drain(settings)

    # Drains are triggered by _run_read once the drain interval has elapsed.
    # The fallback timeout fires only if reads stop arriving for an extended period
    # (e.g. sensor failure) so the loop never blocks indefinitely.
    fallback = float(DRAIN_IDLE_SECONDS * 2)
    while True:
        try:
            await asyncio.wait_for(_drain_trigger.wait(), timeout=fallback)
            _drain_trigger.clear()
            print("[drain] triggered")
        except asyncio.TimeoutError:
            print("[drain] fallback — no reads received, draining anyway")
        if jitter:
            print(f"[drain] jitter: {jitter}s")
            await asyncio.sleep(jitter)
        await _run_drain(settings)


async def ingest_loop():
    """Initialise SQLite, probe aux sensors once, then run read and drain loops."""
    queue.init()
    settings = load_settings()
    validate_settings(settings)
    active_sensors = probe_aux_sensors()
    print(
        f"Ingest started — "
        f"reads {READ_ACTIVE_SECONDS}s/{READ_IDLE_SECONDS}s (active/idle) | "
        f"drains {DRAIN_ACTIVE_SECONDS}s/{DRAIN_IDLE_SECONDS}s (active/idle) | "
        f"buffer capacity {BUFFER_CAPACITY}"
        + (f" | aux sensors: {', '.join(s['name'] for s in active_sensors)}" if active_sensors else "")
    )
    await asyncio.gather(
        _read_loop(settings, active_sensors),
        _drain_loop(settings),
    )
