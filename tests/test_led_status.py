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
from unittest.mock import MagicMock, call, patch

import pytest

import led_status


def _wave_capable_pi():
    """A MagicMock pigpio.pi that accepts waves (real pigpiod returns the
    pulse count / wave id / control-block count, all >= 0)."""
    pi = MagicMock()
    pi.connected = True
    pi.wave_add_generic.return_value = 1
    pi.wave_create.return_value = 0
    pi.wave_send_repeat.return_value = 1
    return pi


def _fake_run(active: dict, restarts: dict):
    """Builds a subprocess.run replacement matching led_status._read_service_states:
    ONE `systemctl show <svc...> -p Id -p ActiveState -p NRestarts`, whose stdout
    is one blank-line-separated block per unit, in the order requested."""
    def run(cmd, **kwargs):
        result = MagicMock()
        assert cmd[:2] == ["systemctl", "show"], cmd
        units = [c for c in cmd[2:] if not c.startswith("-") and c not in ("Id", "ActiveState", "NRestarts")]
        result.stdout = "\n\n".join(
            f"Id={u}\nActiveState={'active' if active.get(u, True) else 'inactive'}\n"
            f"NRestarts={restarts.get(u, 0)}"
            for u in units) + "\n"
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
    fake_pi = _wave_capable_pi()

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

    # cleanup must halt the wave (it would otherwise keep playing with no
    # owner), drive the pin low, and disconnect
    fake_pi.wave_tx_stop.assert_called()
    fake_pi.write.assert_called_with(led_status.GPIO_LED, 0)
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
    fake_pi = _wave_capable_pi()
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


def test_connect_pigpiod_retries_until_daemon_is_up():
    pis = [MagicMock(connected=False), MagicMock(connected=False), MagicMock(connected=True)]
    fake_pigpio = MagicMock()
    fake_pigpio.pi.side_effect = pis
    with patch("time.sleep") as sleep:
        assert led_status._connect_pigpiod(fake_pigpio, timeout_s=60, poll_s=0.5) is pis[2]
    assert fake_pigpio.pi.call_count == 3
    assert sleep.call_count == 2


def test_connect_pigpiod_gives_up_after_timeout():
    fake_pigpio = MagicMock()
    fake_pigpio.pi.return_value = MagicMock(connected=False)
    assert led_status._connect_pigpiod(fake_pigpio, timeout_s=0, poll_s=0) is None


def test_boot_in_progress_true_while_systemd_is_starting():
    result = MagicMock(stdout="starting\n")
    with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=lambda s: MagicMock(read=lambda: "45.2 40.0"), __exit__=lambda *a: None))), \
         patch("subprocess.run", return_value=result):
        assert led_status._boot_in_progress() is True


def test_boot_in_progress_false_once_running():
    result = MagicMock(stdout="running\n")
    with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=lambda s: MagicMock(read=lambda: "45.2 40.0"), __exit__=lambda *a: None))), \
         patch("subprocess.run", return_value=result):
        assert led_status._boot_in_progress() is False


def test_boot_in_progress_gives_up_after_grace_period():
    # even if systemd still says "starting", a stuck boot job must not
    # suppress health checks forever
    with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=lambda s: MagicMock(read=lambda: "9999.0 9000.0"), __exit__=lambda *a: None))), \
         patch("subprocess.run") as run:
        assert led_status._boot_in_progress() is False
    run.assert_not_called()


def test_boot_in_progress_true_while_systemd_is_initializing():
    # the state systemd reports before basic.target — exactly when the LED
    # daemon now starts; regression: only "starting" used to be recognised
    result = MagicMock(stdout="initializing\n")
    with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=lambda s: MagicMock(read=lambda: "12.0 10.0"), __exit__=lambda *a: None))), \
         patch("subprocess.run", return_value=result):
        assert led_status._boot_in_progress() is True


