#!/usr/bin/env python3
"""led_status.py — status LED driver (indoor units only, needs pigpiod).

Shows one of a small set of patterns on GPIO24, chosen by whatever the other
services last wrote to LED_STATE_FILE (plain text, one word). Producers —
wizard.py, netwatch.sh, jobs/ingest.py — write their state independently and
never talk to pigpio directly; this is the only process that touches the pin.

Each pattern is handed to pigpiod ONCE, as a "wave" (a precomputed list of
timed on/off pulses, repeated forever), and then played entirely by pigpiod's
DMA engine. This process does nothing while a pattern runs except poll the
state file at 5Hz and hand over a new wave when the state changes. So the
animation is exactly timed and stays perfectly smooth however busy the CPU is
(a Pi Zero W is pegged for its whole boot), and costs ~0 CPU — the previous
implementation woke up 50x/s to set the duty cycle, ~6s of CPU per boot and
visibly stuttering under load. See _pattern_segments()/_send_pattern().

States (unrecognized/missing file content falls back to "thinking"):
  ok        — all's good, uploading normally         → slow breathe
  thinking  — connecting / validating / registering   → sharp, fast pulse
  ap        — AP mode, awaiting setup                 → double blink (pair every 2.35s)
  error     — registration/upload error                → single blink every 1s
  no_sensor — sensor read is failing                   → solid on
  (device has no power)                                → off — never written by us

On top of that, this process independently polls sen6x/schoolair/
schoolair-netwatch every few seconds and forces "error" regardless of
LED_STATE_FILE if any of them are down or have restarted since the last
check. This matters specifically when the process that would normally
write "error" itself is what's broken — e.g. a crashed main.py never
reaches the code that writes to LED_STATE_FILE at all, so a stale "ok"
from before the crash would otherwise just sit there being rendered
forever. See _check_watched_services().

LED_STATE_FILE's "error" is downgraded to "ap" while the device isn't
registered yet (see _is_registered/_resolve_state) — a networking/auth
failure is *expected* during AP-mode setup (jobs/ingest.py still runs and
tries to upload with no token), so it shouldn't look like a real problem.
The independent health check above still overrides everything, including
this downgrade — a genuinely crashed service is a real problem either way.
"""

import bisect
import math
import os
import signal
import subprocess
import threading
import time

GPIO_LED = 24
LED_STATE_FILE = "/run/schoolair-led-state"
PWM_FREQ_HZ = 100      # well above flicker-fusion; low enough for a wide duty-cycle range
WAVE_PERIOD_US = 1_000_000 // PWM_FREQ_HZ   # one PWM period; the brightness can change once per period
WAVE_STEP_US = 5       # duty-cycle resolution = pigpiod's sample rate (5us default)
WAVE_STEPS = WAVE_PERIOD_US // WAVE_STEP_US  # 2000 distinct brightness levels, as with pigpio's own PWM
PEAK_FRAC = 0.1        # single global brightness cap for every state
PEAK_STEPS = round(WAVE_STEPS * PEAK_FRAC)   # 200
GAMMA = 2.8            # perceptual correction so dimming looks linear to the eye
TICK = 0.02            # per-step dwell cap in the breath table (see _build_breath_table)
OK_PERIOD_S = 8.0        # breathe: 4s up, 4s down (nominal — actual is shorter, see below)
THINKING_PERIOD_S = 1.2  # pulse: 0.6s up, 0.6s down (nominal)

# Breathe/pulse ("ok" / "thinking") tuning. Live-tuned by eye on a real unit.
#  - BREATH_PEAK_FRAC: brightness cap of the two table-driven patterns; 0.8 of
#    PEAK_FRAC because the top of the breath read too bright and stayed bright
#    too long. The blink/solid states keep PEAK_FRAC.
#  - *_TEMPO_SCALE scale the nominal period AND the per-step dwell cap together
#    (see _build_breath_table) — the only way to change speed without distorting
#    the low-end-vs-peak timing shape. Lowering the peak removes brightness steps
#    and so shortens the cycle by itself; these are set for a ~5.0s breath and
#    a 1.49s pulse (the pulse's speed is unchanged).
#  - BREATH_WEIGHT_EXPONENT: >1 shifts time toward the dim end (and shortens the
#    cycle, so raise the tempo to compensate); 1.0 = the original timing.
BREATH_PEAK_FRAC = 0.08
BREATH_PEAK_STEPS = round(WAVE_STEPS * BREATH_PEAK_FRAC)  # 160
OK_TEMPO_SCALE = 1.33
THINKING_TEMPO_SCALE = 1.525
BREATH_WEIGHT_EXPONENT = 1.0

