#!/usr/bin/env python3
"""Play candidate breathing curves on the real LED, back to back, for eyeballing.

Run ON the Pi, as root (it stops schoolair-led.service for the duration and starts
it again afterwards):

    sudo python3 ~/schoolair/scripts/led_curve_preview.py            # all candidates
    sudo python3 ~/schoolair/scripts/led_curve_preview.py B D        # just these
    sudo python3 ~/schoolair/scripts/led_curve_preview.py -s 20 B E  # 20 s each

Each candidate is announced by N quick blinks (A = 1, B = 2, ...), then 1 s of dark,
then the breathing curve. The table below is what "peak", "cycle" and "low-heavy"
mean; edit CANDIDATES to try other values. Whatever you settle on goes into
led_status.py (BREATH_PEAK_FRAC, OK_TEMPO_SCALE, BREATH_WEIGHT_EXPONENT).
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import led_status as L  # noqa: E402

# label: (description, peak fraction of full brightness, target cycle seconds, weight exponent)
CANDIDATES = {
    "A": ("before: 10% peak, ~6 s", 0.10, 6.0, 1.0),
    "B": ("new default: 8% peak, 5 s", 0.08, 5.0, 1.0),
    "C": ("6% peak, 5 s", 0.06, 5.0, 1.0),
    "D": ("8% peak, 5 s, a bit low-heavy (weight^1.25)", 0.08, 5.0, 1.25),
    "E": ("8% peak, 5 s, low-heavy (weight^1.5)", 0.08, 5.0, 1.5),
}


def build(peak_frac, target_cycle, exponent):
    """(table, actual_cycle): tempo is solved so the cycle comes out at target_cycle."""
    peak = round(L.WAVE_STEPS * peak_frac)

    def cycle(tempo):
        return L._build_breath_table(peak, L.OK_PERIOD_S * tempo, L.TICK * tempo, exponent)[2]

    lo, hi = 0.3, 8.0
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if cycle(mid) < target_cycle else (lo, mid)
    tempo = (lo + hi) / 2
    table = L._build_breath_table(peak, L.OK_PERIOD_S * tempo, L.TICK * tempo, exponent)
    return table, tempo


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
    try:
        for label in labels:
            desc, peak_frac, cycle, exponent = CANDIDATES[label]
            table, tempo = build(peak_frac, cycle, exponent)
            n = list(CANDIDATES).index(label) + 1
            print(f"{label}: {desc}  (actual cycle {table[2]:.2f} s, tempo {tempo:.3f})  -- {n} blink(s), then the curve")
            blink = L._pattern_segments(lambda t: L.PEAK_STEPS if t < 100_000 else 0, 0.35)
            wave = L._send_pattern(pi, pigpio, blink, wave)
            time.sleep(0.35 * n)
            pi.wave_tx_stop()
            pi.write(L.GPIO_LED, 0)
            time.sleep(1.0)
            segs = L._pattern_segments(lambda t_us: L._table_lookup(table, t_us / 1e6), table[2])
            wave = L._send_pattern(pi, pigpio, segs, wave)
            time.sleep(args.seconds)
    finally:
        L._shutdown_led(pi)
        subprocess.run(["systemctl", "start", "schoolair-led"], check=False)
    print("done - schoolair-led.service restarted")


if __name__ == "__main__":
    main()
