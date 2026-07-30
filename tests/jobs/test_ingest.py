"""tests/jobs/test_ingest.py

Unit tests for jobs.ingest: scheduling intervals, breach detection,
alert buffering, live-event signalling, write-through pipeline,
credit management, and shared verification task with severity scoring.

Run laptop-safe tests only:
    pytest -m "not hardware"
"""

import asyncio
import json
import re
from datetime import time, datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import jobs.ingest as ingest
import db.queue as queue
from jobs.ingest import (
    current_read_interval,
    current_drain_interval,
    _seconds_to_next_boundary,
    _window_hours,
    validate_settings,
    _breached,
    _near_or_breached,
    _buffer_alert,
    _verify_all,
    _run_read,
    _try_post,
    _wait_for_live,
    _drain_backlog,
    _handle_response,
    _update_credit,
    _get_upload_offset,
    _auth_headers,
    _trigger_update,
    _ensure_drain_jitter,
    ALERT_NEAR_PCT,
    ALERT_BUFFER_CAPACITY,
    VERSION,
    DRAIN_JITTER_MAX,
    READ_ACTIVE_SECONDS  as READ_ACTIVE,
    READ_IDLE_SECONDS    as READ_IDLE,
    DRAIN_ACTIVE_SECONDS as DRAIN_ACTIVE,
    DRAIN_IDLE_SECONDS   as DRAIN_IDLE,
)

S = {
    "active_window": {"start": "07:00", "end": "16:00"},
}


# ── Shared fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_ingest_state():
    """Clear mutable module-level state before and after each test."""
    ingest._alert_buffer.clear()
    ingest._pending_live    = None
    ingest._live_event      = None
    ingest._credit_bytes    = 0
    ingest._recommended_delay = 0.0
    ingest._verifying.clear()
    ingest.alert_cooldown.clear()
    yield
    ingest._alert_buffer.clear()
    ingest._pending_live    = None
    ingest._live_event      = None
    ingest._credit_bytes    = 0
    ingest._recommended_delay = 0.0
    ingest._verifying.clear()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "DB_PATH", tmp_path / "test_queue.db")
    queue.init()


# ── Read interval ──────────────────────────────────────────────────────────────

def test_read_inside_window():
    assert current_read_interval(S, time(9, 0)) == READ_ACTIVE

def test_read_before_window():
    assert current_read_interval(S, time(6, 59)) == READ_IDLE

def test_read_start_is_inclusive():
    assert current_read_interval(S, time(7, 0)) == READ_ACTIVE

def test_read_end_is_exclusive():
    assert current_read_interval(S, time(16, 0)) == READ_IDLE

def test_read_midnight_crossing():
    night = {**S, "active_window": {"start": "22:00", "end": "06:00"}}
    assert current_read_interval(night, time(23, 0)) == READ_ACTIVE
    assert current_read_interval(night, time(2, 0))  == READ_ACTIVE
    assert current_read_interval(night, time(12, 0)) == READ_IDLE


# ── Drain interval ─────────────────────────────────────────────────────────────

def test_drain_inside_window():
    assert current_drain_interval(S, time(9, 0)) == DRAIN_ACTIVE

def test_drain_before_window():
    assert current_drain_interval(S, time(6, 59)) == DRAIN_IDLE

def test_drain_end_is_exclusive():
    assert current_drain_interval(S, time(16, 0)) == DRAIN_IDLE


# ── Clock-boundary sleep ───────────────────────────────────────────────────────

def test_next_boundary_mid_interval():
    """5 s past a 300 s boundary → sleep 295 s to the next mark."""
    t = datetime(2026, 1, 1, 8, 0, 5, tzinfo=timezone.utc)
    assert _seconds_to_next_boundary(300, now=t) == pytest.approx(295.0, abs=0.01)

def test_next_boundary_exactly_on_boundary():
    """Exactly on a 300 s boundary → sleep the full 300 s to the next one."""
    t = datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
    assert _seconds_to_next_boundary(300, now=t) == pytest.approx(300.0, abs=0.01)

