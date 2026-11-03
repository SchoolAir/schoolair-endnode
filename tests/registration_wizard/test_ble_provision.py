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


# ── Reconnecting a registered device that lost its network ────────────────────

@pytest.fixture
def reconnect_prov(monkeypatch):
    monkeypatch.setattr(ble_provision.asyncio, "sleep", AsyncMock())
    p = ble_provision.Provisioner(bus=None, registered_as="Room 12")
    p._adv_mgr = AsyncMock()
    p.adapter = AsyncMock()
    p._bluez_interface = AsyncMock(return_value=p.adapter)
    return p


async def test_registered_device_says_so_and_only_reconnects(backend, reconnect_prov):
    p = reconnect_prov
    assert _status(p) == {"code": "idle", "registered": True, "nickname": "Room 12",
                          "message": 'Registered as "Room 12"; not connected to a network'}

    p._on_registration_write(b'{"token": "tok-1"}', 0, "/org/bluez/hci0/dev_AA")  # no name needed
    assert _status(p)["code"] == "registration_received"
    await p._handle(improv.CMD_WIFI_SETTINGS, _wifi("NewNet"))

    assert _status(p) == {"code": "done", "message": 'Back online on "NewNet"'}
    backend["_validate_token"].assert_awaited_once_with("tok-1")   # checked…
    backend["_post_heartbeat"].assert_not_awaited()                 # …but not used up
    backend["write_status"].assert_not_called()
    backend["_write_auth_token"].assert_not_called()
    backend["_commit_wifi_profile"].assert_awaited_once_with("NewNet")
    backend["_delayed_shutdown"].assert_awaited_once()


async def test_reconnect_needs_a_valid_token(backend, reconnect_prov):
    backend["_validate_token"].return_value = (False, "Token rejected by SchoolAir Cloud")
    reconnect_prov._on_registration_write(b'{"token": "stolen"}', 0, "")
    await reconnect_prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())

    assert _status(reconnect_prov)["code"] == "registration_rejected"
    backend["_commit_wifi_profile"].assert_not_awaited()
    backend["_revert_to_ap"].assert_awaited_once()


async def test_unregistered_idle_status_says_not_registered(prov):
    assert _status(prov)["registered"] is False


@pytest.mark.parametrize("has_token,mismatch,ap_up,expected", [
    (False, False, False, (True, None)),       # unregistered: register
    (True,  True,  False, (True, None)),       # card from another Pi: register
    (True,  False, False, (False, None)),      # registered and online: stay quiet
    (True,  False, True,  (True, "Room 12")),  # registered, network lost: reconnect only
])
async def test_when_to_advertise(monkeypatch, has_token, mismatch, ap_up, expected):
    b = ble_provision.backend
    monkeypatch.setattr(b, "_has_token", lambda: has_token)
    monkeypatch.setattr(b, "_ap_is_active", AsyncMock(return_value=ap_up))
    monkeypatch.setattr(b, "read_wizard_registration", lambda: {"asset_name": "Room 12"})
    monkeypatch.setattr(ble_provision.device_identity, "mismatch_detected", lambda: mismatch)
    assert await ble_provision.registered_as() == expected


# ── Guards ─────────────────────────────────────────────────────────────────────

async def test_password_never_reaches_status_or_logs(backend, prov, capsys):
    backend["nmcli"]["up"] = (4, "", "Error: psk 'hunter22' rejected by supplicant")
    prov._on_registration_write(json.dumps(REG).encode(), 0, "")
    await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi(password="hunter22"))

    assert "hunter22" not in _status(prov)["message"]
    assert "hunter22" not in capsys.readouterr().out


async def test_five_failures_in_a_row_lock_the_device(backend, prov, monkeypatch):
    backend["_validate_token"].return_value = (False, "Token rejected by SchoolAir Cloud")
    locked = AsyncMock()
    monkeypatch.setattr(prov, "_lock", locked)
    for _ in range(ble_provision.MAX_FAILED_ATTEMPTS - 1):
        prov._on_registration_write(json.dumps(REG).encode(), 0, "")
        await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())
    await asyncio_tick()
    locked.assert_not_awaited()

    prov._on_registration_write(b'{"nickname": "no token"}', 0, "")  # bad input counts too
    await asyncio_tick()
    locked.assert_awaited_once()


async def test_success_resets_the_failure_count(backend, prov):
    backend["_validate_token"].return_value = (False, "nope")
    for _ in range(3):
        prov._on_registration_write(json.dumps(REG).encode(), 0, "")
        await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())
    assert prov._failures == 3
    backend["_validate_token"].return_value = (True, "")
    prov._on_registration_write(json.dumps(REG).encode(), 0, "")
    await prov._handle(improv.CMD_WIFI_SETTINGS, _wifi())
    assert prov._failures == 0


