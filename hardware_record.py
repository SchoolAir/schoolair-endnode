#!/usr/bin/env python3
"""hardware_record.py

What hardware this unit has had, and when: HARDWARE_FILE
(/var/lib/schoolair/hardware.json). One entry per part, kept when the part
goes away, so the file shows when each part first and last appeared:

  {"first_check": "...", "last_check": "...",
   "checks": {"sen6x": "...", "flower": "...", ...},   <- last check of each kind
   "parts": {
     "SEN63C":     {"kind": "sen6x", "how": "detected", "present": true,
                    "first_seen": "...", "last_seen": "..."},
     "SEN65":      {"kind": "sen6x", "how": "detected", "present": false,
                    "first_seen": "...", "last_seen": "...",      <- last confirmed
                    "missing_since": "...", "last_missed": "..."}, <- first/last check without it
     "status_led": {"kind": "status_led", "how": "assumed",
                    "basis": "...", "present": true, ...},
     "mgs_v2":     {"kind": "aux_sensor", "how": "detected", "address": "0x08", ...}}}

  - A part found for the first time gets first_seen. Every later sighting
    moves last_seen.
  - A part not found keeps its entry: present turns false, last_seen stays
    the last time it was confirmed, and missing_since / last_missed record
    the first and the latest check that didn't find it.
  - A part that comes back is present again; its last absence moves to
    last_gap {"from", "to"}.
  - A part that was never found is not listed.
  - "how": "assumed" marks a part that can't be detected and is taken to be
    there on this kind of unit (today: the plain status LED on indoor
    units); "basis" says why. Its "seen" times are when the assumption was
    applied.
  - A check that can't tell (sensor not ready, pins in use) changes nothing.

Each SEN6x model is its own part, so a swap shows the old one missing and
the new one first seen.

Written by:
  - detect_unit_type.sh (first boot, setup) and detect_flower.sh (every boot,
    first boot, setup, schoolair-redetect);
  - `hardware_record.py check` (schoolair-hardware-check.service at every
    boot, schoolair-redetect, setup): the SEN6x and the status-light assumption;
  - jobs/ingest.py (telemetry): the auxiliary I2C sensors it probes at start,
    and the SEN6x as it reads it (at most every SEN6X_REFRESH_S, or at once
    on a change).
Read by the Bluetooth registration (which sensor this unit has) and by people.

Writers take a lock, and root's writes leave the file owned by admin, so
the telemetry service can write too.
Stdlib-only: imported by the app (venv), the wizard (system python) and run
from shell scripts.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atomic_file import write_atomic  # noqa: E402

HARDWARE_FILE  = os.getenv("HARDWARE_FILE", "/var/lib/schoolair/hardware.json")
UNIT_TYPE_FILE = os.getenv("UNIT_TYPE_FILE", "/etc/schoolair-unit-type")
SEN6X_READ     = os.getenv("SEN6X_READ", "/home/admin/i2c/sen6x/sen6x_read")
LATEST_READING_FILE = os.getenv("LATEST_READING_FILE", "/run/schoolair/latest.json")
LATEST_MAX_AGE_S = 5 * 60
SEN6X_MODELS = ("SEN63C", "SEN65")
SEN6X_NOT_READY = 2  # sen6x_read's exit status: the sensor answered, no data yet

LED_BASIS = ("a plain LED on GPIO24 can't be detected; every indoor unit is "
             "assumed to have one (README.md, Hardware detection)")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── The record ────────────────────────────────────────────────────────────────

def observe(record: dict, part: str, found: "bool | None", now: str, *,
            kind: str, how: str = "detected", basis: "str | None" = None,
            extra: "dict | None" = None) -> bool:
    """Apply one check of `part` to `record` (in place). found None: couldn't
    tell. Returns whether anything changed."""
    if found is None:
        return False
    parts = record.setdefault("parts", {})
    entry = parts.get(part)
    if not found:
        if entry is None:
            return False  # never had it
        if entry.get("present"):
            entry["present"] = False
            entry["missing_since"] = now
        entry["last_missed"] = now
        return True

    if entry is None:
        entry = parts[part] = {"kind": kind, "how": how, "first_seen": now}
    elif not entry.get("present"):
        entry["last_gap"] = {"from": entry.pop("missing_since", None), "to": entry.pop("last_missed", None)}
    entry.update({"kind": kind, "how": how, "present": True, "last_seen": now})
    if basis:
        entry["basis"] = basis
    else:
        entry.pop("basis", None)
    entry.update(extra or {})
    return True


def load(path: "str | None" = None) -> dict:
    try:
        data = json.loads(Path(path or HARDWARE_FILE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def _locked(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def update(observations: "list[dict]", path: "str | None" = None, now: "str | None" = None) -> dict:
    """Apply checks, each a dict of observe()'s arguments (part, found, kind, …),
    and save. Returns the record."""
    path = path or HARDWARE_FILE
    now = now or now_iso()
    with _locked(path):
        record = load(path)
        changed = False
        for o in observations:
            o = dict(o)
            if o["found"] is not None:
                record.setdefault("checks", {})[o["kind"]] = now
                changed = True
            observe(record, o.pop("part"), o.pop("found"), now, **o)
        if changed:
            record.setdefault("first_check", now)
            record["last_check"] = now
            record = _ordered(record)
            write_atomic(path, json.dumps(record, indent=2) + "\n")
            _own_like_app(path)
        return record


_ENTRY_ORDER = ("kind", "how", "basis", "present", "first_seen", "last_seen",
                "missing_since", "last_missed", "last_gap")


def _ordered(record: dict) -> dict:
    """Keys in reading order, parts by name, so the file reads top-down."""
    def entry(e: dict) -> dict:
        return {**{k: e[k] for k in _ENTRY_ORDER if k in e}, **{k: v for k, v in e.items() if k not in _ENTRY_ORDER}}
    out = {k: record[k] for k in ("first_check", "last_check", "checks") if k in record}
    out["parts"] = {name: entry(e) for name, e in sorted(record.get("parts", {}).items())}
    return {**out, **{k: v for k, v in record.items() if k not in out}}


def _own_like_app(path: str) -> None:
    """Root's writes: give the file (and a directory root had to create) to
    the app's owner, admin, like this script, so the telemetry service can
    write it too."""
    if os.geteuid() != 0:
        return
    st = os.stat(os.path.abspath(__file__))
    for p in (os.path.dirname(path), path, path + ".lock"):
        try:
            os.chown(p, st.st_uid, st.st_gid)
        except OSError:
            pass


def sen6x_model(record: dict) -> "tuple[bool, str | None]":
    """(known, model): whether a SEN6x check has been recorded at all, and
    the one present now (None: none answering)."""
    present = [name for name, p in record.get("parts", {}).items()
               if p.get("kind") == "sen6x" and p.get("present")]
    return "sen6x" in record.get("checks", {}), (present[0] if present else None)


# ── Checking the SEN6x ────────────────────────────────────────────────────────

def model_in_output(text: str) -> "str | None":
    """The model behind a sen6x_read reading (its JSON, or --init's output),
    as detect_unit_type.sh tells them apart: the SEN63C measures CO2, the
    SEN65 VOC."""
    if '"co2"' in text:
        return "SEN63C"
    if '"voc"' in text:
        return "SEN65"
    return None


def model_in_reading(reading: "dict | None") -> "str | None":
    """The same from a parsed reading, {"sen6x": {...}, "mgs_v2": {...}}: only
    the sen6x part counts (an MGS-V2 reports "voc" too)."""
    sen6x = (reading or {}).get("sen6x")
    if not isinstance(sen6x, dict):
        return None
    if "co2" in sen6x:
        return "SEN63C"
    if "voc" in sen6x:
        return "SEN65"
    return None


def sen6x_observations(model: "str | None", conclusive: bool = True) -> "list[dict]":
    """One check of the SEN6x as observations: the model found and the other
    one not; nothing answering, neither. An inconclusive check (the sensor
    answered but isn't ready, or it couldn't be asked) changes nothing."""
    if model is None and not conclusive:
        return []
    return [{"part": m, "found": m == model, "kind": "sen6x"} for m in SEN6X_MODELS]


def check_sen6x() -> "tuple[str | None, bool]":
    """(model, conclusive) now: the telemetry service's latest reading if it is
    recent (no second reader on the bus), else one read of our own. A failed
    reading means nothing answers; a reading without a SEN6x model, or a
    sensor not ready yet, is inconclusive."""
    try:
        latest = json.loads(Path(LATEST_READING_FILE).read_text())
        age = time.time() - datetime.fromisoformat(latest["recorded_at"]).timestamp()
        if age <= LATEST_MAX_AGE_S:
            data = latest.get("data")
            model = model_in_reading(data)
            return model, model is not None or data is None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        r = subprocess.run([SEN6X_READ], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    model = model_in_output(r.stdout)
    return model, model is not None or r.returncode != SEN6X_NOT_READY


def led_observation() -> dict:
    """The status light is assumed on indoor units, not on outdoor ones."""
    try:
        unit = Path(UNIT_TYPE_FILE).read_text().strip()
    except OSError:
        unit = ""
    found = {"indoor": True, "outdoor": False}.get(unit)
    return {"part": "status_led", "found": found, "kind": "status_led", "how": "assumed", "basis": LED_BASIS}


# ── Command line (shell scripts, systemd) ─────────────────────────────────────

USAGE = """usage:
  hardware_record.py check                    SEN6x + status-light assumption, now
  hardware_record.py sen6x [EXIT_STATUS]      record sen6x_read output read from stdin
  hardware_record.py part NAME yes|no|unknown KIND
  hardware_record.py led                      status-light assumption, from the unit type
  hardware_record.py sen6x-model              print the SEN6x present (or "none")
  hardware_record.py show"""


def main(argv: "list[str]") -> int:
    cmd = argv[0] if argv else ""
    if cmd == "check":
        model, conclusive = check_sen6x()
        update(sen6x_observations(model, conclusive) + [led_observation()])
        print(f"[hardware] SEN6x: {model or ('none' if conclusive else 'unknown')}")
    elif cmd == "sen6x":
        text = sys.stdin.read()
        status = int(argv[1]) if len(argv) > 1 and argv[1].lstrip("-").isdigit() else 0
        model = model_in_output(text)
        update(sen6x_observations(model, conclusive=model is not None or status != SEN6X_NOT_READY))
    elif cmd == "part" and len(argv) == 4 and argv[2] in ("yes", "no", "unknown"):
        update([{"part": argv[1], "found": {"yes": True, "no": False}.get(argv[2]), "kind": argv[3]}])
    elif cmd == "led":
        update([led_observation()])
    elif cmd == "sen6x-model":
        print(sen6x_model(load())[1] or "none")
    elif cmd == "show":
        print(json.dumps(load(), indent=2))
    else:
        print(USAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