def test_next_boundary_idle_interval():
    """5 s past a 900 s boundary (7:45:05) → sleep 895 s to 8:00:00."""
    t = datetime(2026, 1, 1, 7, 45, 5, tzinfo=timezone.utc)
    assert _seconds_to_next_boundary(900, now=t) == pytest.approx(895.0, abs=0.01)

def test_next_boundary_with_offset():
    """offset shifts the grid: with offset=60, the next slot is 60 s away from an exact 300 s boundary."""
    t = datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
    # No offset: exactly on boundary → 300 s to go
    assert _seconds_to_next_boundary(300, offset=0, now=t) == pytest.approx(300.0, abs=0.01)
    # offset=60: device grid is shifted — next slot at :01 → 60 s away
    assert _seconds_to_next_boundary(300, offset=60, now=t) == pytest.approx(60.0, abs=0.01)


# ── Window helpers ─────────────────────────────────────────────────────────────

def test_window_hours_handles_midnight():
    assert _window_hours({"start": "22:00", "end": "06:00"}) == 8
    assert _window_hours({"start": "07:00", "end": "16:00"}) == 9

def test_validate_rejects_long_window():
    bad = {**S, "active_window": {"start": "06:00", "end": "20:00"}}  # 14 h
    with pytest.raises(SystemExit):
        validate_settings(bad)

def test_validate_rejects_non_quarter_hour_start():
    bad = {**S, "active_window": {"start": "07:10", "end": "16:00"}}
    with pytest.raises(SystemExit):
        validate_settings(bad)

def test_validate_rejects_non_quarter_hour_end():
    bad = {**S, "active_window": {"start": "07:00", "end": "16:05"}}
    with pytest.raises(SystemExit):
        validate_settings(bad)

def test_validate_accepts_quarter_hour_boundaries():
    for start, end in [("07:00", "16:00"), ("07:15", "15:45"), ("08:30", "15:30")]:
        validate_settings({**S, "active_window": {"start": start, "end": end}})


# ── Breach detection ───────────────────────────────────────────────────────────

def test_breached_above_true_when_value_exceeds():
    assert _breached(1001.0, 1000.0, "above") is True

def test_breached_above_false_at_exactly_threshold():
    assert _breached(1000.0, 1000.0, "above") is False

def test_breached_above_false_when_below():
    assert _breached(999.0, 1000.0, "above") is False

def test_breached_below_true_when_value_under():
    assert _breached(9.0, 10.0, "below") is True

def test_breached_below_false_at_exactly_threshold():
    assert _breached(10.0, 10.0, "below") is False

def test_breached_below_false_when_above():
    assert _breached(11.0, 10.0, "below") is False


def test_near_or_breached_exactly_at_threshold_above():
    assert _near_or_breached(1000.0, 1000.0, "above") is True

def test_near_or_breached_exactly_at_threshold_below():
    assert _near_or_breached(10.0, 10.0, "below") is True

def test_near_or_breached_within_margin_above():
    margin = 1000.0 * ALERT_NEAR_PCT / 100
    assert _near_or_breached(1000.0 - margin, 1000.0, "above") is True

def test_near_or_breached_outside_margin_above():
    margin = 1000.0 * ALERT_NEAR_PCT / 100
    assert _near_or_breached(1000.0 - margin - 1, 1000.0, "above") is False

def test_near_or_breached_within_margin_below():
    margin = 10.0 * ALERT_NEAR_PCT / 100
    assert _near_or_breached(10.0 + margin, 10.0, "below") is True

def test_near_or_breached_outside_margin_below():
    margin = 10.0 * ALERT_NEAR_PCT / 100
    assert _near_or_breached(10.0 + margin + 1, 10.0, "below") is False


# ── Alert buffer ───────────────────────────────────────────────────────────────

def test_buffer_alert_appends_to_in_memory_buffer(tmp_db):
    alert = {
        "metric": "co2", "value": 1500, "threshold": 800,
        "recorded_at": "2026-06-23T10:00:00+00:00",
    }
    _buffer_alert(alert)
    assert len(ingest._alert_buffer) == 1
    assert queue.get_pending_alerts() == []   # SQLite must not be touched yet


