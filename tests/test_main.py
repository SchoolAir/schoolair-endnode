"""tests/test_main.py

Unit tests for main.py: _graceful_shutdown flushes the in-flight live
reading and alert buffer to SQLite before cancelling asyncio tasks (SIGTERM).
"""

from unittest.mock import patch

import pytest
import db.queue as queue
import jobs.ingest as ingest
import main


@pytest.fixture(autouse=True)
def clean_state():
    ingest._pending_live = None
    ingest._alert_buffer.clear()
    yield
    ingest._pending_live = None
    ingest._alert_buffer.clear()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "DB_PATH", tmp_path / "test_queue.db")
    queue.init()


async def test_graceful_shutdown_flushes_pending_live(tmp_db):
    ts = "2026-06-23T10:00:00+00:00"
    ingest._pending_live = {"data": {"sen6x": {"co2": 400}}, "recorded_at": ts}

    with patch("asyncio.all_tasks", return_value=[]):
        await main._graceful_shutdown()

    assert ingest._pending_live is None
    assert queue.count_pending() == 1


async def test_graceful_shutdown_flushes_alert_buffer(tmp_db):
    ts = "2026-06-23T10:00:00+00:00"
    ingest._alert_buffer.append({"metric": "co2", "recorded_at": ts})

    with patch("asyncio.all_tasks", return_value=[]):
        await main._graceful_shutdown()

    assert ingest._alert_buffer == []
    assert len(queue.get_pending_alerts()) == 1


async def test_graceful_shutdown_flushes_both(tmp_db):
    ts = "2026-06-23T10:00:00+00:00"
    ingest._pending_live = {"data": {"sen6x": {"co2": 400}}, "recorded_at": ts}
    ingest._alert_buffer.append({"metric": "co2", "recorded_at": ts})

    with patch("asyncio.all_tasks", return_value=[]):
        await main._graceful_shutdown()

    assert ingest._pending_live is None
    assert ingest._alert_buffer == []
    assert queue.count_pending() == 1
    assert len(queue.get_pending_alerts()) == 1


async def test_graceful_shutdown_empty_state_is_noop(tmp_db):
    """Calling _graceful_shutdown with nothing buffered must not raise."""
    with patch("asyncio.all_tasks", return_value=[]):
        await main._graceful_shutdown()

    assert queue.count_pending() == 0
    assert queue.get_pending_alerts() == []
