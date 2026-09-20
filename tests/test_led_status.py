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
    pi.get_PWM_real_range.return_value = 10000   # 1us pigpiod sample rate at 100Hz
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
PEAK_US = led_status.PEAK_US                                 # on-time per PWM period at the cap


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


def test_pwm_constants():
    """100Hz PWM; every pattern is capped at BRIGHTNESS x 10% duty; 1us pulse resolution by default."""
    assert PERIOD == 10_000
    assert led_status.BRIGHTNESS == 0.8
    assert PEAK_US == 800                                 # 8%: ALL states, not just the breath
    assert led_status.WAVE_STEP_US == 1


def test_pattern_segments_are_whole_pwm_periods_with_no_empty_or_adjacent_duplicates():
    for state in ("ok", "thinking", "ap", "error", "no_sensor"):
        segs = led_status._state_segments(state)
        assert _total_us(segs) % PERIOD == 0, state          # loops seamlessly
        assert all(us > 0 for _, us in segs), state           # pigpio rejects zero delays
        assert all(a[0] != b[0] for a, b in zip(segs, segs[1:])), state  # merged


def test_no_sensor_is_solid_pwm_at_the_cap():
    assert led_status._state_segments("no_sensor") == [(1, PEAK_US), (0, PERIOD - PEAK_US)]


def test_error_blinks_100ms_every_second():
    segs = led_status._state_segments("error")
    assert _total_us(segs) == 1_000_000
    assert _on_us(segs) == 10 * PEAK_US                      # 10 PWM periods lit at the cap
    # all of the light falls inside the first 100ms; the rest is one long dark stretch
    assert all(start + us <= 100_000 for start, (lvl, us) in _starts(segs) if lvl)
    assert segs[-1] == (0, 1_000_000 - 90_000 - PEAK_US)      # last lit pulse ends at 90ms + 0.8ms, then dark until 1s
    assert _level_at(segs, 500_000) == 0 and _level_at(segs, 999_999) == 0


def _starts(segs):
    running = 0
    for seg in segs:
        yield running, seg
        running += seg[1]


def test_ap_is_a_double_blink_every_2_35s():
    segs = led_status._state_segments("ap")
    assert _total_us(segs) == 2_350_000
    assert _on_us(segs) == 20 * PEAK_US                      # two 100ms windows
    lit = [(start, start + us) for start, (lvl, us) in _starts(segs) if lvl]
    assert all(end <= 100_000 or 250_000 <= start and end <= 350_000 for start, end in lit)
    assert any(start < 100_000 for start, _ in lit) and any(250_000 <= start < 350_000 for start, _ in lit)
    assert _level_at(segs, 200_000) == 0 and _level_at(segs, 1_000_000) == 0


def _per_period_on_us(segments):
    """The on-time of each 10ms PWM period, recovered from a pulse list."""
    out, on, acc = [], 0, 0
    for level, us in segments:
        while us > 0:
            take = min(us, PERIOD - acc)
            on += take if level else 0
            acc += take
            us -= take
            if acc == PERIOD:
                out.append(on)
                on, acc = 0, 0
    return out


def _share_above_midpoint(on_us_per_period):
    """Fraction of the cycle spent above perceived-lightness L* = 50."""
    return sum(1 for x in on_us_per_period
               if led_status._luminance_to_lightness(x / PEAK_US) >= 50) / len(on_us_per_period)