def test_buffer_alert_flushes_to_sqlite_at_capacity(tmp_db):
    alert = {
        "metric": "co2", "value": 1500, "threshold": 800,
        "recorded_at": "2026-06-23T10:00:00+00:00",
    }
    for _ in range(ALERT_BUFFER_CAPACITY):
        _buffer_alert(alert)
    assert ingest._alert_buffer == []
    assert len(queue.get_pending_alerts()) == ALERT_BUFFER_CAPACITY


# ── Shared verification task (_verify_all) ────────────────────────────────────

_CO2_CRITERION = {
    "metric": "co2", "threshold": 1000.0, "condition": "above", "severity": "warning",
}
_TEMP_CRITERION = {
    "metric": "temp", "threshold": 30.0, "condition": "above", "severity": "warning",
}

def _breach_entry(co2=1500, temp=32.0):
    return {
        "data": {"sen6x": {"co2": co2, "temp": temp}},
        "recorded_at": "2026-06-23T10:00:00+00:00",
        "severity": 0,
    }

def _low_read():
    return {"sen6x": {"co2": 400.0, "temp": 20.0}}

def _high_read():
    return {"sen6x": {"co2": 1200.0, "temp": 35.0}}


async def test_verify_all_fluke_sets_severity_1():
    """Both stage-1 reads low → fluke, severity=1, no stage-2, verifying cleared."""
    entry = _breach_entry()
    ingest._verifying.update(["co2", "temp"])

    with patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("jobs.ingest.read_sensor", side_effect=[_low_read(), _low_read()]), \
         patch("jobs.ingest.state"):
        await _verify_all([("co2", _CO2_CRITERION), ("temp", _TEMP_CRITERION)], entry)

    assert entry["severity"] == 1
    assert not ingest._verifying, "_verifying must be cleared on completion"


async def test_verify_all_momentary_r3_stored_in_sqlite(tmp_db):
    """Stage 1 both high, stage 2 both low → momentary event: T+1m stored in SQLite."""
    entry = _breach_entry()
    ingest._verifying.update(["co2"])

    reads = [_high_read(), _high_read(), _low_read(), _low_read()]
    with patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("jobs.ingest.read_sensor", side_effect=reads), \
         patch("jobs.ingest.state"):
        await _verify_all([("co2", _CO2_CRITERION)], entry)

    # severity = 1 (baseline) + 1 (r1 high) + 1 (r2 high) = 3
    assert entry["severity"] == 3
    assert queue.count_pending() == 1, "T+1m read must be stored in SQLite for momentary event"
    row = queue.get_pending(limit=1)[0]
    assert json.loads(row["data"]) == _low_read()


async def test_verify_all_alert_sends_on_persistent_breach():
    """Stage 1 both high, stage 2 one high → persistent breach, alert sent."""
    entry = _breach_entry()
    ingest._verifying.update(["co2"])

    reads = [_high_read(), _high_read(), _high_read(), _low_read()]
    with patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("jobs.ingest.read_sensor", side_effect=reads), \
         patch("jobs.ingest.state"), \
         patch("jobs.ingest._send_or_queue_alert", new_callable=AsyncMock) as mock_send, \
         patch.dict("os.environ", {"NEW_AUTH_TOKEN": "tok"}):
        await _verify_all([("co2", _CO2_CRITERION)], entry)

    # severity = 1 + 1 + 1 + 2 = 5
    assert entry["severity"] == 5
    assert mock_send.call_count == 1
    assert not ingest._verifying


async def test_verify_all_clears_verifying_on_sensor_error():
    """Even when a sensor read fails mid-task, _verifying must be cleared."""
    entry = _breach_entry()
    ingest._verifying.add("co2")

    with patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("jobs.ingest.read_sensor", side_effect=RuntimeError("sensor off")), \
         patch("jobs.ingest.state"):
        await _verify_all([("co2", _CO2_CRITERION)], entry)

    assert not ingest._verifying


# ── _run_read signals live event ───────────────────────────────────────────────

async def test_run_read_sets_pending_live_and_signals_event():
    """A successful read stores _pending_live and signals _live_event."""
    ingest._live_event = asyncio.Event()

    with patch("jobs.ingest.read_sensor", return_value={"sen6x": {"co2": 400}}), \
         patch("jobs.ingest.load_criteria", return_value=[]), \
         patch("jobs.ingest.state"):
        await _run_read(S, [])

    assert ingest._pending_live is not None
    assert ingest._pending_live["data"] == {"sen6x": {"co2": 400}}
    assert ingest._live_event.is_set()


