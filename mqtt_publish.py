#!/usr/bin/env python3
"""mqtt_publish.py: send every sensor sample to an MQTT broker.

So that anyone can open an MQTT app (MQTT Explorer, IoT MQTT Panel, Home
Assistant, Node-RED) and graph a unit's air within minutes. Spec:
docs/mqtt.md in the schoolair repo.

How it works
------------
The ingest service (main.py) writes every sample, about every 15 s, to
/run/schoolair/latest.json (see state.py). This service watches that file and
publishes each new sample. It is a separate process on purpose: the upload to
the SchoolAir server never waits for MQTT, and if the broker is down the unit
carries on as before.

Topics, under MQTT_TOPIC_PREFIX (default schoolair/v1/units) and the unit ID:

    .../{unit}/co2          905          one plain number per topic: what MQTT
    .../{unit}/pm25         3.1          apps graph with one click
    .../{unit}/temperature  21.4         (also pm1, pm4, pm10, humidity,
                                          voc, nox: whatever the sensor has)
    .../{unit}/readings     {"v": 1, "t": "...", "co2": 905, ...}   for scripts
    .../{unit}/online       true / false (false is sent by the broker when the
                                          unit vanishes: MQTT's "last will")
    .../{unit}/info         {"v": 1, "kind": "flower", "sensor": "indoor", ...}

All are retained: the broker keeps the last value of each topic and hands it
to a new subscriber at once, so an app shows a value as soon as it connects.

The unit ID is 10 characters whose first 5 are the sticker ID (see
public_id()), so a teacher finds their unit by its sticker.

Settings, in the unit's .env (never in this repo):

    MQTT_ENABLED=1                 off unless set
    MQTT_HOST=cp-localmqtt.com     the in-house test broker (default)
    MQTT_PORT=1883
    MQTT_TLS=0                     1 = TLS with the system's certificates
    MQTT_USERNAME=...              optional: no username = no login, as
    MQTT_PASSWORD=...                the test broker allows
    MQTT_TOPIC_PREFIX=schoolair/v1/units
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import device_identity  # noqa: E402

ENV_PATH = Path(os.getenv("SCHOOLAIR_ENV", str(HERE / ".env")))
LATEST_FILE = os.getenv("LATEST_READING_FILE", "/run/schoolair/latest.json")
FLOWER_MARKER = os.getenv("FLOWER_MARKER", "/etc/schoolair-flower-fitted")
UNIT_TYPE_FILE = os.getenv("UNIT_TYPE_FILE", "/etc/schoolair-unit-type")
INGEST_PY = HERE / "jobs" / "ingest.py"      # holds VERSION

POLL_S = 1.0          # how often latest.json is checked; samples come every ~15 s
KEEPALIVE_S = 60      # the broker marks the unit offline about 1.5 x this after it vanishes

# The sensor's field names -> topic names. Careful: Sensirion's "pm10" is
# PM1.0 and "pm100" is PM10, so they are renamed here, or a graph labelled
# "pm10" would show the wrong particles.
METRICS = {
    "co2":         "co2",          # ppm, indoor (SEN63C)
    "pm10":        "pm1",          # PM1.0, µg/m³
    "pm25":        "pm25",         # PM2.5, µg/m³
    "pm40":        "pm4",          # PM4.0, µg/m³
    "pm100":       "pm10",         # PM10, µg/m³
    "temp":        "temperature",  # °C, after the unit's temperature offset
    "humidity":    "humidity",     # % RH
    "voc":         "voc",          # Sensirion index, outdoor (SEN65)
    "nox":         "nox",          # Sensirion index, outdoor (SEN65)
}

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base32, as the sticker


# ── Pure helpers (tested on a laptop) ────────────────────────────────────────

def public_id(cpu_serial: str) -> str:
    """10-character unit ID for the topics: 50 bits of SHA-256 of the CPU
    serial. Its first 5 characters are the sticker ID (device_identity.short_id
    takes the first 25 of the same bits), so the sticker finds the unit, while
    10 characters keep IDs apart across thousands of units."""
    serial = cpu_serial.strip().lower()
    value = int.from_bytes(hashlib.sha256(serial.encode()).digest()[:7], "big") >> 6
    return "".join(_ALPHABET[(value >> (5 * i)) & 31] for i in reversed(range(10)))


def read_env(path: Path) -> dict:
    """KEY=value lines of a .env file; a missing file is empty."""
    env = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def load_config(env: dict) -> dict:
    """The MQTT settings from .env values, with the in-house test broker's
    defaults (cp-localmqtt.com, plain MQTT, no login; Martin, 2026-11-26)."""
    def on(key, default="0"):
        return env.get(key, default).strip().lower() in ("1", "true", "yes", "on")
    return {
        "enabled":  on("MQTT_ENABLED"),
        "host":     env.get("MQTT_HOST", "cp-localmqtt.com"),
        "port":     int(env.get("MQTT_PORT", "1883")),
        "tls":      on("MQTT_TLS", "0"),
        "username": env.get("MQTT_USERNAME", ""),
        "password": env.get("MQTT_PASSWORD", ""),
        "prefix":   env.get("MQTT_TOPIC_PREFIX", "schoolair/v1/units").rstrip("/"),
    }


def _number(value):
    """A finite number, or None (missing, null, or not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def messages_for(doc: dict, base: str) -> list:
    """The (topic, payload) pairs for one latest.json document.

    A good sample gives one plain-number topic per metric the sensor has,
    plus the JSON readings topic. A failed read gives only
    {"v": 1, "t": ..., "error": "sensor"} on readings: the number topics keep
    their last value and a graph shows a gap, never a fake value."""
    t = doc.get("recorded_at")
    data = doc.get("data")
    if not isinstance(data, dict):
        return [(f"{base}/readings", json.dumps({"v": 1, "t": t, "error": "sensor"}))]

    values = {}
    for sensor in data.values():                 # {"sen6x": {...}, "mgs_v2": {...}}
        if not isinstance(sensor, dict):
            continue
        for field, name in METRICS.items():
            v = _number(sensor.get(field))
            if v is not None and name not in values:
                values[name] = round(v, 2)
        no2 = _number(sensor.get("no2"))         # optional extra sensor
        if no2 is not None and "no2" not in values:
            values["no2"] = round(no2, 2)

    out = [(f"{base}/{name}", json.dumps(v)) for name, v in values.items()]
    reading = {"v": 1, "t": t, **values}
    if doc.get("mode") and doc["mode"] != "normal":
        reading["mode"] = doc["mode"]            # "demo" or "incident": faster samples
    out.append((f"{base}/readings", json.dumps(reading)))
    return out


def firmware_version(path: Path = INGEST_PY) -> str:
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', path.read_text(), re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def unit_info(sticker: str) -> dict:
    try:
        sensor = Path(UNIT_TYPE_FILE).read_text().strip() or "unknown"
    except OSError:
        sensor = "unknown"
    return {
        "v": 1,
        "kind": "flower" if os.path.exists(FLOWER_MARKER) else "box",
        "sensor": sensor,                        # indoor (SEN63C) / outdoor (SEN65)
        "firmware": firmware_version(),
        "sticker": sticker,
    }


def read_latest(path: str = LATEST_FILE):
    """latest.json as a dict, or None if it is missing or half-written."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ── The service ──────────────────────────────────────────────────────────────

def main() -> int:
    cfg = load_config({**read_env(ENV_PATH), **{k: v for k, v in os.environ.items() if k.startswith("MQTT_")}})
    if not cfg["enabled"]:
        print("[mqtt] MQTT_ENABLED is not set in .env: not publishing")
        return 0

    import paho.mqtt.client as mqtt               # on the Pi only; tests don't need it

    serial = device_identity.read_cpu_serial()
    unit = public_id(serial)
    base = f"{cfg['prefix']}/{unit}"
    login = f"as {cfg['username']}" if cfg["username"] else "without a login"
    print(f"[mqtt] unit {unit} (sticker {unit[:5]}) -> {cfg['host']}:{cfg['port']} "
          f"{'TLS' if cfg['tls'] else 'plain'}, {login}, topics {base}/...")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"schoolair-{unit}")
    if cfg["username"]:
        client.username_pw_set(cfg["username"], cfg["password"] or None)
    if cfg["tls"]:
        client.tls_set()                          # the system's CA certificates
    client.will_set(f"{base}/online", "false", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=5, max_delay=300)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print(f"[mqtt] broker refused the connection: {reason_code}")
            return
        print("[mqtt] connected")
        c.publish(f"{base}/online", "true", qos=1, retain=True)
        c.publish(f"{base}/info", json.dumps(unit_info(unit[:5])), qos=1, retain=True)

    def on_disconnect(c, userdata, flags, reason_code, properties):
        print(f"[mqtt] disconnected ({reason_code}); reconnecting by itself")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.connect_async(cfg["host"], cfg["port"], keepalive=KEEPALIVE_S)
    client.loop_start()                           # network in a background thread

    last_seen = None
    while True:
        doc = read_latest()
        key = None if doc is None else (doc.get("recorded_at"), doc.get("error"))
        # Only a sample actually handed to the broker counts as sent, so the
        # current one goes out as soon as the (re)connection is up. Samples
        # taken while offline are not queued: this is a live feed, and the
        # server keeps the history.
        if doc is not None and key != last_seen and client.is_connected():
            for topic, payload in messages_for(doc, base):
                client.publish(topic, payload, qos=0, retain=True)
            last_seen = key
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
