"""tests/jobs/test_demo.py

Demo mode (jobs/demo.py): while /run/schoolair/demo exists the sensor is read
every few seconds; it expires by itself; uploads are unchanged.
"""

import os
import time
from unittest.mock import patch

import jobs.ingest as ingest
import state
from jobs.demo import DemoSwitch, load_config, DEFAULTS


def test_off_when_the_file_is_missing(tmp_path, capsys):
    d = DemoSwitch(path=str(tmp_path / "demo"))
    assert d.active() is False
    assert "[demo]" not in capsys.readouterr().out      # nothing to say when nothing changed


def test_on_while_the_file_exists_and_logs_each_transition(tmp_path, capsys):
    f = tmp_path / "demo"
    d = DemoSwitch(path=str(f))
    f.touch()
    assert d.active() is True and d.active() is True
    f.unlink()
    assert d.active() is False
    out = capsys.readouterr().out
    assert out.count("demo mode ON") == 1 and out.count("demo mode OFF") == 1


def test_expires_after_max_minutes_by_deleting_the_file(tmp_path, capsys):
    f = tmp_path / "demo"
    f.touch()
    d = DemoSwitch({"sample_seconds": 3, "max_minutes": 120}, path=str(f))
    assert d.active(now=time.time() + 119 * 60) is True
    assert d.active(now=time.time() + 121 * 60) is False
    assert not f.exists()
    assert "switched off" in capsys.readouterr().out


def test_touching_the_file_again_extends_a_demo(tmp_path):
    f = tmp_path / "demo"
    f.touch()
    d = DemoSwitch({"sample_seconds": 3, "max_minutes": 120}, path=str(f))
    later = time.time() + 100 * 60
    os.utime(f, (later, later))                            # `touch` 100 min in
    assert d.active(now=later + 60 * 60) is True           # 160 min after the start, 60 after the touch


def test_config_defaults_and_overrides():
    assert load_config({}) == DEFAULTS
    assert load_config({"demo": {"sample_seconds": 5}})["sample_seconds"] == 5
    assert load_config({"demo": {"sample_seconds": 0, "max_minutes": "long"}}) == DEFAULTS


async def test_sampling_loop_runs_at_demo_cadence_regardless_of_incident(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "SAMPLE_SECONDS", 60)
    f = tmp_path / "demo"; f.touch()
    ingest._demo = DemoSwitch({"sample_seconds": 3, "max_minutes": 120}, path=str(f))
    ingest._incident.active = True                       # incident would say 10 s; demo wins
    clock = [0.0]; waits = []

    async def fake_wait(seconds):
        waits.append(round(seconds)); clock[0] += seconds

    with patch("jobs.ingest._time_mod.monotonic", side_effect=lambda: clock[0]), \
         patch("jobs.ingest._wait_for_boundary", side_effect=fake_wait), \
         patch("jobs.ingest.read_sensor", return_value={"sen6x": {"co2": 500}}), \
         patch("jobs.ingest.state"):
        await ingest._sample_until_boundary(10, [])

    assert waits == [3, 3, 3, 1]


async def test_demo_readings_do_not_inflate_the_upload_mean(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "SAMPLE_SECONDS", 60)
    f = tmp_path / "demo"; f.touch()
    ingest._demo = DemoSwitch({"sample_seconds": 3, "max_minutes": 120}, path=str(f))
    clock = [1000.0]
    with patch("jobs.ingest._time_mod.monotonic", side_effect=lambda: clock[0]), \
         patch("jobs.ingest.read_sensor", return_value={"sen6x": {"co2": 500}}), \
         patch("jobs.ingest.state") as mock_state:
        for _ in range(41):                                # two minutes at 3 s
            ingest._take_sample([]); clock[0] += 3
    assert mock_state.set.call_count == 41
    assert len(ingest._samples) == 3                       # 1000, 1060, 1120


def test_latest_json_says_which_mode(tmp_path, monkeypatch):
    target = tmp_path / "latest.json"
    monkeypatch.setattr(state, "LATEST_READING_FILE", str(target))
    state.set({"sen6x": {"co2": 500}}, "t1", mode="demo")
    import json
    assert json.loads(target.read_text())["mode"] == "demo"
    assert state.latest_mode == "demo"


def test_take_sample_labels_the_mode(tmp_path):
    f = tmp_path / "demo"; f.touch()
    ingest._demo = DemoSwitch(path=str(f))
    with patch("jobs.ingest.read_sensor", return_value={"sen6x": {"co2": 500}}), \
         patch("jobs.ingest.state") as mock_state:
        ingest._take_sample([])
    assert mock_state.set.call_args.kwargs.get("mode") == "demo"
