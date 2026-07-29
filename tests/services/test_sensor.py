"""tests/services/test_sensor.py

Unit tests for services.sensor:
  - extract_metric         searches nested sensor dicts
  - read_sensor            subprocess + JSON parsing + failure/reinit logic
  - _i2c_scan              parses i2cdetect output into a set of addresses
  - probe_aux_sensors      maps detected addresses to registry entries
  - read_aux_sensor        runs a driver subprocess and parses its JSON

All tests run on laptop (no hardware required).
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import services.sensor as sensor
from services.sensor import extract_metric, read_sensor


# ── extract_metric ─────────────────────────────────────────────────────────────

def test_extract_metric_finds_value_in_nested_dict():
    data = {"sen6x": {"co2": 500, "temp": 22.1}}
    assert extract_metric(data, "co2") == 500.0


def test_extract_metric_returns_none_when_key_missing():
    data = {"sen6x": {"temp": 22.1}}
    assert extract_metric(data, "co2") is None


def test_extract_metric_searches_all_sensor_dicts():
    data = {"sen6x": {"temp": 22.0}, "mgs_v2": {"no2": 150}}
    assert extract_metric(data, "no2") == 150.0


def test_extract_metric_returns_none_for_non_numeric():
    data = {"sen6x": {"co2": "bad"}}
    assert extract_metric(data, "co2") is None


def test_extract_metric_ignores_non_dict_top_level_values():
    data = {"sen6x": {"co2": 400}, "timestamp": 1234567}
    assert extract_metric(data, "co2") == 400.0


# ── read_sensor ────────────────────────────────────────────────────────────────

def _proc(stdout="", returncode=0, stderr=""):
    m = MagicMock()
    m.stdout, m.returncode, m.stderr = stdout, returncode, stderr
    return m


def test_read_sensor_parses_json_output():
    payload = {"sen6x": {"co2": 412, "temp": 22.3}}
    with patch("subprocess.run", return_value=_proc(json.dumps(payload))):
        assert read_sensor() == payload


def test_read_sensor_raises_on_nonzero_exit():
    with patch("subprocess.run", return_value=_proc(returncode=1, stderr="fail")):
        with pytest.raises(RuntimeError, match="Sensor script failed"):
            read_sensor()


def test_read_sensor_raises_on_invalid_json():
    with patch("subprocess.run", return_value=_proc("not json")):
        with pytest.raises(RuntimeError, match="invalid JSON"):
            read_sensor()


def test_read_sensor_raises_on_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 10)):
        with pytest.raises(RuntimeError, match="timed out"):
            read_sensor()


def test_read_sensor_resets_failure_counter_on_success():
    sensor._consecutive_failures = 3
    payload = {"sen6x": {"co2": 400}}
    with patch("subprocess.run", return_value=_proc(json.dumps(payload))):
        read_sensor()
    assert sensor._consecutive_failures == 0


# ── _i2c_scan ─────────────────────────────────────────────────────────────────

I2CDETECT_OUTPUT = """\
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:          -- -- -- -- -- 08 -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
40: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
50: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
60: -- -- -- UU -- -- -- -- -- -- -- -- -- -- -- -- --
70: -- -- -- 73 -- -- -- --
"""


def _scan_proc(output):
    m = MagicMock()
    m.stdout = output
    return m


def test_i2c_scan_parses_present_addresses():
    with patch("subprocess.run", return_value=_scan_proc(I2CDETECT_OUTPUT)):
        addrs = sensor._i2c_scan()
    assert 0x08 in addrs
    assert 0x73 in addrs


def test_i2c_scan_excludes_uu_entries():
    with patch("subprocess.run", return_value=_scan_proc(I2CDETECT_OUTPUT)):
        addrs = sensor._i2c_scan()
    assert 0x63 not in addrs


def test_i2c_scan_excludes_dashes():
    with patch("subprocess.run", return_value=_scan_proc(I2CDETECT_OUTPUT)):
        addrs = sensor._i2c_scan()
    assert 0x00 not in addrs
    assert 0x10 not in addrs


def test_i2c_scan_returns_empty_on_subprocess_error():
    with patch("subprocess.run", side_effect=OSError("no i2c")):
        addrs = sensor._i2c_scan()
    assert addrs == set()


# ── probe_aux_sensors ─────────────────────────────────────────────────────────

def test_probe_returns_active_sensor_when_detected_and_driver_present(tmp_path):
    driver = tmp_path / "i2c" / "mgs_v2" / "read_mgs_v2.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("# stub")

    with patch.object(sensor, "_i2c_scan", return_value={0x08}), \
         patch.object(sensor, "_BASE_DIR", tmp_path):
        active = sensor.probe_aux_sensors()

    assert len(active) == 1
    assert active[0]["name"] == "mgs_v2"
    assert active[0]["detected_addr"] == 0x08


def test_probe_skips_sensor_not_on_bus():
    with patch.object(sensor, "_i2c_scan", return_value=set()):
        active = sensor.probe_aux_sensors()
    assert active == []


def test_probe_uses_alternate_address_for_o3(tmp_path):
    driver = tmp_path / "i2c" / "o3" / "read_o3.py"
    driver.parent.mkdir(parents=True)
    driver.write_text("# stub")

    with patch.object(sensor, "_i2c_scan", return_value={0x71}), \
         patch.object(sensor, "_BASE_DIR", tmp_path):
        active = sensor.probe_aux_sensors()

    o3 = [s for s in active if s["name"] == "o3"]
    assert len(o3) == 1
    assert o3[0]["detected_addr"] == 0x71


def test_probe_fetches_driver_when_missing(tmp_path):
    with patch.object(sensor, "_i2c_scan", return_value={0x08}), \
         patch.object(sensor, "_BASE_DIR", tmp_path), \
         patch.object(sensor, "_fetch_driver", return_value=True) as mock_fetch:
        sensor.probe_aux_sensors()

    mock_fetch.assert_called_once()


def test_probe_skips_sensor_when_fetch_fails(tmp_path):
    with patch.object(sensor, "_i2c_scan", return_value={0x08}), \
         patch.object(sensor, "_BASE_DIR", tmp_path), \
         patch.object(sensor, "_fetch_driver", return_value=False):
        active = sensor.probe_aux_sensors()

    assert active == []


# ── read_aux_sensor ───────────────────────────────────────────────────────────

MGS_CFG = {"name": "mgs_v2", "driver": "i2c/mgs_v2/read_mgs_v2.py", "detected_addr": 0x08}
O3_CFG  = {"name": "o3",     "driver": "i2c/o3/read_o3.py",          "detected_addr": 0x73}


def _aux_proc(stdout, returncode=0):
    m = MagicMock()
    m.returncode, m.stdout, m.stderr = returncode, stdout, ""
    return m


def test_read_aux_sensor_returns_nested_dict():
    payload = {"mgs_v2": {"no2": 100, "co": 200, "voc": 300, "c2h5oh": 400}}
    with patch("subprocess.run", return_value=_aux_proc(json.dumps(payload))):
        assert sensor.read_aux_sensor(MGS_CFG) == payload


def test_read_aux_sensor_passes_detected_addr():
    payload = {"o3": {"o3_ppb": 22.5}}
    captured = []

    def fake_run(args, **kwargs):
        captured.extend(args)
        return _aux_proc(json.dumps(payload))

    with patch("subprocess.run", side_effect=fake_run):
        sensor.read_aux_sensor(O3_CFG)

    assert "--addr" in captured
    assert "0x73" in captured


def test_read_aux_sensor_returns_none_on_nonzero_exit():
    with patch("subprocess.run", return_value=_aux_proc("", returncode=1)):
        assert sensor.read_aux_sensor(MGS_CFG) is None


def test_read_aux_sensor_returns_none_on_invalid_json():
    with patch("subprocess.run", return_value=_aux_proc("not json")):
        assert sensor.read_aux_sensor(MGS_CFG) is None


def test_read_aux_sensor_returns_none_on_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
        assert sensor.read_aux_sensor(MGS_CFG) is None
