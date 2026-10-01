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

Usage:
  flower_service.py                 run forever (what the systemd unit does)
  flower_service.py --nod           one nod, then exit
  flower_service.py --once          one read-score-move cycle, then exit
  flower_service.py --score         print the latest reading and the height it maps to
  flower_service.py --goto 40       move to 40 mm and exit (assumes position is known)
  flower_service.py --home          home only
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

    def move(self, mm, direction, us, full=False):
        """Move |mm| in 'up' or 'down'. Blocks until done. Returns the mm actually commanded."""
        seq = FULL if full else HALF
        steps_per_rev = 2048 if full else 4096
        if direction == "up":
            seq = seq[::-1]
        steps = int(round(abs(mm) / self.mm_per_turn * steps_per_rev))
        cycles = steps // len(seq)
        if cycles == 0:
            return 0.0
        flagged = raise_moving_flag()
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

    def _move(self, mm, direction, us, full=False):
        try:
            return self.st.move(mm, direction, us, full=full)
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
        window = float(self.cal["mapping"].get("smoothing_window_s", 0))
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
        return {"status": "ok", "co2": sm.get("co2"), "pm25": sm.get("pm25"), "n": len(self.recent),
                "age_min": round(age or 0, 1), "heights": per_metric, "target_mm": target,
                "position_mm": self.pos, "moved": moved}

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
        log.info("cold boot: self-test (home, erect, then current reading)")
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

    def maybe_nod(self, status):
        hb = self.cal.get("heartbeat", {})
        if not hb.get("enabled", False) or status.get("status") != "ok" or status.get("moved"):
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
        try:
            if self.cal.get("self_test", {}).get("on_cold_boot", True) and self._is_cold_boot():
                self.self_test()
            elif self.cal.get("position_state") == "known" and isinstance(self.pos, (int, float)):
                log.info("position known (%.1f mm), skipping homing", self.pos)
            else:
                log.info("position %s, homing", self.cal.get("position_state", "unknown"))
                self.home()
        except MoveInterrupted:
            pass                                 # position unknown now; the first cycle homes
        log.info("running; polling every %d s", poll)
        while True:
            try:
                s = self.step()
                if s.get("moved") or s.get("status") != "ok":
                    log.info("%s", json.dumps(s))
                if s.get("status") != "ok":          # refresh every poll: the LED drops requests older than 5 min
                    self._led(self.cal.get("self_test", {}).get("led_state_when_no_reading", "thinking"))
                else:
                    self._led_clear()
                self.maybe_nod(s)
            except MoveInterrupted:
                pass                             # logged by _move; goto() homes on the next cycle
            time.sleep(poll)

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
        fl.home(); return
    if "--nod" in argv:
        fl.nod(); return
    if "--self-test" in argv:
        fl.self_test(); return
    if "--goto" in argv:
        fl.goto(float(argv[argv.index("--goto") + 1])); return
    if "--once" in argv:
        print(json.dumps(fl.step(), indent=2)); return
    fl.run()

if __name__ == "__main__":
    main(sys.argv[1:])
