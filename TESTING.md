# SchoolAir Gateway — Field Testing Checklist

Live testing on a Raspberry Pi with a **SEN63C** sensor.

> **SEN63C note:** reports PM1, PM2.5, PM4, PM10, VOC index, NOx index,
> temperature, humidity. **No CO2.** The `co2` field will be 0 or absent from
> all readings. CO2 alert thresholds in `criteria.json` will never fire —
> that is expected behaviour on this hardware.

---

## Prerequisites

- Raspberry Pi (any model with WiFi — Zero W 2, 3B+, 4, 5)
- SEN63C wired to I2C (SDA → GPIO2, SCL → GPIO3, VDD → 3.3V, GND → GND)
- SD card (≥ 8 GB), laptop with Pi Imager and SSH client
- The Pi on the same LAN as your laptop, or USB-to-serial adapter for console

---

## Phase 1 — Fresh Install

### 1.1  Flash the SD card

- [ ] Open Raspberry Pi Imager
- [ ] OS: **Raspberry Pi OS Lite (Trixie)** — 32-bit for a Pi Zero W, 64-bit for newer boards
- [ ] In "Advanced settings" (⚙):
  - Set username: `admin`
  - Set password (note it)
  - Add your WiFi SSID + password so the Pi connects on first boot
  - Enable SSH (use password auth for now)
- [ ] Flash and insert SD into Pi

### 1.2  Boot and connect

- [ ] Power the Pi, wait ~60s
- [ ] Find its IP: `arp -n | grep -i "b8:27\|dc:a6\|e4:5f\|d8:3a"` or check your router
- [ ] SSH in:
  ```bash
  ssh admin@<pi-ip>
  ```

### 1.3  Run the setup script

```bash
curl -sSL https://raw.githubusercontent.com/SchoolAir/schoolair-endnode/main/schoolair_setup.sh | sudo bash
```

Expected: each step prints `✓`. The final block should read:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Setup complete — all checks passed.
```

- [ ] All verification checks pass (no `⚠` warnings)
- [ ] Note the hostname assigned (e.g. `schoolair-260622-3a1f`)

> If any check fails, scroll up to the first `⚠` line — the log is also at
> `/var/log/schoolair-setup.log`.

---

## Phase 2 — First Boot & Service Verification

### 2.1  Reboot

```bash
sudo reboot
```

Wait ~30s, then SSH back in. The hostname will now be the assigned one:

```bash
ssh admin@<pi-ip>
```

### 2.2  Hostname

- [ ] `hostname` returns `schoolair-YYMMDD-XXXX` (not `raspberrypi` or `schoolair-template`)

### 2.3  Services

```bash
sudo systemctl status sen6x schoolair schoolair-launcher --no-pager
```

- [ ] `sen6x.service` — **active (running)**
- [ ] `schoolair.service` — **active (running)**
- [ ] `schoolair-launcher.service` — **active (exited)** (it's a oneshot)

Check for errors:

```bash
journalctl -u schoolair-launcher -u schoolair -u sen6x -n 40 --no-pager
```

- [ ] No `ERROR` or `FATAL` lines
- [ ] `schoolair-launcher` log ends with either:
  - `Connected to '<SSID>' but no AUTH_TOKEN found — starting wizard for registration.`
  - or if WiFi failed: `Starting AP hotspot…`

---

## Phase 3 — Registration

### 3.1  Determine how the wizard is reachable

```bash
sudo systemctl status schoolair-wizard --no-pager
```

- [ ] Wizard is **active (running)**

Two cases:

**a) Pi connected to LAN (most likely — you set WiFi in Imager):**

- Open browser: `http://<pi-ip>` (port 80 via wizard)
- [ ] Registration form loads

**b) No LAN — Pi opened its own hotspot:**

- Join WiFi: `SchoolAir_Setup` (open, no password)
- Open browser: `http://192.168.4.1`
- [ ] Captive-portal prompt appears, or navigate manually

### 3.2  Complete registration

- [ ] Enter the organisation token
- [ ] Submit — success screen appears
- [ ] Wizard closes the AP (if it opened one)

### 3.3  Verify token written