_VALID_STATES = {"ok", "thinking", "ap", "error", "no_sensor"}

# Independent health check — don't depend on schoolair.service itself to
# report its own breakage. A real live-fire OTA rollback test found this
# exact gap: a crashed main.py never even reaches the code that would
# write "error" to LED_STATE_FILE, so a stale "ok" from before the crash
# just sits there being rendered forever. Runs in its own thread
# (_health_monitor_loop) — shelling out to systemctl is slow on this
# hardware (six spawns took 1.365s combined, which froze the old
# tick-driven animation), so it is a single spawn per check now. A single
# is-active snapshot isn't enough either — the same
# rollback test showed Type=simple marks a service "active" the instant
# its process is spawned, even if it crashes moments later — so this also
# tracks each service's restart count between checks and treats an
# increase as evidence of a crash within that window, even if the service
# happens to be up again by the time it's sampled.
WATCHED_SERVICES = ("sen6x.service", "schoolair.service", "schoolair-netwatch.service")
HEALTH_CHECK_INTERVAL_S = 30.0
STATE_POLL_S = 1.0        # how often main() checks LED_STATE_FILE for a change (see main())
HEARTBEAT_S = 5.0         # how often main() checks pigpiod is still there
PIGPIOD_WAIT_S = 60.0     # how long main() waits for pigpiod to accept connections at startup
UNHEALTHY_HOLD_S = 30.0    # once flagged, hold the "error" override at least this long
BOOT_GRACE_MAX_S = 600.0   # never suppress health checks for longer than this after boot
BOOT_CHECK_TIMEOUT_S = 20.0  # systemctl can be very slow mid-boot on a Pi Zero W; this runs off the render loop


ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _is_registered() -> bool:
    """Mirrors jobs/ingest.py's own upload gate (NEW_AUTH_TOKEN in .env).
    Lets the render loop tell an *expected* networking/auth failure during
    the AP-mode setup window — nothing to upload to yet, so every attempt
    "fails" and jobs/ingest.py writes "error" — apart from a real error on
    an already-registered device. See the precedence note in main()."""
    try:
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith("NEW_AUTH_TOKEN="):
                    return bool(line.split("=", 1)[1].strip())
    except OSError:
        pass
    return False


def _read_state() -> str:
    try:
        with open(LED_STATE_FILE) as f:
            s = f.read().strip()
        if s in _VALID_STATES:
            return s
    except OSError:
        pass
    return "thinking"


def _ease(elapsed: float, period: float) -> float:
    """Smooth 0->1->0 breathing envelope over `period` seconds — a raised
    cosine, not a triangle wave. This matters specifically at the peak: a
    linear triangle wave has a sharp corner there (slope flips sign
    instantly), and gamma correction's own slope is steepest right at
    frac=1 — stacking those two produces the fastest-changing point of
    the whole cycle exactly at the peak, which reads as a visible "step"
    right around the top of the breath. A cosine has zero slope exactly
    at both the peak and trough, so there's no corner for gamma to make
    worse — this is what was actually producing the visible step reported
    near the peak of the breathe/pulse patterns."""
    return (1 - math.cos(2 * math.pi * elapsed / period)) / 2


def _curve(frac: float, peak: int) -> int:
    frac = max(0.0, min(1.0, frac))
    return round(peak * (frac ** GAMMA))


def _perceptual_weight(v: int) -> float:
    """How visually significant one more step away from value v is, per
    Weber-Fechner: perceived brightness change tracks the *ratio* between
    consecutive values, not their difference. 0->1->2 are each 100%+
    relative jumps (large weight); 199->200 is a 0.5% jump (tiny weight).
    +1/+2 offsets just avoid a log(0) singularity at v=0."""
    return math.log((v + 2) / (v + 1))


