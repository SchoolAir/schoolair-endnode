"""db/queue.py

Manages two local SQLite queues:
  measurements_queue — readings that failed to send during a server outage.
  alerts_queue       — confirmed alerts waiting to be POSTed to the server.

On startup, resets any rows stuck in 'sending' back to 'pending' for retry.
"""

import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path


DB_PATH = Path("queue.db")
DB_MAX_SEND = 500 # max rows returned from `get_pending`


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init():
    """Create table and reset any stuck 'sending' rows on startup."""
    with _connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS measurements_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                data        TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                is_aggregated INTEGER NOT NULL DEFAULT 0
            )
        """)
        
        # Speeds up both get_pending and get_aggregatable, esp. during a long
        # outage when the queue grows large.
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_queue_status_recorded
            ON measurements_queue (status, recorded_at)
        """)
        
        con.execute("""
            CREATE TABLE IF NOT EXISTS alerts_queue (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                data         TEXT NOT NULL,
                triggered_at TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending'
            )
        """)

        # Reset rows stuck mid-send on last run back to 'pending'
        con.execute("""
            UPDATE measurements_queue
            SET status = 'pending'
            WHERE status = 'sending'
        """)
        con.execute("""
            UPDATE alerts_queue
            SET status = 'pending'
            WHERE status = 'sending'
        """)


def enqueue(data: dict, recorded_at: str):
    """Add a failed measurement to the queue for later retry."""
    with _connect() as con:
        con.execute(
            "INSERT INTO measurements_queue (data, recorded_at) VALUES (?, ?)",
            (json.dumps(data), recorded_at)
        )


def get_pending(limit: int = DB_MAX_SEND) -> list[sqlite3.Row]:
    """Fetch up to `limit` pending measurements."""
    with _connect() as con:
        return con.execute(
            "SELECT * FROM measurements_queue WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
            (limit,)
        ).fetchall()


def count_pending() -> int:
    """Return number of pending measurements in the queue."""
    with _connect() as con:
        row = con.execute(
            "SELECT COUNT(*) as count FROM measurements_queue WHERE status = 'pending'"
        ).fetchone()
        return row["count"]


def get_aggregatable(before: str) -> list[sqlite3.Row]:
    """Pending, not-yet-aggregated rows recorded before the given ISO timestamp.
 
    Coarse pre-filter only: the caller applies the precise per-bucket cutoff.
    """
    with _connect() as con:
        return con.execute(
            """SELECT * FROM measurements_queue
               WHERE status = 'pending' AND is_aggregated = 0 AND recorded_at < ?
               ORDER BY recorded_at ASC""",
            (before,)
        ).fetchall()


def fold_bucket(keeper_id: int, data: dict, recorded_at: str, drop_ids: list[int]):
    """Atomically collapse one hourly bucket into a single aggregated row.
 
    The earliest row in the bucket (`keeper_id`) is rewritten to hold the
    aggregated data at the bucket-start timestamp and flagged is_aggregated;
    the remaining rows (`drop_ids`) are deleted. Single transaction, so a
    crash mid-fold leaves the bucket untouched rather than half-merged.
    """
    with _connect() as con:
        con.execute(
            """UPDATE measurements_queue
               SET data = ?, recorded_at = ?, is_aggregated = 1
               WHERE id = ?""",
            (json.dumps(data), recorded_at, keeper_id)
        )
        if drop_ids: # might be only one row in the bucket, so check before
            con.executemany(
                "DELETE FROM measurements_queue WHERE id = ?",
                [(i,) for i in drop_ids]
            )


def trim_aggregated(count: int) -> int:
    """Delete up to `count` oldest aggregated rows. Never touches raw rows.
    Returns the number actually deleted."""
    if count <= 0:
        return 0
    with _connect() as con:
        cur = con.execute(
            """DELETE FROM measurements_queue
               WHERE id IN (
                   SELECT id FROM measurements_queue
                   WHERE status = 'pending' AND is_aggregated = 1
                   ORDER BY recorded_at ASC
                   LIMIT ?
               )""",
            (count,)
        )
        return cur.rowcount


def set_status_many(ids: list[int], status: str):
    if not ids:
        return
    with _connect() as con:
        con.executemany(
            "UPDATE measurements_queue SET status = ? WHERE id = ?",
            [(status, id) for id in ids]
        )
        

def remove_many(ids: list[int]):
    if not ids:
        return
    with _connect() as con:
        con.executemany(
            "DELETE FROM measurements_queue WHERE id = ?",
            [(id,) for id in ids]
        )


# ── Alert queue ─────────────────────────────────────────────────────────────


def enqueue_alert(data: dict, triggered_at: str):
    """Persist a confirmed alert for sending at next drain."""
    with _connect() as con:
        con.execute(
            "INSERT INTO alerts_queue (data, triggered_at) VALUES (?, ?)",
            (json.dumps(data), triggered_at)
        )


def get_pending_alerts() -> list[sqlite3.Row]:
    with _connect() as con:
        return con.execute(
            "SELECT * FROM alerts_queue WHERE status = 'pending' ORDER BY id ASC"
        ).fetchall()


def set_alert_status_many(ids: list[int], status: str):
    if not ids:
        return
    with _connect() as con:
        con.executemany(
            "UPDATE alerts_queue SET status = ? WHERE id = ?",
            [(status, i) for i in ids]
        )


def remove_alerts(ids: list[int]):
    if not ids:
        return
    with _connect() as con:
        con.executemany(
            "DELETE FROM alerts_queue WHERE id = ?",
            [(i,) for i in ids]
        )


# ── Clock correction ─────────────────────────────────────────────────────────


def apply_clock_correction(boundary_iso: str, delta_seconds: float) -> int:
    """Shift recorded_at on all rows at or before boundary_iso by delta_seconds.

    Called once after the first NTP sync when the Pi booted without a synced
    clock.  Returns the number of rows updated.
    """
    delta = timedelta(seconds=delta_seconds)
    with _connect() as con:
        rows = con.execute(
            "SELECT id, recorded_at FROM measurements_queue WHERE recorded_at <= ?",
            (boundary_iso,),
        ).fetchall()
        if not rows:
            return 0
        updates = []
        for row in rows:
            try:
                ts = datetime.fromisoformat(row["recorded_at"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                updates.append(((ts + delta).isoformat(), row["id"]))
            except ValueError:
                pass
        if updates:
            con.executemany(
                "UPDATE measurements_queue SET recorded_at = ? WHERE id = ?",
                updates,
            )
        return len(updates)
