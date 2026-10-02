# schoolair-pi

Raspberry Pi gateway for the SchoolAir platform. Handles device registration,
sensor reading, data ingestion to the central server, and local alert detection.

---

## Quick start — fresh Pi

```bash
curl -sSL https://raw.githubusercontent.com/SchoolAir/schoolair-ex-RMIT-pi/main/schoolair_setup.sh | sudo bash
```

This clones the repo to `/home/admin/schoolair/`, installs dependencies, builds
the SEN6x binaries, configures a Wi-Fi hotspot for first-boot registration,
and enables all systemd services. Idempotent — safe to re-run.

To use a different admin username:

```bash
curl -sSL ... | sudo ADMIN_USER=pi bash
```

### OTA updates — already-deployed Pis

Once a Pi is registered and in the field, the server can push an update without
physical access. Set `SCHOOLAIR_MIN_VERSION` on the server to the new target
version — every Pi will self-update within its next drain cycle (≤ 30 min during
school hours, ≤ 2 h outside them).

**What happens on the Pi:**

1. The drain loop receives `"update_available": true` in the ingest response.
2. It calls `sudo /usr/local/bin/schoolair-update` (pre-approved via sudoers).
3. The wrapper downloads the current `schoolair_setup.sh` from GitHub and runs
   it with `--update`, skipping host-config steps (hostname, apt, I2C, networking).
4. Updated code is deployed, pip deps installed, C binary recompiled if changed,
   and `sen6x.service` + `schoolair.service` are restarted.
5. `.env` and all hardware configuration are left untouched.

**To trigger a fleet update:**

```bash
# 1. Bump VERSION in jobs/ingest.py (e.g. "2.1.0")
# 2. Merge to the main branch
# 3. Set on the server and restart:
SCHOOLAIR_MIN_VERSION=2.1.0
```

**Channels.** Each unit follows a channel, written in `/etc/schoolair-channel`:

| Channel | Units | Installs from | Updated when |
|---|---|---|---|
| `stable` (default, file absent) | schools | `main` | the server's `SCHOOLAIR_MIN_VERSION` is raised |
| `dev` | the bench unit | `dev` | `schoolair-dev-update` finds a newer `VERSION` on the `dev` branch: it runs from `schoolair-dev-update.timer` every hour and installs it, without anyone running the update by hand |

Both use the same `schoolair-update` and the same 40-minute rollback watchdog.
A dev update waits for any flower move to finish first, and a version that was
rolled back is recorded in `/var/lib/schoolair/dev-update-rolled-back` and not
tried again. To put a unit on dev: `echo dev | sudo tee /etc/schoolair-channel`.
The channel is sent in the `X-Schoolair-Channel` header with every upload.

**To update a single Pi manually:**

```bash
sudo schoolair-update
```

The DB column `devices.firmware_version` is updated on every ingest so you can
query which Pi is running which version.

---

## How it works

### Service topology

Six systemd services run on every Pi:

```
sen6x.service            One-shot — runs `sen6x_read --init` at boot. Starts
                         continuous measurement and waits for the first valid
                         sample (fast if sensor already running, up to 75 s
                         cold). schoolair.service depends on it completing.

schoolair-first-boot     One-shot — assigns a unique hostname to cloned images
  .service               (schoolair-YYMDD-XXXX). No-op if already set.

schoolair-launcher       One-shot — waits up to 60 s for a client WiFi
  .service               connection. Connected + AUTH_TOKEN → exits (normal
                         operation). Connected, no token → starts wizard.
                         No connection → brings up AP hotspot + starts wizard.

schoolair-wizard         Browser-based registration + Wi-Fi setup (Microdot,
  .service               port 80). Started on demand. Stops itself after a
                         successful registration; idle-timeout stops it on LAN.

schoolair.service        Main telemetry process (see below). Starts after the
                         launcher exits. Restarts automatically on failure.

schoolair-netwatch       Persistent — monitors WiFi after boot. On uplink loss
  .service               for 2 min, brings up the AP hotspot and wizard. While
                         in AP mode with saved networks, probes for those
                         networks every 5 min (briefly closing the AP during
                         each probe). Closes AP and restarts telemetry on
                         reconnect.
```

### Latest reading file

Every sample the telemetry process takes (every 15 s, `SAMPLE_INTERVAL`) is
written, atomically, to `/run/schoolair/latest.json`. A metric the sensor had no
valid value for is `null`:

