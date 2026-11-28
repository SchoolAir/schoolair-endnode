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

Every minute, how the unit is doing:

    .../{unit}/wifi_ssid      CP-IOT     the Wi-Fi network it is connected to
    .../{unit}/wifi_rssi      -48        Wi-Fi signal, dBm (above -67 is good)
    .../{unit}/uptime         86400      seconds since the Pi started
    .../{unit}/flower_height  64.1       flower units: the stem's height, mm
    .../{unit}/flower_state   known      flower units: known / unknown (homes next)
    .../{unit}/flower_activity homing    flower units: what it is doing right now (homing,
                                         nod, status nod, shiver, bounce, moving); empty
                                         while it stands still. Sent as soon as it changes.

And, for Home Assistant, a discovery message per value under
homeassistant/sensor/schoolair_{unit}/.../config, so the unit appears there
by itself with names and units (MQTT_HA_DISCOVERY=0 turns that off).

All are retained: the broker keeps the last value of each topic and hands it
to a new subscriber at once, so an app shows a value as soon as it connects.

The unit ID is 10 characters whose first 5 are the sticker ID (see
public_id()), so a teacher finds their unit by its sticker.

Settings, in the unit's .env (never in this repo):

    MQTT_ENABLED=0                 on unless set to 0 (Martin, 2026-11-27)
    MQTT_HOST=cp-localmqtt.com     the in-house test broker (default; see
                                   private_broker_address() for its guard)
    MQTT_PORT=1883
    MQTT_TLS=0                     1 = TLS with the system's certificates
    MQTT_USERNAME=...              optional: no username = no login, as
    MQTT_PASSWORD=...                the test broker allows
    MQTT_TOPIC_PREFIX=schoolair/v1/units
    MQTT_HA_DISCOVERY=1            Home Assistant discovery messages (on)
    MQTT_HA_PREFIX=homeassistant
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
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
FLOWER_CAL = os.getenv("FLOWER_CALIBRATION", "/home/admin/flower/calibration.json")
PROC_WIRELESS = os.getenv("PROC_WIRELESS", "/proc/net/wireless")
PROC_UPTIME = os.getenv("PROC_UPTIME", "/proc/uptime")
DEFAULT_HOST = "cp-localmqtt.com"
INGEST_PY = HERE / "jobs" / "ingest.py"      # holds VERSION

POLL_S = 1.0          # how often latest.json is checked; samples come every ~15 s
KEEPALIVE_S = 60      # the broker marks the unit offline about 1.5 x this after it vanishes
STATUS_EVERY_S = 60   # Wi-Fi, uptime and flower topics
RESOLVE_RETRY_S = 300 # the default broker not found (not on the CP network): look again

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
    defaults (cp-localmqtt.com, plain MQTT, no login; Martin, 2026-11-26).
    On unless MQTT_ENABLED=0 (Martin, 2026-11-27: "standard and on unless
    turned off")."""
    def on(key, default="0"):
        return env.get(key, default).strip().lower() in ("1", "true", "yes", "on")
    return {
        "enabled":  on("MQTT_ENABLED", "1"),
        "host":     env.get("MQTT_HOST", DEFAULT_HOST),
        "port":     int(env.get("MQTT_PORT", "1883")),
        "tls":      on("MQTT_TLS", "0"),
        "username": env.get("MQTT_USERNAME", ""),
        "password": env.get("MQTT_PASSWORD", ""),
        "prefix":   env.get("MQTT_TOPIC_PREFIX", "schoolair/v1/units").rstrip("/"),
        "ha":       on("MQTT_HA_DISCOVERY", "1"),
        "ha_prefix": env.get("MQTT_HA_PREFIX", "homeassistant").rstrip("/"),
    }


def private_broker_address(host: str, resolve=socket.getaddrinfo):
    """The address to use for the default broker, or None.

    cp-localmqtt.com is a name only Can Picard's router knows (it answers
    192.168.6.47); on the public internet the name is not registered
    (2026-11-27), so anyone could buy it. With MQTT on by default, every unit
    would then send its readings and Wi-Fi name to them. So the default broker
    is used only where the name points into a private network; anywhere else
    the unit sends nothing. A broker set in .env (MQTT_HOST) is trusted as set."""
    try:
        infos = resolve(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr)
        if ip.is_private and not ip.is_loopback:
            return addr
    return None


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


# ── Every minute: Wi-Fi, uptime, flower ──────────────────────────────────────

def parse_wireless(text: str, iface: str = "wlan0"):
    """Signal level in dBm from /proc/net/wireless, or None."""
    for line in text.splitlines():
        if line.strip().startswith(iface + ":"):
            fields = line.split(":", 1)[1].split()
            try:
                dbm = float(fields[2].rstrip("."))
            except (IndexError, ValueError):
                return None
            return int(dbm) if -120 < dbm < 0 else None
    return None


def parse_nmcli_ssid(text: str):
    """The connected network's name from `nmcli -t -f ACTIVE,SSID dev wifi list`,
    or None. nmcli escapes a ":" inside a name as "\\:"."""
    for line in text.splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":") or None
    return None


