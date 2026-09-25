"""tests/test_device_identity.py

device_identity.enforce() decides on every schoolair start whether this uSD
card belongs to the Pi it's running on (see device_identity.py's docstring).
The hardware readers are patched out; the .env and mismatch flag live in
tmp_path.
"""

import pytest

import device_identity as di

PI_A = ("10000000abcdef01", "b8:27:eb:00:00:01")
PI_B = ("10000000abcdef02", "b8:27:eb:00:00:02")


@pytest.fixture
def pi(monkeypatch, tmp_path):
    """Returns (env_path, set_hw) — set_hw(serial, mac) picks the 'live' hardware."""
    flag = tmp_path / "identity-mismatch"
    monkeypatch.setattr(di, "MISMATCH_FILE", str(flag))
    hw = {"serial": PI_A[0], "mac": PI_A[1]}
    monkeypatch.setattr(di, "read_cpu_serial", lambda: hw["serial"])
    monkeypatch.setattr(di, "read_mac", lambda: hw["mac"])

    def set_hw(serial, mac):
        hw["serial"], hw["mac"] = serial, mac

    return tmp_path / ".env", set_hw


def _env(path):
    return di._read_env(path)


def test_unregistered_runs_and_records_nothing(pi):
    env, _ = pi
    env.write_text("AUTH_TOKEN=\nPORT=8080\n")
    assert di.enforce(env) is True
    assert di.SERIAL_KEY not in _env(env)
    assert not di.mismatch_detected()


def test_registered_without_identity_records_current_pi(pi):
    """Cards registered before this check existed: trust-on-first-use."""
    env, _ = pi
    env.write_text("AUTH_TOKEN=aB3xQr7Z\nNEW_AUTH_TOKEN=deadbeef\nPORT=8080\n")
    assert di.enforce(env) is True
    saved = _env(env)
    assert (saved[di.SERIAL_KEY], saved[di.MAC_KEY]) == PI_A
    # Existing keys untouched
    assert saved["NEW_AUTH_TOKEN"] == "deadbeef" and saved["PORT"] == "8080"


def test_unreadable_hardware_is_not_recorded(pi):
    env, set_hw = pi
    env.write_text("NEW_AUTH_TOKEN=deadbeef\n")
    set_hw(di.UNKNOWN, PI_A[1])
    assert di.enforce(env) is True
    assert di.SERIAL_KEY not in _env(env)


def test_matching_identity_runs_and_clears_stale_flag(pi):
    env, _ = pi
    env.write_text(f"NEW_AUTH_TOKEN=x\n{di.SERIAL_KEY}={PI_A[0]}\n{di.MAC_KEY}={PI_A[1]}\n")
    open(di.MISMATCH_FILE, "w").close()
    assert di.enforce(env) is True
    assert not di.mismatch_detected()


def test_matching_is_case_insensitive(pi):
    env, _ = pi
    env.write_text(f"NEW_AUTH_TOKEN=x\n{di.SERIAL_KEY}={PI_A[0].upper()}\n"
                   f"{di.MAC_KEY}={PI_A[1].upper()}\n")
    assert di.enforce(env) is True


def test_swapped_card_is_refused_and_flagged(pi):
    env, set_hw = pi
    env.write_text(f"NEW_AUTH_TOKEN=x\n{di.SERIAL_KEY}={PI_A[0]}\n{di.MAC_KEY}={PI_A[1]}\n")
    set_hw(*PI_B)
    assert di.enforce(env) is False
    assert di.mismatch_detected()
    # The saved identity is NOT overwritten — only a re-registration may do that.
    assert (_env(env)[di.SERIAL_KEY], _env(env)[di.MAC_KEY]) == PI_A


@pytest.mark.parametrize("hw", [(PI_B[0], PI_A[1]), (PI_A[0], PI_B[1]), (di.UNKNOWN, PI_A[1])])
def test_any_single_field_mismatch_is_refused(pi, hw):
    env, set_hw = pi
    env.write_text(f"NEW_AUTH_TOKEN=x\n{di.SERIAL_KEY}={PI_A[0]}\n{di.MAC_KEY}={PI_A[1]}\n")
    set_hw(*hw)
    assert di.enforce(env) is False


def test_save_rebinds_to_current_pi(pi):
    """What the wizard does after re-registering a swapped card."""
    env, set_hw = pi
    env.write_text(f"NEW_AUTH_TOKEN=x\n{di.SERIAL_KEY}={PI_A[0]}\n{di.MAC_KEY}={PI_A[1]}\n")
    set_hw(*PI_B)
    assert di.enforce(env) is False
    assert di.save(env) == PI_B
    di.clear_mismatch()
    assert di.enforce(env) is True
    assert not di.mismatch_detected()


def test_save_appends_to_file_without_trailing_newline(pi):
    env, _ = pi
    env.write_text("NEW_AUTH_TOKEN=x")
    di.save(env)
    assert _env(env) == {"NEW_AUTH_TOKEN": "x", di.SERIAL_KEY: PI_A[0], di.MAC_KEY: PI_A[1]}


def test_flag_write_failure_still_refuses(pi, monkeypatch, tmp_path):
    """No RuntimeDirectory (e.g. a dev checkout): still refuse to run."""
    env, set_hw = pi
    monkeypatch.setattr(di, "MISMATCH_FILE", str(tmp_path / "missing-dir" / "flag"))
    env.write_text(f"NEW_AUTH_TOKEN=x\n{di.SERIAL_KEY}={PI_A[0]}\n{di.MAC_KEY}={PI_A[1]}\n")
    set_hw(*PI_B)
    assert di.enforce(env) is False


def test_schoolair_unit_matches_module_constants():
    """schoolair.service must not restart-loop on the mismatch exit, and must
    provide the /run directory the flag lives in."""
    from pathlib import Path
    unit = (Path(di.__file__).parent / "deploy" / "schoolair.service").read_text()
    assert f"RestartPreventExitStatus={di.EXIT_MISMATCH}" in unit
    assert di.MISMATCH_FILE.startswith("/run/schoolair/")
    assert "RuntimeDirectory=schoolair" in unit
    assert "RuntimeDirectoryPreserve=yes" in unit
