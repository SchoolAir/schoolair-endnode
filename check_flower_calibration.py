"""check_flower_calibration.py

Run before every start of schoolair-flower.service (ExecStartPre in
deploy/schoolair-flower-fitted.conf).

The flower service rewrites ~/flower/calibration.json around every move (its
position lives there) with a plain rename and no fsync, so a power cut can
leave the file empty or cut short. The service then fails at start, again
every 10 s, for good: schoolair_setup.sh only ever creates that file, never
replaces it. The write itself belongs to SchoolAir/Flower-End-node (flower/
here is a hash-checked copy); until it is fixed there, this puts the bundled
default back when the file can't be read. The default says "position unknown",
so the flower homes and carries on. Positions measured on this unit are lost;
the broken file is kept beside it as calibration.json.corrupt.
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


def repair(calibration: Path = CALIBRATION, default: Path = DEFAULT) -> bool:
    """Put the default back if calibration.json is missing or unreadable.
    True if it was replaced."""
    if _readable(calibration):
        return False
    if calibration.exists():
        os.replace(calibration, calibration.with_name(calibration.name + ".corrupt"))
    write_atomic(calibration, default.read_text())
    print(f"[flower] {calibration} was missing or unreadable — default restored, the flower will home")
    return True


if __name__ == "__main__":
    try:
        repair()
    except OSError as e:
        print(f"[flower] could not check {CALIBRATION}: {e}")
        sys.exit(1)
