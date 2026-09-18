#!/usr/bin/env python3
"""led_status.py — status LED driver (indoor units only, needs pigpiod).

Renders one of a small set of patterns on GPIO24 via pigpiod's DMA-driven
PWM, continuously, based on whatever the other services last wrote to
LED_STATE_FILE (plain text, one word). Producers — wizard.py, netwatch.sh,
jobs/ingest.py — write their state independently and never talk to pigpio
directly; this is the only process that touches the pin.

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
"""

import bisect
import math
import os
import subprocess
import threading
import time

GPIO_LED = 24
LED_STATE_FILE = "/run/schoolair-led-state"
PWM_FREQ_HZ = 100      # well above flicker-fusion; low enough for a wide duty-cycle range
PEAK_FRAC = 0.1        # single global brightness cap for every state
GAMMA = 2.8            # perceptual correction so dimming looks linear to the eye
TICK = 0.02            # render granularity
OK_PERIOD_S = 8.0        # breathe: 4s up, 4s down (nominal — actual is shorter, see below)
THINKING_PERIOD_S = 1.2  # pulse: 0.6s up, 0.6s down (nominal)
TEMPO_SCALE = 2.0        # scales both the nominal period and the per-step dwell
                          # cap together (see _build_breath_table) — the only
                          # knob that changes overall breathe/pulse speed
                          # without distorting the low-end-vs-peak timing shape

_VALID_STATES = {"ok", "thinking", "ap", "error", "no_sensor"}

# Independent health check — don't depend on schoolair.service itself to
# report its own breakage. A real live-fire OTA rollback test found this
# exact gap: a crashed main.py never even reaches the code that would
# write "error" to LED_STATE_FILE, so a stale "ok" from before the crash
# just sits there being rendered forever. Runs in its own thread
# (_health_monitor_loop), not the render loop — these shell out to
# systemctl, measured at 1.365s combined real time on this hardware, and
# that much blocking time every HEALTH_CHECK_INTERVAL_S in a 20ms render
# loop was a very real, very visible freeze (found live, on a real
# device). A single is-active snapshot isn't enough either — the same
# rollback test showed Type=simple marks a service "active" the instant
# its process is spawned, even if it crashes moments later — so this also
# tracks each service's restart count between checks and treats an
# increase as evidence of a crash within that window, even if the service
# happens to be up again by the time it's sampled.
WATCHED_SERVICES = ("sen6x.service", "schoolair.service", "schoolair-netwatch.service")
HEALTH_CHECK_INTERVAL_S = 5.0
UNHEALTHY_HOLD_S = 30.0    # once flagged, hold the "error" override at least this long


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


def _build_breath_table(peak: int, period: float, max_dwell: float = TICK):
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

    weights = [_perceptual_weight(v) for v in values]
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


def _service_is_active(svc: str) -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", svc], timeout=2,
        ).returncode == 0
    except subprocess.SubprocessError:
        return True  # don't flag unhealthy just because the check itself hiccuped


