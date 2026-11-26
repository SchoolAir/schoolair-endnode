#!/usr/bin/env python3
"""flower_service.py — keep the SchoolAir wilting flower in step with the room's air.

What it does, once a minute:
  1. read the newest sensor reading the endnode has stored locally (CO2, PM2.5)
  2. turn each metric into a stem height using the tables in calibration.json
  3. take the LOWER of the two heights (the worse metric wins)
  4. if that differs from where the flower is by more than the hysteresis, move there

Position is counted from the bottom stop and written to calibration.json after
every move, with a position_state of "known", "moving" or "unknown". At start-up
the service homes ONLY if the state is not "known": first boot, a stop that
interrupted a move, or after `--home`. Homing drives the carriage down past the
bottom stop (the 28BYJ-48's gearbox stalls harmlessly) and calls that 0 mm. The
stop is 25 mm below the wilted position, where the stem is folded hard and the
first lift is the hardest the motor ever makes, so the climb out uses the
slowest, highest-torque setting. Routine restarts never go there.

The positions and tables are data, not code. See calibration.json:
  positions: erect / leaning / horizontal / wilted, in mm above the bottom stop
  mapping:   for each metric, a list of [value, position-name] pairs; heights are
             interpolated linearly between pairs. Horizontal is placed on the
             guideline line the dashboard draws (1000 ppm CO2, 15 ug/m3 PM2.5).

Cold boot: the first start after power-up (a marker in /run/schoolair-flower is
absent) runs a visible self-test: home down to the stop, rise to erect, then go
to the current reading. A plain service restart does not. If no reading is
available yet, the flower never parks at wilted: it returns to the last known
position (or stays erect) and asks the status LED for "thinking" through the
endnode's LED state file, so wilted always means bad air.

Heartbeat: with a fresh reading in hand, once an hour in school hours the flower
dips a few millimetres and comes back. A nod says "alive"; air never moves that
fast both ways, so it is not mistaken for a reading. No fresh reading, no nod,
so a still flower means the sensor or the flower has stopped. See
calibration.json "heartbeat".

Status motions (STATUS-MOTIONS.md in SchoolAir/schoolair, hardware/wilting-flower):
the flower has no LED, so its movements say what a height cannot. A still flower
is always a reading; a moving flower is telling you something else. Built so far:
  1 waiting for setup   the endnode has no AUTH_TOKEN: wilted, a small upward nod
                        every 10 s (every 60 s after 30 min), readings ignored
  4 normal service      height = air quality, no gestures (the heartbeat stays off)
  5 sensor fault        no valid reading for 2 min: hold the height, shiver every 60 s
  7 factory reset       the token goes away: back to state 1, no reboot needed
Off switch, checked every few seconds, no restart needed: "status_motions":
{"enabled": false} in calibration.json (a calibration.json without the block counts
as off, so units already in schools keep their behaviour), or an empty file named
flower-quiet on the SD card's boot partition. Off = exactly the behaviour before.

Usage:
  flower_service.py                 run forever (what the systemd unit does)
  flower_service.py --nod           one nod, then exit
  flower_service.py --shiver        one shiver, then exit
  flower_service.py --status-motions [on|off]   show, or switch the status motions
  flower_service.py --once          one read-score-move cycle, then exit
  flower_service.py --score         print the latest reading and the height it maps to
  flower_service.py --goto 40       move to 40 mm and exit (assumes position is known)
  flower_service.py --home          home only
  flower_service.py --home --goto 57   home, then climb to 57 mm (slow recovery climb out of the stop first)
Run as the same user that owns calibration.json; needs pigpiod running.
"""
import json, os, sqlite3, sys, time, signal, logging
import pigpio

HERE = os.path.dirname(os.path.abspath(__file__))
CAL_PATH = os.environ.get("FLOWER_CALIBRATION", os.path.join(HERE, "calibration.json"))
QUEUE_DB = os.environ.get("SCHOOLAIR_QUEUE_DB", "/home/admin/schoolair/queue.db")

log = logging.getLogger("flower")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

# ---------------------------------------------------------------- calibration
def load_cal():
    with open(CAL_PATH) as f:
        return json.load(f)

def save_cal(cal):
    tmp = CAL_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cal, f, indent=2)
    os.replace(tmp, CAL_PATH)

# ---------------------------------------------------------------- stepper
# pigpiod has ONE wave transmitter for the whole Pi and the endnode's status LED
# (schoolair-led.service) breathes with waves too. Starting a wave replaces the
# one playing, so an LED state change during a move would cut the step pattern
# and lose the position (found 2026-09-29: a home hung for ever on the LED's
# repeating wave). Protocol: raise MOVING_FLAG, wait LED_YIELD_S for the LED
# daemon (it polls once a second) to switch to plain PWM, move, lower the flag.
# The LED resends its pattern when the flag goes. A move that is still busy
# well past its computed duration, or whose transmission is no longer our chain,
# is declared interrupted and the position becomes unknown.
MOVING_FLAG = os.environ.get("FLOWER_MOVING_FLAG", "/run/schoolair-flower/moving")
LED_REQUEST_FILE = "/run/schoolair-flower/led-request"   # read by schoolair-led, see Flower._led
DEMO_FLAG = "/run/schoolair/demo"   # present = demo mode; shared with the firmware (reads every ~3 s, deletes it after demo.max_minutes); cleared by a reboot

