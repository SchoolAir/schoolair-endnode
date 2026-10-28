#!/usr/bin/env python3
"""ble_provision.py — register an unregistered endnode over Bluetooth (BLE).

Runs beside the hotspot wizard (deploy/schoolair-ble.service is pulled in by
schoolair-wizard.service and stops with it). The registration web page, on
the installer's phone, connects over Web Bluetooth and drives:

  Improv Wi-Fi service (improv.py)       device info, Wi-Fi scan, Wi-Fi settings
  SchoolAir service    (ble_flow.py)     registration token + name, live status

A Send Wi-Fi settings command runs the same steps as the wizard's
run_registration() — join the network, check the token, register, save the
tokens — reusing the wizard's helpers, and reports each step on Status. On any
failure the network goes back to how it was (the hotspot, if it was up), so
the installer can fix the input and retry over the same connection. On success
the device stops advertising and hands over to the wizard's own shutdown.

Only advertises while the device needs registering: a registered device
(AUTH_TOKEN set, no identity mismatch) exits straight away.

BlueZ over D-Bus, via dbus-next. Writes to the Improv RPC Command and the
Registration characteristics need an encrypted link, so the first one makes
the phone pair ("Just Works", LE Secure Connections).
"""

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from dbus_next import BusType, DBusError, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method

import ble_flow
import improv
import wizard as backend  # the wizard's Wi-Fi, token and registration helpers
import device_identity    # importable once wizard has set up sys.path

BLUEZ        = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"
APP_PATH     = "/org/schoolair/ble"
ADV_PATH     = APP_PATH + "/advertisement"
AGENT_PATH   = APP_PATH + "/agent"

DEVICE_NAME  = f"SchoolAir-{device_identity.short_id()}"
CAPABILITIES = improv.CAP_DEVICE_INFO | improv.CAP_WIFI_SCAN  # no LED pattern for Identify yet
INTERNET_CHECK_TIMEOUT = 10  # seconds

_APP_ROOT = Path(__file__).resolve().parent.parent


def log(msg: str) -> None:
    print(f"[schoolair-ble] {msg}", flush=True)


def _firmware_version() -> str:
    """jobs/ingest.py's VERSION, read as text (importing ingest needs the telemetry venv)."""
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', (_APP_ROOT / "jobs" / "ingest.py").read_text(), re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def _hardware_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().rstrip("\x00").strip()
    except OSError:
        return "Raspberry Pi"


# ── D-Bus objects ─────────────────────────────────────────────────────────────

class GattService(ServiceInterface):
    def __init__(self, uuid: str):
        super().__init__("org.bluez.GattService1")
        self._uuid = uuid

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True


class Characteristic(ServiceInterface):
    """One GATT characteristic. `on_write(value, offset, device_path)` handles writes;
    set_value() updates what reads return and notifies subscribers."""

    def __init__(self, uuid: str, flags: "list[str]", service_path: str, on_write=None):
        super().__init__("org.bluez.GattCharacteristic1")
        self._uuid = uuid
        self._flags = flags
        self._service_path = service_path
        self._on_write = on_write
        self._value = b""
        self._notifying = False

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self._service_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self._value

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        offset = options["offset"].value if "offset" in options else 0
        return self._value[offset:]

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        if self._on_write is None:
            raise DBusError("org.bluez.Error.NotPermitted", "Read-only characteristic")
        if "prepare-authorize" in options and options["prepare-authorize"].value:
            return  # BlueZ asking whether a long write may be queued: yes
        offset = options["offset"].value if "offset" in options else 0
        device = options["device"].value if "device" in options else ""
        self._on_write(bytes(value), offset, device)

    @method()
    def StartNotify(self):
        self._notifying = True

    @method()
    def StopNotify(self):
        self._notifying = False

    def set_value(self, value: bytes) -> None:
        self._value = bytes(value)
        if self._notifying:
            self.emit_properties_changed({"Value": self._value})


class Advertisement(ServiceInterface):
    """Improv requires the service UUID and its service data in the advertisement
    itself; BlueZ moves the name to the scan response when the 31 bytes are full."""

    def __init__(self):
        super().__init__("org.bluez.LEAdvertisement1")
        self.state = improv.STATE_AUTHORIZED

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return [improv.SERVICE_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def ServiceData(self) -> "a{sv}":
        return {improv.SERVICE_DATA_UUID: Variant("ay", improv.service_data(self.state, CAPABILITIES))}

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return DEVICE_NAME

    @method()
    def Release(self):
        log("advertisement released by BlueZ")


class Agent(ServiceInterface):
    """No input, no output: pairing is "Just Works". BlueZ rarely calls these
    for that, but an agent must exist for the pairing to be accepted."""

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):
        pass

    @method()
    def RequestAuthorization(self, device: "o"):
        pass

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        pass

    @method()
    def Cancel(self):
        pass


