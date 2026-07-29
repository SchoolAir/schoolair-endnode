"""services/sensor.py

Primary sensor (SEN6x): read_sensor() executes a compiled C binary and returns
its JSON output.  The result is a nested dict keyed by sensor name, e.g.
{"sen6x": {"temp": 22.4, "co2": 512, ...}}.

Auxiliary sensors: probe_aux_sensors() runs once at service startup, scans the
I2C bus, and returns a list of sensor configs for every known sensor that is
physically present and has a driver available.  read_aux_sensor() runs one
driver per call and merges its output into the same nested-dict shape.
"""

import json
import subprocess
import os
import urllib.request
from pathlib import Path

SCRIPT = os.getenv("MOCK_SENSOR_SCRIPT", "./read-sensor.sh")

# Re-init is skipped in mock/dev mode (MOCK_SENSOR_SCRIPT set) because there
# is no real binary to call.  In production, sen6x_read --init recovers from
# sensor power-cycles, I2C lockups, and hardware swaps without a Pi reboot.
_REINIT_BIN = (
    ""
    if "MOCK_SENSOR_SCRIPT" in os.environ
    else os.getenv("SENSOR_REINIT_BIN", "/home/admin/i2c/sen6x/sen6x_read")
)
_REINIT_AFTER = 5  # consecutive failures before attempting re-init

_consecutive_failures = 0


def _try_reinit() -> None:
    print("[sensor] repeated failures — attempting re-init")
    try:
        r = subprocess.run(
            [_REINIT_BIN, "--init"],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode == 0:
            print("[sensor] re-init succeeded")
        else:
            print(f"[sensor] re-init failed (exit {r.returncode}): {r.stderr.strip()}")
    except subprocess.TimeoutExpired:
        print("[sensor] re-init timed out after 90 s")


def extract_metric(data: dict, metric: str) -> float | None:
    """Extract a named metric from a nested sensor reading.

    Searches all top-level sensor dicts (e.g. data["sen6x"]["co2"]).
    Returns the first numeric match, or None if not found in any sensor.
    """
    for sensor_data in data.values():
        if not isinstance(sensor_data, dict):
            continue
        val = sensor_data.get(metric)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def read_sensor() -> dict:
    """Execute the sensor script and return its raw nested JSON payload.

    Raises RuntimeError if the script fails or output is not valid JSON.
    After _REINIT_AFTER consecutive failures, calls sen6x_read --init to
    recover from mid-run sensor resets (power-cycle, I2C lockup, swap).
    """
    global _consecutive_failures
    error: Exception | None = None

    try:
        result = subprocess.run(
            SCRIPT,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            error = RuntimeError(f"Sensor script failed: {result.stderr.strip()}")
        else:
            data = json.loads(result.stdout)
            _consecutive_failures = 0
            return data
    except json.JSONDecodeError as e:
        error = RuntimeError(f"Sensor script returned invalid JSON: {e}")
    except subprocess.TimeoutExpired:
        error = RuntimeError("Sensor script timed out")

    _consecutive_failures += 1
    if _consecutive_failures == _REINIT_AFTER and _REINIT_BIN:
        _try_reinit()
    raise error


# ── Auxiliary sensor registry ─────────────────────────────────────────────────

_BASE_DIR  = Path(__file__).resolve().parent.parent   # ~/schoolair/
_REPO_RAW  = "https://raw.githubusercontent.com/SchoolAir/schoolair-ex-RMIT-pi/main"

# Closed list of sensors we have drivers for.
# i2c_addrs: all possible addresses for this sensor model (DIP-switch variants).
# The first address actually detected on the bus is used; it is passed to the
# driver as --addr so the driver does not need to hardcode it.
SENSOR_REGISTRY: list[dict] = [
    {
        "name":       "mgs_v2",
        "i2c_addrs":  [0x08],
        "driver":     "i2c/mgs_v2/read_mgs_v2.py",
        "repo_files": [
            "i2c/mgs_v2/read_mgs_v2.py",
            "i2c/mgs_v2/multichannel_gas_gmxxx.py",
        ],
    },
    {
        "name":       "o3",
        "i2c_addrs":  [0x70, 0x71, 0x72, 0x73],  # DIP-switch selectable
        "driver":     "i2c/o3/read_o3.py",
        "repo_files": [
            "i2c/o3/read_o3.py",
            "i2c/o3/DFRobot_Ozone.py",
        ],
    },
]


def _i2c_scan() -> set[int]:
    """Return the set of I2C addresses present on bus 1 via i2cdetect."""
    try:
        r = subprocess.run(
            ["i2cdetect", "-y", "1"],
            capture_output=True, text=True, timeout=10,
        )
        addrs: set[int] = set()
        for line in r.stdout.splitlines():
            parts = line.split(":", 1)
            if len(parts) < 2:
                continue
            for token in parts[1].split():
                if token not in ("--", "UU") and len(token) == 2:
                    try:
                        addrs.add(int(token, 16))
                    except ValueError:
                        pass
        return addrs
    except Exception as e:
        print(f"[sensors] i2cdetect failed: {e}")
        return set()


def _fetch_driver(sensor: dict) -> bool:
    """Fetch missing driver files from the GitHub repo. Returns True if all present after fetch."""
    all_ok = True
    for rel in sensor["repo_files"]:
        dest = _BASE_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            continue
        url = f"{_REPO_RAW}/{rel}"
        try:
            urllib.request.urlretrieve(url, str(dest))
            dest.chmod(0o755)
            print(f"[sensors] fetched {rel}")
        except Exception as e:
            print(f"[sensors] could not fetch {rel}: {e}")
            all_ok = False
    return all_ok


def probe_aux_sensors() -> list[dict]:
    """Scan I2C bus once and return active sensor configs.

    Called once in ingest_loop() at service startup.  The returned list is
    held in memory for the process lifetime — no per-read bus scanning.
    Each entry is a copy of the registry dict with an extra 'detected_addr'
    key set to the address that was actually found on the bus.
    """
    detected = _i2c_scan()
    if not detected:
        print("[sensors] I2C scan found no devices (bus error or no aux sensors)")
        return []

    active: list[dict] = []
    for sensor in SENSOR_REGISTRY:
        matched = next((a for a in sensor["i2c_addrs"] if a in detected), None)
        if matched is None:
            continue
        driver_path = _BASE_DIR / sensor["driver"]
        if not driver_path.exists():
            print(
                f"[sensors] {sensor['name']} at 0x{matched:02x} — driver missing, fetching…"
            )
            if not _fetch_driver(sensor):
                print(f"[sensors] {sensor['name']} skipped (driver unavailable)")
                continue
        active.append({**sensor, "detected_addr": matched})
        print(f"[sensors] {sensor['name']} active (0x{matched:02x})")

    if not active:
        print("[sensors] no auxiliary sensors detected")
    return active


def read_aux_sensor(sensor: dict) -> dict | None:
    """Execute one aux sensor driver and return its nested data dict.

    The driver is passed --addr <hex> so it targets the address actually
    detected on the bus (handles non-default DIP-switch configs).
    Returns None on any failure; the caller skips the sensor for this read.
    """
    driver = str(_BASE_DIR / sensor["driver"])
    addr   = f"0x{sensor['detected_addr']:02x}"
    try:
        r = subprocess.run(
            ["python3", driver, "--addr", addr],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            print(f"[{sensor['name']}] driver error: {r.stderr.strip()}")
            return None
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        print(f"[{sensor['name']}] driver timed out")
        return None
    except json.JSONDecodeError as e:
        print(f"[{sensor['name']}] invalid JSON from driver: {e}")
        return None
