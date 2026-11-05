"""tests/registration_wizard/test_ble_flow.py

registration_wizard/ble_flow.py: the SchoolAir extension's payloads — the
Registration JSON the page writes, the Status JSON the device reports — and
reading nmcli's error text.
"""

import json

import pytest

import ble_flow


def test_registration_needs_a_token_and_a_name():
    reg = ble_flow.parse_registration(json.dumps({"token": " t1 ", "nickname": "Room 12"}).encode())
    assert reg == {"token": "t1", "nickname": "Room 12", "site": "", "environment": "indoor", "migrate": False,
                   "current_wifi": False, "reregister": False}
    with pytest.raises(ValueError, match="token"):
        ble_flow.parse_registration(b'{"nickname": "Room 12"}')
    with pytest.raises(ValueError, match="name"):
        ble_flow.parse_registration(b'{"token": "t1"}')


def test_registration_rejects_bad_json_and_environment():
    with pytest.raises(ValueError, match="JSON"):
        ble_flow.parse_registration(b'{"token": ')
    with pytest.raises(ValueError, match="indoor or outdoor"):
        ble_flow.parse_registration(b'{"token": "t", "nickname": "n", "environment": "roof"}')


def test_status_payload_is_compact_json_with_a_known_code():
    assert json.loads(ble_flow.status_payload("done", "ok")) == {"code": "done", "message": "ok"}
    with pytest.raises(ValueError):
        ble_flow.status_payload("nope")


@pytest.mark.parametrize("err, code", [
    ("Error: Connection activation failed: Secrets were required, but not provided.", "wifi_auth_failed"),
    ("Error: No network with SSID 'SchoolNet' found.", "wifi_ap_not_found"),
    ("Error: Connection activation failed: IP configuration could not be reserved", "wifi_failed"),
])
def test_wifi_error_code(err, code):
    assert ble_flow.wifi_error_code(err) == code


def test_reconnecting_a_registered_device_needs_only_the_token():
    reg = ble_flow.parse_registration(b'{"token": "t1"}', require_nickname=False)
    assert reg["token"] == "t1" and reg["nickname"] == ""
    with pytest.raises(ValueError, match="token"):
        ble_flow.parse_registration(b'{"nickname": "Room 12"}', require_nickname=False)
    with pytest.raises(ValueError, match="Asset name"):  # registering it again needs one
        ble_flow.parse_registration(b'{"token": "t1", "reregister": true}', require_nickname=False)


def test_registration_flags_must_be_true_not_truthy():
    reg = ble_flow.parse_registration(b'{"token": "t", "nickname": "n", "current_wifi": "no", "reregister": 1}')
    assert reg["current_wifi"] is False and reg["reregister"] is False
    reg = ble_flow.parse_registration(b'{"token": "t", "nickname": "n", "current_wifi": true, "reregister": true}')
    assert reg["current_wifi"] is True and reg["reregister"] is True


@pytest.mark.parametrize("reading, model", [
    ({"sen6x": {"co2": 512, "pm25": 1.0}}, "SEN63C"),
    ({"sen6x": {"co2": None, "temp": 20}}, "SEN63C"),            # warming up: the key is there
    ({"sen6x": {"voc": 100, "nox": 1}}, "SEN65"),
    ({"sen6x": {"co2": 512}, "mgs_v2": {"voc": 155}}, "SEN63C"),
    ({"mgs_v2": {"voc": 155}}, None),                            # an MGS-V2's VOC is not a SEN65
    (None, None),                                                # latest.json after a failed read
])
def test_sensor_model(reading, model):
    assert ble_flow.sensor_model(reading) == model


def test_idle_status_can_say_the_device_is_registered():
    payload = json.loads(ble_flow.status_payload("idle", "", registered=True, nickname="Room 12"))
    assert payload == {"code": "idle", "message": "", "registered": True, "nickname": "Room 12"}
    assert ble_flow.LED_STATES["locked"] == "error"