def demo_mode():
    """Demonstrations only: the firmware reads every few seconds while this file
    exists, and the flower polls and smooths faster (mapping.demo). Checked on every
    cycle so it can be toggled by hand without restarting anything."""
    return os.path.exists(DEMO_FLAG)
# Status motions. The endnode is set up once its .env holds an AUTH_TOKEN (the same
# test its registration wizard makes); a factory reset or a fresh image clears it.
ENDNODE_ENV = os.environ.get("SCHOOLAIR_ENV", "/home/admin/schoolair/.env")
# An empty file with this name on the boot partition turns the status motions off:
# anyone can make it by putting the card in a computer, no SSH needed.
QUIET_FILES = tuple(os.environ.get("FLOWER_QUIET_FILES", "/boot/firmware/flower-quiet:/boot/flower-quiet").split(":"))

# Raised by the endnode (device_identity.py) when this card's .env belongs to another
# Pi, e.g. a card cloned from a registered unit: its AUTH_TOKEN is not this unit's.
IDENTITY_MISMATCH = os.environ.get("SCHOOLAIR_IDENTITY_MISMATCH", "/run/schoolair/identity-mismatch")

def endnode_set_up(path=None):
    """True once the endnode is registered: the same test as its ssh_register.py, a
    non-empty AUTH_TOKEN and no identity mismatch (a clone is not set up). If the .env
    can't be read for any reason other than not existing, answer True: normal
    service is the safe default."""
    if os.path.exists(IDENTITY_MISMATCH):
        return False
    try:
        with open(path or ENDNODE_ENV) as f:
            for line in f:
                if line.startswith("AUTH_TOKEN="):
                    return bool(line[len("AUTH_TOKEN="):].strip())
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return True

def led_service_running():
    """Is the endnode's status LED daemon active? A flower unit has no LED (since
    2026-11-07), and a gesture every 10 s should not wait 3 s for one that isn't there.
    Unsure (no systemctl) counts as running."""
    import subprocess
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "schoolair-led"], timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True

def status_motions_on(cal):
    """The off switch, read fresh from calibration.json so it works without a restart."""
    if any(os.path.exists(q) for q in QUIET_FILES):
        return False
    try:
        block = load_cal().get("status_motions", {})
    except (OSError, ValueError):
        block = cal.get("status_motions", {})
    return bool(block.get("enabled", False))

LED_YIELD_S = 3.0      # the LED daemon polls the flag about once a second; 1.5 s lost a race on 2026-10-01
MOVE_GRACE_S = 3.0
WAVE_CHAIN = 9998      # pigpio wave_tx_at(): a chain of several waves is playing (a one-wave chain reports that wave's id)
WAVE_NONE = 9999       # pigpio wave_tx_at(): nothing is playing

class MoveInterrupted(RuntimeError):
    """The step pattern stopped before the move was complete; the position is unknown."""

