"""tests/test_led_status.py

Unit tests for led_status.py's independent service-health check
(_check_watched_services). This is the piece that catches a scenario the
"read LED_STATE_FILE" path can't: the process that would normally write
"error" is itself what's broken, so nothing ever writes anything — see
the module docstring for the real live-fire test that found this gap.

subprocess.run is mocked throughout — no real systemctl/systemd involved.
"""

import signal
import sys
from unittest.mock import MagicMock, patch

import pytest

import led_status


def _fake_run(active: dict, restarts: dict):
    """Builds a subprocess.run replacement matching led_status._service_is_active
    (systemctl is-active --quiet <svc>, returncode 0/1) and
    _service_restarts (systemctl show <svc> -p NRestarts --value, stdout)."""
    def run(cmd, **kwargs):
        result = MagicMock()
        if cmd[:2] == ["systemctl", "is-active"]:
            svc = cmd[-1]
            result.returncode = 0 if active.get(svc, True) else 1
        elif cmd[:2] == ["systemctl", "show"]:
            svc = cmd[2]
            result.stdout = str(restarts.get(svc, 0))
        return result
    return run


def test_healthy_when_all_active_and_no_restart_change():
    last_restarts = {svc: 0 for svc in led_status.WATCHED_SERVICES}
    with patch("subprocess.run", side_effect=_fake_run(active={}, restarts={s: 0 for s in led_status.WATCHED_SERVICES})):
        unhealthy = led_status._check_watched_services(last_restarts)
    assert unhealthy is False


def test_unhealthy_when_a_service_is_inactive():
    last_restarts = {svc: 0 for svc in led_status.WATCHED_SERVICES}
    target = led_status.WATCHED_SERVICES[1]
    with patch("subprocess.run", side_effect=_fake_run(
        active={target: False}, restarts={s: 0 for s in led_status.WATCHED_SERVICES},
    )):
        unhealthy = led_status._check_watched_services(last_restarts)
    assert unhealthy is True


def test_unhealthy_when_restart_count_increases_even_if_active():
    """The exact scenario a real live-fire test caught: is-active reports
    true (Type=simple marks it active the instant it's spawned) but the
    restart count climbed since the last check — it crashed and came back
    up within this window."""
    target = led_status.WATCHED_SERVICES[0]
    last_restarts = {svc: 0 for svc in led_status.WATCHED_SERVICES}
    last_restarts[target] = 2

    with patch("subprocess.run", side_effect=_fake_run(
        active={}, restarts={**{s: 0 for s in led_status.WATCHED_SERVICES}, target: 5},
    )):
        unhealthy = led_status._check_watched_services(last_restarts)

    assert unhealthy is True
    assert last_restarts[target] == 5  # baseline updated for the next check


def test_first_call_seeds_baseline_without_flagging_unhealthy():
    """A service that already had restarts before this process started
    shouldn't be flagged just for existing in that state — only a further
    increase, observed between two of our own checks, counts."""
    last_restarts: dict = {}  # nothing recorded yet

    with patch("subprocess.run", side_effect=_fake_run(
        active={}, restarts={s: 7 for s in led_status.WATCHED_SERVICES},
    )):
        unhealthy = led_status._check_watched_services(last_restarts)

    assert unhealthy is False
    assert last_restarts == {s: 7 for s in led_status.WATCHED_SERVICES}


def test_second_call_with_unchanged_restarts_stays_healthy():
    last_restarts = {s: 4 for s in led_status.WATCHED_SERVICES}
    with patch("subprocess.run", side_effect=_fake_run(
        active={}, restarts={s: 4 for s in led_status.WATCHED_SERVICES},
    )):
        unhealthy = led_status._check_watched_services(last_restarts)
    assert unhealthy is False


# ── SIGTERM handling ────────────────────────────────────────────────────────
#
# Python's default SIGTERM disposition kills the process outright and never
# runs a `finally` block — found live: "systemctl stop"/"restart" left the
# LED frozen at its last duty cycle, looking "on" for a daemon that was
# actually dead. main() now converts SIGTERM into SystemExit so its finally
# block (which zeroes the duty cycle) still runs.

def test_on_sigterm_raises_systemexit():
    """The registered handler itself: SIGTERM -> SystemExit, not a raw kill."""
    with pytest.raises(SystemExit):
        led_status._on_sigterm(signal.SIGTERM, None)


def test_main_turns_led_off_on_sigterm(monkeypatch, tmp_path):
    """End-to-end: simulate SIGTERM arriving mid-loop and confirm the finally
    block's cleanup — set_PWM_dutycycle(GPIO_LED, 0) then pi.stop() — actually
    runs, rather than the process just dying with the LED stuck lit."""
    fake_pi = MagicMock()
    fake_pi.connected = True
    fake_pi.get_PWM_real_range.return_value = 2000

    fake_pigpio = MagicMock()
    fake_pigpio.pi.return_value = fake_pi

    unit_type_file = tmp_path / "schoolair-unit-type"
    unit_type_file.write_text("indoor")
    led_state_file = tmp_path / "schoolair-led-state"

    monkeypatch.setattr(led_status, "LED_STATE_FILE", str(led_state_file))
    monkeypatch.setattr(led_status, "_read_state", lambda: "ok")
    monkeypatch.setattr(led_status, "_health_monitor_loop", lambda health: None)
    monkeypatch.setattr(__import__("threading"), "Thread", lambda *a, **k: MagicMock())

    real_open = open
    def fake_open(path, *args, **kwargs):
        if path == "/etc/schoolair-unit-type":
            path = str(unit_type_file)
        return real_open(path, *args, **kwargs)

    registered_handler = {}
    def fake_signal(signum, handler):
        registered_handler[signum] = handler

    def fake_sleep(seconds):
        # Simulate the signal arriving during the loop's sleep.
        registered_handler[signal.SIGTERM](signal.SIGTERM, None)

    with patch.dict(sys.modules, {"pigpio": fake_pigpio}), \
         patch("builtins.open", side_effect=fake_open), \
         patch("signal.signal", side_effect=fake_signal), \
         patch("time.sleep", side_effect=fake_sleep):
        with pytest.raises(SystemExit):
            led_status.main()

    fake_pi.set_PWM_dutycycle.assert_called_with(led_status.GPIO_LED, 0)
    fake_pi.stop.assert_called_once()
