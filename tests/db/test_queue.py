"""tests/db/test_queue.py

Unit tests for db.queue — measurements queue and alerts queue.
All tests run on laptop (no hardware required).
Uses tmp_db fixture from conftest.py.
"""

import json
from datetime import datetime, timezone, timedelta

import pytest
import db.queue as queue


TS  = datetime.now(timezone.utc).isoformat()   # "recent" — must stay above any aggregation cutoff
OLD = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
DATA = {"co2": 400, "temp": 22.5}


# ── Initialisation ─────────────────────────────────────────────────────────────

def test_init_is_idempotent(tmp_db):
    queue.init()  # second call must not raise or corrupt


def test_init_resets_sending_status_to_pending(tmp_db):
    queue.enqueue(DATA, TS)
    ids = [r["id"] for r in queue.get_pending()]
    queue.set_status_many(ids, "sending")
    assert queue.count_pending() == 0

    queue.init()  # simulates a service restart mid-drain
    assert queue.count_pending() == 1


# ── Measurements queue ─────────────────────────────────────────────────────────

def test_enqueue_and_get_pending(tmp_db):
    queue.enqueue({"co2": 100}, TS)
    queue.enqueue({"co2": 200}, TS)
    rows = queue.get_pending()
    assert len(rows) == 2
    assert json.loads(rows[0]["data"])["co2"] == 100
    assert json.loads(rows[1]["data"])["co2"] == 200


def test_get_pending_returns_in_insertion_order(tmp_db):
    for i in range(5):
        queue.enqueue({"seq": i}, TS)
    rows = queue.get_pending()
    seqs = [json.loads(r["data"])["seq"] for r in rows]
    assert seqs == list(range(5))


def test_get_pending_respects_limit(tmp_db):
    for _ in range(10):
        queue.enqueue(DATA, TS)
    assert len(queue.get_pending(limit=3)) == 3


def test_count_pending(tmp_db):
    queue.enqueue(DATA, TS)
    queue.enqueue(DATA, TS)
    assert queue.count_pending() == 2


def test_set_status_many_marks_as_sending(tmp_db):
    queue.enqueue(DATA, TS)
    queue.enqueue(DATA, TS)
    ids = [r["id"] for r in queue.get_pending()]
    queue.set_status_many([ids[0]], "sending")
    assert queue.count_pending() == 1  # one is now 'sending'


def test_remove_many_deletes_specified_rows(tmp_db):
    queue.enqueue(DATA, TS)
    queue.enqueue(DATA, TS)
    ids = [r["id"] for r in queue.get_pending()]
    queue.remove_many([ids[0]])
    assert queue.count_pending() == 1


def test_remove_many_empty_list_is_noop(tmp_db):
    queue.enqueue(DATA, TS)
    queue.remove_many([])
    assert queue.count_pending() == 1


# ── Aggregation helpers ────────────────────────────────────────────────────────

def test_get_aggregatable_returns_only_old_unaggregated_rows(tmp_db):
    queue.enqueue(DATA, OLD)
    queue.enqueue(DATA, TS)  # recent row — should stay out
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    rows = queue.get_aggregatable(cutoff)
    assert len(rows) == 1
    assert rows[0]["recorded_at"] == OLD


def test_fold_bucket_rewrites_keeper_drops_others(tmp_db):
    ts1 = (datetime.now(timezone.utc) - timedelta(days=20, minutes=10)).isoformat()
    ts2 = (datetime.now(timezone.utc) - timedelta(days=20, minutes=40)).isoformat()
    queue.enqueue({"temp": 20.0}, ts1)
    queue.enqueue({"temp": 24.0}, ts2)
    rows = queue.get_pending()
    ids  = [r["id"] for r in rows]
    bucket_start = (datetime.now(timezone.utc) - timedelta(days=20)).replace(
        minute=0, second=0, microsecond=0
    )
    queue.fold_bucket(ids[0], {"temp": 22.0}, bucket_start.isoformat(), [ids[1]])
    result = queue.get_pending()
    assert len(result) == 1
    assert json.loads(result[0]["data"])["temp"] == 22.0
    assert result[0]["is_aggregated"] == 1
    assert result[0]["recorded_at"] == bucket_start.isoformat()


def test_trim_aggregated_only_removes_aggregated_rows(tmp_db):
    queue.enqueue(DATA, OLD)
    rows = queue.get_pending()
    # fold it (makes it aggregated)
    queue.fold_bucket(rows[0]["id"], DATA, OLD, [])
    queue.enqueue(DATA, OLD)  # a fresh raw row
    assert queue.count_pending() == 2
    trimmed = queue.trim_aggregated(1)
    assert trimmed == 1
    assert queue.count_pending() == 1
    # surviving row must be the raw one
    assert queue.get_pending()[0]["is_aggregated"] == 0


def test_trim_aggregated_zero_count_is_noop(tmp_db):
    queue.enqueue(DATA, OLD)
    rows = queue.get_pending()
    queue.fold_bucket(rows[0]["id"], DATA, OLD, [])
    assert queue.trim_aggregated(0) == 0
    assert queue.count_pending() == 1


# ── Alert queue ────────────────────────────────────────────────────────────────

def test_enqueue_alert_and_get_pending_alerts(tmp_db):
    alert = {"metric": "co2", "value": 1500, "threshold": 800}
    queue.enqueue_alert(alert, TS)
    rows = queue.get_pending_alerts()
    assert len(rows) == 1
    assert json.loads(rows[0]["data"])["metric"] == "co2"


def test_alerts_returned_in_insertion_order(tmp_db):
    for metric in ("co2", "pm25", "voc"):
        queue.enqueue_alert({"metric": metric}, TS)
    rows = queue.get_pending_alerts()
    assert [json.loads(r["data"])["metric"] for r in rows] == ["co2", "pm25", "voc"]


def test_set_alert_status_many(tmp_db):
    queue.enqueue_alert({"metric": "co2"}, TS)
    queue.enqueue_alert({"metric": "pm25"}, TS)
    rows = queue.get_pending_alerts()
    queue.set_alert_status_many([rows[0]["id"]], "sending")
    pending = queue.get_pending_alerts()
    assert len(pending) == 1
    assert json.loads(pending[0]["data"])["metric"] == "pm25"


def test_remove_alerts(tmp_db):
    queue.enqueue_alert({"metric": "co2"}, TS)
    rows = queue.get_pending_alerts()
    queue.remove_alerts([rows[0]["id"]])
    assert queue.get_pending_alerts() == []


def test_alert_init_resets_sending_to_pending(tmp_db):
    queue.enqueue_alert({"metric": "co2"}, TS)
    rows = queue.get_pending_alerts()
    queue.set_alert_status_many([rows[0]["id"]], "sending")
    assert queue.get_pending_alerts() == []

    queue.init()
    assert len(queue.get_pending_alerts()) == 1
