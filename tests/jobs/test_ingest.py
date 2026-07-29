"""tests/jobs/test_ingest.py

Unit tests for jobs.ingest: scheduling intervals, breach detection,
alert buffering, trigger_drain event, drain token guard, and shared
verification task with severity scoring.

Run laptop-safe tests only:
    pytest -m "not hardware"
"""

import asyncio
import json
import re
from datetime import time, datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

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
    _run_drain,
    _run_read,
    _should_drain,
    trigger_drain,
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
    ingest._buffer.clear()
    ingest._verifying.clear()
    ingest.alert_cooldown.clear()
    ingest._last_drained_at = None
    yield
    ingest._alert_buffer.clear()
    ingest._buffer.clear()
    ingest._last_drained_at = None


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
    """5 s past a 300 s boundary → sleep 295 s to the next :05 mark."""
    t = datetime(2026, 1, 1, 8, 0, 5, tzinfo=timezone.utc)  # 8:00:05
    assert _seconds_to_next_boundary(300, now=t) == pytest.approx(295.0, abs=0.01)

def test_next_boundary_exactly_on_boundary():
    """Exactly on a 300 s boundary → sleep the full 300 s to the next one."""
    t = datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)  # 8:00:00
    assert _seconds_to_next_boundary(300, now=t) == pytest.approx(300.0, abs=0.01)

def test_next_boundary_idle_interval():
    """5 s past a 900 s boundary (7:45:05) → sleep 895 s to land at 8:00:00."""
    t = datetime(2026, 1, 1, 7, 45, 5, tzinfo=timezone.utc)
    assert _seconds_to_next_boundary(900, now=t) == pytest.approx(895.0, abs=0.01)


# ── Window helpers ─────────────────────────────────────────────────────────────

def test_window_hours_handles_midnight():
    assert _window_hours({"start": "22:00", "end": "06:00"}) == 8
    assert _window_hours({"start": "07:00", "end": "16:00"}) == 9

def test_validate_rejects_long_window():
    bad = {**S, "active_window": {"start": "06:00", "end": "20:00"}}  # 14 h
    with pytest.raises(SystemExit):
        validate_settings(bad)


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


# ── trigger_drain ──────────────────────────────────────────────────────────────

def test_trigger_drain_noop_before_event_created():
    """trigger_drain must not raise when _drain_trigger is None."""
    original = ingest._drain_trigger
    ingest._drain_trigger = None
    try:
        trigger_drain()
    finally:
        ingest._drain_trigger = original


async def test_trigger_drain_sets_event():
    original = ingest._drain_trigger
    ingest._drain_trigger = asyncio.Event()
    try:
        assert not ingest._drain_trigger.is_set()
        trigger_drain()
        assert ingest._drain_trigger.is_set()
    finally:
        ingest._drain_trigger = original


# ── _run_drain token guard ─────────────────────────────────────────────────────

async def test_run_drain_holds_buffer_in_memory_when_no_token(tmp_db, monkeypatch):
    """Buffer below capacity stays in memory when AUTH_TOKEN is absent."""
    monkeypatch.setenv("NEW_AUTH_TOKEN", "")
    ingest._buffer.append({
        "data": {"sen6x": {"co2": 400}},
        "recorded_at": "2026-06-23T10:00:00+00:00",
    })
    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}):
        await _run_drain(S)

    assert len(ingest._buffer) == 1
    assert queue.count_pending() == 0


async def test_run_drain_flushes_to_sqlite_when_full_and_no_token(tmp_db, monkeypatch):
    """Buffer at capacity is flushed to SQLite when AUTH_TOKEN is absent."""
    monkeypatch.setenv("NEW_AUTH_TOKEN", "")
    monkeypatch.setattr(ingest, "BUFFER_CAPACITY", 5)
    entry = {
        "data": {"sen6x": {"co2": 400}},
        "recorded_at": "2026-06-23T10:00:00+00:00",
    }
    for _ in range(5):
        ingest._buffer.append(entry)

    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}):
        await _run_drain(S)

    assert ingest._buffer == []
    assert queue.count_pending() == 5


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


