"""tests/test_check_flower_calibration.py

check_flower_calibration.py: an unreadable ~/flower/calibration.json (power cut
mid-write) is replaced with the bundled default before the flower starts.
"""

import json
from pathlib import Path

import check_flower_calibration as check

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "flower" / "calibration.example.json"


def test_a_good_calibration_is_left_alone(tmp_path):
    cal = tmp_path / "calibration.json"
    cal.write_text(json.dumps({"current_position_mm": 42.0, "position_state": "known"}))

    assert check.repair(cal, DEFAULT) is False
    assert json.loads(cal.read_text())["current_position_mm"] == 42.0


def test_an_empty_calibration_is_replaced_and_kept_aside(tmp_path):
    cal = tmp_path / "calibration.json"
    cal.write_text("")

    assert check.repair(cal, DEFAULT) is True
    assert json.loads(cal.read_text()) == json.loads(DEFAULT.read_text())
    assert (tmp_path / "calibration.json.corrupt").exists()


def test_a_truncated_calibration_is_replaced(tmp_path):
    cal = tmp_path / "calibration.json"
    cal.write_text(DEFAULT.read_text()[:200])

    assert check.repair(cal, DEFAULT) is True
    assert json.loads(cal.read_text())["position_state"] == "unknown"   # so the flower homes


def test_a_missing_calibration_is_created(tmp_path):
    cal = tmp_path / "calibration.json"

    assert check.repair(cal, DEFAULT) is True
    assert cal.exists()


def test_the_flower_drop_in_runs_the_check_without_blocking_the_start():
    conf = (ROOT / "deploy/schoolair-flower-fitted.conf").read_text()
    assert "ExecStartPre=-/usr/bin/python3 /home/admin/schoolair/check_flower_calibration.py" in conf


# ── the unit's own last good copy (2026-11-27) ──────────────────────────────

UNIT = {"working_max_mm": 57.5, "mapping": {"x": 1}, "current_position_mm": 42.0, "position_state": "known"}


def test_a_good_calibration_is_copied_aside(tmp_path):
    cal = tmp_path / "calibration.json"
    cal.write_text(json.dumps(UNIT))
    check.repair(cal, DEFAULT)
    assert json.loads((tmp_path / "calibration.json.good").read_text()) == UNIT


def test_a_broken_file_comes_back_from_the_units_own_copy_not_the_default(tmp_path):
    cal = tmp_path / "calibration.json"
    cal.write_text(json.dumps(UNIT))
    check.repair(cal, DEFAULT)            # a normal start keeps the copy
    cal.write_text("")                    # power cut mid-write
    assert check.repair(cal, DEFAULT) is True
    back = json.loads(cal.read_text())
    assert back["working_max_mm"] == 57.5 and back["mapping"] == {"x": 1}   # its own heights
    assert back["position_state"] == "unknown"                              # so it homes
    assert back["current_position_mm"] is None
    assert (tmp_path / "calibration.json.corrupt").read_text() == ""


def test_no_good_copy_falls_back_to_the_default(tmp_path):
    cal = tmp_path / "calibration.json"
    cal.write_text("{half")
    (tmp_path / "calibration.json.good").write_text("{also broken")
    assert check.repair(cal, DEFAULT) is True
    assert json.loads(cal.read_text()) == json.loads(DEFAULT.read_text())


def test_failing_to_keep_a_copy_never_stops_the_start(tmp_path, monkeypatch, capsys):
    cal = tmp_path / "calibration.json"
    cal.write_text(json.dumps(UNIT))
    def boom(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(check, "write_atomic", boom)
    assert check.repair(cal, DEFAULT) is False
    assert "could not keep a good copy" in capsys.readouterr().out