async def test_run_read_no_signal_on_sensor_error():
    """A failed sensor read returns early without setting _pending_live or event."""
    ingest._live_event = asyncio.Event()

    with patch("jobs.ingest.read_sensor", side_effect=RuntimeError("sensor off")):
        await _run_read(S, [])

    assert ingest._pending_live is None
    assert not ingest._live_event.is_set()


# ── _try_post ─────────────────────────────────────────────────────────────────

def _mock_client(response_json: dict, status: int = 200):
    """Return a mock httpx.AsyncClient context manager with a canned response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.raise_for_status = MagicMock()
    if status == 401:
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=mock_resp
        )
    mock_resp.json.return_value = response_json

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


async def test_try_post_returns_none_on_connect_error(monkeypatch):
    """Connection failure returns None without raising."""
    monkeypatch.setattr(ingest, "_PRIMARY_INGEST_URL", "http://localhost:0/ingest")
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _try_post([{"recorded_at": "t", "data": {}}], 0)

    assert result is None


async def test_try_post_returns_none_on_auth_rejection(monkeypatch):
    """401 from server returns None."""
    monkeypatch.setattr(ingest, "_PRIMARY_INGEST_URL", "http://server/ingest")
    mock_client = _mock_client({}, status=401)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _try_post([{"recorded_at": "t", "data": {}}], 0)

    assert result is None


async def test_try_post_includes_backlog_metadata_when_backlog_nonzero(monkeypatch):
    """Request body carries backlog_readings and bytes_per_reading when backlog > 0."""
    monkeypatch.setattr(ingest, "_PRIMARY_INGEST_URL", "http://server/ingest")
    monkeypatch.setenv("NEW_AUTH_TOKEN", "tok")

    captured: dict = {}

    async def fake_post(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"credit_bytes": 500}
        return resp

    mock_client = AsyncMock()
    mock_client.post.side_effect = fake_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _try_post(
            [{"recorded_at": "t", "data": {"co2": 400}}],
            backlog_count=5,
            bpr=100,
        )

    assert result == {"credit_bytes": 500}
    assert captured["backlog_readings"] == 5
    assert captured["bytes_per_reading"] == 100
    assert len(captured["readings"]) == 1


async def test_try_post_omits_bytes_per_reading_when_no_backlog(monkeypatch):
    """bytes_per_reading must be absent from the request body when backlog_count=0."""
    monkeypatch.setattr(ingest, "_PRIMARY_INGEST_URL", "http://server/ingest")
    monkeypatch.setenv("NEW_AUTH_TOKEN", "tok")

    captured: dict = {}

    async def fake_post(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"credit_bytes": 0}
        return resp

    mock_client = AsyncMock()
    mock_client.post.side_effect = fake_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _try_post([{"recorded_at": "t", "data": {}}], backlog_count=0)

    assert "bytes_per_reading" not in captured


# ── _wait_for_live ─────────────────────────────────────────────────────────────

async def test_wait_for_live_returns_false_on_timeout():
    """Timeout expires without an event → False."""
    ingest._live_event = asyncio.Event()
    result = await _wait_for_live(0.01)
    assert result is False


async def test_wait_for_live_returns_true_when_event_already_set():
    """If the event is already set before the call, return True immediately."""
    ingest._live_event = asyncio.Event()
    ingest._live_event.set()
    result = await _wait_for_live(1.0)
    assert result is True


async def test_wait_for_live_returns_false_when_no_event():
    """When _live_event is None, return False without blocking."""
    ingest._live_event = None
    result = await _wait_for_live(1.0)
    assert result is False


# ── _update_credit ─────────────────────────────────────────────────────────────

def test_update_credit_overwrites_not_accumulates():
    """A second credit grant overwrites the first — credits never accumulate."""
    ingest._credit_bytes      = 5000
    ingest._recommended_delay = 10.0

    _update_credit({"credit_bytes": 2000, "recommended_delay_seconds": 5.0})
    assert ingest._credit_bytes      == 2000
    assert ingest._recommended_delay == 5.0

    _update_credit({"credit_bytes": 3000, "recommended_delay_seconds": 0.0})
    assert ingest._credit_bytes == 3000  # overwritten, not 2000 + 3000
    assert ingest._recommended_delay == 0.0


# ── _drain_backlog ─────────────────────────────────────────────────────────────

async def test_drain_backlog_exits_immediately_when_empty(tmp_db):
    """No queued rows → drain returns without touching the server."""
    ingest._credit_bytes = 10_000

    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}), \
         patch("jobs.ingest._try_post", new_callable=AsyncMock) as mock_post:
        await _drain_backlog()

    mock_post.assert_not_called()


async def test_drain_backlog_exits_when_no_credit(tmp_db):
    """credit_bytes=0 → no drain even when rows exist."""
    queue.enqueue({"co2": 400}, "2026-06-23T10:00:00+00:00")
    ingest._credit_bytes = 0

    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}), \
         patch("jobs.ingest._try_post", new_callable=AsyncMock) as mock_post:
        await _drain_backlog()

    mock_post.assert_not_called()
    assert queue.count_pending() == 1


async def test_drain_backlog_sends_and_removes_rows(tmp_db):
    """With sufficient credit, queued rows are POSTed and removed from SQLite."""
    queue.enqueue({"co2": 400}, "2026-06-23T10:00:00+00:00")
    queue.enqueue({"co2": 410}, "2026-06-23T10:05:00+00:00")
    ingest._credit_bytes = 100_000  # generous credit

    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}), \
         patch("jobs.ingest._try_post", new_callable=AsyncMock,
               return_value={"credit_bytes": 0, "recommended_delay_seconds": 0}), \
         patch("jobs.ingest._handle_response", new_callable=AsyncMock):
        await _drain_backlog()

    assert queue.count_pending() == 0


async def test_drain_backlog_holds_rows_on_server_error(tmp_db):
    """When _try_post returns None, rows remain pending for retry."""
    queue.enqueue({"co2": 400}, "2026-06-23T10:00:00+00:00")
    ingest._credit_bytes = 10_000

    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}), \
         patch("jobs.ingest._try_post", new_callable=AsyncMock, return_value=None):
        await _drain_backlog()

    assert queue.count_pending() == 1


# ── _handle_response ──────────────────────────────────────────────────────────

async def test_handle_response_saves_criteria():
    """Criteria list in response is persisted to disk."""
    criteria = [{"metric": "co2", "threshold": 1000, "condition": "above", "severity": "warning"}]
    with patch("jobs.ingest.save_criteria") as mock_save, \
         patch("jobs.ingest._drain_alerts", new_callable=AsyncMock):
        await _handle_response({"criteria": criteria})
    mock_save.assert_called_once_with(criteria)


async def test_handle_response_schedules_update_when_flagged():
    """update_available=True causes a task to be scheduled for _trigger_update."""
    tasks = []
    with patch("jobs.ingest._drain_alerts", new_callable=AsyncMock), \
         patch("asyncio.create_task", side_effect=tasks.append):
        await _handle_response({"update_available": True})
    assert len(tasks) >= 1


async def test_handle_response_no_update_when_flag_false():
    """update_available=False means no update task is created."""
    tasks = []
    with patch("jobs.ingest._drain_alerts", new_callable=AsyncMock), \
         patch("asyncio.create_task", side_effect=tasks.append):
        await _handle_response({"update_available": False})
    assert len(tasks) == 0


# ── _upload_loop queues on missing token ──────────────────────────────────────

async def test_upload_loop_queues_to_sqlite_when_no_token(tmp_db, monkeypatch):
    """When no auth token, live reading is stored in SQLite without an HTTP call."""
    monkeypatch.setenv("NEW_AUTH_TOKEN", "")

    entry = {
        "data": {"sen6x": {"co2": 400}},
        "recorded_at": "2026-06-23T10:00:00+00:00",
        "severity": 0,
    }
    from jobs.ingest import _upload_loop

    task = asyncio.create_task(_upload_loop(S))
    await asyncio.sleep(0)  # let the loop start and create _live_event

    ingest._pending_live = entry
    ingest._live_event.set()

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert queue.count_pending() == 1


# ── OTA update ────────────────────────────────────────────────────────────────

def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), \
        f"VERSION must be major.minor.patch, got {VERSION!r}"

def test_auth_headers_include_version():
    with patch.dict("os.environ", {"NEW_AUTH_TOKEN": "testtoken"}):
        headers = _auth_headers()
    assert "X-Schoolair-Version" in headers
    assert headers["X-Schoolair-Version"] == VERSION

def test_auth_headers_include_bearer():
    with patch.dict("os.environ", {"NEW_AUTH_TOKEN": "tok123"}):
        headers = _auth_headers()
    assert headers["Authorization"] == "Bearer tok123"


async def test_trigger_update_guard_skips_subprocess_when_already_running():
    """Second call while an update is in flight must not spawn a second process."""
    ingest._update_in_progress = True
    try:
        mock_exec = AsyncMock()
        with patch("asyncio.create_subprocess_exec", mock_exec):
            await _trigger_update()
        mock_exec.assert_not_called()
    finally:
        ingest._update_in_progress = False


async def test_trigger_update_resets_flag_after_success():
    """`_update_in_progress` must be False after a successful subprocess run."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"ok", b"")
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await _trigger_update()
    assert ingest._update_in_progress is False