async def test_verify_all_momentary_severity_and_r3_buffered():
    """Stage 1 both high, stage 2 both low → momentary, T+1m added to buffer."""
    entry = _breach_entry()
    ingest._verifying.update(["co2"])

    reads = [_high_read(), _high_read(), _low_read(), _low_read()]
    with patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("jobs.ingest.read_sensor", side_effect=reads), \
         patch("jobs.ingest.state"):
        await _verify_all([("co2", _CO2_CRITERION)], entry)

    # severity = 1 (baseline) + 1 (r1 high) + 1 (r2 high) = 3
    assert entry["severity"] == 3
    assert len(ingest._buffer) == 1, "T+1m read must be added to buffer for momentary event"
    assert ingest._buffer[0]["data"] == _low_read()


async def test_verify_all_alert_sends_and_triggers_drain():
    """Stage 1 both high, stage 2 one high → severity>=4, alert sent, drain triggered."""
    entry = _breach_entry()
    ingest._verifying.update(["co2"])

    reads = [_high_read(), _high_read(), _high_read(), _low_read()]
    drain_calls = []
    with patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("jobs.ingest.read_sensor", side_effect=reads), \
         patch("jobs.ingest.state"), \
         patch("jobs.ingest.trigger_drain", side_effect=lambda: drain_calls.append(1)), \
         patch("jobs.ingest._send_or_queue_alert", new_callable=AsyncMock) as mock_send, \
         patch.dict("os.environ", {"NEW_AUTH_TOKEN": "tok"}):
        await _verify_all([("co2", _CO2_CRITERION)], entry)

    # severity = 1 + 1 + 1 + 2 = 5
    assert entry["severity"] == 5
    assert mock_send.call_count == 1
    assert drain_calls, "trigger_drain must be called after persistent breach"
    assert not ingest._buffer, "no verification reads should be in buffer for alert outcome"


async def test_verify_all_clears_verifying_on_sensor_error():
    """Even when a sensor read fails mid-task, _verifying must be cleared."""
    entry = _breach_entry()
    ingest._verifying.add("co2")

    with patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("jobs.ingest.read_sensor", side_effect=RuntimeError("sensor off")), \
         patch("jobs.ingest.state"):
        await _verify_all([("co2", _CO2_CRITERION)], entry)

    assert not ingest._verifying


# ── Read-triggered drain ───────────────────────────────────────────────────────

async def test_run_read_triggers_drain_when_interval_elapsed(monkeypatch):
    """A standard read triggers drain once the drain interval has elapsed."""
    ingest._last_drained_at = (
        datetime.now(timezone.utc) - timedelta(seconds=DRAIN_IDLE + 10)
    )
    triggered = []
    monkeypatch.setattr(ingest, "trigger_drain", lambda: triggered.append(1))

    with patch("jobs.ingest.read_sensor", return_value={"sen6x": {"co2": 400}}), \
         patch("jobs.ingest.load_criteria", return_value=[]), \
         patch("jobs.ingest.state"):
        await _run_read(S, [])

    assert triggered, "trigger_drain should be called after drain interval elapses"


async def test_run_read_does_not_trigger_drain_before_interval(monkeypatch):
    """A read does not trigger drain when the interval has not yet elapsed."""
    ingest._last_drained_at = datetime.now(timezone.utc)  # just drained
    triggered = []
    monkeypatch.setattr(ingest, "trigger_drain", lambda: triggered.append(1))

    with patch("jobs.ingest.read_sensor", return_value={"sen6x": {"co2": 400}}), \
         patch("jobs.ingest.load_criteria", return_value=[]), \
         patch("jobs.ingest.state"):
        await _run_read(S, [])

    assert not triggered, "trigger_drain must not be called before drain interval"