def test_breathe_wave_is_a_five_second_perceptual_cosine_capped_at_the_brightness_scale():
    segs = led_status._state_segments("ok")
    per = _per_period_on_us(segs)
    assert _total_us(segs) == 5_000_000 == led_status.OK_CYCLE_S * 1_000_000
    assert max(per) == PEAK_US                                   # reaches the cap exactly, never above
    assert per[0] == 0 and per[len(per) // 2] == PEAK_US         # dark at the start, peak at mid-cycle
    # quantisation to whole microseconds keeps the wanted average brightness
    model_mean = sum(led_status._breath_on_us(i * PERIOD, 5.0) for i in range(len(per))) / len(per)
    assert sum(per) / len(per) == pytest.approx(model_mean, rel=0.005)


def test_symmetric_breath_spends_half_its_cycle_above_the_perceptual_midpoint():
    """The point of defining the curve in perceived lightness: the old table
    timing spent 68% of the cycle above the midpoint (L* 50); a symmetric cosine
    spends 50%."""
    per = _per_period_on_us(led_status._breath_segments(5.0, shape=1.0))
    assert _share_above_midpoint(per) == pytest.approx(0.50, abs=0.03)


def test_default_breath_spends_forty_percent_above_the_midpoint():
    """Shape 1.6, chosen by eye (of 1.0 / 1.3 / 1.6) as the least bright-for-too-long."""
    assert led_status.BREATH_SHAPE_EXPONENT == 1.6
    for state in ("ok", "thinking"):
        per = _per_period_on_us(led_status._state_segments(state))
        assert _share_above_midpoint(per) == pytest.approx(0.40, abs=0.03), state


def test_breath_shape_exponent_above_one_spends_less_time_bright():
    base = _per_period_on_us(led_status._breath_segments(5.0, shape=1.0))
    dim = _per_period_on_us(led_status._breath_segments(5.0, shape=1.3))
    assert _share_above_midpoint(dim) < _share_above_midpoint(base) - 0.03


def test_lightness_to_luminance_is_the_cie_1976_inverse():
    f = led_status._lightness_to_luminance
    assert f(0.0) == 0.0 and f(1.0) == pytest.approx(1.0)
    assert f(0.5) == pytest.approx(0.184, abs=0.001)             # L* 50 = 18.4% of full luminance
    assert f(0.08) == pytest.approx(8 / 903.3)                   # toe, meets the cube-root branch at L* 8
    assert f(0.08 + 1e-9) == pytest.approx(f(0.08), abs=1e-6)    # continuous there
    steps = [f(i / 100) for i in range(101)]
    assert steps == sorted(steps)                                # monotonic
    for p in (0.02, 0.3, 0.5, 0.9):                              # round trip with the forward transform
        assert led_status._luminance_to_lightness(f(p)) == pytest.approx(100 * p, rel=1e-6)
    assert f(-1) == 0.0 and f(2) == pytest.approx(1.0)           # clamped


def test_breathe_wave_is_small_enough_for_pigpio():
    for state in ("ok", "thinking"):
        assert len(led_status._state_segments(state)) < 12000   # pigpio's per-wave pulse limit


def test_state_segments_rejects_unknown_state():
    with pytest.raises(ValueError):
        led_status._state_segments("bogus")


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
    pi = _wave_capable_pi()
    led_status._steady_glow(pi)
    pi.wave_tx_stop.assert_called_once()
    pi.set_PWM_dutycycle.assert_called_with(led_status.GPIO_LED, round(10000 * 800 / 10000))   # 8% of pigpiod's PWM range


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


def test_every_state_is_capped_at_the_same_brightness():
    """BRIGHTNESS applies to all modes: breathe, pulse, blinks and solid alike."""
    for state in ("ok", "thinking", "ap", "error", "no_sensor"):
        segs = led_status._state_segments(state)
        assert max(us for lvl, us in segs if lvl) <= PEAK_US, state
    assert max(us for lvl, us in led_status._state_segments("error") if lvl) == PEAK_US


def test_cycle_lengths():
    assert _total_us(led_status._state_segments("ok")) == 5_000_000
    assert _total_us(led_status._state_segments("thinking")) == 1_500_000


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


# ── dim-end resolution: no visible stepping, no jitter ───────────────────────

def test_detect_step_us_reads_pigpiods_sample_rate():
    """real_range at 100Hz is the number of sample ticks per PWM period."""
    pi = _wave_capable_pi()
    pi.get_PWM_real_range.return_value = 10000          # pigpiod -s 1
    assert led_status._detect_step_us(pi) == 1
    pi.get_PWM_real_range.return_value = 2000           # pigpiod's default -s 5
    assert led_status._detect_step_us(pi) == 5
    pi.set_PWM_frequency.assert_called_with(led_status.GPIO_LED, led_status.PWM_FREQ_HZ)


def test_on_times_are_quantised_to_the_pulse_resolution(monkeypatch):
    for step in (1, 5):
        monkeypatch.setattr(led_status, "WAVE_STEP_US", step)
        per = _per_period_on_us(led_status._state_segments("ok"))
        assert all(x % step == 0 for x in per), step


def _dim_end_quality(step, monkeypatch):
    """(worst gap between the realised and the ideal fade in CIE L*, eye-averaged over
    ~60ms; longest time stuck on one level while the ideal moved >1.5 L* away)."""
    monkeypatch.setattr(led_status, "WAVE_STEP_US", step)
    per = _per_period_on_us(led_status._breath_segments(5.0))
    n = len(per)
    ideal = [led_status._breath_on_us(i * PERIOD, 5.0) for i in range(n)]
    lstar = lambda on: led_status._luminance_to_lightness(on / PEAK_US)
    worst = 0.0
    for i in range(n):
        a, b = max(0, i - 3), min(n, i + 4)
        worst = max(worst, abs(lstar(sum(per[a:b]) / (b - a)) - lstar(sum(ideal[a:b]) / (b - a))))
    stuck = best = 0
    for i in range(1, n):
        if per[i] == per[i - 1] and per[i] > 0 and abs(lstar(ideal[i]) - lstar(per[i])) > 1.5:
            stuck += 1
        else:
            stuck = 0
        best = max(best, stuck)
    return worst, best * PERIOD / 1000


def test_one_microsecond_resolution_gives_a_smooth_dim_end(monkeypatch):
    """Regression for the visible stepping / jitter at the bottom of the breath: with
    5us steps the realised fade strays >2 L* from the ideal and sticks on a level;
    at 1us it stays well under one visible step."""
    worst_1, stuck_1 = _dim_end_quality(1, monkeypatch)
    worst_5, stuck_5 = _dim_end_quality(5, monkeypatch)
    assert worst_1 < 0.5 and stuck_1 == 0
    assert worst_5 > 1.5 and stuck_5 > 0                 # documents what the old resolution did
    assert worst_1 < worst_5 / 4


# ── the very bottom: dithering of the last few microseconds ──────────────────

def test_dither_keeps_sub_microsecond_levels_right_on_average():
    """A wanted 0.3us on-time is below one 1us step: plain rounding gives 0 forever
    (the fade holds dark, then jumps to 1us); error diffusion averages it out."""
    plain = led_status._pattern_segments_us(lambda t: 0.3, 2.0)                       # 200 slots
    dithered = led_status._pattern_segments_us(lambda t: 0.3, 2.0, dither_below_us=16)
    assert _on_us(plain) == 0
    assert _on_us(dithered) == pytest.approx(0.3 * 200, abs=1)
    assert set(_per_period_on_us(dithered)) == {0, 1}


def test_dither_only_applies_below_its_threshold():
    """Above the threshold the steps are small relative jumps: identical to plain rounding."""
    fn = lambda t: 40.4 if t < 1_000_000 else 3.4
    dithered = _per_period_on_us(led_status._pattern_segments_us(fn, 2.0, dither_below_us=16))
    plain = _per_period_on_us(led_status._pattern_segments_us(fn, 2.0))
    assert dithered[:100] == plain[:100] == [40] * 100
    assert dithered[100:] != plain[100:]                   # the dim half is dithered (3, 4, 3, 3, 4 ...)
    assert sum(dithered[100:]) == pytest.approx(3.4 * 100, abs=1)


def test_dither_is_off_at_5us_resolution(monkeypatch):
    """At 5us steps the same dithering measured as jitter (32% slot-to-slot deviation)."""
    monkeypatch.setattr(led_status, "WAVE_STEP_US", 5)
    per = _per_period_on_us(led_status._breath_segments(5.0))
    assert all(x % 5 == 0 for x in per)
    assert per == _per_period_on_us(led_status._breath_segments(5.0, dither_below_us=0.0))


def test_dithering_smooths_the_bottom_of_the_fade_as_the_eye_sees_it():
    """Regression for the visibly stepped very-bottom (worst on the way down), on the
    curve WITHOUT a black point: plain 1us rounding holds levels 1-2us for 80-140ms, so
    100ms-window brightness strays from the ideal by up to 100% (rms 18%); dithered it
    is 35% worst / 4% rms."""
    n = 500
    ideal = [led_status._breath_on_us(i * PERIOD, 5.0, black_point_us=0.0) for i in range(n)]

    def eye_error(per):
        rel = []
        for i in range(n - 10):
            want = sum(ideal[i:i + 10]) / 10
            if want >= 0.15:
                rel.append(abs(sum(per[i:i + 10]) / 10 - want) / want)
        return max(rel), (sum(r * r for r in rel) / len(rel)) ** 0.5

    seg = lambda dither: led_status._breath_segments(5.0, dither_below_us=dither, black_point_us=0.0)
    plain_worst, plain_rms = eye_error(_per_period_on_us(seg(0.0)))
    worst, rms = eye_error(_per_period_on_us(seg(64.0)))
    assert plain_rms > 0.12 and rms < 0.06
    assert worst < 0.5 and plain_worst > 0.9
    assert rms < plain_rms / 2


def test_the_default_black_point_also_removes_most_of_the_stepping_on_its_own():
    """With the 2us black point the fade leaves dark at the curve's natural slope instead of
    crawling through the flat sparse zone, so even plain rounding is close (3.8% rms vs 18%);
    dithering still improves on it."""
    n = 500
    ideal = [led_status._breath_on_us(i * PERIOD, 5.0) for i in range(n)]

    def rms(per):
        rel = [abs(sum(per[i:i + 10]) / 10 - sum(ideal[i:i + 10]) / 10) / (sum(ideal[i:i + 10]) / 10)
               for i in range(n - 10) if sum(ideal[i:i + 10]) / 10 >= 0.15]
        return (sum(r * r for r in rel) / len(rel)) ** 0.5

    plain = rms(_per_period_on_us(led_status._breath_segments(5.0, dither_below_us=0.0)))
    dithered = rms(_per_period_on_us(led_status._breath_segments(5.0)))
    assert plain < 0.06 and dithered <= plain


def test_no_pulse_is_ever_dropped_or_negative_when_dithering_down_to_dark():
    per = _per_period_on_us(led_status._breath_segments(5.0))
    assert min(per) == 0
    assert all(0 <= x <= PEAK_US for x in per)
    # the fade actually reaches full dark, and stays a sequence of small steps into it
    assert per[0] == 0 and per[-1] == 0


# ── both dark ends, the higher threshold, and the black point ────────────────

def _eye_window_error(per, ideal, idx_range, win=10):
    """(worst, rms) relative error of 100ms-window brightness vs the ideal fade."""
    rel = []
    for i in idx_range:
        want = sum(ideal[i:i + win]) / win
        if want >= 0.15:
            rel.append(abs(sum(per[i:i + win]) / win - want) / want)
    return max(rel), (sum(r * r for r in rel) / len(rel)) ** 0.5


def test_dither_threshold_default_is_64us():
    assert led_status.BREATH_DIM_DITHER_US == 64.0


def test_dithering_treats_the_rising_and_falling_dark_ends_alike():
    """Error diffusion runs in time order, so it smooths the brightening exactly as
    it smooths the darkening: same eye-window error on both halves."""
    n, half = 500, 250
    ideal = [led_status._breath_on_us(i * PERIOD, 5.0) for i in range(n)]
    per = _per_period_on_us(led_status._breath_segments(5.0))
    rise_worst, rise_rms = _eye_window_error(per, ideal, range(0, half - 10))
    fall_worst, fall_rms = _eye_window_error(per, ideal, range(half, n - 10))
    assert rise_rms < 0.06 and fall_rms < 0.06
    assert rise_rms == pytest.approx(fall_rms, rel=0.35)
    assert rise_worst < 0.5 and fall_worst < 0.5


def test_rising_and_falling_pulse_trains_are_mirror_images_at_the_dark_end():
    """So any difference in how the two ends look is perceptual (dark adaptation),
    not something in the signal."""
    per = _per_period_on_us(led_status._breath_segments(5.0))
    rise, fall = per[:50], per[-50:][::-1]
    assert sum(1 for x in rise if x) == pytest.approx(sum(1 for x in fall if x), abs=3)
    assert sum(rise) == pytest.approx(sum(fall), rel=0.15)


def test_threshold_above_16us_makes_no_difference_to_the_eye_integrated_error():
    """Documents why raising it was not the lever for the steppy rise: the remaining
    error is sub-3us; above ~16us plain rounding is already within a percent or two."""
    n = 500
    ideal = [led_status._breath_on_us(i * PERIOD, 5.0) for i in range(n)]
    e16 = _eye_window_error(_per_period_on_us(led_status._breath_segments(5.0, dither_below_us=16.0)), ideal, range(n - 10))
    e64 = _eye_window_error(_per_period_on_us(led_status._breath_segments(5.0, dither_below_us=64.0)), ideal, range(n - 10))
    assert e64[1] == pytest.approx(e16[1], abs=0.01)


def test_black_point_default_is_2us_and_an_explicit_zero_restores_the_plain_curve():
    assert led_status.BREATH_BLACK_POINT_US == 2.0
    for t in (0, 700_000, 1_500_000, 2_500_000, 4_000_000):
        assert led_status._breath_on_us(t, 5.0) == led_status._breath_on_us(t, 5.0, black_point_us=2.0)
    assert led_status._breath_on_us(700_000, 5.0, black_point_us=0.0) > led_status._breath_on_us(700_000, 5.0)


def test_black_point_darkens_the_bottom_and_keeps_the_peak():
    peak_t = 2_500_000
    assert led_status._breath_on_us(peak_t, 5.0, black_point_us=2.0) == pytest.approx(PEAK_US)
    # everything the plain curve has below the black point is dark
    for t in range(0, 5_000_000, 10_000):
        plain = led_status._breath_on_us(t, 5.0, black_point_us=0.0)
        shifted = led_status._breath_on_us(t, 5.0, black_point_us=2.0)
        if plain <= 2.0:
            assert shifted == 0.0
        assert shifted <= plain + 1e-9


def test_black_point_shortens_the_single_spark_zone_of_the_brightening():
    """The sparse zone (100ms-window mean below 1us per slot, where the LED emits
    single 1us pulses) is what a dark-adapted eye sees as steps on the way up."""
    def sparks_ms(black):
        per = _per_period_on_us(led_status._breath_segments(5.0, black_point_us=black))
        return sum(1 for i in range(240) if 0 < sum(per[i:i + 10]) / 10 < 1.0) * 10
    assert sparks_ms(1.0) < sparks_ms(0.0) / 1.8
    assert sparks_ms(2.0) <= sparks_ms(1.0)
