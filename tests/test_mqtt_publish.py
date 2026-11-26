"""tests/test_mqtt_publish.py: the MQTT feed (mqtt_publish.py).

Pure parts on a laptop; one end-to-end test against a real local Mosquitto
(skipped when mosquitto isn't installed) checks the retained topics, the
password, and the "last will" that marks a vanished unit offline.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import device_identity  # noqa: E402
import mqtt_publish as mp  # noqa: E402

GOOD = {
    "data": {"sen6x": {"co2": 905, "pm10": 1.04, "pm25": 3.1, "pm40": 3.9, "pm100": 4.0,
                       "temp": 21.437, "humidity": 48.2}},
    "recorded_at": "2026-11-26T22:41:00+00:00",
}


# ── unit ID ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("serial", ["00000000a1b2c3d4", "10000000deadbeef", "0000000012345678"])
def test_public_id_starts_with_the_sticker_id(serial):
    pid = mp.public_id(serial)
    assert len(pid) == 10
    assert pid[:5] == device_identity.short_id(serial)


def test_public_id_uses_the_sticker_alphabet():
    assert set(mp.public_id("00000000a1b2c3d4")) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_public_ids_differ_between_units():
    ids = {mp.public_id(f"{i:016x}") for i in range(2000)}
    assert len(ids) == 2000


# ── settings ─────────────────────────────────────────────────────────────────

def test_off_by_default():
    assert mp.load_config({})["enabled"] is False


def test_config_from_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text('AUTH_TOKEN=x\nMQTT_ENABLED=1\nMQTT_USERNAME="unit"\nMQTT_PASSWORD=p=w\n'
                   "# MQTT_PORT=9\nMQTT_TOPIC_PREFIX=test/units/\n")
    cfg = mp.load_config(mp.read_env(env))
    assert cfg["enabled"] and not cfg["tls"]
    assert (cfg["host"], cfg["port"]) == ("cp-localmqtt.com", 1883)     # the commented port is ignored
    assert (cfg["username"], cfg["password"]) == ("unit", "p=w")        # "=" inside a password survives
    assert cfg["prefix"] == "test/units"


def test_missing_env_file_is_just_off(tmp_path):
    assert mp.load_config(mp.read_env(tmp_path / "nope"))["enabled"] is False


def test_main_does_nothing_when_off(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mp, "ENV_PATH", tmp_path / ".env")
    for k in list(os.environ):
        if k.startswith("MQTT_"):
            monkeypatch.delenv(k)
    assert mp.main() == 0
    assert "not publishing" in capsys.readouterr().out


def test_tls_can_be_switched_on():
    assert mp.load_config({"MQTT_TLS": "1"})["tls"] is True


# ── messages ─────────────────────────────────────────────────────────────────

def test_one_plain_number_per_topic():
    msgs = dict(mp.messages_for(GOOD, "p/U"))
    assert msgs["p/U/co2"] == "905"
    assert msgs["p/U/pm25"] == "3.1"
    assert msgs["p/U/temperature"] == "21.44"
    assert msgs["p/U/humidity"] == "48.2"


def test_sensirion_pm_names_are_translated():
    # Sensirion's pm10 is PM1.0 and its pm100 is PM10.
    msgs = dict(mp.messages_for(GOOD, "p/U"))
    assert msgs["p/U/pm1"] == "1.04"
    assert msgs["p/U/pm4"] == "3.9"
    assert msgs["p/U/pm10"] == "4.0"


def test_readings_json_carries_everything():
    r = json.loads(dict(mp.messages_for(GOOD, "p/U"))["p/U/readings"])
    assert r["v"] == 1 and r["t"] == GOOD["recorded_at"]
    assert r["co2"] == 905 and r["pm10"] == 4.0 and r["pm1"] == 1.04
    assert "mode" not in r


def test_null_values_are_left_out_not_faked():
    doc = {"data": {"sen6x": {"co2": None, "pm25": 2.0, "temp": float("nan")}}, "recorded_at": "t"}
    msgs = dict(mp.messages_for(doc, "p/U"))
    assert "p/U/co2" not in msgs and "p/U/temperature" not in msgs
    assert msgs["p/U/pm25"] == "2.0"


def test_failed_read_sends_only_an_error():
    doc = {"data": None, "recorded_at": "t", "error": "Sensor script failed: x"}
    assert mp.messages_for(doc, "p/U") == [("p/U/readings", json.dumps({"v": 1, "t": "t", "error": "sensor"}))]


def test_outdoor_unit_sends_voc_and_nox():
    doc = {"data": {"sen6x": {"voc": 102, "nox": 1, "pm25": 3}}, "recorded_at": "t"}
    msgs = dict(mp.messages_for(doc, "p/U"))
    assert msgs["p/U/voc"] == "102" and msgs["p/U/nox"] == "1" and "p/U/co2" not in msgs


def test_demo_mode_is_marked():
    r = json.loads(dict(mp.messages_for({**GOOD, "mode": "demo"}, "p/U"))["p/U/readings"])
    assert r["mode"] == "demo"


def test_info_says_flower_or_box(tmp_path, monkeypatch):
    marker, utype = tmp_path / "flower", tmp_path / "type"
    utype.write_text("indoor\n")
    monkeypatch.setattr(mp, "FLOWER_MARKER", str(marker))
    monkeypatch.setattr(mp, "UNIT_TYPE_FILE", str(utype))
    assert mp.unit_info("ABCDE")["kind"] == "box"
    marker.touch()
    info = mp.unit_info("ABCDE")
    assert info["kind"] == "flower" and info["sensor"] == "indoor" and info["sticker"] == "ABCDE"
    assert info["firmware"] == mp.firmware_version() != "unknown"


def test_read_latest_tolerates_missing_and_broken_files(tmp_path):
    assert mp.read_latest(str(tmp_path / "none")) is None
    (tmp_path / "bad").write_text("{half")
    assert mp.read_latest(str(tmp_path / "bad")) is None


# ── service file ─────────────────────────────────────────────────────────────

def test_service_runs_the_publisher_as_admin_from_the_venv():
    unit = (ROOT / "deploy" / "schoolair-mqtt.service").read_text()
    assert "User=admin" in unit
    assert "ExecStart=/home/admin/schoolair/.venv/bin/python -u /home/admin/schoolair/mqtt_publish.py" in unit
    assert "Restart=on-failure" in unit      # exit 0 when off must not loop


def test_paho_is_pinned():
    assert "paho-mqtt==2.1.0" in (ROOT / "requirements.txt").read_text().splitlines()


def test_no_real_credentials_in_env_example():
    text = (ROOT / ".env.example").read_text()
    assert "# MQTT_PASSWORD=<unit password>" in text


# ── end to end, against a real local broker ──────────────────────────────────

MOSQUITTO = shutil.which("mosquitto") or "/opt/homebrew/sbin/mosquitto"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(params=["password", "anonymous"])
def broker(request, tmp_path):
    paho = pytest.importorskip("paho.mqtt.client")
    if not os.path.exists(MOSQUITTO):
        pytest.skip("mosquitto not installed")
    passwd = shutil.which("mosquitto_passwd") or str(Path(MOSQUITTO).parent.parent / "bin" / "mosquitto_passwd")
    pwfile = tmp_path / "pw"
    subprocess.run([passwd, "-c", "-b", str(pwfile), "unit", "unitpw"], check=True, capture_output=True)
    subprocess.run([passwd, "-b", str(pwfile), "viewer", "viewpw"], check=True, capture_output=True)
    port = _free_port()
    conf = tmp_path / "m.conf"
    if request.param == "password":
        conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous false\npassword_file {pwfile}\n")
    else:                                   # like the in-house test broker, cp-localmqtt.com
        conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
    proc = subprocess.Popen([MOSQUITTO, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.1)
    yield paho, port, request.param
    proc.terminate()
    proc.wait(5)


def _subscribe(paho, port, user, pw, topic, seconds=3.0):
    got, codes = {}, []
    c = paho.Client(paho.CallbackAPIVersion.VERSION2)
    c.username_pw_set(user, pw)
    c.on_connect = lambda cl, u, f, rc, p: (codes.append(rc), cl.subscribe(topic))
    c.on_message = lambda cl, u, m: got.__setitem__(m.topic, m.payload.decode())
    c.connect("127.0.0.1", port)
    c.loop_start()
    time.sleep(seconds)
    c.loop_stop()
    c.disconnect()
    return got, codes


def test_end_to_end_retained_topics_password_and_last_will(broker, tmp_path, monkeypatch):
    paho, port, mode = broker
    login = "MQTT_USERNAME=unit\nMQTT_PASSWORD=unitpw\n" if mode == "password" else ""
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps(GOOD))
    env = tmp_path / ".env"
    env.write_text(f"MQTT_ENABLED=1\nMQTT_HOST=127.0.0.1\nMQTT_PORT={port}\nMQTT_TLS=0\n"
                   + login + "MQTT_TOPIC_PREFIX=t/units\n")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("MQTT_")}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "mqtt_publish.py")],
        env={**clean, "PYTHONUNBUFFERED": "1", "SCHOOLAIR_ENV": str(env), "LATEST_READING_FILE": str(latest),
             "FLOWER_MARKER": str(tmp_path / "no-flower"), "UNIT_TYPE_FILE": str(tmp_path / "no-type")},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(3)                                    # connect + first publish
        # A new subscriber gets every retained value at once (the "quick graph").
        got, _ = _subscribe(paho, port, "viewer", "viewpw", "t/units/#")
        unit = next(t.split("/")[2] for t in got)
        base = f"t/units/{unit}"
        assert got[f"{base}/co2"] == "905"
        assert got[f"{base}/pm10"] == "4.0"
        assert got[f"{base}/online"] == "true"
        assert json.loads(got[f"{base}/info"])["kind"] == "box"
        assert json.loads(got[f"{base}/readings"])["co2"] == 905

        # A new sample is published within a couple of seconds.
        latest.write_text(json.dumps({**GOOD, "recorded_at": "later",
                                      "data": {"sen6x": {**GOOD["data"]["sen6x"], "co2": 1200}}}))
        time.sleep(2.5)
        got, _ = _subscribe(paho, port, "viewer", "viewpw", f"{base}/co2", seconds=1.5)
        assert got[f"{base}/co2"] == "1200"
    finally:
        proc.kill()                                       # vanish without a goodbye
        out = proc.communicate(timeout=5)[0]
    assert "connected" in out, out

    # Killed without a goodbye: the broker publishes the last will at once
    # (the socket closed), so apps see the unit go offline.
    got, _ = _subscribe(paho, port, "viewer", "viewpw", f"{base}/online", seconds=1.5)
    assert got[f"{base}/online"] == "false"

    if mode == "password":
        # The password matters: a wrong one is refused.
        _, codes = _subscribe(paho, port, "viewer", "wrong", "#", seconds=1.0)
        assert codes and codes[0].is_failure


def test_setup_installs_enables_and_restarts_the_feed():
    setup = (ROOT / "schoolair_setup.sh").read_text()
    loop = next(l for l in setup.splitlines() if l.startswith("for svc in sen6x.service"))
    assert "schoolair-mqtt.service" in loop                       # installed with the others
    assert "systemctl enable schoolair-mqtt.service" in setup
    assert "systemctl restart schoolair-mqtt.service" in setup     # new code / .env on update
    assert 'chk "paho-mqtt importable"' in setup