async def test_run_read_does_not_trigger_drain_during_verification(monkeypatch):
    """During alert verification _verifying is non-empty; drain must be suppressed."""
    ingest._last_drained_at = (
        datetime.now(timezone.utc) - timedelta(seconds=DRAIN_IDLE + 10)
    )
    ingest._verifying.add("co2")
    triggered = []
    monkeypatch.setattr(ingest, "trigger_drain", lambda: triggered.append(1))

    with patch("jobs.ingest.read_sensor", return_value={"sen6x": {"co2": 400}}), \
         patch("jobs.ingest.load_criteria", return_value=[]), \
         patch("jobs.ingest.state"):
        await _run_read(S, [])

    assert not triggered, "trigger_drain must be suppressed during alert verification"


async def test_run_read_does_not_trigger_drain_on_sensor_error(monkeypatch):
    """A failed sensor read returns early without touching the drain logic."""
    ingest._last_drained_at = (
        datetime.now(timezone.utc) - timedelta(seconds=DRAIN_IDLE + 10)
    )
    triggered = []
    monkeypatch.setattr(ingest, "trigger_drain", lambda: triggered.append(1))

    with patch("jobs.ingest.read_sensor", side_effect=RuntimeError("sensor off")):
        await _run_read(S, [])

    assert not triggered, "trigger_drain must not be called when sensor read fails"
    assert ingest._buffer == [], "nothing should be buffered on sensor error"


def test_should_drain_false_when_never_drained():
    """_should_drain returns False at startup (initial drain handled by _drain_loop)."""
    assert ingest._last_drained_at is None
    assert _should_drain(S) is False


def test_should_drain_true_after_interval_elapsed():
    """_should_drain returns True once the drain interval has passed."""
    ingest._last_drained_at = (
        datetime.now(timezone.utc) - timedelta(seconds=DRAIN_IDLE + 1)
    )
    assert _should_drain(S) is True


def test_should_drain_false_before_interval_elapsed():
    """_should_drain returns False when the drain interval has not yet passed."""
    ingest._last_drained_at = datetime.now(timezone.utc)
    assert _should_drain(S) is False


def test_should_drain_triggers_one_read_before_deadline():
    """Drain fires when exactly drain_interval - read_interval seconds have elapsed.

    The criterion is: 'would skipping this read cause the deadline to be missed?'
    At elapsed = drain_interval - read_interval the answer is yes (the next read
    would land after the deadline), so _should_drain must return True here.
    """
    ingest._last_drained_at = (
        datetime.now(timezone.utc) - timedelta(seconds=DRAIN_IDLE - READ_IDLE)
    )
    assert _should_drain(S) is True


def test_should_drain_false_one_second_before_deadline():
    """One second before the trigger point, _should_drain is still False."""
    ingest._last_drained_at = (
        datetime.now(timezone.utc) - timedelta(seconds=DRAIN_IDLE - READ_IDLE - 1)
    )
    assert _should_drain(S) is False


def test_validate_rejects_non_quarter_hour_start():
    """validate_settings exits when the window start minute is not :00/:15/:30/:45."""
    bad = {**S, "active_window": {"start": "07:10", "end": "16:00"}}
    with pytest.raises(SystemExit):
        validate_settings(bad)


def test_validate_rejects_non_quarter_hour_end():
    """validate_settings exits when the window end minute is not :00/:15/:30/:45."""
    bad = {**S, "active_window": {"start": "07:00", "end": "16:05"}}
    with pytest.raises(SystemExit):
        validate_settings(bad)


def test_validate_accepts_quarter_hour_boundaries():
    """validate_settings passes for :00/:15/:30/:45 boundaries."""
    for start, end in [("07:00", "16:00"), ("07:15", "15:45"), ("08:30", "15:30")]:
        validate_settings({**S, "active_window": {"start": start, "end": end}})


# ── Option 1: buffer slice on POST success ─────────────────────────────────────