def _service_restarts(svc: str) -> int:
    try:
        out = subprocess.run(
            ["systemctl", "show", svc, "-p", "NRestarts", "--value"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return int(out)
    except (ValueError, subprocess.SubprocessError):
        return 0


def _check_watched_services(last_restarts: dict) -> bool:
    """Returns True if anything looks unhealthy right now. Mutates
    last_restarts in place with the freshly observed counts — the first
    call for any given service just seeds its baseline (no prior value to
    compare against yet), so it never flags unhealthy purely for having
    restarted at some point before this process started."""
    unhealthy = False
    for svc in WATCHED_SERVICES:
        if not _service_is_active(svc):
            unhealthy = True
        now_restarts = _service_restarts(svc)
        if svc in last_restarts and now_restarts > last_restarts[svc]:
            unhealthy = True
        last_restarts[svc] = now_restarts
    return unhealthy


def _health_monitor_loop(shared: dict) -> None:
    """Runs in its own daemon thread — deliberately NOT in the render loop.
    Each of the 6 systemctl calls in _check_watched_services() spawns a
    real process; measured at 1.365s combined, real, on this hardware. That
    much blocking time inline in a 20ms render loop is a full-second-plus
    freeze of the LED every ~5s, landing at a different phase of the
    breathe/pulse cycle each time (5s and 8s share no common short cycle)
    — which is exactly what "irregular steps, otherwise smooth" turned out
    to be. The render loop only ever reads shared["unhealthy_until"], a
    single float — cheap, and safe without an explicit lock under
    CPython's GIL for a single plain assignment/read like this."""
    last_restarts: dict = {}
    while True:
        if _check_watched_services(last_restarts):
            shared["unhealthy_until"] = time.monotonic() + UNHEALTHY_HOLD_S
        time.sleep(HEALTH_CHECK_INTERVAL_S)


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
    try:
        if not os.path.exists(LED_STATE_FILE):
            with open(LED_STATE_FILE, "w") as f:
                f.write("thinking")
        os.chmod(LED_STATE_FILE, 0o666)
    except OSError as e:
        print(f"[led] warning: could not prepare {LED_STATE_FILE}: {e}")

    pi = pigpio.pi()
    if not pi.connected:
        raise SystemExit("[led] could not connect to pigpiod")

    pi.set_PWM_frequency(GPIO_LED, PWM_FREQ_HZ)
    real_range = pi.get_PWM_real_range(GPIO_LED)
    # set_PWM_dutycycle() validates against the *nominal* range from
    # set_PWM_range() (default 255) — completely separate from real_range,
    # which is just the DMA-tick count at the current frequency. Without
    # this, every dutycycle value we compute against real_range gets
    # rejected as out-of-range on any device that hasn't had this GPIO's
    # nominal range touched before (i.e. every fresh device).
    pi.set_PWM_range(GPIO_LED, real_range)
    peak = round(real_range * PEAK_FRAC)
    print(f"[led] pigpiod ready — GPIO{GPIO_LED}, real_range={real_range}, peak_duty={peak}")

    ok_table = _build_breath_table(peak, OK_PERIOD_S * TEMPO_SCALE, TICK * TEMPO_SCALE)
    thinking_table = _build_breath_table(peak, THINKING_PERIOD_S * TEMPO_SCALE, TICK * TEMPO_SCALE)
    print(f"[led] breathe table: {len(ok_table[0])} steps, actual cycle {ok_table[2]:.2f}s "
          f"(nominal {OK_PERIOD_S}s); pulse table: {len(thinking_table[0])} steps, "
          f"actual cycle {thinking_table[2]:.2f}s (nominal {THINKING_PERIOD_S}s)")

    health = {"unhealthy_until": 0.0}
    threading.Thread(target=_health_monitor_loop, args=(health,), daemon=True).start()

    last_state = None
    t0 = time.monotonic()

    try:
        while True:
            now_mono = time.monotonic()
            state = "error" if now_mono < health["unhealthy_until"] else _read_state()
            if state != last_state:
                t0 = time.monotonic()  # restart the pattern cleanly at each state change
                last_state = state
            elapsed = time.monotonic() - t0

            if state == "ok":
                # slow breathe: perceptually-weighted, ~8s nominal
                pi.set_PWM_dutycycle(GPIO_LED, _table_lookup(ok_table, elapsed))

            elif state == "thinking":
                # sharp, fast pulse: perceptually-weighted, ~1.2s nominal
                pi.set_PWM_dutycycle(GPIO_LED, _table_lookup(thinking_table, elapsed))

            elif state == "ap":
                # double blink: on 0-100ms, off 100-250ms, on 250-350ms,
                # then off until the next pair starts 2s later (2.35s cycle)
                cycle = elapsed % 2.35
                on = (0.0 <= cycle < 0.10) or (0.25 <= cycle < 0.35)
                pi.set_PWM_dutycycle(GPIO_LED, peak if on else 0)

            elif state == "error":
                # single blink every 1s: on 100ms, off 900ms
                cycle = elapsed % 1.0
                on = cycle < 0.10
                pi.set_PWM_dutycycle(GPIO_LED, peak if on else 0)

            elif state == "no_sensor":
                pi.set_PWM_dutycycle(GPIO_LED, peak)

            time.sleep(TICK)
    finally:
        pi.set_PWM_dutycycle(GPIO_LED, 0)
        pi.stop()


if __name__ == "__main__":
    main()