async def test_trigger_update_resets_flag_after_subprocess_failure():
    """`_update_in_progress` must be False even when the update script exits non-zero."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"fatal error", b"")
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await _trigger_update()
    assert ingest._update_in_progress is False


# ── Upload offset / drain jitter ───────────────────────────────────────────────

def test_get_upload_offset_prefers_env_var(tmp_path, monkeypatch):
    """UPLOAD_OFFSET env var takes precedence over jitter in settings."""
    monkeypatch.setenv("UPLOAD_OFFSET", "77")
    settings = {"active_window": {"start": "07:00", "end": "16:00"}}
    assert _get_upload_offset(settings) == 77


def test_get_upload_offset_falls_back_to_jitter(tmp_path, monkeypatch):
    """When UPLOAD_OFFSET is absent, the jitter from settings is used."""
    monkeypatch.setenv("UPLOAD_OFFSET", "")
    settings_file = tmp_path / "config" / "settings.json"
    monkeypatch.setattr(ingest, "SETTINGS_PATH", settings_file)
    settings = {"active_window": {"start": "07:00", "end": "16:00"},
                "drain_jitter_seconds": 42}
    assert _get_upload_offset(settings) == 42


def test_ensure_drain_jitter_generates_and_saves_when_absent(tmp_path, monkeypatch):
    """When drain_jitter_seconds is missing, a value is generated and persisted."""
    settings_file = tmp_path / "config" / "settings.json"
    monkeypatch.setattr(ingest, "SETTINGS_PATH", settings_file)

    settings = {"active_window": {"start": "07:00", "end": "16:00"}}
    jitter = _ensure_drain_jitter(settings)

    assert 0 <= jitter <= DRAIN_JITTER_MAX
    assert settings["drain_jitter_seconds"] == jitter
    saved = json.loads(settings_file.read_text())
    assert saved["drain_jitter_seconds"] == jitter


def test_ensure_drain_jitter_is_stable_across_calls(tmp_path, monkeypatch):
    """The same value is returned on repeated calls — no re-randomisation."""
    settings_file = tmp_path / "config" / "settings.json"
    monkeypatch.setattr(ingest, "SETTINGS_PATH", settings_file)

    settings = {"active_window": {"start": "07:00", "end": "16:00"}}
    first  = _ensure_drain_jitter(settings)
    second = _ensure_drain_jitter(settings)
    assert first == second


def test_ensure_drain_jitter_honours_existing_value(tmp_path, monkeypatch):
    """When drain_jitter_seconds is already present it is used as-is and the
    file is not rewritten (server-assigned slots must never be overwritten)."""
    settings_file = tmp_path / "config" / "settings.json"
    monkeypatch.setattr(ingest, "SETTINGS_PATH", settings_file)

    settings = {"active_window": {"start": "07:00", "end": "16:00"},
                "drain_jitter_seconds": 42}
    jitter = _ensure_drain_jitter(settings)

    assert jitter == 42
    assert not settings_file.exists(), "file must not be written when value already present"
