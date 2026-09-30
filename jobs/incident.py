"""jobs/incident.py

Incident mode: read the sensor every few seconds while the air is changing
fast, so a local consumer (the wilting flower) can follow within seconds
instead of a minute. Found on 2026-09-30, cooking test: with one reading a
minute the flower moved in one-minute steps behind the room.

  Normal    — a reading every SAMPLE_INTERVAL (60 s), as before.
  Incident  — a reading every `sample_seconds` (10 s), entered when a reading
              jumps: PM2.5 more than `pm25_jump_factor` times the previous
              reading, or rising across `pm25_threshold`; CO2 up by
              `co2_jump_ppm` or more since the previous reading, or rising
              across `co2_threshold`.
  Back to normal once every reading of the last `steady_minutes` sits within
  ±`steady_pm25` and ±`steady_co2` of that window's mean (so a slow drift
  counts as steady, a wobble does not).

Only latest.json sees the extra readings. The upload mean keeps one reading
per SAMPLE_INTERVAL (ingest's `_samples`), so an incident does not weigh more
in the 5/15-minute average than a calm minute does, and uploads are unchanged.

All numbers live in settings.json under "incident", with these defaults:

    "incident": {
        "sample_seconds": 10,
        "pm25_jump_factor": 2.0, "pm25_threshold": 15,
        "co2_jump_ppm": 100,     "co2_threshold": 1000,
        "steady_minutes": 5,     "steady_pm25": 2, "steady_co2": 50
    }

"enabled": false under the same key switches it off.
"""

from services.sensor import extract_metric

DEFAULTS = {
    "enabled": True,
    "sample_seconds": 10,
    "pm25_jump_factor": 2.0,
    "pm25_threshold": 15.0,
    "co2_jump_ppm": 100.0,
    "co2_threshold": 1000.0,
    "steady_minutes": 5.0,
    "steady_pm25": 2.0,
    "steady_co2": 50.0,
}


def load_config(settings: dict) -> dict:
    """The "incident" block of settings.json over DEFAULTS; a value that isn't a
    number (or "enabled" that isn't a bool) falls back to its default."""
    raw = settings.get("incident") if isinstance(settings, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    cfg = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if key == "enabled":
            cfg[key] = value if isinstance(value, bool) else default
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            cfg[key] = float(value) if isinstance(default, float) else int(value)
    return cfg


class IncidentDetector:
    """Feed it every reading with observe(); ask active / sample_seconds."""

    def __init__(self, config: dict | None = None):
        self.cfg = dict(DEFAULTS if config is None else config)
        self.active = False
        self.started_at: float | None = None      # monotonic seconds
        self._prev: dict | None = None            # previous reading's metrics
        self._window: list[tuple[float, float | None, float | None]] = []   # (t, pm25, co2)

    @property
    def sample_seconds(self) -> int | None:
        """The read interval while an incident is on; None means the normal one."""
        return self.cfg["sample_seconds"] if self.active else None

    def observe(self, data: dict, now: float) -> str | None:
        """Register a reading taken at monotonic time `now`. Returns "start" or
        "end" when the mode changes, else None."""
        pm25 = extract_metric(data, "pm25")
        co2 = extract_metric(data, "co2")
        cur = {"pm25": pm25, "co2": co2}
        prev, self._prev = self._prev, cur
        if not self.cfg["enabled"]:
            self.active = False
            return None

        if not self.active:
            if prev is not None and self._jumped(prev, cur):
                self.active = True
                self.started_at = now
                self._window = [(now, pm25, co2)]
                return "start"
            return None

        self._window.append((now, pm25, co2))
        horizon = now - self.cfg["steady_minutes"] * 60
        self._window = [w for w in self._window if w[0] >= horizon]
        if self.started_at is not None and now - self.started_at >= self.cfg["steady_minutes"] * 60 and self._steady():
            self.active = False
            self.started_at = None
            self._window = []
            return "end"
        return None

    def _jumped(self, prev: dict, cur: dict) -> bool:
        c = self.cfg
        p0, p1 = prev["pm25"], cur["pm25"]
        if p0 is not None and p1 is not None:
            if p0 > 0 and p1 > p0 * c["pm25_jump_factor"]:
                return True
            if p0 < c["pm25_threshold"] <= p1:
                return True
        c0, c1 = prev["co2"], cur["co2"]
        if c0 is not None and c1 is not None:
            if c1 - c0 >= c["co2_jump_ppm"]:
                return True
            if c0 < c["co2_threshold"] <= c1:
                return True
        return False

    def _steady(self) -> bool:
        """Every reading in the window within ±steady_* of the window's mean."""
        for idx, tol in ((1, self.cfg["steady_pm25"]), (2, self.cfg["steady_co2"])):
            values = [w[idx] for w in self._window if w[idx] is not None]
            if not values:
                continue
            mean = sum(values) / len(values)
            if any(abs(v - mean) > tol for v in values):
                return False
        return True