def test_boot_in_progress_assumes_booting_when_systemctl_times_out():
    # regression (found live on a Pi Zero W): a slow `systemctl is-system-running`
    # was read as "boot finished", arming the health check ~80s early
    import subprocess
    with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=lambda s: MagicMock(read=lambda: "94.0 80.0"), __exit__=lambda *a: None))), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 20)):
        assert led_status._boot_in_progress() is True


def test_resolve_state_uses_provided_raw_state_without_reading_file(monkeypatch):
    """The render loop passes a cached raw state (polled at 5Hz) — it must not
    re-read the state file itself every tick."""
    def boom():
        raise AssertionError("_read_state must not be called when raw_state is given")
    monkeypatch.setattr(led_status, "_read_state", boom)
    assert led_status._resolve_state(_healthy(), now_mono=0.0, raw_state="ok") == "ok"



# ── pigpio wave patterns ─────────────────────────────────────────────────────
# The LED animation is played by pigpiod's DMA engine from precomputed pulse
# lists, not driven tick-by-tick from Python, so it stays smooth under CPU
# load. These tests pin the pulse lists down: exact timings, brightness, and
# the hand-over to pigpiod.

PERIOD = led_status.WAVE_PERIOD_US
PEAK_US = led_status.PEAK_STEPS * led_status.WAVE_STEP_US   # on-time per PWM period at the cap


def _total_us(segments):
    return sum(us for _, us in segments)


def _on_us(segments):
    return sum(us for level, us in segments if level)


def _level_at(segments, t_us):
    """Pin level at t_us into the cycle."""
    running = 0
    for level, us in segments:
        running += us
        if t_us < running:
            return level
    raise AssertionError("t beyond the cycle")


def _tables():
    return led_status._build_tables()


def test_constants_match_the_original_pwm_setup():
    """Same signal as the pigpio-PWM renderer: 100Hz, 2000 levels, cap 200."""
    assert PERIOD == 10_000
    assert led_status.WAVE_STEPS == 2000
    assert led_status.PEAK_STEPS == 200                  # blink/solid cap: 10%
    assert led_status.BREATH_PEAK_STEPS == 160           # breathe/pulse cap: 8%
    assert PEAK_US == 1000  # 10% of a 10ms period


def test_pattern_segments_are_whole_pwm_periods_with_no_empty_or_adjacent_duplicates():
    ok, thinking = _tables()
    for state in ("ok", "thinking", "ap", "error", "no_sensor"):
        segs = led_status._state_segments(state, ok, thinking)
        assert _total_us(segs) % PERIOD == 0, state          # loops seamlessly
        assert all(us > 0 for _, us in segs), state           # pigpio rejects zero delays
        assert all(a[0] != b[0] for a, b in zip(segs, segs[1:])), state  # merged


def test_no_sensor_is_solid_pwm_at_the_cap():
    ok, thinking = _tables()
    assert led_status._state_segments("no_sensor", ok, thinking) == [(1, PEAK_US), (0, PERIOD - PEAK_US)]


def test_error_blinks_100ms_every_second():
    ok, thinking = _tables()
    segs = led_status._state_segments("error", ok, thinking)
    assert _total_us(segs) == 1_000_000
    assert _on_us(segs) == 10 * PEAK_US                      # 10 PWM periods lit at the cap
    # all of the light falls inside the first 100ms; the rest is one long dark stretch
    assert all(start + us <= 100_000 for start, (lvl, us) in _starts(segs) if lvl)
    assert segs[-1] == (0, 1_000_000 - 91_000)               # last lit pulse ends at 91ms, then dark until 1s
    assert _level_at(segs, 500_000) == 0 and _level_at(segs, 999_999) == 0


def _starts(segs):
    running = 0
    for seg in segs:
        yield running, seg
        running += seg[1]


def test_ap_is_a_double_blink_every_2_35s():
    ok, thinking = _tables()
    segs = led_status._state_segments("ap", ok, thinking)
    assert _total_us(segs) == 2_350_000
    assert _on_us(segs) == 20 * PEAK_US                      # two 100ms windows
    lit = [(start, start + us) for start, (lvl, us) in _starts(segs) if lvl]
    assert all(end <= 100_000 or 250_000 <= start and end <= 350_000 for start, end in lit)
    assert any(start < 100_000 for start, _ in lit) and any(250_000 <= start < 350_000 for start, _ in lit)
    assert _level_at(segs, 200_000) == 0 and _level_at(segs, 1_000_000) == 0


