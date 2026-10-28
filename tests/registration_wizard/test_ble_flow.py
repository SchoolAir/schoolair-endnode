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
    assert reg == {"token": "t1", "nickname": "Room 12", "site": "", "environment": "indoor", "migrate": False}
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