```json
{"data": {"sen6x": {"co2": 742, "pm25": 0.8, "temp": 28.5, ...}}, "recorded_at": "2026-09-28T21:31:43+00:00"}
```

If a read fails altogether, the file says so instead of keeping the last good
reading, so a consumer can react at once rather than wait for the reading to go
stale:

```json
{"data": null, "recorded_at": "2026-09-29T21:40:00+00:00", "error": "Sensor script failed: ..."}
```

This is the interface for other services on the same Pi that need the current
reading without opening `queue.db` (whose rows disappear as they upload) or
talking to the sensor. It lives on tmpfs, so it costs no SD-card writes and is
simply absent until the first reading after boot. The first consumer is the
**wilting-flower actuator** (`schoolair-flower.service`, repository
`SchoolAir/Flower-End-node`): a separate service that reads this file once a
minute and drives a stepper so a silk flower stands or droops with CO₂ and
PM2.5. That service owns the flower mechanics; this one owns the data. Neither
imports the other, and removing either leaves the other working.

### Sharing pigpiod with the flower

pigpiod has a single wave transmitter, and starting a wave replaces the one
playing. The status LED breathes with waves; the wilting-flower service drives
its stepper with waves too, and it has no position sensor, so a wave that cuts a
move mid-way loses the flower's position. The two agree on one file:
`/run/schoolair-flower/moving`, which the flower service creates before every
move and removes after it. While it exists `led_status.py` sends no waves and
holds a steady glow on plain PWM (timed independently by pigpiod); when it goes,
the LED resends its current pattern. The unit's `ExecStopPost` and the daemon's
own cleanup skip `wave_tx_stop`/`wave_clear` while the flag exists, for the same
reason. The flower side waits 1.5 s after raising the flag, watches
`wave_tx_at()` during the move, and treats a replaced or overdue wave as an
interruption (position unknown, re-home).

The flower can also ask the LED for "thinking" when it is holding still because
it has no valid reading (a sensor fault, or a reading too old). It writes the
word to `/run/schoolair-flower/led-request`, refreshes it every poll while it
holds, and deletes it when it has a reading again. That request can only turn
"ok" into "thinking"; errors, AP mode and "no_sensor" still show. A request not
refreshed for 5 minutes is ignored. It has its own file because the shared
`/run/schoolair-led-state` is rewritten to "ok" by every upload.

### Installing the flower

The flower's code is developed in `SchoolAir/Flower-End-node`, which is
private, and units have no GitHub credentials. So the files a unit needs are
bundled here, in `flower/`, at one pinned commit. `flower/SOURCE` records that
commit and a checksum per file. Setup and every OTA update install them on
indoor units into `~/flower`, offline, backing up what they replace so a
rollback restores the previous flower too. `calibration.json` is created once
and never replaced. A commit that is already installed is skipped.

To ship a new flower version: test it on a bench unit, then, on a machine with
access to `Flower-End-node`, run `scripts/vendor_flower.sh <commit>`, run the
tests, and commit `flower/`. Don't edit the files in `flower/` by hand; a test
fails if they no longer match `flower/SOURCE`.

The service is installed and enabled on every indoor unit. A drop-in
(`deploy/schoolair-flower-fitted.conf`) lets it start only when both of these
are true:

- **A flower is fitted.** The AQU dock's harness ties GPIO26 (physical pin 37)
  to ground (pin 39). `detect_flower.sh` switches on the pull-up and reads the
  pin: low means a strap, and it creates `/etc/schoolair-flower-fitted`. It runs
  at a clone's first boot and on every setup/update. `prepare_image.sh` removes
  the marker from golden images. A unit without the harness (the bench unit) is
  marked by hand: `sudo touch /etc/schoolair-flower-fitted`.
- **pigpiod is installed** (`/var/lib/schoolair-pigpio-installed`). On a new
  unit that happens only once the network is up. `schoolair-pigpio-setup.service`
  then starts the flower.

Updates restart pigpiod only when its drop-in changed, and restart the flower
only when its pin changed. Both first wait for any move in progress to finish
(`wait_for_flower_move`).

### Telemetry process

`main.py` runs two concurrent coroutines:

- **Logs stay on the card.** The journal is persistent (64 MB cap, synced
  every minute), so after a reboot `journalctl -b -1` reads the previous boot
  and `journalctl --list-boots` lists them. Before 2.3.21 it lived in RAM.
