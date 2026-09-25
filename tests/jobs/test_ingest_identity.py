"""tests/jobs/test_ingest_identity.py

ingest_loop() checks the card belongs to this Pi (device_identity.enforce)
before its first server contact — the boot ping. On a mismatch, readings go on
but nothing is sent: no ping, no upload, no alert; readings stay in SQLite and
the wizard is started (it brings up the AP).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jobs.ingest as ingest


@pytest.fixture(autouse=True)
def unlocked():
    ingest._identity_locked = False
    ingest._pending_live = None
    ingest._alert_buffer.clear()
    yield
    ingest._identity_locked = False
    ingest._pending_live = None
    ingest._alert_buffer.clear()


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(ingest, "_PRIMARY_SERVER_URL", "http://server")
    monkeypatch.setenv("NEW_AUTH_TOKEN", "tok")
    ingest._identity_locked = True


async def test_locked_ping_contacts_nothing(locked):
    with patch("httpx.AsyncClient") as client:
        assert await ingest._startup_connectivity_ping() is False
    client.assert_not_called()


async def test_locked_try_post_contacts_nothing(locked):
    with patch("httpx.AsyncClient") as client:
        assert await ingest._try_post([{"x": 1}], 0) is None
    client.assert_not_called()


async def test_locked_alert_is_buffered_not_sent(locked):
    criterion = {"threshold": 10, "condition": "above", "severity": 2}
    entry = {"data": {}, "recorded_at": "2026-09-25T10:00:00+00:00"}
    with patch("httpx.AsyncClient") as client, \
         patch("jobs.ingest.extract_metric", return_value=50.0):
        await ingest._send_or_queue_alert("pm25", criterion, entry,
                                          ingest.datetime.now(ingest.timezone.utc),
                                          {}, {}, {}, None)
    client.assert_not_called()
    assert len(ingest._alert_buffer) == 1


async def test_locked_upload_loop_keeps_reading_in_sqlite(locked, monkeypatch):
    enqueue = MagicMock()
    monkeypatch.setattr(ingest.queue, "enqueue", enqueue)
    try_post = AsyncMock()
    monkeypatch.setattr(ingest, "_try_post", try_post)

    task = asyncio.create_task(ingest._upload_loop())
    await asyncio.sleep(0)  # let it create _live_event
    ingest._pending_live = {"data": {"pm25": 3}, "recorded_at": "t"}
    ingest._live_event.set()
    await asyncio.sleep(0.01)
    task.cancel()

    enqueue.assert_called_once_with({"pm25": 3}, "t")
    try_post.assert_not_awaited()


async def test_enter_lockout_locks_sets_led_and_starts_wizard(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "LED_STATE_FILE", str(tmp_path / "led"))
    proc = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await ingest._enter_identity_lockout()
    assert ingest._identity_locked is True
    assert (tmp_path / "led").read_text() == "ap"
    assert spawn.await_args.args == ("sudo", "-n", "systemctl", "start", "schoolair-wizard")


async def test_enter_lockout_survives_wizard_start_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "LED_STATE_FILE", str(tmp_path / "led"))
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("no sudo"))):
        await ingest._enter_identity_lockout()
    assert ingest._identity_locked is True


@pytest.fixture
def boot(monkeypatch, tmp_path):
    """ingest_loop() with everything but the identity decision stubbed out."""
    monkeypatch.setattr(ingest.queue, "init", MagicMock())
    monkeypatch.setattr(ingest, "load_settings", lambda: {"active_window": {"start": "07:00", "end": "16:00"}})
    monkeypatch.setattr(ingest, "probe_aux_sensors", lambda: [])
    for loop in ("_read_loop", "_upload_loop", "_ntp_correction_task"):
        monkeypatch.setattr(ingest, loop, AsyncMock())
    ping = AsyncMock()
    monkeypatch.setattr(ingest, "_connectivity_ping_loop", ping)
    lockout = AsyncMock()
    monkeypatch.setattr(ingest, "_enter_identity_lockout", lockout)
    return ping, lockout


async def test_boot_match_pings_server(boot, monkeypatch):
    ping, lockout = boot
    monkeypatch.setattr(ingest.device_identity, "enforce", lambda: True)
    await ingest.ingest_loop()
    await asyncio.sleep(0)
    ping.assert_awaited_once()
    lockout.assert_not_awaited()


async def test_boot_mismatch_locks_out_and_never_pings(boot, monkeypatch):
    ping, lockout = boot
    monkeypatch.setattr(ingest.device_identity, "enforce", lambda: False)
    await ingest.ingest_loop()
    await asyncio.sleep(0)
    lockout.assert_awaited_once()
    ping.assert_not_called()
    ingest._read_loop.assert_awaited_once()   # readings still go on