def _build_breath_table(peak: int, period: float, max_dwell: float = TICK,
                        weight_exponent: float = 1.0):
    """Precomputed once at startup per (peak, period) pair — not per-tick.
    Returns (values, cumulative_end_times, total_duration).

    Two distinct problems, found live, in tension with each other:

    1. Near the extremes, gamma-corrected brightness changes so little
       per unit time that many consecutive fine-grained time samples
       round to the identical integer before it can tick down/up again —
       showed up as visibly held plateaus, worst on the way down.
    2. Giving every *distinct* value an equal time slice (the direct fix
       for #1) creates a new problem: a linear step near the peak
       (199->200, a 0.5% relative change) gets the same on-screen time as
       a linear step near the trough (1->2, a 100% relative change) —
       so the perceptually-tiny steps near the peak, being individually
       indistinguishable, collectively read as one long stuck-feeling
       plateau even though nothing is technically repeating.

    Fix: weight each achievable value's dwell time by _perceptual_weight()
    instead of giving every one an equal slice, then cap the maximum any
    single value can claim at `max_dwell` — chosen so the low end (whose
    natural weight exceeds the cap almost everywhere) ends up ~unchanged,
    while higher values — whose natural weight is much smaller — get
    progressively less than the cap the closer they are to the peak. No
    renormalization: capping trims the total below `period` somewhat,
    which just means the breath completes a bit faster than nominal —
    an acceptable trade for an ambient indicator, not a metronome.

    `period` and `max_dwell` should be scaled together (see TEMPO_SCALE)
    to change overall speed without distorting this shape — scaling only
    `period` would shift the cap relative to the curve and change which
    values get capped at all, not just how fast things move."""
    SAMPLE_S = 0.001  # fine enough to catch every achievable integer transition
    n_samples = max(2, round(period / SAMPLE_S))
    values = []
    last = None
    for i in range(n_samples + 1):
        t = (i / n_samples) * period
        v = _curve(_ease(t, period), peak)
        if v != last:
            values.append(v)
            last = v

    weights = [_perceptual_weight(v) ** weight_exponent for v in values]
    total_weight = sum(weights)
    dwell = [min(max_dwell, period * w / total_weight) for w in weights]

    cumulative = []
    running = 0.0
    for d in dwell:
        running += d
        cumulative.append(running)
    return values, cumulative, running


def _table_lookup(table, elapsed: float) -> int:
    values, cumulative, total_duration = table
    t_in_cycle = elapsed % total_duration
    idx = bisect.bisect_right(cumulative, t_in_cycle)
    return values[min(idx, len(values) - 1)]