- **Starts without a network.** `schoolair.service` waits only for the sensor,
  not for Wi-Fi: readings and `latest.json` begin within about a minute of
  power-up wherever the unit is, and uploads queue in SQLite until a network
  appears.
- **Ingest loop** — samples the sensor every 15 s and publishes each sample
  locally (see *Latest reading file*). On a clock-aligned schedule (5 min during
  the active window, 15 min outside it) the mean of that interval's samples
  becomes one reading, which is uploaded at once or kept in SQLite and drained
  to the server in batches. SEN6x "no valid value" codes (CO2 32766/32767,
  PM 6553.5, …) are recorded as `null` and left out of the mean. When a reading breaches a threshold it launches
  a shared two-stage verification task (see below) to distinguish spikes from
  real events before sending an alert.

- **Microdot server** — lightweight local web server (port 8080). Serves the
  real-time dashboard at `/` and pushes live sensor state over WebSocket at
  `/ws`. nginx on port 80 proxies to this once the device is registered.

### Buffer strategy

The ingest loop is RAM-first: up to 500 readings are held in memory. SQLite is
only written when the server is unreachable **and** the RAM buffer is full. On
reconnect the next drain sends both the SQLite backlog and the current RAM
buffer in a single batch. On clean shutdown (`systemctl stop`, `sudo reboot`)
both in-memory buffers (measurements and alerts) are flushed to SQLite before
exit.

### Alert verification

When a reading breaches a threshold the ingest loop launches a single background
task that covers **all** metrics that breached in that same read. A shared sensor
read is taken once per timing point, with each metric evaluated against that
single reading — if both CO2 and temperature are high at the same time, the four
verification reads are taken once total, not once per metric.

**Timing:**

```
T        Original breach reading (already in buffer)
T+10s    Stage 1 read 1
T+30s    Stage 1 read 2       ← stage 1 complete
T+1m     Stage 2 read 1
T+2m     Stage 2 read 2       ← stage 2 complete (~2 min total)
```

**Severity scoring (integer 0–7):**

The `severity` field on the original reading at T encodes how many timing points
confirmed the breach. It is set to 0 on every reading and updated when the
verification task completes.

| Points | Condition |
|--------|-----------|
| +1 | Stage 1 task launched (always — baseline for any breach event) |
| +1 | T+10s: any breaching metric still near/above its threshold |
| +1 | T+30s: any breaching metric still near/above its threshold |
| +2 | T+1m: any breaching metric still near/above its threshold |
| +2 | T+2m: any breaching metric still near/above its threshold |

Maximum is 7 (all timing points confirmed). `severity >= 4` means at least one
stage-2 read confirmed the breach — an alert is sent immediately per metric.

**Outcomes and what reaches the server:**

| Severity | Interpretation | Readings sent |
|----------|----------------|---------------|
| 0 | No breach | Original reading |
| 1 | Fluke — stage 1 fired, both reads clear | Original reading with severity=1; verification reads dropped |
| 2–3 | Momentary — stage 1 confirmed, stage 2 clear | Original reading with severity; T+1m reading added to buffer to show recovery; other verification reads dropped |
| ≥4 | Persistent — stage 2 confirmed | Original reading with severity; per-metric alerts sent; all verification reads dropped |

The original reading is never modified beyond having its `severity` field set.
The raw sensor values at T are always preserved exactly as measured.

### Drain timing

Drains are event-driven: the read loop triggers a drain whenever the time since
the last drain would exceed the configured interval if the current read were
skipped. This guarantees drains happen *within* the configured window (25–30 min
active, 105–120 min idle) without a separate polling timer.

---

## First boot — registration

On first boot the launcher detects no `AUTH_TOKEN` and starts the wizard.
Connect to the `SchoolAir_Setup` Wi-Fi hotspot or navigate to
`http://schoolair-register.local` on the same network. The wizard guides you
through:

1. Device registration with the SchoolAir server
2. Wi-Fi configuration (connects the Pi to the school network)

After registration the wizard writes `AUTH_TOKEN` to `.env`, restarts
`schoolair.service`, and exits. nginx activates to proxy port 80 → port 8080.

---

## WiFi loss recovery

The `schoolair-netwatch` service handles network loss **after boot** without
requiring a reboot.

