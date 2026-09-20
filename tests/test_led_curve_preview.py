"""tests/test_led_curve_preview.py

Smoke test for scripts/led_curve_preview.py — a hardware-facing dev tool that had
silently rotted once (it called helpers that led_status.py had since removed, and
only failed on the Pi). Runs its whole main() against a fake pigpio.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "led_curve_preview.py"


@pytest.fixture
def preview():
    spec = importlib.util.spec_from_file_location("led_curve_preview", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_candidate_builds_a_valid_pulse_list(preview):
    for label, (_, build) in preview.CANDIDATES.items():
        segs = preview.CANDIDATES[label][1]()
        assert segs and all(us > 0 for _, us in segs), label
        assert sum(us for _, us in segs) % preview.L.WAVE_PERIOD_US == 0, label
        assert preview.describe(segs)


def test_blink_marker_is_a_valid_pulse_list(preview):
    segs = preview.blink_segments()
    assert sum(us for _, us in segs) == 350_000
    assert max(us for lvl, us in segs if lvl) == preview.L.PEAK_US


def test_main_plays_each_candidate_and_restores_the_led_service(preview, monkeypatch):
    pi = MagicMock(connected=True)
    pi.wave_add_generic.return_value = 1
    pi.wave_create.return_value = 0
    pi.wave_send_repeat.return_value = 1
    pi.get_PWM_real_range.return_value = 10000
    fake_pigpio = MagicMock(OUTPUT=1)
    fake_pigpio.pi.return_value = pi
    fake_pigpio.pulse = lambda a, b, c: (a, b, c)
    calls = []
    monkeypatch.setattr(sys, "argv", ["led_curve_preview.py", "-s", "0", "A", "B"])
    with patch.dict(sys.modules, {"pigpio": fake_pigpio}), \
         patch.object(preview.subprocess, "run", side_effect=lambda *a, **k: calls.append(a[0])), \
         patch.object(preview.time, "sleep"):
        preview.main()
    assert calls[0] == ["systemctl", "stop", "schoolair-led"]
    assert calls[-1] == ["systemctl", "start", "schoolair-led"]       # always given back
    assert pi.wave_send_repeat.call_count == 4                         # marker + curve, twice


def test_main_restores_the_led_service_even_if_playing_fails(preview, monkeypatch):
    pi = MagicMock(connected=True)
    pi.wave_add_generic.side_effect = RuntimeError("pigpiod died")
    pi.get_PWM_real_range.return_value = 10000
    fake_pigpio = MagicMock(OUTPUT=1)
    fake_pigpio.pi.return_value = pi
    fake_pigpio.pulse = lambda a, b, c: (a, b, c)
    calls = []
    monkeypatch.setattr(sys, "argv", ["led_curve_preview.py", "-s", "0", "A"])
    with patch.dict(sys.modules, {"pigpio": fake_pigpio}), \
         patch.object(preview.subprocess, "run", side_effect=lambda *a, **k: calls.append(a[0])), \
         patch.object(preview.time, "sleep"), pytest.raises(RuntimeError):
        preview.main()
    assert calls[-1] == ["systemctl", "start", "schoolair-led"]


def test_modes_plays_every_status_mode_once_and_restores_the_led_service(preview, monkeypatch, capsys):
    pi = MagicMock(connected=True)
    pi.wave_add_generic.return_value = 1
    pi.wave_create.return_value = 0
    pi.wave_send_repeat.return_value = 1
    pi.get_PWM_real_range.return_value = 10000
    fake_pigpio = MagicMock(OUTPUT=1)
    fake_pigpio.pi.return_value = pi
    fake_pigpio.pulse = lambda a, b, c: (a, b, c)
    calls = []
    monkeypatch.setattr(sys, "argv", ["led_curve_preview.py", "--modes", "-s", "0.001"])
    with patch.dict(sys.modules, {"pigpio": fake_pigpio}), \
         patch.object(preview.subprocess, "run", side_effect=lambda *a, **k: calls.append(a[0])), \
         patch.object(preview.time, "sleep"):
        preview.main()
    assert pi.wave_send_repeat.call_count == 5                          # thinking, ok, ap, error, no_sensor
    out = capsys.readouterr().out
    for state in ("thinking", "ok", "ap", "error", "no_sensor"):
        assert state in out
    assert calls[0] == ["systemctl", "stop", "schoolair-led"]
    assert calls[-1] == ["systemctl", "start", "schoolair-led"]


def test_every_mode_in_the_showcase_is_a_real_led_state(preview):
    for state, _, _ in preview.MODES:
        assert preview.L._state_segments(state)
