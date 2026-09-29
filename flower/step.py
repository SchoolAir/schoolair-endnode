"""step.py DIRECTION MM [US_PER_STEP] [full]
Drive the flower lead screw: 28BYJ-48 via ULN2003 on GPIO17/27/22/23, Tr8x8 screw (8 mm/turn).
DIRECTION: down | up.  Default 2000 us per half-step (8 s/turn, 1 mm/s). Add "full" for
two-coil full steps (more torque, 2048 steps/rev). Forward half-step sequence = down (bench, 2026-09-28).
Homing: run "down 90 3000" — the gearbox stalls harmlessly at the bottom stop; that is 0 mm.
Position bookkeeping lives in calibration.json (current_position_mm); this script does not update it.
"""
import sys, time, pigpio
PINS = [17, 27, 22, 23]
SEQ = [(1,0,0,0),(1,1,0,0),(0,1,0,0),(0,1,1,0),(0,0,1,0),(0,0,1,1),(0,0,0,1),(1,0,0,1)]
PITCH_MM = 8.0; STEPS_PER_REV = 4096
direction = sys.argv[1]; mm = float(sys.argv[2]); us = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
if len(sys.argv) > 4 and sys.argv[4] == "full":
    SEQ = [(1,1,0,0),(0,1,1,0),(0,0,1,1),(1,0,0,1)]; STEPS_PER_REV = 2048
seq = SEQ if direction == "down" else SEQ[::-1]
steps = int(round(mm / PITCH_MM * STEPS_PER_REV)); CYCLE = len(seq); cycles = steps // CYCLE
pi = pigpio.pi()
for p in PINS: pi.set_mode(p, pigpio.OUTPUT); pi.write(p, 0)
pi.wave_clear(); pulses = []
for pattern in seq:
    on = sum(1 << p for p, v in zip(PINS, pattern) if v); off = sum(1 << p for p, v in zip(PINS, pattern) if not v)
    pulses.append(pigpio.pulse(on, off, us))
pi.wave_add_generic(pulses); wid = pi.wave_create()
chain = []; remaining = cycles
while remaining > 0:
    n = min(remaining, 65535); chain += [255, 0, wid, 255, 1, n & 255, n >> 8]; remaining -= n
t0 = time.time(); pi.wave_chain(chain)
print("%s %.2f mm = %d steps, %.1f turns, est %.0f s" % (direction, mm, cycles*CYCLE, cycles*CYCLE/STEPS_PER_REV, cycles*CYCLE*us/1e6), flush=True)
while pi.wave_tx_busy(): time.sleep(0.2)
for p in PINS: pi.write(p, 0)
pi.wave_delete(wid); pi.stop()
print("done in %.0f s, coils off" % (time.time() - t0), flush=True)