# ── Provisioning ──────────────────────────────────────────────────────────────

class Provisioner:
    def __init__(self, bus: MessageBus):
        self.bus = bus
        self.done = asyncio.Event()
        self.registration: "dict | None" = None
        self._registration_buf = bytearray()
        self._commands = improv.CommandBuffer()
        self._busy = False
        self._last_device = ""

        improv_path = APP_PATH + "/improv"
        sa_path     = APP_PATH + "/schoolair"
        self.services = {improv_path: GattService(improv.SERVICE_UUID),
                         sa_path:     GattService(ble_flow.SCHOOLAIR_SERVICE_UUID)}
        self.state_char   = Characteristic(improv.CURRENT_STATE_UUID, ["read", "notify"], improv_path)
        self.error_char   = Characteristic(improv.ERROR_STATE_UUID,   ["read", "notify"], improv_path)
        self.rpc_char     = Characteristic(improv.RPC_COMMAND_UUID,   ["encrypt-write"],  improv_path,
                                           on_write=self._on_rpc_write)
        self.result_char  = Characteristic(improv.RPC_RESULT_UUID,    ["read", "notify"], improv_path)
        self.caps_char    = Characteristic(improv.CAPABILITIES_UUID,  ["read"],           improv_path)
        self.reg_char     = Characteristic(ble_flow.REGISTRATION_UUID, ["encrypt-write"], sa_path,
                                           on_write=self._on_registration_write)
        self.status_char  = Characteristic(ble_flow.STATUS_UUID,      ["read", "notify"], sa_path)
        self.characteristics = {
            improv_path + "/state":   self.state_char,
            improv_path + "/error":   self.error_char,
            improv_path + "/rpc":     self.rpc_char,
            improv_path + "/result":  self.result_char,
            improv_path + "/caps":    self.caps_char,
            sa_path + "/registration": self.reg_char,
            sa_path + "/status":      self.status_char,
        }
        self.advertisement = Advertisement()

        self.state_char.set_value(bytes([improv.STATE_AUTHORIZED]))
        self.error_char.set_value(bytes([improv.ERROR_NONE]))
        self.caps_char.set_value(bytes([CAPABILITIES]))
        self.status_char.set_value(ble_flow.status_payload("idle"))

    # ── BlueZ setup ──

    async def _bluez_interface(self, path: str, name: str):
        introspection = await self.bus.introspect(BLUEZ, path)
        return self.bus.get_proxy_object(BLUEZ, path, introspection).get_interface(name)

    async def start(self) -> None:
        await backend._cmd("rfkill unblock bluetooth 2>/dev/null; true")
        props = await self._bluez_interface(ADAPTER_PATH, "org.freedesktop.DBus.Properties")
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        await props.call_set("org.bluez.Adapter1", "Alias", Variant("s", DEVICE_NAME))
        await props.call_set("org.bluez.Adapter1", "Pairable", Variant("b", True))

        for path, service in self.services.items():
            self.bus.export(path, service)
        for path, char in self.characteristics.items():
            self.bus.export(path, char)
        self.bus.export(ADV_PATH, self.advertisement)
        self.bus.export(AGENT_PATH, Agent())

        await self._register_agent()

        gatt_mgr = await self._bluez_interface(ADAPTER_PATH, "org.bluez.GattManager1")
        await gatt_mgr.call_register_application(APP_PATH, {})
        self._adv_mgr = await self._bluez_interface(ADAPTER_PATH, "org.bluez.LEAdvertisingManager1")
        await self._adv_mgr.call_register_advertisement(ADV_PATH, {})
        log(f"advertising as {DEVICE_NAME}")

    async def _register_agent(self) -> None:
        agent_mgr = await self._bluez_interface("/org/bluez", "org.bluez.AgentManager1")
        await agent_mgr.call_register_agent(AGENT_PATH, "NoInputNoOutput")
        await agent_mgr.call_request_default_agent(AGENT_PATH)

    async def _readvertise(self) -> None:
        """Improv clients read the state from the advertisement too; BlueZ only
        reads its properties on registration, so re-register after a change."""
        try:
            await self._adv_mgr.call_unregister_advertisement(ADV_PATH)
            if self.advertisement.state != improv.STATE_PROVISIONED:
                await self._adv_mgr.call_register_advertisement(ADV_PATH, {})
        except DBusError as e:
            log(f"could not update advertisement: {e.text}")

    # ── State reporting ──

    def _set_state(self, state: int) -> None:
        self.state_char.set_value(bytes([state]))
        self.advertisement.state = state
        asyncio.get_running_loop().create_task(self._readvertise())

    def _set_error(self, code: int) -> None:
        self.error_char.set_value(bytes([code]))

    def _status(self, code: str, message: str = "") -> None:
        log(f"status {code}: {message}")
        self.status_char.set_value(ble_flow.status_payload(code, message))
        led = ble_flow.LED_STATES.get(code)
        if led:
            backend._set(led, message)

    # ── Writes ──

    def _on_registration_write(self, value: bytes, offset: int, device: str) -> None:
        if offset == 0:
            self._registration_buf = bytearray()
        self._registration_buf += value
        try:
            self.registration = ble_flow.parse_registration(bytes(self._registration_buf))
        except ValueError as e:
            # A long write may still be arriving; only complain once it parses as JSON.
            try:
                json.loads(self._registration_buf.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            self.registration = None
            self._status("missing_registration", str(e))
            return
        self._last_device = device or self._last_device
        self._status("registration_received", f'Ready to register "{self.registration["nickname"]}"')

    def _on_rpc_write(self, value: bytes, offset: int, device: str) -> None:
        self._last_device = device or self._last_device
        try:
            packet = self._commands.feed(value)
        except improv.ImprovError as e:
            log(f"bad RPC packet: {e}")
            self._set_error(e.code)
            return
        if packet is not None:
            asyncio.get_running_loop().create_task(self._handle(*packet))

    async def _handle(self, command: int, data: bytes) -> None:
        self._set_error(improv.ERROR_NONE)
        try:
            if command == improv.CMD_DEVICE_INFO:
                self.result_char.set_value(improv.build_result(command, [
                    "SchoolAir Endnode", _firmware_version(), _hardware_model(), DEVICE_NAME]))
            elif command == improv.CMD_WIFI_SCAN:
                self.result_char.set_value(improv.scan_result(await backend._scan_networks()))
            elif command == improv.CMD_WIFI_SETTINGS:
                ssid, password = improv.parse_wifi_settings(data)
                await self._provision(ssid, password)
            else:
                self._set_error(improv.ERROR_UNKNOWN_RPC)
        except improv.ImprovError as e:
            log(f"RPC {command:#04x} rejected: {e}")
            self._set_error(e.code)

    # ── The registration attempt ──

    async def _provision(self, ssid: str, password: str) -> None:
        reg = self.registration
        if reg is None:
            self._status("missing_registration", "Send the registration details before the Wi-Fi settings")
            self._set_error(improv.ERROR_UNKNOWN)
            return
        if self._busy or os.path.exists(backend.WIZARD_BUSY_FILE):
            self._status("busy", "A registration is already running on this device")
            self._set_error(improv.ERROR_UNKNOWN)
            return

        self._busy = True
        # Same lock the wizard holds: netwatch.sh leaves the network alone while it exists.
        try:
            with open(backend.WIZARD_BUSY_FILE, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
        ap_was_up = await backend._ap_is_active()
        self._set_state(improv.STATE_PROVISIONING)
        try:
            if await self._attempt(ssid, password, reg):
                await self._finish()
            else:
                await backend._cmd(f'nmcli con delete "{backend.TEMP_PROFILE}" 2>/dev/null; true')
                if ap_was_up:
                    await backend._revert_to_ap()
                self._set_state(improv.STATE_AUTHORIZED)
        finally:
            self._busy = False
            try:
                os.remove(backend.WIZARD_BUSY_FILE)
            except FileNotFoundError:
                pass

    async def _attempt(self, ssid: str, password: str, reg: dict) -> bool:
        """One connect → check → register pass. Returns True once registered;
        on failure sets Status and the Improv error, and returns False."""
        self._status("wifi_connecting", f'Connecting to "{ssid}"…')
        ok, msg = await backend._setup_client_profile(ssid, password)
        if not ok:
            return self._fail("wifi_failed", msg, improv.ERROR_UNABLE_TO_CONNECT)
        rc, _, err = await backend._cmd(f'nmcli con up "{backend.TEMP_PROFILE}"')
        if rc != 0:
            code = ble_flow.wifi_error_code(err)
            text = {"wifi_auth_failed":  f'Could not join "{ssid}": check the password',
                    "wifi_ap_not_found": f'"{ssid}" is not in range'}.get(code, f'Could not join "{ssid}": {err}')
            return self._fail(code, text, improv.ERROR_UNABLE_TO_CONNECT)

        self._status("wifi_connecting", f'Joined "{ssid}". Waiting for an address…')
        if not await backend._wait_for_ip(timeout=30):
            return self._fail("dhcp_failed", f'Joined "{ssid}" but got no IP address', improv.ERROR_UNABLE_TO_CONNECT)

        if not await self._internet_ok():
            return self._fail("no_internet", f'"{ssid}" has no internet access to SchoolAir', improv.ERROR_UNABLE_TO_CONNECT)

        self._status("registering", "Registering with SchoolAir…")
        valid, vmsg = await backend._validate_token(reg["token"])
        if not valid:
            return self._fail("registration_rejected", backend._friendly_error(vmsg), improv.ERROR_UNKNOWN)

        success, hb_msg, device_auth_token = await backend._post_heartbeat({
            "token":       reg["token"],
            "mac_address": backend._get_mac_address(),
            "cpu_serial":  backend._get_cpu_serial(),
            "nickname":    reg["nickname"],
            "migrate":     reg["migrate"],
            "new_asset":   {"nickname": reg["nickname"], "type": reg["environment"],
                            "site_name": reg["site"] or None},
        })
        if not success:
            return self._fail("registration_rejected", backend._friendly_error(hb_msg), improv.ERROR_UNKNOWN)

        # Saved exactly as run_registration() saves a wizard registration.
        await backend._commit_wifi_profile(ssid)
        backend.write_status({
            "token":         reg["token"],
            "site":          reg["site"],
            "asset_name":    reg["nickname"],
            "environment":   reg["environment"],
            "ssid":          ssid,
            "registered_at": backend.datetime.now(backend.timezone.utc).isoformat(),
            "via":           "ble",
        })
        try:
            backend._write_auth_token(reg["token"])
            if device_auth_token:
                backend._write_new_auth_token(device_auth_token)
        except Exception as e:
            log(f"warning: could not write tokens: {e}")
        backend._bind_identity()
        try:
            os.remove(backend.STAGING_FILE)
        except FileNotFoundError:
            pass
        return True

    def _fail(self, code: str, message: str, improv_error: int) -> bool:
        self._status(code, message)
        self._set_error(improv_error)
        return False

    async def _internet_ok(self) -> bool:
        url = f"{backend.NEW_SERVER_BASE_URL.rstrip('/')}/health"

        def _get():
            with urllib.request.urlopen(url, timeout=INTERNET_CHECK_TIMEOUT) as r:
                return r.status

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _get) == 200
        except (urllib.error.URLError, OSError):
            return False

    async def _finish(self) -> None:
        self._status("done", f"Registered as {DEVICE_NAME}")
        self.result_char.set_value(improv.build_result(improv.CMD_WIFI_SETTINGS,
                                                       [backend.NEW_SERVER_BASE_URL]))
        self._set_state(improv.STATE_PROVISIONED)  # also stops advertising
        await asyncio.sleep(3)  # let the phone read the final notifications
        if self._last_device:
            try:
                adapter = await self._bluez_interface(ADAPTER_PATH, "org.bluez.Adapter1")
                await adapter.call_remove_device(self._last_device)  # drop the pairing record
            except DBusError as e:
                log(f"could not remove pairing: {e.text}")
        self.done.set()
        # Tears down the hotspot, restarts telemetry and stops the wizard,
        # which stops this service too (PartOf=schoolair-wizard.service).
        await backend._delayed_shutdown()


async def main() -> None:
    if backend._has_token() and not device_identity.mismatch_detected():
        log("device is registered — not advertising")
        return
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    provisioner = Provisioner(bus)
    await provisioner.start()
    await provisioner.done.wait()
    await asyncio.sleep(30)  # _delayed_shutdown normally stops us well before this


if __name__ == "__main__":
    asyncio.run(main())
