"""jobs/ingest.py

Write-through pipeline with local fallback and server-controlled backlog drain:

  Read loop   — collects a sensor reading every READ_ACTIVE_SECONDS (active
                window) or READ_IDLE_SECONDS (outside window), aligned to the
                device's upload offset, and signals _live_event.

  Upload loop — on each live reading: POSTs immediately to the server with
                the current backlog count; falls back to SQLite on failure.
                On success, drains any SQLite backlog using the server-granted
                credit_bytes, pacing between batches with recommended_delay_seconds.
                A new live reading interrupts the drain sleep and is sent first;
                the drain then resumes with the freshly returned credit.

  NTP task    — monitors wall-clock divergence from the monotonic projection.
                On first NTP step fires a one-time bulk timestamp correction
                on SQLite readings taken before sync was established.
"""

import asyncio
import json
import os
import random
import time as _time_mod
from datetime import datetime, timezone, timedelta, time
from pathlib import Path
import httpx
from dotenv import load_dotenv
from services.sensor import read_sensor, extract_metric, probe_aux_sensors, read_aux_sensor
import db.queue as queue
import jobs.aggregate as aggregate
import state

load_dotenv()

VERSION = "2.2.0"


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _version_is_older_than(min_version: str) -> bool:
    try:
        return _version_tuple(VERSION) < _version_tuple(min_version)
    except (ValueError, AttributeError):
        return False

_PRIMARY_SERVER_URL   = os.getenv("NEW_SERVER_URL", "").rstrip("/")
_PRIMARY_INGEST_URL   = os.getenv("NEW_INGEST_URL", f"{_PRIMARY_SERVER_URL}/aqc/v1/ingest") if _PRIMARY_SERVER_URL else ""
_SECONDARY_SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
_SECONDARY_INGEST_URL = os.getenv("INGEST_URL", f"{_SECONDARY_SERVER_URL}/aqc/v1/ingest")
_SECONDARY_AUTH_TOKEN = os.getenv("AUTH_TOKEN", "").strip()

ALERT_NEAR_PCT        = float(os.getenv("ALERT_NEAR_PCT", 10))
ALERT_COOLDOWN_HRS    = float(os.getenv("ALERT_COOLDOWN_HOURS", 1))

# Read intervals (not user-configurable; env vars for testing only)
READ_ACTIVE_SECONDS   = int(os.getenv("READ_INTERVAL_ACTIVE",  300))   # 5 min
READ_IDLE_SECONDS     = int(os.getenv("READ_INTERVAL_IDLE",    900))   # 15 min

# Drain interval constants kept as reference (no longer drive a timer)
DRAIN_ACTIVE_SECONDS  = int(os.getenv("DRAIN_INTERVAL_ACTIVE", 1800))
DRAIN_IDLE_SECONDS    = int(os.getenv("DRAIN_INTERVAL_IDLE",   7200))
DRAIN_JITTER_MAX      = int(os.getenv("DRAIN_JITTER_MAX",       120))  # fallback jitter cap

CRITERIA_PATH = Path("config/criteria.json")
SETTINGS_PATH = Path("config/settings.json")

DEFAULT_SETTINGS = {"active_window": {"start": "07:00", "end": "16:00"}}

MAX_ACTIVE_HOURS = 9

NTP_CORRECTED_KEY    = "ntp_clock_corrected"
NTP_STEP_THRESHOLD_S = 30  # divergence above this (seconds) indicates an NTP step

# ── Upload state ──────────────────────────────────────────────────────────────

_pending_live:      dict | None   = None   # reading ready to POST (set by _run_read)
_live_event:        asyncio.Event | None = None  # signalled when _pending_live is ready
_credit_bytes:      int   = 0              # server-granted byte budget; always overwritten
_recommended_delay: float = 0.0            # seconds to wait between drain batches

# ── Settings state ────────────────────────────────────────────────────────────

_settings:        dict             = {}    # live settings; updated by server schedule pushes
_settings_event:  asyncio.Event | None = None  # fired when active_window changes mid-sleep

# ── Alert state ───────────────────────────────────────────────────────────────

_alert_buffer:        list[dict]         = []
ALERT_BUFFER_CAPACITY = int(os.getenv("ALERT_BUFFER_CAPACITY", 50))
alert_cooldown:       dict[str, datetime] = {}
_verifying:           set[str]           = set()

# ── OTA state ─────────────────────────────────────────────────────────────────

_update_in_progress = False

# ── NTP correction state ──────────────────────────────────────────────────────

_ntp_fake_wall: datetime | None = None   # wall-clock value recorded at service start
_ntp_mono_ref:  float    | None = None   # monotonic value recorded at service start