def wifi_ssid():
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi", "list", "--rescan", "no"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_nmcli_ssid(out)


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def flower_status(path: str = FLOWER_CAL) -> dict:
    """The stem's height, whether the flower knows it, and what it is doing
    right now, from the flower service's calibration.json (rewritten on every
    move). The flower writes "activity" only while it moves; "" then means it
    stands still, and an empty retained payload clears the topic."""
    try:
        with open(path) as f:
            cal = json.load(f)
    except (OSError, ValueError):
        return {}
    state = cal.get("position_state")
    out = {"flower_state": state} if isinstance(state, str) else {}
    mm = _number(cal.get("current_position_mm"))
    if state == "known" and mm is not None:
        out["flower_height"] = round(mm, 1)
    if out:
        activity = cal.get("activity")
        out["flower_activity"] = activity if isinstance(activity, str) else ""
    return out


def status_values(ssid=None, wireless_text=None, uptime_text=None, flower=None) -> dict:
    """The every-minute values; anything unreadable is left out."""
    values = {}
    if ssid:
        values["wifi_ssid"] = ssid
    rssi = parse_wireless(wireless_text if wireless_text is not None else _read(PROC_WIRELESS))
    if rssi is not None:
        values["wifi_rssi"] = rssi
    try:
        values["uptime"] = int(float((uptime_text if uptime_text is not None else _read(PROC_UPTIME)).split()[0]))
    except (IndexError, ValueError):
        pass
    values.update(flower or {})
    return values


def status_messages(values: dict, base: str) -> list:
    """Numbers as plain numbers, names (SSID, flower state) as plain text."""
    return [(f"{base}/{k}", v if isinstance(v, str) else json.dumps(v)) for k, v in values.items()]


def flower_change_messages(before: dict, after: dict, base: str) -> list:
    """Only the flower values that changed, checked every POLL_S, so a display
    shows "Rehoming" when the home starts, not up to a minute later."""
    return status_messages({k: v for k, v in after.items() if before.get(k) != v}, base)


# ── Home Assistant discovery ─────────────────────────────────────────────────

# topic name -> (label, unit, Home Assistant device class, diagnostic?)
HA_SENSORS = {
    "co2":           ("CO₂", "ppm", "carbon_dioxide", False),
    "pm1":           ("PM1", "µg/m³", "pm1", False),
    "pm25":          ("PM2.5", "µg/m³", "pm25", False),
    "pm4":           ("PM4", "µg/m³", None, False),
    "pm10":          ("PM10", "µg/m³", "pm10", False),
    "temperature":   ("Temperature", "°C", "temperature", False),
    "humidity":      ("Humidity", "%", "humidity", False),
    "voc":           ("VOC index", None, None, False),
    "nox":           ("NOx index", None, None, False),
    "no2":           ("NO₂", None, None, False),
    "flower_height": ("Flower height", "mm", "distance", False),
    "flower_state":  ("Flower position", None, None, True),
    "flower_activity": ("Flower activity", None, None, True),
    "wifi_ssid":     ("Wi-Fi network", None, None, True),
    "wifi_rssi":     ("Wi-Fi signal", "dBm", "signal_strength", True),
    "uptime":        ("Uptime", "s", "duration", True),
}
_TEXT_SENSORS = {"flower_state", "flower_activity", "wifi_ssid"}