async def test_run_drain_does_not_discard_reading_added_during_post(tmp_db, monkeypatch):
    """Reading appended to _buffer during the async POST is preserved after drain."""
    monkeypatch.setenv("NEW_AUTH_TOKEN", "tok")
    ingest._last_drained_at = datetime.now(timezone.utc) - timedelta(hours=1)

    initial_entry = {"data": {"sen6x": {"co2": 400}}, "recorded_at": "2026-06-23T10:00:00+00:00"}
    concurrent_entry = {"data": {"sen6x": {"co2": 410}}, "recorded_at": "2026-06-23T10:05:00+00:00"}
    ingest._buffer.append(initial_entry)

    async def fake_post(client, measurements):
        # Simulate a reading arriving during the HTTP call
        ingest._buffer.append(concurrent_entry)
        return {}

    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}), \
         patch("jobs.ingest._post_batch", new=fake_post), \
         patch("jobs.ingest._drain_alerts"):
        await _run_drain(S)

    assert ingest._buffer == [concurrent_entry], (
        "the reading added during POST must survive — only the pre-POST entries are cleared"
    )

# ── OTA update / version ───────────────────────────────────────────────────────

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
    """`_update_in_progress` must be False even when the update script exits non-zero.

    A stuck True would silently suppress all future OTA signals until the
    process is restarted.
    """
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"fatal error", b"")
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await _trigger_update()
    assert ingest._update_in_progress is False


async def test_run_drain_fires_trigger_update_when_update_available(tmp_db, monkeypatch):
    """`_run_drain` must schedule `_trigger_update` when the server flags update_available."""
    monkeypatch.setenv("NEW_AUTH_TOKEN", "tok")
    ingest._last_drained_at = datetime.now(timezone.utc) - timedelta(hours=1)
    ingest._buffer.append({"data": {"sen6x": {"co2": 400}}, "recorded_at": "2026-06-23T10:00:00+00:00"})

    async def fake_post(client, measurements):
        return {"update_available": True}

    trigger_mock = AsyncMock()
    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}), \
         patch("jobs.ingest._post_batch", new=fake_post), \
         patch("jobs.ingest._drain_alerts"), \
         patch("jobs.ingest._trigger_update", trigger_mock):
        await _run_drain(S)

    trigger_mock.assert_called_once()


async def test_run_drain_skips_trigger_update_when_flag_false(tmp_db, monkeypatch):
    monkeypatch.setenv("NEW_AUTH_TOKEN", "tok")
    ingest._last_drained_at = datetime.now(timezone.utc) - timedelta(hours=1)
    ingest._buffer.append({"data": {"sen6x": {"co2": 400}}, "recorded_at": "2026-06-23T10:00:00+00:00"})

    async def fake_post(client, measurements):
        return {"update_available": False}

    trigger_mock = AsyncMock()
    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}), \
         patch("jobs.ingest._post_batch", new=fake_post), \
         patch("jobs.ingest._drain_alerts"), \
         patch("jobs.ingest._trigger_update", trigger_mock):
        await _run_drain(S)

    trigger_mock.assert_not_called()


async def test_run_drain_skips_trigger_update_when_key_absent(tmp_db, monkeypatch):
    monkeypatch.setenv("NEW_AUTH_TOKEN", "tok")
    ingest._last_drained_at = datetime.now(timezone.utc) - timedelta(hours=1)
    ingest._buffer.append({"data": {"sen6x": {"co2": 400}}, "recorded_at": "2026-06-23T10:00:00+00:00"})

    async def fake_post(client, measurements):
        return {}

    trigger_mock = AsyncMock()
    with patch("jobs.ingest.aggregate.run_aggregation",
               return_value={"buckets": 0, "rows_in": 0, "rows_removed": 0}), \
         patch("jobs.ingest._post_batch", new=fake_post), \
         patch("jobs.ingest._drain_alerts"), \
         patch("jobs.ingest._trigger_update", trigger_mock):
        await _run_drain(S)

    trigger_mock.assert_not_called()


# ── Drain jitter ───────────────────────────────────────────────────────────────

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
    """When drain_jitter_seconds is already present, it is used as-is and the
    file is not rewritten (server-assigned slots must never be overwritten)."""
    settings_file = tmp_path / "config" / "settings.json"
    monkeypatch.setattr(ingest, "SETTINGS_PATH", settings_file)

    settings = {"active_window": {"start": "07:00", "end": "16:00"},
                "drain_jitter_seconds": 42}
    jitter = _ensure_drain_jitter(settings)

    assert jitter == 42
    assert not settings_file.exists(), "file must not be written when value already present"