```bash
grep AUTH_TOKEN /home/admin/schoolair/.env
```

- [ ] `AUTH_TOKEN=<non-empty value>`

### 3.4  Verify schoolair restarted with token

```bash
sudo systemctl status schoolair --no-pager
journalctl -u schoolair -n 20 --no-pager
```

- [ ] Service is **active (running)**
- [ ] Log contains `[ingest] starting` or similar — no auth errors

---

## Phase 4 — SEN6x sensor

### 4.1  One-shot read

```bash
/home/admin/i2c/sen6x/sen6x_read
```

- [ ] Exits 0 and prints JSON to stdout
- [ ] JSON contains `measured_at`, `temp`, `humidity`, `pm10`, `pm25`, `pm40`, `pm100`
- [ ] `co2` present (SEN63C) or `voc`/`nox` present (SEN65)
- [ ] Values are physically plausible:
  - `temp`: 15–35°C
  - `humidity`: 20–80%
  - `pm25`: < 20 µg/m³ indoors at rest
  - `co2`: 400–2000 ppm

### 4.2  sen6x.service initialisation

```bash
sudo systemctl restart sen6x
systemctl status sen6x
journalctl -u sen6x -n 20
```

- [ ] Service exits with status 0/SUCCESS
- [ ] Log shows either "sensor already running — skipping reset" (fast path) or a sequence of "waiting for first valid reading" lines ending with valid JSON (cold boot)
- [ ] `systemctl is-active sen6x` returns `active` (RemainAfterExit=yes)

### 4.3  schoolair CLI

```bash
schoolair --status
```

- [ ] Prints latest reading with labelled fields (temp, humidity, PM values, VOC, NOx)

---

## Phase 5 — Telemetry & Dashboard

### 5.1  Local dashboard

Open in browser: `http://<pi-ip>:8080`

- [ ] Dashboard loads
- [ ] Device hostname and nickname shown
- [ ] Sensor readings displayed (updated ~ every 30s via WebSocket)
- [ ] No "not registered" banner

### 5.2  Ingest logs (first drain cycle)

The read loop runs every 5 min (active hours) and the drain runs every 30 min.
To watch in real time:

```bash
journalctl -u schoolair -f
```

- [ ] `[ingest] read:` lines appear every ~5 min with current readings
- [ ] Within 30 min: `[ingest] drain: POST /node/aqc/v1/ingest → 200` (or similar success)
- [ ] After drain: `[ingest] drain: cleared N readings from buffer`

> **Shortcut for impatient testing:** temporarily reduce intervals in
> `config/settings.json` (`interval_read_active: 30`, `interval_drain_active: 60`),
> then `sudo systemctl restart schoolair`. Remember to revert after testing.

### 5.3  Verify data on server

Log in to the SchoolAir dashboard and confirm readings are appearing for this device.

- [ ] Readings visible on server within one drain cycle

---

## Phase 6 — Alert Verification

### 6.1  Check active thresholds

```bash
cat /home/admin/schoolair/config/criteria.json
```

- [ ] JSON loaded, shows thresholds for `pm25`, `temp`, etc.
- [ ] `co2` threshold present but will never fire (SEN63C) — that's fine

### 6.2  Trigger a test alert (VOC spike)

The fastest way with a SEN63C is a brief VOC spike (hand sanitiser, marker, or just
breathing directly onto the sensor for 20s). The verification routine needs to see
the metric elevated at T+10s, T+30s, T+1m, T+2m to confirm.

**Or: lower a threshold temporarily:**

```bash
# Example: lower pm25 threshold to just below current reading
# Edit config/criteria.json, change "threshold" for pm25 to e.g. "1"
nano /home/admin/schoolair/config/criteria.json
sudo systemctl restart schoolair
```

Watch logs:

```bash
journalctl -u schoolair -f
```

Expected sequence after a breach:

```
[verify/pm25] stage 1: T+10s read = X.X µg/m³
[verify/pm25] stage 1: T+30s read = X.X µg/m³  avg=X.X — near/above threshold
[verify/pm25] stage 2: T+1m  read = X.X µg/m³
[verify/pm25] stage 2: T+2m  read = X.X µg/m³  avg=X.X — persistent → POST alert
```

