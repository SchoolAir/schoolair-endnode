"""tests/test_hardware_record.py

hardware_record.py: when each part was first and last seen, what happens when
one goes away or comes back, assumed parts, and the checks that feed it.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import hardware_record as hr

ROOT = Path(__file__).resolve().parent.parent
T1, T2, T3, T4 = (f"2026-11-0{d}T08:00:00+00:00" for d in (1, 2, 3, 4))


def _flower(found, now, record):
    return hr.observe(record, "flower", found, now, kind="flower")


def test_first_and_last_seen():
    rec = {}
    _flower(True, T1, rec)
    _flower(True, T2, rec)
    assert rec["parts"]["flower"] == {"kind": "flower", "how": "detected", "present": True,
                                      "first_seen": T1, "last_seen": T2}


def test_a_part_that_goes_away_is_kept_with_when_it_was_last_seen():
    rec = {}
    _flower(True, T1, rec)
    _flower(False, T2, rec)
    _flower(False, T3, rec)
    assert rec["parts"]["flower"] == {"kind": "flower", "how": "detected", "present": False,
                                      "first_seen": T1, "last_seen": T1,
                                      "missing_since": T2, "last_missed": T3}


def test_a_part_that_comes_back_keeps_its_last_absence():
    rec = {}
    _flower(True, T1, rec)
    _flower(False, T2, rec)
    _flower(True, T3, rec)
    entry = rec["parts"]["flower"]
    assert entry["present"] and entry["first_seen"] == T1 and entry["last_seen"] == T3
    assert entry["last_gap"] == {"from": T2, "to": T2}
    assert "missing_since" not in entry and "last_missed" not in entry


def test_never_found_is_not_listed_and_unknown_changes_nothing():
    rec = {}
    assert _flower(False, T1, rec) is False and rec.get("parts", {}) == {}
    _flower(True, T1, rec)
    before = json.dumps(rec)
    assert _flower(None, T2, rec) is False
    assert json.dumps(rec) == before


def test_assumed_part_says_so_and_why():
    rec = {}
    hr.observe(rec, "status_led", True, T1, kind="status_led", how="assumed", basis="indoor unit")
    assert rec["parts"]["status_led"]["how"] == "assumed"
    assert rec["parts"]["status_led"]["basis"] == "indoor unit"


def test_a_sensor_swap_shows_the_old_one_missing_and_the_new_one_first_seen(tmp_path):
    path = str(tmp_path / "hw.json")
    hr.update(hr.sen6x_observations("SEN63C"), path, now=T1)
    hr.update(hr.sen6x_observations("SEN65"), path, now=T2)
    rec = hr.load(path)
    assert rec["parts"]["SEN63C"]["present"] is False and rec["parts"]["SEN63C"]["last_seen"] == T1
    assert rec["parts"]["SEN63C"]["missing_since"] == T2
    assert rec["parts"]["SEN65"] == {"kind": "sen6x", "how": "detected", "present": True,
                                     "first_seen": T2, "last_seen": T2}
    assert rec["first_check"] == T1 and rec["last_check"] == T2 and rec["checks"] == {"sen6x": T2}
    assert hr.sen6x_model(rec) == (True, "SEN65")


def test_sen6x_model_tells_unchecked_from_none(tmp_path):
    path = str(tmp_path / "hw.json")
    assert hr.sen6x_model(hr.load(path)) == (False, None)   # never checked
    hr.update(hr.sen6x_observations(None), path, now=T1)    # checked: nothing answers
    assert hr.sen6x_model(hr.load(path)) == (True, None)
    assert hr.load(path)["parts"] == {}                     # and nothing ever had


def test_unidentified_answer_records_nothing():
    assert hr.sen6x_observations(None, conclusive=False) == []


@pytest.mark.parametrize("text, model", [
    ('{"sen6x": {"co2": 512, "pm25": 1.0}}', "SEN63C"),
    ('[sen6x_read] init ok\n{"sen6x": {"voc": 100, "nox": 1}}', "SEN65"),
    ("[sen6x_read] cannot detect sensor (I2C error or sensor absent)", None),
])
def test_model_in_output(text, model):
    assert hr.model_in_output(text) == model


@pytest.mark.parametrize("reading, model", [
    ({"sen6x": {"co2": 512}}, "SEN63C"),
    ({"sen6x": {"co2": None, "temp": 20}}, "SEN63C"),            # warming up: the key is there
    ({"sen6x": {"voc": 100, "nox": 1}}, "SEN65"),
    ({"sen6x": {"co2": 512}, "mgs_v2": {"voc": 155}}, "SEN63C"),
    ({"mgs_v2": {"voc": 155}}, None),                            # an MGS-V2's VOC is not a SEN65
    (None, None),
])
def test_model_in_reading(reading, model):
    assert hr.model_in_reading(reading) == model


def _latest(tmp_path, monkeypatch, data, age_s):
    path = tmp_path / "latest.json"
    recorded = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    path.write_text(json.dumps({"data": data, "recorded_at": recorded.isoformat()}))
    monkeypatch.setattr(hr, "LATEST_READING_FILE", str(path))


def _fake_read(tmp_path, monkeypatch, out, status):
    script = tmp_path / "sen6x_read"
    script.write_text(f"#!/bin/sh\necho '{out}'\nexit {status}\n")
    script.chmod(0o755)
    monkeypatch.setattr(hr, "SEN6X_READ", str(script))


def test_check_uses_a_fresh_latest_reading(tmp_path, monkeypatch):
    _latest(tmp_path, monkeypatch, {"sen6x": {"co2": 600}}, 30)
    monkeypatch.setattr(hr, "SEN6X_READ", "/nonexistent")
    assert hr.check_sen6x() == ("SEN63C", True)


def test_a_fresh_failed_reading_means_no_sensor(tmp_path, monkeypatch):
    _latest(tmp_path, monkeypatch, None, 30)
    assert hr.check_sen6x() == (None, True)


def test_check_reads_the_sensor_when_the_reading_is_stale(tmp_path, monkeypatch):
    _latest(tmp_path, monkeypatch, {"sen6x": {"co2": 600}}, 3600)
    _fake_read(tmp_path, monkeypatch, '{"sen6x": {"voc": 9}}', 0)
    assert hr.check_sen6x() == ("SEN65", True)


@pytest.mark.parametrize("status, expected", [(1, (None, True)),     # nothing answers
                                              (2, (None, False))])   # answered, not ready: inconclusive
def test_check_without_a_reading(tmp_path, monkeypatch, status, expected):
    monkeypatch.setattr(hr, "LATEST_READING_FILE", str(tmp_path / "none.json"))
    _fake_read(tmp_path, monkeypatch, "[sen6x_read] no data", status)
    assert hr.check_sen6x() == expected


def _cli(*args, stdin="", env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, str(ROOT / "hardware_record.py"), *args],
                          input=stdin, capture_output=True, text=True, env=env)


def test_cli_records_sen6x_output_and_the_led_assumption(tmp_path, hardware_file):
    unit_type = tmp_path / "unit-type"
    unit_type.write_text("indoor\n")
    assert _cli("sen6x", "0", stdin='{"sen6x": {"co2": 700}}').returncode == 0
    assert _cli("led", env_extra={"UNIT_TYPE_FILE": str(unit_type)}).returncode == 0
    rec = json.loads(hardware_file.read_text())
    assert rec["parts"]["SEN63C"]["present"] is True
    assert rec["parts"]["status_led"]["how"] == "assumed" and "GPIO24" in rec["parts"]["status_led"]["basis"]
    assert _cli("sen6x-model").stdout.strip() == "SEN63C"

    unit_type.write_text("outdoor\n")                 # no light assumed on outdoor units
    _cli("led", env_extra={"UNIT_TYPE_FILE": str(unit_type)})
    assert json.loads(hardware_file.read_text())["parts"]["status_led"]["present"] is False


def test_cli_rejects_bad_usage():
    assert _cli("part", "flower", "maybe", "flower").returncode == 2
