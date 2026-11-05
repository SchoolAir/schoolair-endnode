"""tests/jobs/test_ingest_hardware_record.py

The read loop keeps the hardware record current: the SEN6x model as it is
read (not on every sample), a sensor gone quiet for long enough, and the
auxiliary sensors probed at start.
"""

import json
from unittest.mock import patch

import jobs.ingest as ingest


def _parts(hardware_file):
    return json.loads(hardware_file.read_text())["parts"] if hardware_file.exists() else {}


async def _samples(clock, reads, hardware_file, steps):
    """Take one sample per (seconds to advance) in `steps`; `reads` yields a
    reading or an exception per sample."""
    readings = iter(reads)

    def read():
        r = next(readings)
        if isinstance(r, Exception):
            raise r
        return r
    with patch("jobs.ingest._time_mod.monotonic", side_effect=lambda: clock[0]), \
         patch("jobs.ingest.read_sensor", side_effect=read), \
         patch("jobs.ingest.state"), patch("jobs.ingest._set_led_state"):
        for advance in steps:
            clock[0] += advance
            await ingest._take_sample([])


async def test_the_sen6x_is_confirmed_at_most_every_few_hours(hardware_file):
    clock = [1000.0]
    writes = []
    real_update = ingest.hardware_record.update
    with patch("jobs.ingest.hardware_record.update", side_effect=lambda obs: writes.append(obs) or real_update(obs)):
        await _samples(clock, [{"sen6x": {"co2": 600}}] * 4, hardware_file,
                       [0, 60, 60, ingest.SEN6X_REFRESH_S])
    assert len(writes) == 2                       # the first sample, then after SEN6X_REFRESH_S
    assert _parts(hardware_file)["SEN63C"]["present"] is True


async def test_a_sensor_quiet_for_long_enough_is_recorded_missing(hardware_file):
    clock = [1000.0]
    fail = RuntimeError("Sensor script failed")
    await _samples(clock, [{"sen6x": {"voc": 9}}, fail, fail], hardware_file,
                   [0, 60, 60])
    assert _parts(hardware_file)["SEN65"]["present"] is True       # two failed reads: not yet
    await _samples(clock, [fail], hardware_file, [ingest.SEN6X_MISSING_AFTER_S])
    entry = _parts(hardware_file)["SEN65"]
    assert entry["present"] is False and "missing_since" in entry


async def test_a_read_in_between_restarts_the_quiet_clock(hardware_file):
    clock = [1000.0]
    fail = RuntimeError("Sensor script failed")
    ok = {"sen6x": {"co2": 600}}
    await _samples(clock, [ok, fail, ok, fail], hardware_file,
                   [0, 60, ingest.SEN6X_MISSING_AFTER_S - 120, ingest.SEN6X_MISSING_AFTER_S - 120])
    assert _parts(hardware_file)["SEN63C"]["present"] is True


def test_aux_sensors_found_and_gone(hardware_file):
    o3 = next(s for s in ingest.SENSOR_REGISTRY if s["name"] == "o3")
    ingest._note_aux_sensors([{**o3, "detected_addr": 0x71}])
    parts = _parts(hardware_file)
    assert parts["o3"]["present"] is True and parts["o3"]["address"] == "0x71"
    assert parts["o3"]["kind"] == "aux_sensor" and "mgs_v2" not in parts   # never had one
    ingest._note_aux_sensors([])
    assert _parts(hardware_file)["o3"]["present"] is False
