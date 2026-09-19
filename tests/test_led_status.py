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


# ── _is_registered ───────────────────────────────────────────────────────────

def test_is_registered_true_when_new_auth_token_set(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("NEW_SERVER_URL=https://example.com\nNEW_AUTH_TOKEN=abc123\n")
    monkeypatch.setattr(led_status, "ENV_FILE", str(env))
    assert led_status._is_registered() is True


def test_is_registered_false_when_new_auth_token_empty(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("NEW_AUTH_TOKEN=\n")
    monkeypatch.setattr(led_status, "ENV_FILE", str(env))
    assert led_status._is_registered() is False


def test_is_registered_false_when_key_absent(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SOME_OTHER_KEY=1\n")
    monkeypatch.setattr(led_status, "ENV_FILE", str(env))
    assert led_status._is_registered() is False


def test_is_registered_false_when_env_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(led_status, "ENV_FILE", str(tmp_path / "does-not-exist.env"))
    assert led_status._is_registered() is False


# ── _resolve_state precedence ────────────────────────────────────────────────
#
# Found live: an unregistered device correctly showed "ap" (double blink)
# while waiting in the wizard's AP mode, but flipped to "error" (single
# blink) once jobs/ingest.py's upload loop took a sensor reading and tried
# (and, expectedly, failed) to upload it with no token yet. A networking/
# auth failure is normal during AP-mode setup — it shouldn't look like a
# real problem. A genuine internal error should still win either way.

def _healthy(): return {"unhealthy_until": 0.0}


def test_resolve_state_downgrades_error_to_ap_when_unregistered(monkeypatch):
    monkeypatch.setattr(led_status, "_read_state", lambda: "error")
    monkeypatch.setattr(led_status, "_is_registered", lambda: False)
    assert led_status._resolve_state(_healthy(), now_mono=0.0) == "ap"


def test_resolve_state_keeps_error_when_registered(monkeypatch):
    """Same "error" from LED_STATE_FILE, but on an already-registered
    device — this is now a real signal and must not be downgraded."""
    monkeypatch.setattr(led_status, "_read_state", lambda: "error")
    monkeypatch.setattr(led_status, "_is_registered", lambda: True)
    assert led_status._resolve_state(_healthy(), now_mono=0.0) == "error"


def test_resolve_state_passes_through_no_sensor_when_unregistered(monkeypatch):
    """A real hardware problem must not get swept under the AP-mode rug."""
    monkeypatch.setattr(led_status, "_read_state", lambda: "no_sensor")
    monkeypatch.setattr(led_status, "_is_registered", lambda: False)
    assert led_status._resolve_state(_healthy(), now_mono=0.0) == "no_sensor"


def test_resolve_state_passes_through_ap_unchanged(monkeypatch):
    monkeypatch.setattr(led_status, "_read_state", lambda: "ap")
    monkeypatch.setattr(led_status, "_is_registered", lambda: False)
    assert led_status._resolve_state(_healthy(), now_mono=0.0) == "ap"


def test_resolve_state_health_check_overrides_even_ap_downgrade(monkeypatch):
    """A genuinely crashed watched service always wins — even over the
    AP-mode downgrade of an "error" that would otherwise apply here."""
    monkeypatch.setattr(led_status, "_read_state", lambda: "error")
    monkeypatch.setattr(led_status, "_is_registered", lambda: False)
    unhealthy = {"unhealthy_until": 100.0}
    assert led_status._resolve_state(unhealthy, now_mono=50.0) == "error"


def test_resolve_state_health_check_overrides_ok(monkeypatch):
    monkeypatch.setattr(led_status, "_read_state", lambda: "ok")
    monkeypatch.setattr(led_status, "_is_registered", lambda: True)
    unhealthy = {"unhealthy_until": 100.0}
    assert led_status._resolve_state(unhealthy, now_mono=50.0) == "error"


# ── main() always resets LED_STATE_FILE to "thinking" on startup ───────────

def test_main_resets_stale_state_file_to_thinking_on_startup(monkeypatch, tmp_path):
    """Found live: a race with another writer (e.g. jobs/ingest.py's
    no-token branch) could leave a stale/wrong value as the very first
    thing ever rendered, if it wrote before led_status.py initialised the
    file. main() must unconditionally reset to "thinking" on startup,
    even if the file already exists with something else."""
    fake_pi = MagicMock()
    fake_pi.connected = True
    fake_pi.get_PWM_real_range.return_value = 2000
    fake_pigpio = MagicMock()
    fake_pigpio.pi.return_value = fake_pi

    unit_type_file = tmp_path / "schoolair-unit-type"
    unit_type_file.write_text("indoor")
    led_state_file = tmp_path / "schoolair-led-state"
    led_state_file.write_text("error")  # stale/racy pre-existing value

    monkeypatch.setattr(led_status, "LED_STATE_FILE", str(led_state_file))
    monkeypatch.setattr(led_status, "_health_monitor_loop", lambda health: None)
    monkeypatch.setattr(__import__("threading"), "Thread", lambda *a, **k: MagicMock())

    real_open = open
    def fake_open(path, *args, **kwargs):
        if path == "/etc/schoolair-unit-type":
            path = str(unit_type_file)
        return real_open(path, *args, **kwargs)

    def fake_signal(signum, handler):
        pass

    def fake_sleep(seconds):
        raise SystemExit(0)  # stop after the first loop iteration

    with patch.dict(sys.modules, {"pigpio": fake_pigpio}), \
         patch("builtins.open", side_effect=fake_open), \
         patch("signal.signal", side_effect=fake_signal), \
         patch("time.sleep", side_effect=fake_sleep):
        with pytest.raises(SystemExit):
            led_status.main()

    # The render loop only ever reads LED_STATE_FILE, never writes it — so
    # if this still reads "thinking" (not the pre-seeded "error"), main()'s
    # startup reset is what did it.
    assert led_state_file.read_text() == "thinking"
