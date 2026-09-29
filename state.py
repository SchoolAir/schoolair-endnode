"""state.py

Shared state for the latest sensor reading.

In-process: written by the ingest loop; read by the WebSocket dashboard handler.

On disk: every set() also writes the reading to LATEST_READING_FILE (default
/run/schoolair/latest.json, a tmpfs path created by the systemd unit's
RuntimeDirectory=). This is the published interface for other services on the
same Pi that want the current reading without touching the queue database or
the sensor — the first consumer is the wilting-flower actuator service, which
maps CO2 and PM2.5 to a stem height. The write is best-effort: if the path is
not writable (laptop, tests without the env var) telemetry is unaffected.

File shape:
    {"data": {"sen6x": {"co2": 742, "pm25": 0.8, ...}, ...}, "recorded_at": "2026-09-28T21:31:43+00:00"}
"""

import json
import os

latest_data: dict | None = None
latest_recorded_at: str | None = None

LATEST_READING_FILE = os.getenv("LATEST_READING_FILE", "/run/schoolair/latest.json")
_write_failed_once = False


def set(data: dict, recorded_at: str) -> None:
    global latest_data, latest_recorded_at
    latest_data = data
    latest_recorded_at = recorded_at
    _publish(data, recorded_at)


def _publish(data: dict, recorded_at: str) -> None:
    """Atomically write the reading for other local services. Never raises."""
    global _write_failed_once
    path = LATEST_READING_FILE
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"data": data, "recorded_at": recorded_at}, f)
        os.replace(tmp, path)          # readers never see a half-written file
    except OSError as e:
        if not _write_failed_once:     # say it once, not every 5 minutes
            print(f"[state] latest reading not published to {path}: {e}")
            _write_failed_once = True
