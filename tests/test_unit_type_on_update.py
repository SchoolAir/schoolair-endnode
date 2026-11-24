"""An OTA update must detect the unit type when it is missing.

Units first set up with older firmware never got /etc/schoolair-unit-type
(only first_boot.sh and a fresh install wrote it), so their updates skipped
everything indoor-only, the flower included. Found on unit 26429 (2026-11-17).
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = (ROOT / "schoolair_setup.sh").read_text()


def _update_branch() -> str:
    start = SETUP.index('skip "8–12 / I2C + networking config  (update mode')
    return SETUP[start:SETUP.index("\nfi\n", start)]


def test_update_mode_detects_the_unit_type():
    assert "ensure_unit_type" in _update_branch()


def test_setup_mode_still_detects_after_enabling_i2c():
    start = SETUP.index('step "8 / I2C enable + baudrate"')
    block = SETUP[start:]
    assert block.index("do_i2c 0") < block.index("ensure_unit_type")


def test_detection_comes_after_sen6x_is_stopped_and_before_the_flower():
    stop = SETUP.index("systemctl stop sen6x")
    define = SETUP.index("ensure_unit_type() {")
    calls = [i for i in range(len(SETUP)) if SETUP.startswith("ensure_unit_type\n", i)]
    flower = SETUP.index("# ── Wilting flower (indoor units)")
    assert len(calls) == 2
    assert stop < define < min(calls) and max(calls) < flower


def test_a_known_unit_type_is_never_overwritten():
    fn = SETUP[SETUP.index("ensure_unit_type() {"):]
    fn = fn[:fn.index("\n}\n")]
    assert fn.index("[ -s /etc/schoolair-unit-type ]") < fn.index("detect_unit_type.sh")


def test_update_keeps_the_boot_cmdline():
    fn = SETUP[SETUP.index("ensure_unit_type() {"):]
    assert 'KEEP_CMDLINE="$([[ "$MODE" == "--update" ]]' in fn[:fn.index("\n}\n")]
    detect = (ROOT / "detect_unit_type.sh").read_text()
    assert '"${KEEP_CMDLINE:-0}" != 1 ] && grep -q "console=serial0"' in detect


def _run_detect(tmp_path, json, keep="1"):
    fake = tmp_path / "sen6x_read"
    fake.write_text(f"#!/bin/sh\necho '{json}'\n")
    fake.chmod(0o755)
    out = tmp_path / "unit-type"
    env = {**os.environ, "SEN6X_READ": str(fake), "UNIT_TYPE_FILE": str(out),
           "HW_RECORD": str(tmp_path / "none.py"), "KEEP_CMDLINE": keep}
    r = subprocess.run(["bash", str(ROOT / "detect_unit_type.sh")], env=env,
                       capture_output=True, text=True)
    return r, out


def test_detect_writes_indoor_for_a_sen63c(tmp_path):
    r, out = _run_detect(tmp_path, '{"co2": 600}')
    assert r.returncode == 0 and out.read_text().strip() == "indoor"
    assert "Serial console disabled" not in r.stdout


def test_detect_writes_outdoor_for_a_sen65(tmp_path):
    r, out = _run_detect(tmp_path, '{"voc": 100}')
    assert r.returncode == 0 and out.read_text().strip() == "outdoor"


def test_detect_leaves_the_type_alone_without_a_sensor(tmp_path):
    r, out = _run_detect(tmp_path, '{"error": "no sensor"}')
    assert r.returncode == 1 and not out.exists()
