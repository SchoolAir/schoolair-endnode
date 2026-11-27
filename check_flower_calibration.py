"""check_flower_calibration.py

Run before every start of schoolair-flower.service (ExecStartPre in
deploy/schoolair-flower-fitted.conf).

The flower service rewrites ~/flower/calibration.json around every move (its
position lives there) with a plain rename and no fsync, so a power cut can
leave the file empty or cut short. The service then fails at start, again
every 10 s, for good: schoolair_setup.sh only ever creates that file, never
replaces it. The write itself belongs to SchoolAir/Flower-End-node (flower/
here is a hash-checked copy); until it is fixed there, this puts the bundled
default back when the file can't be read.

Which default: this unit's own last good file first. Every start that finds
calibration.json readable keeps a copy of it as calibration.json.good, and a
broken file is replaced from that copy, with the position marked unknown so
the flower homes. The unit keeps its own heights and mapping: the bundled
calibration.example.json carries one unit's heights (erect 69.5 on the newest
carriage), and an older unit restored from it could aim past its own travel.
Only when there is no good copy (a new unit, or both files broken) does the
bundled default go back; it says "position unknown" too. Either way the
broken file is kept beside it as calibration.json.corrupt.
"""

import json
import os
import sys
from pathlib import Path

from atomic_file import write_atomic

CALIBRATION = Path(os.environ.get("FLOWER_CALIBRATION", "/home/admin/flower/calibration.json"))
DEFAULT = Path(__file__).resolve().parent / "flower" / "calibration.example.json"


def _readable(path: Path) -> bool:
    try:
        return isinstance(json.loads(path.read_text()), dict)
    except (OSError, ValueError):
        return False


def _good_copy(calibration: Path) -> Path:
    return calibration.with_name(calibration.name + ".good")


def _keep_good_copy(calibration: Path) -> None:
    """calibration.json is readable: refresh calibration.json.good from it.
    Best effort: a failure here must never stop the flower starting."""
    good = _good_copy(calibration)
    try:
        text = calibration.read_text()
        if not good.exists() or good.read_text() != text:
            write_atomic(good, text)
    except OSError as e:
        print(f"[flower] could not keep a good copy of {calibration}: {e}")


def repair(calibration: Path = CALIBRATION, default: Path = DEFAULT) -> bool:
    """Put a good calibration back if calibration.json is missing or
    unreadable: this unit's last good copy, else the bundled default.
    True if it was replaced."""
    if _readable(calibration):
        _keep_good_copy(calibration)
        return False
    if calibration.exists():
        os.replace(calibration, calibration.with_name(calibration.name + ".corrupt"))
    good = _good_copy(calibration)
    if _readable(good):
        cal = json.loads(good.read_text())
        cal["position_state"] = "unknown"      # the copy's position may be old: home first
        cal["current_position_mm"] = None
        write_atomic(calibration, json.dumps(cal, indent=2) + "\n")
        print(f"[flower] {calibration} was missing or unreadable — this unit's last good copy "
              f"restored, the flower will home")
    else:
        write_atomic(calibration, default.read_text())
        print(f"[flower] {calibration} was missing or unreadable and there is no good copy — "
              f"bundled default restored, the flower will home")
    return True


if __name__ == "__main__":
    try:
        repair()
    except OSError as e:
        print(f"[flower] could not check {CALIBRATION}: {e}")
        sys.exit(1)