```
uplink lost
    │
    ▼ (2-minute grace period — ignores brief blips)
    │
    ▼
AP + wizard started
    │
    ├── user connects to SchoolAir_AP and reconfigures WiFi via wizard
    │       └── uplink restored → AP closed → telemetry restarted
    │
    └── no manual action taken
            │
            ▼ (every 5 minutes, if saved networks exist AND no client on AP)
            briefly close AP → try each saved network profile → reopen AP if failed
            │
            └── saved network available → AP closed → telemetry restarted
```

**Grace period (2 min):** Brief WiFi blips — DHCP renewal, AP restart — don't
trigger the fallback.

**Reconnect probe:** While the AP is up, the watchdog checks every 5 minutes
whether any of the Pi's saved WiFi profiles are now reachable. The AP drops for
about 10 seconds during each probe. If a client is currently connected to the
AP hotspot, that probe cycle is skipped so as not to interrupt an active wizard
session.

**Timing overrides** (useful when testing):

| Env var              | Default | Meaning |
|----------------------|---------|---------|
| `NETWATCH_POLL`      | `30` s  | Connectivity check interval |
| `NETWATCH_GRACE`     | `120` s | Uplink-loss grace period before AP mode |
| `NETWATCH_RECONNECT` | `300` s | Interval between reconnect probes in AP mode |

---

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: set SERVER_URL and optionally DEVICE_NICKNAME
```

Run with the mock sensor (no Pi hardware needed):

```bash
MOCK_SENSOR_SCRIPT=./scripts/mock-sensor.sh python main.py
```

Run with the real sensor bridge (Pi only):

```bash
python main.py   # uses read-sensor.sh by default via MOCK_SENSOR_SCRIPT in .env
```

For one-off sensor checks:

```bash
bash scripts/preview.sh
```

---

## Configuration

`config/settings.json` controls the active window — the period during which the
Pi reads and drains at the higher cadence (school hours).

```json
{
    "active_window": { "start": "07:00", "end": "16:00" }
}
```

- Window boundaries must be on a 15-minute mark (`:00`, `:15`, `:30`, `:45`).
- Maximum window length is 9 hours.
- `incident` (optional block): while the air is changing fast, the sensor is
  read every `sample_seconds` (10) instead of every 15 s, so the wilting
  flower can follow within seconds. An incident starts when PM2.5 more than
  doubles (`pm25_jump_factor` 2) or rises across `pm25_threshold` (15 µg/m³),
  or CO2 rises by `co2_jump_ppm` (100) between two readings or across `co2_threshold`
  (1000 ppm). It ends after `steady_minutes` (5) with every reading within
  ±`steady_pm25` (2) and ±`steady_co2` (50) of the window's mean. Only
  `latest.json` sees the extra readings: the upload mean keeps one reading a
  minute, so uploads are unchanged. `"enabled": false` switches it off. See
  `jobs/incident.py`.
- `demo` (optional block): **for demonstrations only.** While the file
  `/run/schoolair/demo` exists (`touch /run/schoolair/demo` as admin, no
  restart needed) the sensor is read every `sample_seconds` (3) whatever the
  incident detector says, so the flower reacts while people watch. `rm` the
  file to end it; it ends by itself after `max_minutes` (120, from the file's
  mtime, so a new `touch` extends it) and at reboot. Uploads are unchanged.
  Each switch is logged, `latest.json` carries `"mode": "demo"`, and the local
  dashboard shows it. See `jobs/demo.py`.
- `temp_offset_c` (default `0`): this unit's temperature correction in °C,
  for a sensor warmed by its enclosure. Measure the unit against a room
  thermometer and enter the difference, e.g. `-4.7` if it reads 4.7 °C high.
  Then `sudo systemctl restart schoolair`. The SEN6x applies the offset itself,
  so its humidity reading is corrected too. The offset is lost whenever the
  sensor resets, so the telemetry service sends it at every start and after
  every sensor re-init. Updates keep `settings.json`, so it is set once per
  unit.
- Read and drain intervals are fixed in source (`jobs/ingest.py`) and
  overridable via env vars for testing:

| Env var                  | Default   | Meaning                        |
|--------------------------|-----------|--------------------------------|
| `SAMPLE_INTERVAL`        | `15` s    | local sensor read and `latest.json` cadence (60 s before 2.3.20) |
| `READ_INTERVAL_ACTIVE`   | `300` s   | 5 min — upload cadence inside the window (mean of its samples) |
| `READ_INTERVAL_IDLE`     | `900` s   | 15 min — upload cadence outside the window (mean of its samples) |
| `DRAIN_INTERVAL_ACTIVE`  | `1800` s  | 30 min — max time between drains inside the window |
| `DRAIN_INTERVAL_IDLE`    | `7200` s  | 2 hr — max time between drains outside the window  |

---

## Environment variables

See `.env.example` for the full list. Key ones:

| Variable              | Default                   | Description |
|-----------------------|---------------------------|-------------|
| `AUTH_TOKEN`          | (empty)                   | Device auth token; written by the wizard on first boot |
| `SERVER_URL`          | `https://data.schoolair.org` | Central server base URL (no trailing slash) |
| `INGEST_URL`          | `{SERVER_URL}/node/aqc/v1/ingest` | Override the full ingest endpoint |
| `MOCK_SENSOR_SCRIPT`  | `./read-sensor.sh`        | Sensor script; replace with `./scripts/mock-sensor.sh` for development |
| `DEVICE_NICKNAME`     | (hostname)                | Display name shown on the dashboard |
| `PORT`                | `8080`                    | Local Microdot server port |
| `ALERT_NEAR_PCT`      | `10`                      | How close to a threshold counts as "near" during verification (%) |
| `ALERT_COOLDOWN_HOURS`| `1`                       | Minimum hours between alerts for the same metric |
| `BUFFER_CAPACITY`     | `500`                     | RAM buffer size (readings) before SQLite overflow |
| `LATEST_READING_FILE` | `/run/schoolair/latest.json` | Where each new reading is published for other local services (see below) |

