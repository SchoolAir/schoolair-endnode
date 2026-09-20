#!/usr/bin/env python3
"""Play candidate breathing curves on the real LED, back to back, for eyeballing.

Run ON the Pi, as root (it stops schoolair-led.service for the duration and starts
it again afterwards):

    sudo python3 ~/schoolair/scripts/led_curve_preview.py            # all candidates
    sudo python3 ~/schoolair/scripts/led_curve_preview.py B C        # just these
    sudo python3 ~/schoolair/scripts/led_curve_preview.py -s 20 A B  # 20 s each

Each candidate is announced by N quick blinks (A = 1, B = 2, ...), then 1 s of dark,
then the breathing curve. Before playing, each one prints how much of its cycle it
spends above the perceptual midpoint (CIE lightness L* >= 50), which is the number
that matters for "does it stay bright too long". Whatever you settle on goes into
led_status.py (BREATH_SHAPE_EXPONENT, BREATH_DIM_DITHER_US, OK_CYCLE_S, BRIGHTNESS).
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import led_status as L  # noqa: E402

CYCLE = 5.0


def breath(dither_below_us, black_point_us=0.0):
    return lambda: L._breath_segments(CYCLE, dither_below_us=dither_below_us, black_point_us=black_point_us)


# label: (description, function returning the pulse list for one cycle)
# All are shape 1.6. Needs pigpiod running with -s 1 (deploy/pigpiod-early.conf).
CANDIDATES = {
    "A": ("dither below 16us (what you saw last): brightening still steppy", breath(16.0)),
    "B": ("dither below 64us: the new default", breath(64.0)),
    "C": ("B + black point 1us: the fade emerges from true dark, so brightening spends far less time on single sparks", breath(64.0, 1.0)),
    "D": ("B + black point 2us: as C, a little more dark", breath(64.0, 2.0)),
}


def blink_segments():
    """One 100 ms blink every 350 ms: the marker announcing which candidate is next."""
    return L._pattern_segments_us(lambda t_us: L.PEAK_US if t_us < 100_000 else 0, 0.35)


def per_period_on_us(segments):
    out, on, acc = [], 0, 0
    for level, us in segments:
        while us > 0:
            take = min(us, L.WAVE_PERIOD_US - acc)
            on += take if level else 0
            acc += take
            us -= take
            if acc == L.WAVE_PERIOD_US:
                out.append(on)
                on, acc = 0, 0
    return out


def describe(segments):
    per = per_period_on_us(segments)
    lightness = [L._luminance_to_lightness(x / L.PEAK_US) for x in per]
    above = sum(1 for v in lightness if v >= 50) / len(per) * 100
    dim = sum(1 for v in lightness if v < 20) / len(per) * 100
    half = len(per) // 2
    sparks_ms = sum(1 for i in range(half - 10)
                    if 0 < sum(per[i:i + 10]) / 10 < 1.0) * L.WAVE_PERIOD_US / 1000    # rising half, 100 ms windows
    return (f"cycle {sum(us for _, us in segments) / 1e6:.2f} s, {above:.0f}% of it above the perceptual midpoint, "
            f"{dim:.0f}% nearly dark (L*<20), mean brightness {sum(per) / len(per) / L.WAVE_PERIOD_US * 100:.2f}% duty, "
            f"peak {max(per) / L.WAVE_PERIOD_US * 100:.1f}%, brightening passes the single-spark zone in {sparks_ms:.0f} ms")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels", nargs="*", help="candidates to play (default: all)")
    ap.add_argument("-s", "--seconds", type=float, default=14.0, help="seconds per candidate")
    args = ap.parse_args()
    labels = [x.upper() for x in args.labels] or list(CANDIDATES)

    import pigpio
    subprocess.run(["systemctl", "stop", "schoolair-led"], check=False)
    pi = pigpio.pi()
    if not pi.connected:
        sys.exit("pigpiod is not running")
    pi.set_mode(L.GPIO_LED, pigpio.OUTPUT)
    wave = None
    if L._detect_step_us(pi) != 1:
        print("WARNING: pigpiod is not running with -s 1; the 1us candidates will be rounded by the hardware")
    try:
        for label in labels:
            desc, build = CANDIDATES[label]
            segs = build()
            n = list(CANDIDATES).index(label) + 1
            print(f"{label}: {desc}\n     {describe(segs)}\n     -> {n} blink(s), 1 s dark, then the curve for {args.seconds:.0f} s", flush=True)
            wave = L._send_pattern(pi, pigpio, blink_segments(), wave)
            time.sleep(0.35 * n)
            pi.wave_tx_stop()
            pi.write(L.GPIO_LED, 0)
            time.sleep(1.0)
            wave = L._send_pattern(pi, pigpio, segs, wave)
            time.sleep(args.seconds)
    finally:
        L._shutdown_led(pi)
        subprocess.run(["systemctl", "start", "schoolair-led"], check=False)
    print("done - schoolair-led.service restarted")


if __name__ == "__main__":
    main()