def _read_service_states(services):
    """{unit: (is_active, restart_count)} for all `services` from ONE
    `systemctl show` spawn, or None if the check itself failed. Spawning
    systemctl is by far the most expensive thing this daemon does on a Pi Zero W
    (measured: six spawns per check cost ~8% of the CPU, several times the
    animation itself — which now costs ~0), so this is one spawn per check.
    "active" here matches `systemctl is-active` (also true while reloading)."""
    try:
        out = subprocess.run(
            ["systemctl", "show", *services, "-p", "Id", "-p", "ActiveState", "-p", "NRestarts"],
            capture_output=True, text=True, timeout=BOOT_CHECK_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    states = {}
    for block in out.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" not in kv:
            continue
        try:
            restarts = int(kv.get("NRestarts") or 0)
        except ValueError:
            restarts = 0
        states[kv["Id"]] = (kv.get("ActiveState") in ("active", "reloading"), restarts)
    return states


def _boot_in_progress() -> bool:
    """True while systemd is still bringing the system up ("initializing" or
    "starting" — missing the first one made every watched service look
    crashed for the first ~2 min and the LED blinked "error"). This process
    now starts at the very beginning of boot (sysinit.target, so the LED can
    show "thinking" before the slow parts of a boot are done), when the
    watched services simply haven't been started yet — that's "not started
    yet", not "crashed", so the health check must stay quiet until boot
    finishes.

    An inconclusive answer counts as "still booting": on a Pi Zero W under
    boot load, `systemctl` itself can take longer than any short timeout,
    and treating that as "boot is over" armed the health check ~80s early
    (found live: LED blinked "error" because netwatch hadn't started yet).
    Bounded by uptime so a stuck boot job or a broken systemctl can't mask
    real breakage forever."""
    try:
        with open("/proc/uptime") as f:
            if float(f.read().split()[0]) > BOOT_GRACE_MAX_S:
                return False
    except (OSError, ValueError):
        return False
    try:
        out = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True, text=True, timeout=BOOT_CHECK_TIMEOUT_S,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return out in ("initializing", "starting")


def _check_watched_services(last_restarts: dict) -> bool:
    """Returns True if anything looks unhealthy right now. Mutates
    last_restarts in place with the freshly observed counts — the first
    call for any given service just seeds its baseline (no prior value to
    compare against yet), so it never flags unhealthy purely for having
    restarted at some point before this process started."""
    states = _read_service_states(WATCHED_SERVICES)
    if states is None:
        return False  # don't flag unhealthy just because the check itself hiccuped
    unhealthy = False
    for svc in WATCHED_SERVICES:
        is_active, now_restarts = states.get(svc, (False, 0))
        if not is_active:
            print(f"[led] health: {svc} is not active")
            unhealthy = True
        if svc in last_restarts and now_restarts > last_restarts[svc]:
            print(f"[led] health: {svc} restarted ({last_restarts[svc]} -> {now_restarts})")
            unhealthy = True
        last_restarts[svc] = now_restarts
    return unhealthy


def _health_monitor_loop(shared: dict) -> None:
    """Runs in its own daemon thread. (Originally this had to stay out of the
    render loop: the six systemctl spawns it once made took 1.365s combined on
    this hardware, a visible freeze of a tick-driven animation. The animation
    is now played by pigpiod and has no render loop left to freeze, but spawn
    cost is still the biggest thing this daemon does — hence a single
    `systemctl show` per HEALTH_CHECK_INTERVAL_S.) The main loop only ever
    reads shared["unhealthy_until"], a single float — safe without a lock
    under CPython's GIL for a plain assignment/read."""
    last_restarts: dict = {}
    boot_gate_logged = False
    while True:
        if _boot_in_progress():
            time.sleep(HEALTH_CHECK_INTERVAL_S)
            continue
        if not boot_gate_logged:
            print("[led] boot finished — service health checks armed")
            boot_gate_logged = True
        if _check_watched_services(last_restarts):
            shared["unhealthy_until"] = time.monotonic() + UNHEALTHY_HOLD_S
        time.sleep(HEALTH_CHECK_INTERVAL_S)


def _resolve_state(health: dict, now_mono: float, raw_state: "str | None" = None) -> str:
    """Precedence, highest to lowest:
      1. Independent health-check failure (health["unhealthy_until"]) — a
         watched service actually crashed. Always wins: that's a real
         internal problem regardless of registration/AP-mode.
      2. Whatever LED_STATE_FILE says, EXCEPT: "error" while the device
         isn't registered yet gets downgraded to "ap". A networking/auth
         failure is *expected* during AP-mode setup (there's nothing to
         reach yet, so a stray sensor-read-and-upload attempt fails every
         time) — it shouldn't look identical to a real upload error on an
         already-registered device. A genuine "no_sensor" still passes
         straight through unchanged — a hardware problem is a hardware
         problem in either mode."""
    state = raw_state if raw_state is not None else _read_state()
    if state == "error" and not _is_registered():
        state = "ap"
    if now_mono < health["unhealthy_until"]:
        state = "error"
    return state


def _on_sigterm(signum, frame) -> None:
    """Converts SIGTERM into a normal SystemExit. Python's default SIGTERM
    disposition kills the process outright and never runs a `finally`
    block — so "systemctl stop" (or a restart, which is stop-then-start)
    would leave the LED frozen at whatever duty cycle it happened to be
    mid-breathe, silently looking "on" for a daemon that's actually dead.
    Routing it through SystemExit lets main()'s finally block turn the
    LED off before the process actually exits."""
    raise SystemExit(0)


def _connect_pigpiod(pigpio, timeout_s: float = PIGPIOD_WAIT_S, poll_s: float = 0.5):
    """Connects to pigpiod, retrying for up to timeout_s. pigpiod.service
    declares no ordering of its own, so at boot it can still be starting
    when this process is — giving up on the first refusal would cost a full
    RestartSec (plus a Python cold start) before the LED ever lit. Polling
    here means the LED starts within ~poll_s of pigpiod being ready. Also
    covers a first boot where pigpiod is still being installed. Returns the
    connected pigpio.pi, or None if it never came up."""
    deadline = time.monotonic() + timeout_s
    while True:
        pi = pigpio.pi()
        if pi.connected:
            return pi
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)


def _state_file_mtime():
    try:
        return os.stat(LED_STATE_FILE).st_mtime_ns
    except OSError:
        return None


def _build_tables():
    """(ok_table, thinking_table): the two breathe/pulse patterns, built once."""
    ok = _build_breath_table(BREATH_PEAK_STEPS, OK_PERIOD_S * OK_TEMPO_SCALE,
                             TICK * OK_TEMPO_SCALE, BREATH_WEIGHT_EXPONENT)
    thinking = _build_breath_table(BREATH_PEAK_STEPS, THINKING_PERIOD_S * THINKING_TEMPO_SCALE,
                                   TICK * THINKING_TEMPO_SCALE, BREATH_WEIGHT_EXPONENT)
    return ok, thinking


