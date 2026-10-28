"""tests/registration_wizard/test_ble_provision.py

registration_wizard/ble_provision.py's Provisioner: what a registration
attempt over Bluetooth does, with BlueZ and the wizard's network/cloud
helpers mocked out. Needs dbus-next (the Pi's system python has it; skipped
where it isn't installed).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("dbus_next")

import ble_provision  # noqa: E402
import improv         # noqa: E402

REG = {"token": "tok-1", "nickname": "Room 12", "site": "Main", "environment": "indoor"}


def _status(p) -> dict:
    return json.loads(p.status_char._value)


@pytest.fixture
def backend(monkeypatch, tmp_path):
    """The wizard helpers ble_provision calls, all succeeding unless a test says otherwise."""
    b = ble_provision.backend
    nmcli = {"up": (0, "", "")}

    async def cmd(command: str):
        if "nmcli con up" in command:
            return nmcli["up"]
        return 0, "", ""

    mocks = {
        "_cmd":                  AsyncMock(side_effect=cmd),
        "_ap_is_active":         AsyncMock(return_value=True),
        "_setup_client_profile": AsyncMock(return_value=(True, "ok")),
        "_wait_for_ip":          AsyncMock(return_value=True),
        "_validate_token":       AsyncMock(return_value=(True, "")),
        "_post_heartbeat":       AsyncMock(return_value=(True, "Registered successfully", "device-secret")),
        "_commit_wifi_profile":  AsyncMock(return_value="schoolair-SchoolNet"),
        "_revert_to_ap":         AsyncMock(),
        "_delayed_shutdown":     AsyncMock(),
        "_scan_networks":        AsyncMock(return_value=[{"ssid": "SchoolNet", "signal": 80, "secured": True}]),
        "write_status":          MagicMock(),
        "_write_auth_token":     MagicMock(),
        "_write_new_auth_token": MagicMock(),
        "_bind_identity":        MagicMock(),
        "_set":                  MagicMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(b, name, mock)
    monkeypatch.setattr(b, "WIZARD_BUSY_FILE", str(tmp_path / "busy"))
    monkeypatch.setattr(b, "STAGING_FILE", str(tmp_path / "staging.json"))
    monkeypatch.setattr(ble_provision.Provisioner, "_internet_ok", AsyncMock(return_value=True))
    mocks["nmcli"] = nmcli
    return mocks


@pytest.fixture
def prov(monkeypatch):
    monkeypatch.setattr(ble_provision.asyncio, "sleep", AsyncMock())
    p = ble_provision.Provisioner(bus=None)
    p._adv_mgr = AsyncMock()
    p.adapter = AsyncMock()
    p._bluez_interface = AsyncMock(return_value=p.adapter)
    return p


def _wifi(ssid="SchoolNet", password="secret"):
    s, pw = ssid.encode(), password.encode()
    return bytes([len(s)]) + s + bytes([len(pw)]) + pw


async def test_registers_and_saves_like_the_wizard(backend, prov):
    prov._on_registration_write(json.dumps(REG).encode(), 0, "/org/bluez/hci0/dev_AA")
    assert _status(prov)["code"] == "registration_received"

    await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())

    assert _status(prov)["code"] == "done"
    assert prov.state_char._value == bytes([improv.STATE_PROVISIONED])
    payload = backend["_post_heartbeat"].call_args.args[0]
    assert payload["token"] == "tok-1"
    assert payload["new_asset"] == {"nickname": "Room 12", "type": "indoor", "site_name": "Main"}
    backend["_write_new_auth_token"].assert_called_once_with("device-secret")
    backend["_bind_identity"].assert_called_once()
    backend["_delayed_shutdown"].assert_awaited_once()
    backend["_revert_to_ap"].assert_not_awaited()
    prov.adapter.call_remove_device.assert_awaited_once_with("/org/bluez/hci0/dev_AA")  # pairing dropped
    assert prov.done.is_set()


async def test_wrong_password_reports_it_and_restores_the_hotspot(backend, prov):
    backend["nmcli"]["up"] = (4, "", "Error: Connection activation failed: Secrets were required, but not provided.")
    prov._on_registration_write(json.dumps(REG).encode(), 0, "")

    await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())

    assert _status(prov)["code"] == "wifi_auth_failed"
    assert prov.error_char._value == bytes([improv.ERROR_UNABLE_TO_CONNECT])
    assert prov.state_char._value == bytes([improv.STATE_AUTHORIZED])
    backend["_revert_to_ap"].assert_awaited_once()
    backend["_post_heartbeat"].assert_not_awaited()


async def test_rejected_registration_keeps_the_session_for_a_retry(backend, prov):
    backend["_post_heartbeat"].return_value = (False, "Token rejected by SchoolAir Cloud", "")
    prov._on_registration_write(json.dumps(REG).encode(), 0, "")

    await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())

    assert _status(prov)["code"] == "registration_rejected"
    assert prov.state_char._value == bytes([improv.STATE_AUTHORIZED])
    backend["write_status"].assert_not_called()

    # Same connection, next token: works without reconnecting Bluetooth.
    backend["_post_heartbeat"].return_value = (True, "Registered successfully", "device-secret")
    prov._on_registration_write(json.dumps({**REG, "token": "tok-2"}).encode(), 0, "")
    await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())
    assert _status(prov)["code"] == "done"


async def test_no_internet_is_its_own_error(backend, prov, monkeypatch):
    monkeypatch.setattr(ble_provision.Provisioner, "_internet_ok", AsyncMock(return_value=False))
    prov._on_registration_write(json.dumps(REG).encode(), 0, "")

    await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())

    assert _status(prov)["code"] == "no_internet"
    backend["_validate_token"].assert_not_awaited()


async def test_wifi_settings_before_registration_are_refused(backend, prov):
    await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())

    assert _status(prov)["code"] == "missing_registration"
    backend["_setup_client_profile"].assert_not_awaited()


async def test_registration_arriving_in_chunks_is_reassembled(backend, prov):
    raw = json.dumps({**REG, "nickname": "A rather long classroom name, second floor"}).encode()
    for offset in range(0, len(raw), 20):
        prov._on_registration_write(raw[offset:offset + 20], offset, "")
    assert prov.registration["nickname"] == "A rather long classroom name, second floor"


async def test_scan_and_device_info_results(backend, prov):
    await prov._handle(improv.CMD_WIFI_SCAN, b"")
    assert b"SchoolNet" in prov.result_char._value

    await prov._handle(improv.CMD_DEVICE_INFO, b"")
    assert ble_provision.DEVICE_NAME.encode() in prov.result_char._value


async def test_identify_is_not_offered(backend, prov):
    await prov._handle(improv.CMD_IDENTIFY, b"")
    assert prov.error_char._value == bytes([improv.ERROR_UNKNOWN_RPC])
    assert not prov.caps_char._value[0] & improv.CAP_IDENTIFY
