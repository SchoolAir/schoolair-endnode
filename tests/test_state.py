"""tests/test_state.py

state.set() must publish the reading to LATEST_READING_FILE atomically, and must
never let a publishing failure reach the ingest loop.
"""

import json
import os

import pytest

import state


def test_set_publishes_latest_reading(tmp_path, monkeypatch):
    target = tmp_path / "latest.json"
    monkeypatch.setattr(state, "LATEST_READING_FILE", str(target))
    data = {"sen6x": {"co2": 742, "pm25": 0.8, "temp": 28.5}}

    state.set(data, "2026-09-28T21:31:43+00:00")

    assert state.latest_data == data
    assert state.latest_recorded_at == "2026-09-28T21:31:43+00:00"
    published = json.loads(target.read_text())
    assert published == {"data": data, "recorded_at": "2026-09-28T21:31:43+00:00"}
    assert not os.path.exists(str(target) + ".tmp"), "temp file must be renamed away"


def test_set_survives_unwritable_path(monkeypatch, capsys):
    monkeypatch.setattr(state, "LATEST_READING_FILE", "/nonexistent-dir/latest.json")
    monkeypatch.setattr(state, "_write_failed_once", False)

    state.set({"sen6x": {"co2": 1}}, "t1")   # must not raise
    state.set({"sen6x": {"co2": 2}}, "t2")

    assert state.latest_data == {"sen6x": {"co2": 2}}
    out = capsys.readouterr().out
    assert out.count("not published") == 1, "warn once, not on every reading"


def test_empty_path_disables_publishing(monkeypatch):
    monkeypatch.setattr(state, "LATEST_READING_FILE", "")
    state.set({"sen6x": {"co2": 3}}, "t3")   # no file, no error
    assert state.latest_recorded_at == "t3"