async def test_lock_stops_advertising_drops_the_phone_and_refuses_writes(prov, monkeypatch):
    phone = "/org/bluez/hci0/dev_AA"
    monkeypatch.setattr(prov, "_connected_devices", AsyncMock(return_value=[phone]))
    monkeypatch.setattr(prov, "_disconnect", AsyncMock())
    seen = {}

    async def during_lockout(seconds):
        if seconds == ble_provision.LOCKOUT_S:
            seen["status"] = _status(prov)["code"]
            with pytest.raises(ble_provision.DBusError):
                prov._on_rpc_write(b"\x03\x00\x03", 0, phone)
            with pytest.raises(ble_provision.DBusError):
                prov._on_registration_write(json.dumps(REG).encode(), 0, phone)

    monkeypatch.setattr(ble_provision.asyncio, "sleep", during_lockout)
    await prov._lock()

    assert seen["status"] == "locked"
    prov._adv_mgr.call_unregister_advertisement.assert_awaited_once()
    prov._disconnect.assert_awaited_once_with(phone)
    # …and afterwards it starts over.
    prov._adv_mgr.call_register_advertisement.assert_awaited_once()
    assert _status(prov)["code"] == "idle"
    prov._on_registration_write(json.dumps(REG).encode(), 0, phone)
    assert _status(prov)["code"] == "registration_received"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def test_quiet_or_lingering_phones_are_disconnected(prov, monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(ble_provision.time, "monotonic", clock)
    flag = tmp_path / "ble-client"
    monkeypatch.setattr(ble_provision, "BLE_CLIENT_FILE", str(flag))
    phone = "/org/bluez/hci0/dev_AA"
    monkeypatch.setattr(prov, "_connected_devices", AsyncMock(return_value=[phone]))
    monkeypatch.setattr(prov, "_disconnect", AsyncMock())

    await prov._check_connections()
    assert flag.exists()                          # netwatch: hold the AP

    clock.now += ble_provision.IDLE_DISCONNECT_S - 10
    prov.status_char.on_access(phone)             # the page's keep-alive read
    clock.now += 20
    await prov._check_connections()
    prov._disconnect.assert_not_awaited()         # active, so kept

    prov._busy = True                             # never mid-attempt
    clock.now += ble_provision.IDLE_DISCONNECT_S
    await prov._check_connections()
    prov._disconnect.assert_not_awaited()

    prov._busy = False
    await prov._check_connections()
    prov._disconnect.assert_awaited_once_with(phone)


async def test_max_connection_time_even_when_active(prov, monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(ble_provision.time, "monotonic", clock)
    monkeypatch.setattr(ble_provision, "BLE_CLIENT_FILE", str(tmp_path / "flag"))
    phone = "/org/bluez/hci0/dev_AA"
    monkeypatch.setattr(prov, "_connected_devices", AsyncMock(return_value=[phone]))
    monkeypatch.setattr(prov, "_disconnect", AsyncMock())
    await prov._check_connections()
    while clock.now < 1000 + ble_provision.MAX_CONNECTION_S:
        clock.now += 30
        prov.status_char.on_access(phone)
        await prov._check_connections()
    prov._disconnect.assert_awaited_once_with(phone)


async def test_flag_file_cleared_when_the_phone_leaves(prov, monkeypatch, tmp_path):
    flag = tmp_path / "flag"
    monkeypatch.setattr(ble_provision, "BLE_CLIENT_FILE", str(flag))
    connected = AsyncMock(return_value=["/org/bluez/hci0/dev_AA"])
    monkeypatch.setattr(prov, "_connected_devices", connected)
    await prov._check_connections()
    assert flag.exists()
    connected.return_value = []
    await prov._check_connections()
    assert not flag.exists()
    assert prov._connected_since == {}


async def test_connected_devices_reads_bluez(prov):
    from dbus_next import Variant
    prov.adapter.call_get_managed_objects.return_value = {
        "/org/bluez/hci0":        {"org.bluez.Adapter1": {}},
        "/org/bluez/hci0/dev_AA": {"org.bluez.Device1": {"Connected": Variant("b", True)}},
        "/org/bluez/hci0/dev_BB": {"org.bluez.Device1": {"Connected": Variant("b", False)}},
    }
    assert await prov._connected_devices() == ["/org/bluez/hci0/dev_AA"]


async def asyncio_tick():
    """Let tasks created with create_task run (asyncio.sleep is mocked here)."""
    import asyncio
    loop = asyncio.get_running_loop()
    for _ in range(3):
        fut = loop.create_future()
        loop.call_soon(fut.set_result, None)
        await fut
