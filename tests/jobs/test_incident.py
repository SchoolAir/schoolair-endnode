"""tests/jobs/test_incident.py

Incident mode (jobs/incident.py): read every 10 s while the air changes fast,
back to 60 s once it has been steady for 5 minutes; uploads unchanged.
"""

import asyncio
from unittest.mock import patch

import pytest

import jobs.ingest as ingest
from jobs.incident import IncidentDetector, load_config, DEFAULTS


def r(pm25=None, co2=None):
    d = {}
    if pm25 is not None: d["pm25"] = pm25
    if co2 is not None: d["co2"] = co2
    return {"sen6x": d}


# ── Triggers ──────────────────────────────────────────────────────────────────

def test_first_reading_never_starts_an_incident():
    assert IncidentDetector().observe(r(pm25=80, co2=2000), 0) is None


@pytest.mark.parametrize("before, after", [
    (r(pm25=4.0), r(pm25=9.0)),          # more than doubled
    (r(pm25=12.0), r(pm25=15.0)),        # crossed 15 upward
    (r(co2=700), r(co2=800)),            # +100 ppm in a minute
    (r(co2=950), r(co2=1000)),           # crossed 1000 upward
])
def test_a_jump_starts_an_incident(before, after):
    d = IncidentDetector()
    d.observe(before, 0)
    assert d.observe(after, 60) == "start"
    assert d.active and d.sample_seconds == 10


@pytest.mark.parametrize("before, after", [
    (r(pm25=4.0), r(pm25=7.9)),          # less than doubled
    (r(pm25=20.0), r(pm25=15.0)),        # crossing 15 downward
    (r(co2=800), r(co2=899)),            # +99 ppm
    (r(co2=1100), r(co2=1000)),          # crossing 1000 downward
    (r(pm25=0.0), r(pm25=1.0)),          # from zero: no ratio
    (r(pm25=None, co2=None), r(pm25=50, co2=3000)),   # previous reading had no values
])
def test_ordinary_changes_do_not(before, after):
    d = IncidentDetector()
    d.observe(before, 0)
    assert d.observe(after, 60) is None
    assert not d.active and d.sample_seconds is None


# ── Ending ────────────────────────────────────────────────────────────────────

def _incident_at_zero():
    d = IncidentDetector()
    d.observe(r(pm25=2, co2=600), -60)
    assert d.observe(r(pm25=30, co2=900), 0) == "start"
    return d


def test_ends_after_five_steady_minutes():
    d = _incident_at_zero()
    t = 0
    change = None
    while t < 400:
        t += 10
        change = d.observe(r(pm25=30 + (t % 20 == 0), co2=900), t)   # wobble of 1 µg/m³, well within ±2
        if change == "end":
            break
    assert change == "end" and 300 <= t <= 310
    assert not d.active and d.sample_seconds is None


def test_does_not_end_while_still_moving():
    d = _incident_at_zero()
    for t in range(10, 601, 10):
        assert d.observe(r(pm25=30 - t / 30, co2=900), t) != "end"   # PM2.5 falling 30 → 10: never steady
    assert d.active


def test_a_slow_drift_counts_as_steady_a_wobble_does_not():
    d = _incident_at_zero()
    for t in range(10, 301, 10):
        d.observe(r(pm25=30, co2=900 + t / 10), t)                   # CO2 drifts 30 ppm over 5 min: fine
    assert not d.active
    d = _incident_at_zero()
    for t in range(10, 301, 10):
        d.observe(r(pm25=30, co2=900 + (120 if t % 20 else 0)), t)     # ±60 around the mean: beyond ±50, not steady
    assert d.active


# ── Settings ──────────────────────────────────────────────────────────────────

def test_config_defaults_and_overrides():
    assert load_config({}) == DEFAULTS
    cfg = load_config({"incident": {"sample_seconds": 5, "co2_threshold": 900, "enabled": False}})
    assert cfg["sample_seconds"] == 5 and cfg["co2_threshold"] == 900 and cfg["enabled"] is False
    assert cfg["pm25_threshold"] == DEFAULTS["pm25_threshold"]


def test_nonsense_settings_fall_back_to_defaults():
    cfg = load_config({"incident": {"sample_seconds": "fast", "steady_minutes": -1, "enabled": "yes"}})
    assert cfg["sample_seconds"] == 10 and cfg["steady_minutes"] == 5.0 and cfg["enabled"] is True


def test_disabled_never_starts():
    d = IncidentDetector(load_config({"incident": {"enabled": False}}))
    d.observe(r(pm25=2), 0)
    assert d.observe(r(pm25=90), 60) is None and not d.active


# ── In the read loop ──────────────────────────────────────────────────────────

async def test_incident_readings_reach_latest_json_but_the_upload_mean_keeps_one_a_minute(monkeypatch):
    """During an incident, samples come every 10 s: all published, one in six kept for the mean."""
    monkeypatch.setattr(ingest, "SAMPLE_SECONDS", 60)
    ingest._incident = IncidentDetector()
    clock = [1000.0]
    readings = iter([r(pm25=2, co2=600)] + [r(pm25=40, co2=900)] * 40)

    with patch("jobs.ingest._time_mod.monotonic", side_effect=lambda: clock[0]), \
         patch("jobs.ingest.read_sensor", side_effect=lambda: next(readings)), \
         patch("jobs.ingest.state") as mock_state:
        ingest._take_sample([])                 # calm
        clock[0] += 60
        ingest._take_sample([])                 # the jump: incident starts
        assert ingest._incident.active
        for _ in range(12):                     # two minutes at 10 s
            clock[0] += 10
            ingest._take_sample([])

    assert mock_state.set.call_count == 14                # every reading published
    assert len(ingest._samples) == 4                      # 1000, 1060, 1120, 1180: one per minute


async def test_sampling_loop_speeds_up_during_an_incident(monkeypatch):
    monkeypatch.setattr(ingest, "SAMPLE_SECONDS", 60)
    ingest._incident = IncidentDetector()
    ingest._incident.active = True
    ingest._incident.started_at = 0.0
    clock = [0.0]
    waits = []

    async def fake_wait(seconds):
        waits.append(round(seconds)); clock[0] += seconds

    with patch("jobs.ingest._time_mod.monotonic", side_effect=lambda: clock[0]), \
         patch("jobs.ingest._wait_for_boundary", side_effect=fake_wait), \
         patch("jobs.ingest.read_sensor", return_value=r(pm25=40, co2=900)), \
         patch("jobs.ingest.state"):
        await ingest._sample_until_boundary(45, [])

    assert waits == [10, 10, 10, 10, 5]
