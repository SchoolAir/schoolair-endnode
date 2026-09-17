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
OK_PERIOD_S = 8.0        # breathe: 4s up, 4s down
THINKING_PERIOD_S = 1.2  # pulse: 0.6s up, 0.6s down

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


def _build_breath_table(peak: int, period: float) -> list:
    """Precomputed once at startup per (peak, period) pair — not per-tick.

    Near the extremes, gamma-corrected brightness changes so little per
    unit time that many consecutive fine-grained time samples round to
    the identical integer duty value before it can tick down/up again —
    a real, unavoidable consequence of only having ~peak distinct levels
    to work with (peak=200 here) spread across a curve shaped to
    compress hard near zero. Sampled every 1ms and rounded, that showed
    up as visibly held plateaus (e.g. "5,5,4,4,3,3,3,2,2,2,1,1,1,0,0,0,0,0")
    especially on the way down.

    Deduplicating consecutive identical samples into a single table entry
    fixes it directly: the render loop walks this table at a constant
    rate (one entry per period/len(table) seconds), so every entry — one
    per *achievable distinct value*, not per raw time-sample — gets an
    equal, short slice of the total period. A plateau that used to hold
    for hundreds of milliseconds collapses to a single slice, exactly as
    fast as any other transition. A region that already produces a new
    distinct value on every fine-grained sample doesn't get deduplicated
    at all — nothing changes structurally for it; it just ends up
    spanning a proportionally larger, and typically slightly longer than
    one raw render tick, share of the walked period once the coarse
    region's redundant duplicates are gone. That's the intended trade:
    faster through the parts with nothing new to show, correspondingly
    a little more time on parts that actually have somewhere to go."""
    SAMPLE_S = 0.001  # fine enough to catch every achievable integer transition
    n_samples = max(2, round(period / SAMPLE_S))
    table = []
    last = None
    for i in range(n_samples + 1):
        t = (i / n_samples) * period
        v = _curve(_ease(t, period), peak)
        if v != last:
            table.append(v)
            last = v
    return table


def _table_lookup(table: list, elapsed: float, period: float) -> int:
    idx = int((elapsed % period) / period * len(table))
    return table[min(idx, len(table) - 1)]


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

    ok_table = _build_breath_table(peak, OK_PERIOD_S)
    thinking_table = _build_breath_table(peak, THINKING_PERIOD_S)
    print(f"[led] breathe table: {len(ok_table)} distinct steps over {OK_PERIOD_S}s, "
          f"pulse table: {len(thinking_table)} steps over {THINKING_PERIOD_S}s")

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
                # slow breathe: 4s up, 4s down
                pi.set_PWM_dutycycle(GPIO_LED, _table_lookup(ok_table, elapsed, OK_PERIOD_S))

            elif state == "thinking":
                # sharp, fast pulse: 0.6s up, 0.6s down
                pi.set_PWM_dutycycle(GPIO_LED, _table_lookup(thinking_table, elapsed, THINKING_PERIOD_S))

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
