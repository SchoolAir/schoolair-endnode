"""jobs/demo.py

Demo mode: for demonstrations only, the sensor is read every few seconds so
the wilting flower reacts while people watch. Never the classroom default.

The switch is a file, /run/schoolair/demo (DEMO_FILE): present = demo on.
Both this firmware and the flower service check it on every loop, so it is
flipped with no restart:

    touch /run/schoolair/demo      # on  (as admin: /run/schoolair is ours)
    rm    /run/schoolair/demo      # off

It lives in /run, not /etc, so admin can create and delete it without sudo, so
the firmware can delete it to expire it, and so a reboot ends a demo.

While it exists the sensor is read every demo.sample_seconds (3), whatever
the incident detector says. Uploads are unchanged: the 5/15-minute mean still
keeps one reading per SAMPLE_INTERVAL (ingest's _samples). A unit left in demo mode by
mistake reads itself out of it: after demo.max_minutes (120, counted from the
file's mtime, so `touch` again extends it) the file is deleted and normal
cadence resumes. Each transition is logged, and latest.json carries
"mode": "demo" so the local dashboard and the flower can show it.

settings.json: "demo": {"sample_seconds": 3, "max_minutes": 120}
"""

import os
import time

DEMO_FILE = "/run/schoolair/demo"

DEFAULTS = {
    "sample_seconds": 3,
    "max_minutes": 120,
}


def load_config(settings: dict) -> dict:
    raw = settings.get("demo") if isinstance(settings, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    cfg = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            cfg[key] = int(value)
    return cfg


class DemoSwitch:
    """active() answers "is demo mode on right now?", expiring a stale file
    and printing one line on each change. Cheap: one stat() per call."""

    def __init__(self, config: dict | None = None, path: str = DEMO_FILE):
        self.cfg = dict(DEFAULTS if config is None else config)
        self.path = path
        self._was_on = False

    @property
    def sample_seconds(self) -> int:
        return self.cfg["sample_seconds"]

    def active(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        try:
            age = now - os.stat(self.path).st_mtime
            on = True
        except OSError:
            on = False
        if on and age > self.cfg["max_minutes"] * 60:
            try:
                os.remove(self.path)
                print(f"[demo] demo mode on for more than {self.cfg['max_minutes']} min — switched off (deleted {self.path})")
            except OSError as e:
                print(f"[demo] demo mode expired but {self.path} could not be deleted: {e}")
            on = False
            self._was_on = False
            return False
        if on != self._was_on:
            if on:
                print(f"[demo] demo mode ON ({self.path} present) — reading every {self.cfg['sample_seconds']}s, "
                      f"off by itself after {self.cfg['max_minutes']} min; uploads unchanged")
            else:
                print(f"[demo] demo mode OFF ({self.path} gone) — back to the normal cadence")
            self._was_on = on
        return on