- [ ] Both stage logs appear
- [ ] If persistent: `POST /aqc/v1/alert → 2xx` (or buffered if server unreachable)
- [ ] After 1h cooldown, same metric can re-trigger

**Restore thresholds** after testing:

```bash
sudo systemctl restart schoolair   # re-fetches criteria on next drain
```

---

## Phase 7 — Offline Resilience

### 7.1  Disconnect from network

```bash
# Disable the active WiFi connection (replace SSID with your network name)
sudo nmcli con down "<your-ssid>"
```

### 7.2  Readings accumulate in RAM

Let two or three read cycles pass (watch the logs via UART or prior SSH session):

- [ ] `[ingest] read:` lines continue appearing
- [ ] Drain attempts log a connection error but don't crash the service
- [ ] Service keeps running — `sudo systemctl status schoolair` stays **active**

### 7.3  Reconnect and drain

```bash
sudo nmcli con up "<your-ssid>"
```

Wait for the next drain cycle:

- [ ] `[ingest] drain: POST … → 200`
- [ ] Log shows all queued readings sent in one batch

> If the buffer fills before reconnecting (> 500 readings by default), SQLite takes
> over automatically. Check with:
> `ls -lh /home/admin/schoolair/queue.db`

---

## Phase 8 — Re-registration

### 8.1  Trigger from dashboard

Open: `http://<pi-ip>:8080` → click **Re-register**

```bash
sudo systemctl status schoolair-wizard --no-pager
```

- [ ] `schoolair-wizard.service` starts (active/running)
- [ ] Browser at `http://<pi-ip>` shows the registration form again

### 8.2  Complete re-registration

- [ ] Submit org token again
- [ ] `AUTH_TOKEN` in `.env` updated (may be the same value)
- [ ] `schoolair.service` restarts automatically

---

## Phase 9 — Golden Image (when ready to mass-deploy)

Only run this when the device is fully validated and you want to create a
master image for flashing multiple units.

### 9.1  Run prepare_image.sh

```bash
bash /home/admin/schoolair/prepare_image.sh
```

- [ ] All services stopped
- [ ] `.env` reset to defaults (AUTH_TOKEN cleared)
- [ ] SQLite queue cleared
- [ ] Hostname reset to `schoolair-template`
- [ ] SSH disconnects at the end (WiFi profiles deleted — expected)

### 9.2  Power off and image

```bash
sudo shutdown -h now
```

On your laptop (replace `/dev/sdX` with your SD card device):

```bash
sudo dd if=/dev/sdX of=schoolair-golden-$(date +%Y%m%d).img bs=4M status=progress
# Optional: shrink with pishrink, then fix superblock if needed:
pishrink.sh schoolair-golden-$(date +%Y%m%d).img
bash /home/oded/projects/Alella\ Green\ Tech/SchoolAir/schoolair/gateway/patch_shrunk_img.sh \
     schoolair-golden-$(date +%Y%m%d).img
```

- [ ] Image file created
- [ ] Flash a second SD card from the image and boot it
- [ ] `first_boot.sh` assigns a new unique hostname on first boot
- [ ] Registration wizard appears (AUTH_TOKEN is blank)

---

## Quick-reference: useful commands

```bash
# Tail all SchoolAir logs live
journalctl -u sen6x -u schoolair-launcher -u schoolair-wizard -u schoolair -f

# Current sensor reading
schoolair --status

# Force an immediate sensor read
PID=$(systemctl show -p MainPID --value sen6x) && sudo kill -USR1 $PID

# Restart just the telemetry service (e.g. after editing .env or criteria.json)
sudo systemctl restart schoolair

# Check buffer / queue state (from Python)
python3 -c "
import sys; sys.path.insert(0, '/home/admin/schoolair')
from db import queue; import sqlite3
con = sqlite3.connect('/home/admin/schoolair/queue.db')
print('measurements pending:', con.execute('SELECT count(*) FROM measurements_queue WHERE status=\"pending\"').fetchone()[0])
print('alerts pending:',       con.execute('SELECT count(*) FROM alerts_queue     WHERE status=\"pending\"').fetchone()[0])
"
```