def test_breathe_wave_matches_the_table_it_was_built_from():
    ok, thinking = _tables()
    segs = led_status._state_segments("ok", ok, thinking)
    assert abs(_total_us(segs) - ok[2] * 1e6) <= PERIOD        # cycle length = table length (to 10ms)
    breath_peak_us = led_status.BREATH_PEAK_STEPS * led_status.WAVE_STEP_US
    assert max(us for lvl, us in segs if lvl) <= breath_peak_us   # never brighter than the breathe cap (8%)
    # ...and reaches (within a step or two of) it: the table dwells only a few ms on the very
    # top values, so a 10ms sample can land just below the peak
    assert max(us for lvl, us in segs if lvl) >= 0.97 * breath_peak_us
    # mean brightness equals the table's dwell-weighted mean (what the old renderer showed)
    values, cumulative, total = ok
    dwell = [cumulative[0]] + [cumulative[i] - cumulative[i - 1] for i in range(1, len(cumulative))]
    expected_mean = sum(v * d for v, d in zip(values, dwell)) / total * led_status.WAVE_STEP_US / PERIOD
    assert _on_us(segs) / _total_us(segs) == pytest.approx(expected_mean, rel=0.03)


def test_breathe_wave_is_small_enough_for_pigpio():
    ok, thinking = _tables()
    for state in ("ok", "thinking"):
        assert len(led_status._state_segments(state, ok, thinking)) < 12000   # pigpio's per-wave pulse limit


def test_state_segments_rejects_unknown_state():
    ok, thinking = _tables()
    with pytest.raises(ValueError):
        led_status._state_segments("bogus", ok, thinking)


class _FakePigpio:
    @staticmethod
    def pulse(gpio_on, gpio_off, delay):
        return (gpio_on, gpio_off, delay)


def test_send_pattern_zeroes_pwm_then_starts_wave_then_deletes_the_old_one():
    """pigpiod ignores a wave on a pin that still has PWM set (found live —
    the boot-time dim "on" leaves exactly that), and the old wave must only
    go once the new one is playing."""
    pi = _wave_capable_pi()
    pi.wave_create.return_value = 7
    calls = []
    for name in ("wave_add_generic", "wave_create", "set_PWM_dutycycle", "wave_send_repeat", "wave_delete"):
        getattr(pi, name).side_effect = (lambda n, ret: (lambda *a, **k: (calls.append(n), ret)[1]))(
            name, {"wave_add_generic": 1, "wave_create": 7, "set_PWM_dutycycle": 0, "wave_send_repeat": 1, "wave_delete": 0}[name])
    segs = [(1, 1000), (0, 9000)]
    assert led_status._send_pattern(pi, _FakePigpio, segs, prev_wave_id=3) == 7
    assert calls == ["wave_add_generic", "wave_create", "set_PWM_dutycycle", "wave_send_repeat", "wave_delete"]
    pi.set_PWM_dutycycle.assert_called_with(led_status.GPIO_LED, 0)
    pi.wave_delete.assert_called_with(3)
    mask = 1 << led_status.GPIO_LED
    pi.wave_add_generic.assert_called_with([(mask, 0, 1000), (0, mask, 9000)])


def test_send_pattern_first_pattern_has_nothing_to_delete():
    pi = _wave_capable_pi()
    pi.wave_create.return_value = 0
    assert led_status._send_pattern(pi, _FakePigpio, [(1, 1000), (0, 9000)], prev_wave_id=None) == 0
    pi.wave_delete.assert_not_called()