# ── Settings ──────────────────────────────────────────────────────────────────


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
        now = datetime.now(timezone.utc).time()
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
    """Return the persisted upload jitter offset for this device.

    If drain_jitter_seconds is already in settings it is used unchanged —
    making migration to server-assigned offsets easy (write the value into
    settings.json).  If absent a random offset is generated, saved, and returned.
    """
    if "drain_jitter_seconds" in settings:
        return int(settings["drain_jitter_seconds"])
    jitter = random.randint(0, DRAIN_JITTER_MAX)
    settings["drain_jitter_seconds"] = jitter
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    print(f"[upload] jitter slot assigned: {jitter}s — saved to {SETTINGS_PATH}")
    return jitter


def _get_upload_offset(settings: dict) -> int:
    """Return upload offset in seconds.

    Prefers server-assigned UPLOAD_OFFSET from .env (written at provisioning).
    Falls back to locally-generated jitter stored in settings.json.
    """
    env_val = os.getenv("UPLOAD_OFFSET", "").strip()
    if env_val.lstrip("-").isdigit():
        return int(env_val)
    return _ensure_drain_jitter(settings)


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


def _seconds_to_next_boundary(interval: int, offset: int = 0, now: datetime | None = None) -> float:
    """Seconds until the next upload boundary for this device.

    offset shifts the boundary grid so devices within an organisation are
    staggered.  offset=0 aligns to the Unix epoch (same as before).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    past = (now.timestamp() - offset) % interval
    return float(interval - past)


# ── Criteria ──────────────────────────────────────────────────────────────────


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


# ── Alert verification ────────────────────────────────────────────────────────


def _breached(value: float, threshold: float, condition: str) -> bool:
    if condition == "above":
        return value > threshold
    return value < threshold


def _near_or_breached(value: float, threshold: float, condition: str) -> bool:
    margin = threshold * ALERT_NEAR_PCT / 100
    if condition == "above":
        return value >= threshold - margin
    return value <= threshold + margin


def _set_cooldown(breaching: list[tuple[str, dict]], now_dt: datetime | None = None) -> None:
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    for metric, _ in breaching:
        alert_cooldown[metric] = now_dt


def _buffer_alert(alert: dict):
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
    """Two-stage verification for all metrics that breached at T."""
    metrics_str = ", ".join(m for m, _ in breaching)

    def any_high(data: dict) -> bool:
        return any(
            (v := extract_metric(data, m)) is not None
            and _near_or_breached(v, float(c["threshold"]), c["condition"])
            for m, c in breaching
        )

    sev = 1
    r3: dict | None = None
    r3_at: str = ""

    try:
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
        else:
            print(
                f"[verify/{metrics_str}] stage 2: both reads low "
                f"— momentary event (severity={sev})"
            )
            # r3 (T+1m) stored in SQLite; drained on next successful live POST
            queue.enqueue(r3, r3_at)

    finally:
        for metric, _ in breaching:
            _verifying.discard(metric)


# ── Alert drain ───────────────────────────────────────────────────────────────


async def _drain_alerts():
    """Flush the in-memory alert buffer (and any SQLite overflow) to the server."""
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
            return

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


# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _auth_headers() -> dict:
    return {
        "Authorization":      f"Bearer {os.getenv('NEW_AUTH_TOKEN', '').strip()}",
        "Content-Type":       "application/json",
        "X-Schoolair-Version": VERSION,
    }


async def _try_post(
    readings: list[dict],
    backlog_count: int,
    bpr: int | None = None,
    timeout: float = 3.0,
) -> dict | None:
    """POST readings to the primary server. Returns parsed response or None on failure.

    Request body:
      readings        — always an array; single element for live readings
      backlog_readings — count still in SQLite after this batch (0 = no backlog)
      bytes_per_reading — serialised size of one reading; present only when backlog > 0
    """
    body: dict = {"readings": readings, "backlog_readings": backlog_count}
    if backlog_count > 0 and bpr is not None:
        body["bytes_per_reading"] = bpr

    wifi_state   = _load_wifi_state()
    pending_acks = wifi_state.get("pending_acks", [])
    if pending_acks:
        body["wifi_acks"] = pending_acks

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.post(
                _PRIMARY_INGEST_URL,
                headers=_auth_headers(),
                json=body,
            )
        if res.status_code == 401:
            print("Ingest failed: auth token rejected.")
            return None
        res.raise_for_status()

        if pending_acks:
            wifi_state["pending_acks"] = []
            _save_wifi_state(wifi_state)

        return res.json()

    except (httpx.ConnectError, httpx.TimeoutException):
        return None
    except Exception as e:
        print(f"Ingest POST error: {e}")
        return None


async def _mirror_batch(readings: list[dict]) -> None:
    """Best-effort POST to the legacy secondary server. Never raises."""
    if not _SECONDARY_INGEST_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                _SECONDARY_INGEST_URL,
                headers={"Authorization": f"Bearer {_SECONDARY_AUTH_TOKEN}", "Content-Type": "application/json"},
                json={"measurements": readings},
            )
        if not res.is_success:
            print(f"[mirror] legacy server returned {res.status_code}")
        else:
            print(f"[mirror] legacy server received {len(readings)} reading(s)")
    except Exception as e:
        print(f"[mirror] legacy server unreachable: {e}")


# ── OTA update ────────────────────────────────────────────────────────────────


async def _trigger_update():
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
            print("[OTA] Update finished successfully — service will restart")
            return  # _update_in_progress stays True; blocks re-trigger before restart
        else:
            print(f"[OTA] Update exited with code {proc.returncode}")
            if output:
                print(output[-2000:])
    except Exception as e:
        print(f"[OTA] Update failed: {e}")
    _update_in_progress = False  # only reached on failure


# ── WiFi push ─────────────────────────────────────────────────────────────────

_WIFI_STATE_FILE  = "config/wifi_state.json"
_WIFI_CONN_PREFIX = "schoolair-"
_WIFI_MAX_ENTRIES = 100
_WIFI_PRUNE_KEEP  = 50


def _load_wifi_state() -> dict:
    try:
        return json.loads(Path(_WIFI_STATE_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {"pending_acks": [], "managed": []}


def _save_wifi_state(s: dict) -> None:
    Path(_WIFI_STATE_FILE).write_text(json.dumps(s, indent=2))


async def _nmcli_run(*args: str) -> bool:
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
    conn_name = f"{_WIFI_CONN_PREFIX}{credential_id}-{ssid[:30]}"
    args = ["connection", "add", "type", "wifi", "con-name", conn_name, "ssid", ssid]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    ok = await _nmcli_run(*args)
    if ok:
        print(f"[wifi-push] Added '{conn_name}' (SSID '{ssid}')")
    return ok


async def _prune_wifi_if_needed(s: dict) -> None:
    managed: list = s.get("managed", [])
    if len(managed) <= _WIFI_MAX_ENTRIES:
        return
    managed.sort(key=lambda e: e.get("added_at", ""))
    to_prune, keep = managed[:-_WIFI_PRUNE_KEEP], managed[-_WIFI_PRUNE_KEEP:]
    for entry in to_prune:
        await _nmcli_run("connection", "delete", entry["conn_name"])
        print(f"[wifi-push] Pruned '{entry['conn_name']}'")
    s["managed"] = keep


async def _handle_wifi_push(push_list: list) -> None:
    if not push_list:
        return
    s       = _load_wifi_state()
    managed: list = s.setdefault("managed", [])
    pending: list = s.setdefault("pending_acks", [])
    known   = {e["credential_id"] for e in managed}

    for entry in push_list:
        cred_id  = entry.get("credential_id")
        ssid     = entry.get("ssid", "")
        password = entry.get("password", "")
        if cred_id is None or not ssid:
            continue
        if cred_id in known:
            continue
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

    await _prune_wifi_if_needed(s)
    _save_wifi_state(s)


# ── Response handling ─────────────────────────────────────────────────────────


def _update_credit(response: dict) -> None:
    """Overwrite credit with the latest server grant. Never accumulates."""
    global _credit_bytes, _recommended_delay
    _credit_bytes      = int(response.get("credit_bytes", 0))
    _recommended_delay = float(response.get("recommended_delay_seconds", 0))


def _apply_window_update(new_window: dict) -> bool:
    """Apply a server-provided active_window if it differs from the current one.

    Compares against the in-memory value first — writes settings.json only on
    actual change.  Invalid windows (bad boundaries, window too wide) are
    silently ignored so a bad server response can't crash the service.
    Returns True if the window was changed.
    """
    global _settings
    if new_window == _settings.get("active_window"):
        return False
    candidate = {**_settings, "active_window": new_window}
    try:
        validate_settings(candidate)
    except SystemExit as e:
        print(f"[settings] server sent invalid active_window — ignoring ({e})")
        return False
    _settings["active_window"] = new_window
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(_settings, indent=2))
    print(f"[settings] active_window updated: {new_window['start']}–{new_window['end']}")
    return True


async def _wait_for_boundary(seconds: float) -> None:
    """Sleep until the next read boundary, waking early if active_window changes."""
    if seconds <= 0:
        return
    if _settings_event is None:
        await asyncio.sleep(seconds)
        return
    _settings_event.clear()
    try:
        await asyncio.wait_for(asyncio.shield(_settings_event.wait()), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _handle_response(response: dict) -> None:
    """Process server directives from any successful ingest response."""
    if response.get("criteria"):
        save_criteria(response["criteria"])
    min_ver = response.get("min_version")
    if min_ver and _version_is_older_than(min_ver):
        asyncio.create_task(_trigger_update())
    schedule = response.get("schedule")
    if schedule and "active_start" in schedule and "active_end" in schedule:
        new_window = {"start": schedule["active_start"], "end": schedule["active_end"]}
        if _apply_window_update(new_window) and _settings_event is not None:
            _settings_event.set()
    if response.get("wifi_push"):
        asyncio.create_task(_handle_wifi_push(response["wifi_push"]))
    await _drain_alerts()


# ── NTP correction ────────────────────────────────────────────────────────────


async def _ntp_correction_task():
    """One-shot: detect first NTP sync and correct pre-sync SQLite timestamps."""
    global _ntp_fake_wall, _ntp_mono_ref

    settings_data = load_settings()
    if settings_data.get(NTP_CORRECTED_KEY):
        return  # already corrected on a previous boot

    # One subprocess call to check if NTP is already synced at startup
    try:
        proc = await asyncio.create_subprocess_exec(
            "timedatectl", "show", "--property=NTPSynchronized",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        if b"NTPSynchronized=yes" in stdout:
            settings_data[NTP_CORRECTED_KEY] = True
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(settings_data, indent=2))
            print("[ntp] already synced at startup — no correction needed")
            return
    except Exception:
        pass  # can't determine — proceed with monitoring

    # Record reference point: wrong wall time + monotonic (never jumps)
    _ntp_fake_wall = datetime.now(timezone.utc)
    _ntp_mono_ref  = _time_mod.monotonic()
    print(f"[ntp] monitoring for NTP step (pre-sync ref: {_ntp_fake_wall.isoformat()})")

    while _ntp_fake_wall is not None:
        await asyncio.sleep(5)

        elapsed    = _time_mod.monotonic() - _ntp_mono_ref
        projected  = _ntp_fake_wall + timedelta(seconds=elapsed)
        divergence = abs((datetime.now(timezone.utc) - projected).total_seconds())

        if divergence < NTP_STEP_THRESHOLD_S:
            continue

        # NTP stepped the clock
        real_now = datetime.now(timezone.utc)
        delta    = (real_now - projected).total_seconds()
        print(f"[ntp] step detected: delta={delta:+.0f}s, boundary={projected.isoformat()}")

        if abs(delta) >= 10:
            corrected = queue.apply_clock_correction(projected.isoformat(), delta)
            if corrected:
                print(f"[ntp] corrected timestamps on {corrected} queued reading(s)")

        current = load_settings()
        current[NTP_CORRECTED_KEY] = True
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(current, indent=2))

        _ntp_fake_wall = None  # exit loop


# ── Read step ─────────────────────────────────────────────────────────────────


async def _run_read(settings: dict, active_sensors: list):
    """Read one sensor sample, check for breaches, signal the upload loop."""
    global _pending_live

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

    # Signal upload loop — overwrites any previous unsent reading
    _pending_live = entry
    if _live_event is not None:
        _live_event.set()


# ── Upload and drain ──────────────────────────────────────────────────────────


def _format_reading(item: dict) -> dict:
    return {
        "recorded_at":   item["recorded_at"],
        "data":          item["data"],
        "is_aggregated": item.get("is_aggregated", False),
        "severity":      item.get("severity", 0),
    }


def _format_sqlite_row(row) -> dict:
    return {
        "recorded_at":   row["recorded_at"],
        "data":          json.loads(row["data"]),
        "is_aggregated": bool(row["is_aggregated"]),
        "severity":      0,
    }


async def _wait_for_live(seconds: float) -> bool:
    """Sleep up to `seconds`. Returns True early if a live reading arrives."""
    if _live_event is None or seconds <= 0:
        return False if seconds <= 0 else _live_event is not None and _live_event.is_set()
    try:
        await asyncio.wait_for(asyncio.shield(_live_event.wait()), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return _live_event.is_set()


async def _drain_backlog() -> None:
    """Drain SQLite backlog using current credit. Interruptible by live readings."""
    global _credit_bytes, _recommended_delay

    # Compact old rows before draining
    try:
        summary = aggregate.run_aggregation()
        if summary["buckets"]:
            print(f"[drain] aggregated {summary['rows_in']} row(s) → {summary['buckets']} hourly")
    except Exception as e:
        print(f"[drain] aggregation error: {e}")

    depth = queue.count_pending()
    if depth > aggregate.MAX_QUEUE_ROWS:
        dropped = queue.trim_aggregated(depth - aggregate.QUEUE_LOW_WATER)
        if dropped:
            print(f"[drain] queue over cap — dropped {dropped} oldest aggregated row(s)")

    while True:
        backlog = queue.count_pending()
        if backlog == 0 or _credit_bytes <= 0:
            break

        # Wait recommended_delay but wake early if a live reading arrives
        if _recommended_delay > 0:
            interrupted = await _wait_for_live(seconds=_recommended_delay)
            if interrupted:
                break  # live reading ready — let _upload_loop handle it first

        # Measure bytes per reading from one queued row to size the batch
        sample = queue.get_pending(limit=1)
        if not sample:
            break
        sample_reading = _format_sqlite_row(sample[0])
        bpr        = len(json.dumps(sample_reading).encode())
        batch_size = max(1, _credit_bytes // bpr)

        rows = queue.get_pending(limit=batch_size)
        ids  = [r["id"] for r in rows]
        readings  = [_format_sqlite_row(r) for r in rows]
        remaining = backlog - len(rows)

        queue.set_status_many(ids, "sending")

        response = await _try_post(readings, remaining, bpr, timeout=10.0)

        if response is None:
            queue.set_status_many(ids, "pending")
            print(f"[drain] server unreachable — {backlog} reading(s) held in SQLite")
            break

        queue.remove_many(ids)
        _update_credit(response)
        await _handle_response(response)
        print(f"[drain] sent {len(rows)} reading(s), {remaining} remaining")

    if queue.count_pending() == 0:
        print("[drain] backlog cleared")


async def _upload_loop() -> None:
    """Send live readings immediately; drain backlog on server credit."""
    global _live_event

    _live_event = asyncio.Event()

    while True:
        await _live_event.wait()
        _live_event.clear()

        entry = _pending_live
        if entry is None:
            continue

        token = os.getenv("NEW_AUTH_TOKEN", "").strip()
        if not token:
            queue.enqueue(entry["data"], entry["recorded_at"])
            print(f"[upload] no token — reading stored in SQLite")
            continue

        backlog_count = queue.count_pending()
        bpr = len(json.dumps(_format_reading(entry)).encode()) if backlog_count > 0 else None

        response = await _try_post([_format_reading(entry)], backlog_count, bpr)

        if response is None:
            queue.enqueue(entry["data"], entry["recorded_at"])
            total = backlog_count + 1
            print(f"[upload] server unreachable — reading queued (SQLite total: {total})")
            continue

        _update_credit(response)
        asyncio.create_task(_mirror_batch([_format_reading(entry)]))
        await _handle_response(response)
        print(f"[upload] live reading sent (backlog: {backlog_count})")

        if backlog_count > 0 and _credit_bytes > 0:
            await _drain_backlog()


# ── Loops ─────────────────────────────────────────────────────────────────────


async def _read_loop(active_sensors: list):
    offset      = _get_upload_offset(_settings)
    prev_active = _in_active_window(_settings)

    while True:
        curr_active = _in_active_window(_settings)

        if curr_active != prev_active:
            if prev_active and not curr_active:
                print("[transition] active→idle")
            else:
                print("[transition] idle→active: immediate read")
            prev_active = curr_active

        await _run_read(_settings, active_sensors)
        interval = current_read_interval(_settings)
        offset   = _get_upload_offset(_settings)  # re-read in case settings changed
        delay    = _seconds_to_next_boundary(interval, offset)
        mode     = "active" if curr_active else "idle"
        print(f"[read/{mode}] next in {delay:.0f}s (offset={offset}s)")
        await _wait_for_boundary(delay)


async def ingest_loop():
    """Initialise SQLite, probe aux sensors, run read / upload / NTP tasks."""
    global _settings, _settings_event
    queue.init()
    _settings = load_settings()
    validate_settings(_settings)
    _settings_event = asyncio.Event()
    active_sensors = probe_aux_sensors()
    print(
        f"Ingest started — "
        f"reads {READ_ACTIVE_SECONDS}s/{READ_IDLE_SECONDS}s (active/idle) | "
        f"write-through with SQLite fallback"
        + (f" | aux sensors: {', '.join(s['name'] for s in active_sensors)}" if active_sensors else "")
    )
    await asyncio.gather(
        _read_loop(active_sensors),
        _upload_loop(),
        _ntp_correction_task(),
    )