def _pattern_segments(duty_at, cycle_s: float):
    """Turns a brightness function into the pulse list for ONE repeat of a
    pattern: [(level, delay_us), ...], level 1 = pin high. `duty_at(t_us)`
    returns the brightness in steps 0..PEAK_STEPS at t_us microseconds into
    the cycle, and is sampled once per PWM period (10ms) — each period is
    `on` for duty*5us then `off` for the rest, i.e. real 100Hz PWM, the same
    signal as before but with the brightness updated every period instead of
    every 20ms tick. Adjacent same-level segments are merged (a long dark
    stretch is a single pulse, not hundreds), keeping waves small."""
    n_periods = max(1, round(cycle_s * 1_000_000 / WAVE_PERIOD_US))
    segments: list = []

    def add(level: int, us: int) -> None:
        if us <= 0:
            return
        if segments and segments[-1][0] == level:
            segments[-1] = (level, segments[-1][1] + us)
        else:
            segments.append((level, us))

    for i in range(n_periods):
        on_us = min(WAVE_PERIOD_US, duty_at(i * WAVE_PERIOD_US) * WAVE_STEP_US)
        add(1, on_us)
        add(0, WAVE_PERIOD_US - on_us)
    return segments


def _state_segments(state: str, ok_table, thinking_table):
    """The repeating pulse pattern for a state. Timings are those of the
    original renderer (see the per-state comments)."""
    if state == "ok":
        # slow breathe: perceptually-weighted table, ~6s cycle
        return _pattern_segments(lambda t_us: _table_lookup(ok_table, t_us / 1e6), ok_table[2])
    if state == "thinking":
        # sharp, fast pulse: perceptually-weighted table, ~1.5s cycle
        return _pattern_segments(lambda t_us: _table_lookup(thinking_table, t_us / 1e6), thinking_table[2])
    if state == "ap":
        # double blink: on 0-100ms, off 100-250ms, on 250-350ms,
        # then off until the next pair starts 2s later (2.35s cycle)
        return _pattern_segments(
            lambda t_us: PEAK_STEPS if (t_us < 100_000 or 250_000 <= t_us < 350_000) else 0, 2.35)
    if state == "error":
        # single blink every 1s: on 100ms, off 900ms
        return _pattern_segments(lambda t_us: PEAK_STEPS if t_us < 100_000 else 0, 1.0)
    if state == "no_sensor":
        # solid on (at the global cap): a single PWM period, repeated
        return _pattern_segments(lambda t_us: PEAK_STEPS, WAVE_PERIOD_US / 1e6)
    raise ValueError(f"unknown LED state: {state!r}")


def _send_pattern(pi, pigpio, segments, prev_wave_id):
    """Hands a pattern to pigpiod and starts it repeating, replacing whatever
    was playing, with no gap. Returns the new wave id, or None if pigpiod
    refused it (caller falls back to a steady glow — never a dark LED).

    Order matters (both found live on a Pi Zero W):
      - pigpiod ignores a wave on a pin that still has a PWM duty cycle set —
        e.g. the dim "on" that schoolair-led.service's ExecStartPre leaves
        behind — so PWM is zeroed immediately before the wave starts.
      - the new wave is created and started BEFORE the old one is deleted;
        starting a wave replaces the running one in place."""
    mask = 1 << GPIO_LED
    pulses = [pigpio.pulse(mask if level else 0, 0 if level else mask, us) for level, us in segments]
    if pi.wave_add_generic(pulses) < 0:
        return None
    wave_id = pi.wave_create()
    if wave_id < 0:
        return None
    pi.set_PWM_dutycycle(GPIO_LED, 0)
    if pi.wave_send_repeat(wave_id) < 0:
        pi.wave_delete(wave_id)
        return None
    if prev_wave_id is not None:
        pi.wave_delete(prev_wave_id)
    return wave_id


def _steady_glow(pi) -> None:
    """Last-resort display if a wave can't be created: plain PWM at the cap."""
    pi.wave_tx_stop()
    pi.set_PWM_frequency(GPIO_LED, PWM_FREQ_HZ)
    pi.set_PWM_range(GPIO_LED, WAVE_STEPS)
    pi.set_PWM_dutycycle(GPIO_LED, PEAK_STEPS)


def _shutdown_led(pi) -> None:
    """Best effort — pigpiod may already be gone. A wave keeps playing after
    its client dies (verified: kill -9 of the client leaves it running), so
    this must run on the way out or the LED stays frozen on its last pattern."""
    try:
        pi.wave_tx_stop()
        pi.wave_clear()
        pi.write(GPIO_LED, 0)
        pi.stop()
    except Exception:
        pass