def raise_moving_flag():
    try:
        with open(MOVING_FLAG, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError as e:
        log.warning("cannot raise %s (%s): the status LED may interrupt this move", MOVING_FLAG, e)
        return False

def lower_moving_flag():
    try:
        os.remove(MOVING_FLAG)
    except OSError:
        pass

HALF = [(1,0,0,0),(1,1,0,0),(0,1,0,0),(0,1,1,0),(0,0,1,0),(0,0,1,1),(0,0,0,1),(1,0,0,1)]
FULL = [(1,1,0,0),(0,1,1,0),(0,0,1,1),(1,0,0,1)]

class Stepper:
    """28BYJ-48 through a ULN2003, driven with pigpio waveforms so timing does not
    depend on Python. Forward half-step sequence moves the carriage DOWN on this
    build (bench, 2026-09-28)."""
    def __init__(self, cal):
        self.pins = [cal["pins_bcm"][k] for k in ("IN1", "IN2", "IN3", "IN4")]
        self.mm_per_turn = cal["mm_per_turn"]
        self.pi = pigpio.pi()
        if not self.pi.connected:
            raise SystemExit("pigpiod is not running")
        for p in self.pins:
            self.pi.set_mode(p, pigpio.OUTPUT); self.pi.write(p, 0)

    def release(self):
        for p in self.pins:
            self.pi.write(p, 0)

    def move(self, mm, direction, us, full=False, flag=True):
        """Move |mm| in 'up' or 'down'. Blocks until done. Returns the mm actually commanded.
        flag=False: the caller already holds the moving flag (a gesture of several
        short moves raises it once, so it waits for the LED once, not before each move)."""
        seq = FULL if full else HALF
        steps_per_rev = 2048 if full else 4096
        if direction == "up":
            seq = seq[::-1]
        steps = int(round(abs(mm) / self.mm_per_turn * steps_per_rev))
        cycles = steps // len(seq)
        if cycles == 0:
            return 0.0
        flagged = raise_moving_flag() if flag else False
        try:
            if flagged:
                time.sleep(LED_YIELD_S)          # let the LED daemon step off the waves
            self.pi.wave_clear()
            pulses = []
            for pattern in seq:
                on = sum(1 << p for p, v in zip(self.pins, pattern) if v)
                off = sum(1 << p for p, v in zip(self.pins, pattern) if not v)
                pulses.append(pigpio.pulse(on, off, us))
            self.pi.wave_add_generic(pulses)
            wid = self.pi.wave_create()
            chain, remaining = [], cycles
            while remaining > 0:
                n = min(remaining, 65535)
                chain += [255, 0, wid, 255, 1, n & 255, n >> 8]
                remaining -= n
            expected_s = cycles * len(seq) * us / 1e6
            deadline = time.monotonic() + expected_s + MOVE_GRACE_S
            self.pi.wave_chain(chain)
            while self.pi.wave_tx_busy():
                # Ours is being played if pigpiod names our wave or a chain (9998);
                # 9999 means it just finished. Anything else replaced us.
                at = self.pi.wave_tx_at()
                if at not in (wid, WAVE_CHAIN, WAVE_NONE):
                    self._abort(wid)
                    raise MoveInterrupted(f"another pigpio client replaced the step pattern (wave {at}) mid-move")
                if time.monotonic() > deadline:
                    self._abort(wid)
                    raise MoveInterrupted(f"step pattern still busy {MOVE_GRACE_S:.0f} s after its {expected_s:.0f} s should have ended")
                time.sleep(0.1)
            self.release()
            self.pi.wave_delete(wid)
        finally:
            if flagged:
                lower_moving_flag()
        return cycles * len(seq) / steps_per_rev * self.mm_per_turn

    def _abort(self, wid):
        """Stop whatever is playing, de-energise, and drop our wave. Never raises."""
        for f in (self.pi.wave_tx_stop, self.release, lambda: self.pi.wave_delete(wid)):
            try:
                f()
            except Exception:
                pass

# ---------------------------------------------------------------- readings
LATEST_FILE = os.environ.get("LATEST_READING_FILE", "/run/schoolair/latest.json")

def _flatten(data):
    flat = {}
    for sensor in (data or {}).values():      # {"sen6x": {...}, "aux": {...}}
        if isinstance(sensor, dict):
            flat.update(sensor)
    return flat

def _from_latest_file():
    try:
        with open(LATEST_FILE) as f:
            doc = json.load(f)
        flat = _flatten(doc.get("data")); flat["recorded_at"] = doc.get("recorded_at"); flat["source"] = "latest.json"
        return flat
    except (OSError, ValueError):
        return None                              # not published on this firmware

def _from_queue(db_path):
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        row = con.execute("SELECT data, recorded_at FROM measurements_queue ORDER BY recorded_at DESC LIMIT 1").fetchone()
        con.close()
    except sqlite3.Error as e:
        log.warning("queue.db not readable: %s", e)
        return None
    if not row:
        return None
    flat = _flatten(json.loads(row[0])); flat["recorded_at"] = row[1]; flat["source"] = "queue.db"
    return flat

def latest_reading(db_path=QUEUE_DB):
    """The endnode's current reading, flattened to one dict plus recorded_at.

    Two sources, and the NEWER one wins:
      - /run/schoolair/latest.json, published by the firmware after every
        measurement (schoolair-endnode PR #2). Can be stale if that firmware
        was stopped or rolled back while the tmpfs file remained (this bit us
        on the night of 2026-09-28: the flower held a 22:24 reading until reboot).
      - the newest row of queue.db. Rows vanish once uploaded, so callers keep
        the last value they saw; the endnode adds a row every 5-15 min.
    """
    cands = [c for c in (_from_latest_file(), _from_queue(db_path)) if c and c.get("recorded_at")]
    if not cands:
        return None
    return max(cands, key=lambda c: c["recorded_at"])

def reading_age_min(reading):
    from datetime import datetime, timezone
    ts = reading.get("recorded_at")
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 60

# ---------------------------------------------------------------- mapping
def height_for(value, table, positions):
    """Piecewise-linear: table = [[value, position_name], ...] ascending by value."""
    pts = [(float(v), float(positions[name])) for v, name in table]
    if value <= pts[0][0]:
        return pts[0][1]
    for (v0, h0), (v1, h1) in zip(pts, pts[1:]):
        if value <= v1:
            return h0 + (h1 - h0) * (value - v0) / (v1 - v0)
    return pts[-1][1]

# The SEN6x reports "no valid value" as the top of its integer range: CO2 32766/32767,
# PM 0xFFFF scaled to 6553.5. On 2026-09-29 four CO2 = 32766 readings each drove the
# flower to wilted, which must only ever mean bad air, never "no data". Anything
# outside these bounds is a sensor fault, not air. calibration.json may override.
DEFAULT_VALID_RANGE = {"co2": [250, 10000], "pm25": [0, 1000]}

def reading_problem(reading, cal):
    """None if every mapped metric is present and plausible, else a short reason.

    One bad metric invalidates the whole reading: scoring the other one alone
    could stand the flower upright in a stuffy room because only PM2.5 was clean.
    """
    ranges = {**DEFAULT_VALID_RANGE, **cal["mapping"].get("valid_range", {})}
    for metric in cal["mapping"]["tables"]:
        v = reading.get(metric)
        if v is None:
            return f"{metric} missing"
        try:
            v = float(v)
        except (TypeError, ValueError):
            return f"{metric} not a number ({v!r})"
        lo, hi = ranges.get(metric, [float("-inf"), float("inf")])
        if not lo <= v <= hi:
            return f"{metric} {v:g} outside {lo:g}-{hi:g}"
    return None

def smoothed_reading(recent, cal):
    """Median per metric over the readings in the last mapping.smoothing_window_s.

    With one reading a minute (normal mode) the window holds a single reading, so
    this is the plain reading and the flower behaves as before. When the firmware
    is in incident mode (a reading every 10 s after a jump) the window holds three,
    and the median stops the motor twitching on every wobble while still following
    a real change within about half a minute. The window is measured on the
    readings' own timestamps, so a stale file or a slow poll cannot widen it.
    """
    if not recent:
        return None
    newest = recent[-1]
    out = dict(newest)
    for metric in cal["mapping"]["tables"]:
        vals = sorted(float(r[metric]) for r in recent if r.get(metric) is not None)
        if vals:
            out[metric] = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    return out

def _epoch(reading):
    from datetime import datetime
    try:
        return datetime.fromisoformat(reading["recorded_at"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def target_height(reading, cal):
    """Returns (height_mm, details). The worse metric (lower height) wins."""
    m = cal["mapping"]; positions = cal["positions"]
    heights = {}
    for metric, table in m["tables"].items():
        v = reading.get(metric)
        if v is None:
            continue
        heights[metric] = round(height_for(float(v), table, positions), 1)
    if not heights:
        return None, {}
    return min(heights.values()), heights

# ---------------------------------------------------------------- controller
class Flower:
    def __init__(self):
        self.cal = load_cal()
        self.st = Stepper(self.cal)
        self.pos = self.cal.get("current_position_mm")
        self.last_reading = None
        self.recent = []                  # valid readings inside the smoothing window, oldest first
        self.just_homed = False
        self._started = time.time()   # heartbeat waits a full interval after start
        mv = self.cal["moves"]
        self.down_us, self.up_us, self.up_full = mv["down_us"], mv["up_us"], mv["up_full_step"]
        # Homing ends in a deliberate stall on the stop: keep it gentle even when the
        # working down speed is fast (the bench runs 1.5 ms since 2026-10-01).
        self.home_us = mv.get("home_us", max(mv["down_us"], 3000))

    def _save(self, state="known", note=None):
        # Re-read the file and change only the position keys, so an edit made to
        # calibration.json while the service runs (a new table, a new speed) is
        # never overwritten by this process's stale in-memory copy.
        fresh = load_cal()
        fresh["current_position_mm"] = self.pos
        fresh["position_state"] = state
        if note:
            fresh["position_note"] = note
        save_cal(fresh)
        for k in ("current_position_mm", "position_state", "position_note"):
            if k in fresh: self.cal[k] = fresh[k]

    def _move(self, mm, direction, us, full=False, flag=True):
        try:
            if flag:
                return self.st.move(mm, direction, us, full=full)
            return self.st.move(mm, direction, us, full=full, flag=False)
        except MoveInterrupted as e:
            self.pos = None
            self._save(state="unknown", note=f"move interrupted {time.strftime('%Y-%m-%d %H:%M')}: {e}; will home")
            log.error("move interrupted: %s; position unknown, homing next", e)
            raise

    def home(self):
        stop = float(self.cal["homing"].get("stop_position_mm", 0.0))
        if self.cal.get("position_state") == "known" and isinstance(self.pos, (int, float)):
            # Position known: only the distance to the stop plus a small margin, so the
            # gearbox stalls against the post for a moment rather than ten seconds.
            dist = max(0.0, self.pos - stop) + float(self.cal["homing"].get("known_overshoot_mm", 3.0))
        else:
            dist = (self.cal["travel_stop_to_stop_mm"] - stop) + self.cal["homing"]["overshoot_mm"]
        log.info("homing: down %.1f mm to the bottom stop", dist)
        self._save(state="moving", note="homing")
        self._move(dist, "down", self.home_us)
        # Where the stop physically is, in the frame all positions are measured in.
        # 0 on the original lid; 25 once the stop spacer (or the Rev H+ 32.5 mm post)
        # puts the stop at the wilted height. Positions and tables stay unchanged.
        self.pos = float(self.cal["homing"].get("stop_position_mm", 0.0))
        self.just_homed = True
        self._homed_at = time.localtime()   # a home after tonight's hour counts as tonight's
        self._save(note="homed " + time.strftime("%Y-%m-%d %H:%M"))

    def goto(self, mm):
        lo, hi = self.cal["working_min_mm"], self.cal["working_max_mm"]
        mm = max(lo, min(hi, mm))
        if self.pos is None:
            self.home()
        delta = mm - self.pos
        if abs(delta) < 0.05:
            return
        self._save(state="moving")
        if delta > 0:
            rec = self.cal["moves"].get("recovery", {})
            if getattr(self, "just_homed", False) and rec:
                # climbing out of the stop: stem folded hardest, use the slow torque setting first
                first = min(delta, rec.get("up_to_mm", 30.0) - self.pos) if rec.get("up_to_mm", 30.0) > self.pos else 0
                if first > 0:
                    self._move(first, "up", rec.get("up_us", 6000), full=True)
                    delta -= first
                self.just_homed = False
            if delta > 0:
                self._move(delta, "up", self.up_us, full=self.up_full)
        else:
            self._move(-delta, "down", self.down_us)
        self.pos = round(mm, 2)
        self._save()
        log.info("moved to %.1f mm", self.pos)

    def step(self):
        """One read-score-move cycle. Returns a status dict."""
        r = latest_reading()
        if r:
            self.last_reading = r
        r = self.last_reading
        if not r:
            return {"status": "no reading yet"}
        age = reading_age_min(r)
        max_age = self.cal["mapping"]["max_reading_age_min"]
        if age is not None and age > max_age:
            return {"status": "reading too old", "age_min": round(age), "held_at_mm": self.pos}
        problem = reading_problem(r, self.cal)
        if problem:
            # Hold: a sensor fault is "no data", and the LED says so ("thinking").
            return {"status": "invalid reading", "problem": problem, "recorded_at": r.get("recorded_at"),
                    "held_at_mm": self.pos}
        # Keep the valid readings of the last smoothing window and score their median.
        demo = demo_mode()
        m = self.cal["mapping"]
        window = float((m.get("demo", {}) if demo else m).get("smoothing_window_s", m.get("smoothing_window_s", 0)))
        t = _epoch(r)
        if not self.recent or r.get("recorded_at") != self.recent[-1].get("recorded_at"):
            self.recent.append(r)
        if t is not None:
            self.recent = [x for x in self.recent if (_epoch(x) or t) > t - window]
        self.recent = self.recent[-25:]
        sm = smoothed_reading(self.recent, self.cal) if window > 0 else r
        target, per_metric = target_height(sm, self.cal)
        if target is None:
            return {"status": "no mapped metric in reading", "reading": r}
        hyst = self.cal["mapping"]["hysteresis_mm"]
        moved = False
        if self.pos is None or abs(target - self.pos) >= hyst:
            self.goto(target); moved = True
        out = {"status": "ok", "co2": sm.get("co2"), "pm25": sm.get("pm25"), "n": len(self.recent),
               "age_min": round(age or 0, 1), "heights": per_metric, "target_mm": target,
               "position_mm": self.pos, "moved": moved}
        if demo:
            out["demo"] = True
        return out

    # ------------------------------------------------------------ cold boot
    def _is_cold_boot(self):
        st = self.cal.get("self_test", {})
        marker = st.get("boot_marker", "/run/schoolair-flower/booted")
        if os.path.exists(marker):
            return False
        try:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            open(marker, "w").write(time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError as e:
            log.warning("cannot write boot marker %s: %s", marker, e)
        return True

    def _led(self, state):
        """Ask the endnode's status LED for a state while we hold without a valid reading.

        The LED daemon (schoolair-endnode, feature/flower-led-request) reads this file
        as a request, not a command: it can only turn "ok" into "thinking", never hide
        an error, AP mode or no_sensor, and ignores a request older than 5 minutes.
        So it is rewritten on every poll while holding and removed once a valid
        reading is back. (Writing the LED's own state file lost to ingest's ping loop
        within about 15 s.)"""
        path = self.cal.get("self_test", {}).get("led_request_file", LED_REQUEST_FILE)
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(state)
            os.replace(tmp, path)                    # the daemon never reads half a word
        except OSError:
            pass                                     # no LED service on this unit

    def _led_clear(self):
        try:
            os.remove(self.cal.get("self_test", {}).get("led_request_file", LED_REQUEST_FILE))
        except OSError:
            pass

    def self_test(self):
        """Home, rise to erect, then settle on the current reading."""
        last_known = self.pos if self.cal.get("position_state") == "known" else None
        log.info("cold boot: self-test (full home, erect, then current reading)")
        # After a power cut the saved position cannot be trusted: the carriage may
        # have moved unpowered, or a move was cut off before its count was saved.
        # On 2026-10-03 a short home from a stale position set a wrong zero and the
        # flower sat far below where it believed. Always home blind on a cold boot.
        self.cal["position_state"] = "unknown"
        self.home()
        self.goto(self.cal["working_max_mm"])
        s = self.step()
        if s.get("status") != "ok":
            # No usable reading yet. Never park at wilted without one.
            hold = last_known if last_known is not None else self.cal["working_max_mm"]
            self._led(self.cal.get("self_test", {}).get("led_state_when_no_reading", "thinking"))
            self.goto(hold)
            log.info("no reading yet (%s); holding at %.1f mm, LED asked for thinking", s.get("status"), self.pos)
        else:
            log.info("self-test done: %s", json.dumps(s))

    # ------------------------------------------------------------ heartbeat
    def _in_heartbeat_hours(self, hb):
        import datetime
        now = datetime.datetime.now()
        if hb.get("weekdays_only", True) and now.weekday() >= 5:
            return False
        start, end = hb.get("hours", "08:00-17:00").split("-")
        return start <= now.strftime("%H:%M") < end

    def nod(self):
        """Dip and return; position unchanged afterwards. Nods upward if too low to dip."""
        if self.pos is None:
            return False
        dip = float(self.cal.get("heartbeat", {}).get("dip_mm", 4.0))
        lo, hi = self.cal["working_min_mm"], self.cal["working_max_mm"]
        if self.pos - dip >= lo:
            first, second = ("down", "up")
        elif self.pos + dip <= hi:
            first, second = ("up", "down")
        else:
            return False
        repeats = int(self.cal.get("heartbeat", {}).get("repeats", 1))
        self._save(state="moving", note="nod")
        for _ in range(repeats):
            for d in (first, second):
                if d == "down":
                    self._move(dip, "down", self.down_us)
                else:
                    self._move(dip, "up", self.up_us, full=self.up_full)
                time.sleep(0.6)
        self._save()
        log.info("nod x%d (%s then %s, %.0f mm) at %.1f mm", repeats, first, second, dip, self.pos)
        return True

    # ------------------------------------------------------------ status motions
    def _sm(self, key, default):
        return self.cal.get("status_motions", {}).get(key, default)

    def _gesture(self, name, moves):
        """Play a list of (direction, mm, pause_s) and end where it started. The moving
        flag is raised once for the whole gesture. Returns False if it cannot fit."""
        if self.pos is None:
            return False
        lo, hi = self.cal["working_min_mm"], self.cal["working_max_mm"]
        h = self.pos
        for d, mm, _ in moves:                    # check the whole path stays in range first
            h += mm if d == "up" else -mm
            if not (lo - 0.01 <= h <= hi + 0.01):
                return False
        self._save(state="moving", note=name)
        flagged = raise_moving_flag()
        try:
            if flagged and led_service_running():
                time.sleep(LED_YIELD_S)          # once per gesture; skipped where no LED runs
            for d, mm, pause in moves:
                if d == "down":
                    self._move(mm, "down", self.down_us, flag=False)
                else:
                    self._move(mm, "up", self.up_us, full=self.up_full, flag=False)
                if pause:
                    time.sleep(pause)
        finally:
            if flagged:
                lower_moving_flag()
        self._save()
        return True

    def nod_up(self):
        """Status nod (state 1): up nod_mm, pause, back down. At the top it dips instead."""
        mm, pause = float(self._sm("nod_mm", 4.0)), float(self._sm("nod_pause_s", 0.5))
        return (self._gesture("status nod", [("up", mm, pause), ("down", mm, 0)])
                or self._gesture("status nod", [("down", mm, pause), ("up", mm, 0)]))

    def shiver(self):
        """Sensor fault (state 5): +-shiver_mm, shiver_times times, around the height."""
        mm, n = float(self._sm("shiver_mm", 1.0)), int(self._sm("shiver_times", 3))
        up_first = [("up", mm, 0.1), ("down", mm, 0.1)] * n
        down_first = [("down", mm, 0.1), ("up", mm, 0.1)] * n
        ok = self._gesture("shiver", down_first) or self._gesture("shiver", up_first)
        if ok:
            log.info("shiver x%d (%.1f mm) at %.1f mm", n, mm, self.pos)
        return ok

    def status_mode(self):
        """'off' (switch off: behaviour as before), 'setup' (state 1) or 'service'."""
        if not status_motions_on(self.cal):
            return "off"
        return "service" if endnode_set_up() else "setup"

    def setup_tick(self, now):
        """State 1: wilted, readings ignored, an upward nod every 10 s, every 60 s after 30 min."""
        if getattr(self, "_setup_since", None) is None:
            self._setup_since, self._last_status_nod = now, 0.0
            log.info("status: waiting for setup (no AUTH_TOKEN in %s); wilted, nodding", ENDNODE_ENV)
        self._led_clear()                         # the motions are the status here
        wilted = float(self.cal["positions"]["wilted"])
        if self.pos is None or abs(self.pos - wilted) >= 0.05:
            self.goto(wilted)
        slow = now - self._setup_since > float(self._sm("setup_slow_after_min", 30)) * 60
        every = float(self._sm("setup_slow_nod_every_s", 60) if slow else self._sm("setup_nod_every_s", 10))
        if now - self._last_status_nod >= every:
            self.nod_up()
            self._last_status_nod = time.time()   # count from the end, so a nod never runs back to back
        return every

    def fault_tick(self, status, now):
        """State 5: without a valid reading for fault_after_s, hold and shiver every shiver_every_s."""
        if status.get("status") == "ok":
            if getattr(self, "_fault_since", None) is not None:
                log.info("status: valid readings again, shiver stops")
            self._fault_since = None
            return
        if getattr(self, "_fault_since", None) is None:
            self._fault_since, self._last_shiver = now, 0.0
            return
        if now - self._fault_since < float(self._sm("fault_after_s", 120)):
            return
        if now - self._last_shiver >= float(self._sm("shiver_every_s", 60)):
            if self._last_shiver == 0.0:
                log.info("status: sensor fault for %d s (%s); holding at %s mm and shivering",
                         now - self._fault_since, status.get("status"), self.pos)
            self.shiver()
            self._last_shiver = time.time()

    def maybe_nightly_home(self, status):
        """Once a night (nightly_home.at, local time, default 04:00) re-home and return,
        so an unexplained loss of position (2026-10-02: the carriage was found 25 mm
        below the count after a quiet night) never lasts longer than a day. Only
        while the flower is otherwise idle, and never twice on the same date."""
        nh = self.cal.get("nightly_home", {})
        if not nh.get("enabled", False) or status.get("moved"):
            return
        now = time.localtime()
        try:
            hh, mm = (int(x) for x in nh.get("at", "04:00").split(":"))
        except ValueError:
            return
        if (now.tm_hour, now.tm_min) < (hh, mm):
            return
        today = time.strftime("%Y-%m-%d", now)
        if getattr(self, "_nightly_done", None) == today:
            return
        # Already homed today after the hour (e.g. the cold-boot self-test after a power
        # cut, 2026-10-07): homing again minutes later only grinds the stop a second time.
        h = getattr(self, "_homed_at", None)
        if h and time.strftime("%Y-%m-%d", h) == today and (h.tm_hour, h.tm_min) >= (hh, mm):
            self._nightly_done = today; return
        if time.time() - self._started < 120:        # a service that starts at 04:00 is not "due"
            self._nightly_done = today; return
        back_to = self.pos
        log.info("nightly home (%s): re-homing, then back to %s mm", nh.get("at", "04:00"), back_to)
        self.home()
        if isinstance(back_to, (int, float)):
            self.goto(back_to)
        self._nightly_done = today

    def maybe_nod(self, status):
        hb = self.cal.get("heartbeat", {})
        if not hb.get("enabled", False) or status.get("status") != "ok" or status.get("moved"):
            return
        if getattr(self, "_mode", "off") != "off":
            # Status motions: a still flower is always a reading, so no nod in service.
            return
        if hb.get("requires_fresh_reading", True) and status.get("age_min", 999) > self.cal["mapping"]["max_reading_age_min"]:
            return
        if not self._in_heartbeat_hours(hb):
            return
        every = float(hb.get("every_min", 60)) * 60
        if time.time() - getattr(self, "_last_nod", self._started) < every:
            return
        if self.nod():
            self._last_nod = time.time()

    def run(self):
        poll = self.cal["mapping"]["poll_seconds"]
        self._demo_logged = False
        self._mode = self.status_mode()
        try:
            cold = self.cal.get("self_test", {}).get("on_cold_boot", True) and self._is_cold_boot()
            if cold and self._mode == "setup":
                # State 1 on power-up: the home is the motor test; then wilted and nodding.
                log.info("cold boot, not set up yet: home (motor test), then wilted")
                self.cal["position_state"] = "unknown"
                self.home()
            elif cold:
                self.self_test()
            elif self.cal.get("position_state") == "known" and isinstance(self.pos, (int, float)):
                log.info("position known (%.1f mm), skipping homing", self.pos)
            else:
                log.info("position %s, homing", self.cal.get("position_state", "unknown"))
                self.home()
        except MoveInterrupted:
            pass                                 # position unknown now; the first cycle homes
        log.info("running; polling every %d s; status motions %s", poll, self._mode)
        next_poll = 0.0
        while True:
            now = time.time()
            mode = self.status_mode()
            if mode != self._mode:
                log.info("status motions: %s -> %s", self._mode, mode)
                if self._mode == "setup":
                    self._setup_since = None
                    next_poll = 0.0               # set up now: go to the reading at once
                self._fault_since = None
                self._mode = mode
            if mode == "setup":
                try:
                    every = self.setup_tick(now)
                    self.maybe_nightly_home({"status": "setup"})
                except MoveInterrupted:
                    every = 10
                time.sleep(max(1.0, min(every, 5.0)))
                continue
            if mode == "service" and now < next_poll:
                try:
                    self.fault_tick(getattr(self, "_last_status", {"status": "ok"}), now)
                except MoveInterrupted:
                    pass
                time.sleep(max(1.0, min(5.0, next_poll - time.time())))
                continue
            try:
                s = self.step()
                self._last_status = s
                if s.get("moved") or s.get("status") != "ok":
                    log.info("%s", json.dumps(s))
                if s.get("status") != "ok":          # refresh every poll: the LED drops requests older than 5 min
                    self._led(self.cal.get("self_test", {}).get("led_state_when_no_reading", "thinking"))
                else:
                    self._led_clear()
                if mode == "service":
                    self.fault_tick(s, time.time())
                self.maybe_nod(s)
                self.maybe_nightly_home(s)
            except MoveInterrupted:
                pass                             # logged by _move; goto() homes on the next cycle
            demo = demo_mode()
            if demo != self._demo_logged:
                log.info("demo mode %s (%s)", "on" if demo else "off", DEMO_FLAG); self._demo_logged = demo
            wait = self.cal["mapping"].get("demo", {}).get("poll_seconds", poll) if demo else poll
            if mode == "off":
                time.sleep(wait)
            else:                                # wake every few seconds for the switch and the gestures
                next_poll = time.time() + wait

def main(argv):
    fl = Flower()
    def bye(*_):
        moving = fl.cal.get("position_state") == "moving"
        if moving:                               # otherwise the playing wave is the LED's
            try: fl.st.pi.wave_tx_stop()
            except Exception: pass
        fl.st.release()
        lower_moving_flag()
        fl._led_clear()                          # a stopped flower asks nothing of the LED
        if moving or fl.pos is None:             # mid-move, or already unknown after an interruption
            fl.pos = None; fl._save(state="unknown", note="stopped with the position unknown; will home on next start")
            log.info("stopped; position unknown")
        else:
            fl._save(); log.info("stopped at %.1f mm", fl.pos)
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye); signal.signal(signal.SIGINT, bye)
    try:
        _dispatch(fl, argv)
    except MoveInterrupted as e:
        print(f"move interrupted: {e}. Position unknown: run --home", file=sys.stderr)
        sys.exit(2)

def _dispatch(fl, argv):
    if "--score" in argv:
        r = latest_reading()
        t, h = (target_height(r, fl.cal) if r else (None, {}))
        problem = reading_problem(r, fl.cal) if r else "no reading"
        print(json.dumps({"reading": r, "heights": h, "target_mm": None if problem else t,
                          "problem": problem}, indent=2)); return
    if "--home" in argv:
        fl.home()
        if "--goto" not in argv: return
        # --home --goto X: the same process still knows it just homed, so the climb
        # out of the stop uses the slow recovery setting (a separate --goto would not).
    if "--nod" in argv:
        fl.nod(); return
    if "--shiver" in argv:
        fl.shiver(); return
    if "--status-motions" in argv:
        i = argv.index("--status-motions")
        want = argv[i + 1] if i + 1 < len(argv) else None
        if want in ("on", "off"):
            cal = load_cal()
            cal.setdefault("status_motions", {})["enabled"] = want == "on"
            save_cal(cal)
        quiet = [q for q in QUIET_FILES if os.path.exists(q)]
        print(json.dumps({"enabled_in_calibration": load_cal().get("status_motions", {}).get("enabled", False),
                          "quiet_file": quiet or None, "set_up": endnode_set_up(),
                          "mode": fl.status_mode()}, indent=2)); return
    if "--self-test" in argv:
        fl.self_test(); return
    if "--goto" in argv:
        fl.goto(float(argv[argv.index("--goto") + 1])); return
    if "--once" in argv:
        print(json.dumps(fl.step(), indent=2)); return
    fl.run()

if __name__ == "__main__":
    main(sys.argv[1:])