def discovery_messages(names, unit: str, base: str, info: dict, ha_prefix: str) -> list:
    """One retained config message per value, so Home Assistant makes the
    sensors itself, grouped as one device per unit."""
    kind = info.get("kind", "box")
    device = {
        "identifiers": [f"schoolair_{unit}"],
        "name": f"SchoolAir {'Flower' if kind == 'flower' else 'sensor'} {unit[:5]}",
        "manufacturer": "SchoolAir",
        "model": f"{kind}, {info.get('sensor', 'unknown')} sensor",
        "sw_version": info.get("firmware", "unknown"),
    }
    out = []
    for name in names:
        if name not in HA_SENSORS:
            continue
        label, unit_of, device_class, diagnostic = HA_SENSORS[name]
        cfg = {
            "name": label,
            "unique_id": f"schoolair_{unit}_{name}",
            "state_topic": f"{base}/{name}",
            "availability_topic": f"{base}/online",
            "payload_available": "true",
            "payload_not_available": "false",
            "device": device,
        }
        if name not in _TEXT_SENSORS:
            cfg["state_class"] = "measurement"
        if unit_of:
            cfg["unit_of_measurement"] = unit_of
        if device_class:
            cfg["device_class"] = device_class
        if diagnostic:
            cfg["entity_category"] = "diagnostic"
        out.append((f"{ha_prefix}/sensor/schoolair_{unit}/{name}/config", json.dumps(cfg, ensure_ascii=False)))
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
        print("[mqtt] MQTT_ENABLED=0 in .env: not publishing")
        return 0

    import paho.mqtt.client as mqtt               # on the Pi only; tests don't need it

    serial = device_identity.read_cpu_serial()
    unit = public_id(serial)
    base = f"{cfg['prefix']}/{unit}"
    login = f"as {cfg['username']}" if cfg["username"] else "without a login"
    address = cfg["host"]
    if cfg["host"] == DEFAULT_HOST:
        while (address := private_broker_address(DEFAULT_HOST)) is None:
            print(f"[mqtt] {DEFAULT_HOST} is not on this network (or not a private address): "
                  f"not publishing; looking again in {RESOLVE_RETRY_S} s")
            time.sleep(RESOLVE_RETRY_S)
    print(f"[mqtt] unit {unit} (sticker {unit[:5]}) -> {cfg['host']} ({address}):{cfg['port']} "
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
        pending["announce"] = pending["status"] = True   # (re)send both from the main loop

    def on_disconnect(c, userdata, flags, reason_code, properties):
        print(f"[mqtt] disconnected ({reason_code}); reconnecting by itself")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    pending = {"announce": False, "status": False}
    client.connect_async(address, cfg["port"], keepalive=KEEPALIVE_S)
    client.loop_start()                           # network in a background thread

    last_seen, last_status, announced = None, 0.0, set()
    last_flower = None    # flower values last sent: a change goes out at once (a home lasts ~1.5 min)

    def announce(names):
        """Home Assistant config for values not announced since the (re)connect."""
        new = [n for n in names if n not in announced]
        if cfg["ha"] and new:
            for topic, payload in discovery_messages(new, unit, base, unit_info(unit[:5]), cfg["ha_prefix"]):
                client.publish(topic, payload, qos=1, retain=True)
        announced.update(new)

    while True:
        if pending["announce"]:
            pending["announce"] = False
            announced.clear()
        connected = client.is_connected()
        if connected and (pending["status"] or time.monotonic() - last_status >= STATUS_EVERY_S):
            pending["status"] = False
            flower = flower_status() if os.path.exists(FLOWER_MARKER) else {}
            values = status_values(ssid=wifi_ssid(), flower=flower)
            for topic, payload in status_messages(values, base):
                client.publish(topic, payload, qos=0, retain=True)
            announce(values)
            last_status, last_flower = time.monotonic(), flower
        elif connected and os.path.exists(FLOWER_MARKER):
            flower = flower_status()
            if flower and flower != last_flower:
                for topic, payload in flower_change_messages(last_flower or {}, flower, base):
                    client.publish(topic, payload, qos=0, retain=True)
                announce(flower)
                last_flower = flower
        doc = read_latest()
        key = None if doc is None else (doc.get("recorded_at"), doc.get("error"))
        # Only a sample actually handed to the broker counts as sent, so the
        # current one goes out as soon as the (re)connection is up. Samples
        # taken while offline are not queued: this is a live feed, and the
        # server keeps the history.
        if doc is not None and key != last_seen and connected:
            messages = messages_for(doc, base)
            for topic, payload in messages:
                client.publish(topic, payload, qos=0, retain=True)
            announce(t.rsplit("/", 1)[1] for t, _ in messages)
            last_seen = key
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