@pytest.mark.parametrize("failing", ["wave_add_generic", "wave_create", "wave_send_repeat"])
def test_send_pattern_returns_none_when_pigpiod_refuses(failing):
    pi = _wave_capable_pi()
    getattr(pi, failing).return_value = -1
    assert led_status._send_pattern(pi, _FakePigpio, [(1, 1000), (0, 9000)], prev_wave_id=3) is None
    # the previously playing wave is never deleted on failure
    assert call(3) not in pi.wave_delete.call_args_list


def test_steady_glow_fallback_shows_the_cap_via_plain_pwm():
    pi = MagicMock()
    led_status._steady_glow(pi)
    pi.wave_tx_stop.assert_called_once()
    pi.set_PWM_dutycycle.assert_called_with(led_status.GPIO_LED, led_status.PEAK_STEPS)


def test_shutdown_led_never_raises_even_if_pigpiod_is_gone():
    pi = MagicMock()
    pi.wave_tx_stop.side_effect = ConnectionResetError("pigpiod died")
    led_status._shutdown_led(pi)  # must not raise from the finally block


def test_shutdown_led_halts_the_wave_and_drives_the_pin_low():
    pi = MagicMock()
    led_status._shutdown_led(pi)
    pi.wave_tx_stop.assert_called_once()
    pi.wave_clear.assert_called_once()
    pi.write.assert_called_once_with(led_status.GPIO_LED, 0)
    pi.stop.assert_called_once()


def test_health_check_uses_a_single_systemctl_spawn():
    """Regression: six spawns per check cost ~8% CPU on a Pi Zero W."""
    with patch("subprocess.run", side_effect=_fake_run(active={}, restarts={})) as run:
        led_status._check_watched_services({})
    assert run.call_count == 1


def test_health_check_ignores_a_failed_check():
    with patch("subprocess.run", side_effect=OSError("no systemctl")):
        assert led_status._check_watched_services({}) is False


def test_health_check_treats_reloading_as_active():
    out = "\n\n".join(f"Id={u}\nActiveState=reloading\nNRestarts=0" for u in led_status.WATCHED_SERVICES)
    with patch("subprocess.run", return_value=MagicMock(stdout=out)):
        assert led_status._check_watched_services({}) is False


def test_breathe_and_pulse_are_capped_below_the_blink_states():
    ok, thinking = _tables()
    cap = led_status.BREATH_PEAK_STEPS * led_status.WAVE_STEP_US
    for state in ("ok", "thinking"):
        segs = led_status._state_segments(state, ok, thinking)
        assert max(us for lvl, us in segs if lvl) <= cap < PEAK_US


def test_breathe_cycle_is_about_five_seconds_and_pulse_about_one_and_a_half():
    ok, thinking = _tables()
    assert ok[2] == pytest.approx(5.0, abs=0.15)
    assert thinking[2] == pytest.approx(1.49, abs=0.05)   # pulse speed unchanged by the dimmer peak


def test_weight_exponent_above_one_shifts_time_toward_the_dim_end():
    peak = led_status.BREATH_PEAK_STEPS
    def share_above(table, frac):
        values, cumulative, total = table
        dwell = [cumulative[0]] + [cumulative[i] - cumulative[i - 1] for i in range(1, len(cumulative))]
        return sum(d for v, d in zip(values, dwell) if v >= frac * peak) / total
    base = led_status._build_breath_table(peak, 8.0 * 1.33, 0.02 * 1.33)
    dim = led_status._build_breath_table(peak, 8.0 * 1.33, 0.02 * 1.33, weight_exponent=1.5)
    assert share_above(dim, 0.5) < share_above(base, 0.5)


def test_state_file_mtime_tracks_changes_and_tolerates_a_missing_file(monkeypatch, tmp_path):
    f = tmp_path / "state"
    monkeypatch.setattr(led_status, "LED_STATE_FILE", str(f))
    assert led_status._state_file_mtime() is None
    f.write_text("ok")
    first = led_status._state_file_mtime()
    assert first is not None
    import os
    os.utime(f, ns=(first + 10**9, first + 10**9))
    assert led_status._state_file_mtime() != first
