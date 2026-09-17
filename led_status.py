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
"""

import os
import time

import pigpio

GPIO_LED = 24
LED_STATE_FILE = "/run/schoolair-led-state"
PWM_FREQ_HZ = 100      # well above flicker-fusion; low enough for a wide duty-cycle range
PEAK_FRAC = 0.5        # cap max brightness at half — comfortable to look at continuously
GAMMA = 2.8            # perceptual correction so dimming looks linear to the eye
TICK = 0.02            # render granularity

_VALID_STATES = {"ok", "thinking", "ap", "error", "no_sensor"}


def _read_state() -> str:
    try:
        with open(LED_STATE_FILE) as f:
            s = f.read().strip()
        if s in _VALID_STATES:
            return s
    except OSError:
        pass
    return "thinking"


def _curve(frac: float, peak: int) -> int:
    frac = max(0.0, min(1.0, frac))
    return round(peak * (frac ** GAMMA))


def main() -> None:
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
    peak = round(real_range * PEAK_FRAC)
    print(f"[led] pigpiod ready — GPIO{GPIO_LED}, real_range={real_range}, peak_duty={peak}")

    last_state = None
    t0 = time.monotonic()

    try:
        while True:
            state = _read_state()
            if state != last_state:
                t0 = time.monotonic()  # restart the pattern cleanly at each state change
                last_state = state
            elapsed = time.monotonic() - t0

            if state == "ok":
                # slow breathe: 4s up, 4s down
                period = 8.0
                phase = (elapsed % period) / period
                frac = phase * 2 if phase < 0.5 else (1 - phase) * 2
                pi.set_PWM_dutycycle(GPIO_LED, _curve(frac, peak))

            elif state == "thinking":
                # sharp, fast pulse: 0.6s up, 0.6s down
                period = 1.2
                phase = (elapsed % period) / period
                frac = phase * 2 if phase < 0.5 else (1 - phase) * 2
                pi.set_PWM_dutycycle(GPIO_LED, _curve(frac, peak))

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