def main() -> None:
    import pigpio  # deferred: Pi-only, keeps this module importable/testable elsewhere

    # Outdoor units never get pigpiod installed (see schoolair-pigpio-setup.service)
    # — bail out quietly rather than spinning on failed pigpiod connections.
    try:
        with open("/etc/schoolair-unit-type") as f:
            if f.read().strip() != "indoor":
                print("[led] not an indoor unit — nothing to drive, exiting")
                return
    except OSError:
        print("[led] /etc/schoolair-unit-type missing — exiting")
        return

    # Create the state file world-writable so jobs/ingest.py (runs as
    # `admin`) and wizard.py/netwatch.sh (run as `root`) can all write to
    # it regardless of which one gets there first — /run itself is root
    # 755, so a non-root writer can only succeed if the file already
    # exists with permissive mode.
    #
    # Always reset to "thinking" here, even if the file already exists —
    # this process starting is the definitive "nothing rendered yet"
    # moment. Without this, another writer (e.g. jobs/ingest.py's no-token
    # branch) can win a startup race and leave a stale/wrong value as the
    # very first thing ever displayed.
    try:
        with open(LED_STATE_FILE, "w") as f:
            f.write("thinking")
        os.chmod(LED_STATE_FILE, 0o666)
    except OSError as e:
        print(f"[led] warning: could not prepare {LED_STATE_FILE}: {e}")

    pi = _connect_pigpiod(pigpio)
    if pi is None:
        raise SystemExit("[led] could not connect to pigpiod")
    print(f"[led] pigpiod ready — GPIO{GPIO_LED}, {PWM_FREQ_HZ}Hz, {WAVE_STEPS} levels, peak {PEAK_STEPS}")

    pi.set_mode(GPIO_LED, pigpio.OUTPUT)
    ok_table, thinking_table = _build_tables()
    print(f"[led] breathe table: {len(ok_table[0])} steps, actual cycle {ok_table[2]:.2f}s "
          f"(nominal {OK_PERIOD_S}s); pulse table: {len(thinking_table[0])} steps, "
          f"actual cycle {thinking_table[2]:.2f}s (nominal {THINKING_PERIOD_S}s)")

    health = {"unhealthy_until": 0.0}
    threading.Thread(target=_health_monitor_loop, args=(health,), daemon=True).start()

    signal.signal(signal.SIGTERM, _on_sigterm)

    last_state = None
    wave_id = None
    # Nothing here needs to be fast: the animation is played by pigpiod, so a
    # state change showing up within a second is plenty. Even so, the cheapest
    # way to check is os.stat() on the (tmpfs, i.e. RAM) state file — 56us versus
    # 621us to open/read/close it, measured on a Pi Zero W — and only re-read it
    # when its mtime changed. (An environment variable can't do this job: a
    # process's environment can't be changed from outside.)
    raw_state = _read_state()
    state_mtime = _state_file_mtime()
    last_heartbeat = time.monotonic()

    try:
        while True:
            now_mono = time.monotonic()
            mtime = _state_file_mtime()
            if mtime != state_mtime:
                raw_state = _read_state()
                state_mtime = mtime
            state = _resolve_state(health, now_mono, raw_state)
            if state != last_state:
                segments = _state_segments(state, ok_table, thinking_table)
                new_wave = _send_pattern(pi, pigpio, segments, wave_id)
                if new_wave is None:
                    print(f"[led] WARNING: pigpiod refused the {state!r} wave — showing a steady glow")
                    _steady_glow(pi)
                    wave_id = None
                else:
                    wave_id = new_wave
                print(f"[led] state {last_state} -> {state} "
                      f"(state file: {_read_state()}, health override: "
                      f"{now_mono < health['unhealthy_until']}; "
                      f"{len(segments)} pulses, {sum(us for _, us in segments) / 1e6:.2f}s cycle)")
                last_state = state
            # Heartbeat: raises if pigpiod has gone away (crashed/restarted —
            # either way the wave died with it), so systemd restarts us and a
            # fresh wave is sent, instead of leaving the LED dark.
            if now_mono - last_heartbeat >= HEARTBEAT_S:
                pi.get_current_tick()
                last_heartbeat = now_mono
            time.sleep(STATE_POLL_S)
    finally:
        _shutdown_led(pi)


if __name__ == "__main__":
    main()