---

## Testing

110 tests. All pass on a laptop except one hardware test that requires a real
SEN6x sensor connected via I2C on a Pi.

```bash
cd schoolair/        # repo root
pytest -m "not hardware"    # laptop-safe (102 tests)
pytest                      # full suite — Pi only
```

See `tests/README.md` for a breakdown by module.

---

## File structure

```
schoolair-pi/
├── config/
│   ├── criteria.json          Alert thresholds — written by the server on first
│   │                          successful drain; persisted across restarts
│   └── settings.json          Active window configuration
│
├── db/
│   └── queue.py               SQLite offline buffer — measurements and alerts
│
├── deploy/
│   ├── sen6x.service          SEN6x one-shot initialisation service
│   ├── schoolair-first-boot   One-shot hostname assignment
│   │   .service
│   ├── schoolair-launcher     Boot-time network check → start wizard or proceed
│   │   .service
│   ├── schoolair-netwatch     Persistent WiFi watchdog — AP fallback + reconnect
│   │   .service               probes (see "WiFi loss recovery")
│   ├── schoolair-wizard       Browser registration + Wi-Fi setup
│   │   .service
│   ├── schoolair.service      Main telemetry service
│   └── schoolair-dev          Development (user) service template
│       .service.example
│
├── i2c/sen6x/                 SEN6x C binaries source (sen6x_d, sen6x_read) and Makefile
│
├── jobs/
│   ├── aggregate.py           Hourly folding of old readings to reduce DB size
│   └── ingest.py              Read loop, drain loop, shared alert verification,
│                              severity scoring
│
├── registration_wizard/
│   ├── launcher.sh            Boot-time decision: start wizard or not
│   ├── netwatch.sh            Persistent WiFi watchdog daemon
│   └── wizard.py              Microdot browser portal (registration + Wi-Fi)
│
├── scripts/
│   ├── mock-sensor.sh         Fake sensor output for local development
│   ├── preview.sh             One-shot sensor read to terminal
│   └── populate_queue.py      Debug util — fill the local SQLite queue
│
├── services/
│   ├── sensor.py              Runs sensor script, parses nested JSON output
│   └── trigger.py             Retired — previously SIGUSR1 daemon trigger
│
├── static/
│   └── dashboard.html         Real-time Alpine.js dashboard (served at /)
│
├── tests/                     103 tests across 7 modules
│
├── main.py                    Entrypoint — ingest loop + Microdot server
├── setup.py                   Recovery CLI (python -m setup)
├── read-sensor.sh             Invokes sen6x_read; sensor.py captures its stdout
├── schoolair_setup.sh         Unified installer + OTA updater (pass --update for field upgrades)
├── schoolair-update           Thin OTA wrapper → /usr/local/bin/ (root-owned, sudoers-approved)
├── state.py                   Shared in-memory sensor state for the dashboard
├── pyproject.toml             Project + pytest config
└── requirements.txt           Python dependencies
```
