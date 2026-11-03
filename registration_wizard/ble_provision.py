#!/usr/bin/env python3
"""ble_provision.py — register an endnode over Bluetooth (BLE), or put a
registered one that lost its network back on Wi-Fi.

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

Advertises whenever the hotspot wizard would be useful: while the device is
unregistered, and — like the hotspot — when a registered device has lost its
network (netwatch.sh brought the AP up). In that second case the page only
reconnects it to Wi-Fi: the token is checked with the server but not used up,
and the registration is left as it is. A registered device that is online
exits straight away.

Guards against someone nearby tying the device up: a phone that goes quiet for
IDLE_DISCONNECT_S (the page reads Status every 30 s while open) or stays
connected past MAX_CONNECTION_S is disconnected, and after MAX_FAILED_ATTEMPTS
failed attempts in a row the device stops advertising and refuses writes for
LOCKOUT_S. While a phone is connected, BLE_CLIENT_FILE tells netwatch.sh not to
close the AP for a reconnect probe (which would stop this service mid-session).
Wi-Fi passwords are never logged.

BlueZ over D-Bus, via dbus-next. Writes to the Improv RPC Command and the
Registration characteristics need an encrypted link, so the first one makes
the phone pair ("Just Works", LE Secure Connections).
"""

import asyncio
import json
import os
import re
import time
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

IDLE_DISCONNECT_S   = 120
MAX_CONNECTION_S    = 15 * 60
CONNECTION_POLL_S   = 10
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_S           = 5 * 60
BLE_CLIENT_FILE     = "/run/schoolair-ble-client"

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
        self.on_access = None  # called with the device path on every read or write

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
        if self.on_access and "device" in options:
            self.on_access(options["device"].value)
        return self._value[offset:]

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        if self._on_write is None:
            raise DBusError("org.bluez.Error.NotPermitted", "Read-only characteristic")
        if "prepare-authorize" in options and options["prepare-authorize"].value:
            return  # BlueZ asking whether a long write may be queued: yes
        offset = options["offset"].value if "offset" in options else 0
        device = options["device"].value if "device" in options else ""
        if self.on_access and device:
            self.on_access(device)
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
    def __init__(self, bus: MessageBus, registered_as: "str | None" = None):
        """registered_as: the device's nickname when it is already registered and
        only needs reconnecting to Wi-Fi; None to register it."""
        self.bus = bus
        self.registered_as = registered_as
        self.done = asyncio.Event()
        self.registration: "dict | None" = None
        self._failures = 0
        self._locked_until = 0.0
        self._connected_since: "dict[str, float]" = {}
        self._last_activity: "dict[str, float]" = {}
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
        for char in self.characteristics.values():
            char.on_access = self._touch
        self.advertisement = Advertisement()

        self.state_char.set_value(bytes([improv.STATE_AUTHORIZED]))
        self.error_char.set_value(bytes([improv.ERROR_NONE]))
        self.caps_char.set_value(bytes([CAPABILITIES]))
        self._idle_status()

    def _idle_status(self) -> None:
        if self.registered_as is None:
            self.status_char.set_value(ble_flow.status_payload("idle", registered=False))
        else:
            self.status_char.set_value(ble_flow.status_payload(
                "idle", f'Registered as "{self.registered_as}"; not connected to a network',
                registered=True, nickname=self.registered_as))

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
        log(f"advertising as {DEVICE_NAME}" + (" (reconnect only)" if self.registered_as is not None else ""))
        asyncio.get_running_loop().create_task(self._watch_connections())

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

    def _refuse_while_locked(self) -> None:
        if time.monotonic() < self._locked_until:
            raise DBusError("org.bluez.Error.NotPermitted", "Too many failed attempts; try again later")

    def _on_registration_write(self, value: bytes, offset: int, device: str) -> None:
        self._refuse_while_locked()
        if offset == 0:
            self._registration_buf = bytearray()
        self._registration_buf += value
        try:
            self.registration = ble_flow.parse_registration(bytes(self._registration_buf),
                                                            require_nickname=self.registered_as is None)
        except ValueError as e:
            # A long write may still be arriving; only complain once it parses as JSON.
            try:
                json.loads(self._registration_buf.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            self.registration = None
            self._status("missing_registration", str(e))
            self._count_failure()
            return
        self._last_device = device or self._last_device
        if self.registered_as is None:
            self._status("registration_received", f'Ready to register "{self.registration["nickname"]}"')
        else:
            self._status("registration_received", f'Ready to reconnect "{self.registered_as}"')

    def _on_rpc_write(self, value: bytes, offset: int, device: str) -> None:
        self._refuse_while_locked()
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
            if self.registered_as is None:
                ok, done_message = await self._attempt(ssid, password, reg), f"Registered as {DEVICE_NAME}"
            else:
                ok, done_message = await self._reconnect(ssid, password, reg), f'Back online on "{ssid}"'
            if ok:
                self._failures = 0
                await self._finish(done_message)
            else:
                await backend._cmd(f'nmcli con delete "{backend.TEMP_PROFILE}" 2>/dev/null; true')
                if ap_was_up:
                    await backend._revert_to_ap()
                self._set_state(improv.STATE_AUTHORIZED)
                self._count_failure()
        finally:
            self._busy = False
            try:
                os.remove(backend.WIZARD_BUSY_FILE)
            except FileNotFoundError:
                pass

    async def _attempt(self, ssid: str, password: str, reg: dict) -> bool:
        """One connect → check → register pass. Returns True once registered;
        on failure sets Status and the Improv error, and returns False."""
        if not await self._join(ssid, password):
            return False

        self._status("registering", "Registering with SchoolAir…")
        valid, vmsg = await backend._validate_token(reg["token"])
        if not valid:
            return self._fail("registration_rejected", backend._friendly_error(vmsg), improv.ERROR_UNKNOWN)
        return await self._register(ssid, reg)

    async def _reconnect(self, ssid: str, password: str, reg: dict) -> bool:
        """A registered device back on Wi-Fi: the same checks as a registration,
        but the token is only validated (not used up) and nothing is re-registered."""
        if not await self._join(ssid, password):
            return False
        self._status("registering", "Checking the registration token…")
        valid, vmsg = await backend._validate_token(reg["token"])
        if not valid:
            return self._fail("registration_rejected", backend._friendly_error(vmsg), improv.ERROR_UNKNOWN)
        await backend._commit_wifi_profile(ssid)
        return True

    async def _join(self, ssid: str, password: str) -> bool:
        """Join the network and check SchoolAir is reachable through it."""
        self._status("wifi_connecting", f'Connecting to "{ssid}"…')
        def hide(text: str) -> str:  # nmcli's errors go to Status and the journal
            return text.replace(password, "•••") if password else text

        ok, msg = await backend._setup_client_profile(ssid, password)
        if not ok:
            return self._fail("wifi_failed", hide(msg), improv.ERROR_UNABLE_TO_CONNECT)
        rc, _, err = await backend._cmd(f'nmcli con up "{backend.TEMP_PROFILE}"')
        err = hide(err)
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
        return True

    async def _register(self, ssid: str, reg: dict) -> bool:
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

    # ── Guards ──

    def _count_failure(self) -> None:
        self._failures += 1
        if self._failures >= MAX_FAILED_ATTEMPTS:
            asyncio.get_running_loop().create_task(self._lock())

    async def _lock(self) -> None:
        """Too many failures in a row: stop advertising, drop the phone, refuse
        writes for LOCKOUT_S, then start over."""
        self._locked_until = time.monotonic() + LOCKOUT_S
        self._failures = 0
        log(f"{MAX_FAILED_ATTEMPTS} failed attempts — pausing for {LOCKOUT_S}s")
        self._status("locked", f"Too many failed attempts. Try again in {LOCKOUT_S // 60} minutes.")
        await asyncio.sleep(2)  # let the phone read why
        try:
            await self._adv_mgr.call_unregister_advertisement(ADV_PATH)
        except DBusError as e:
            log(f"could not stop advertising: {e.text}")
        for path in await self._connected_devices():
            await self._disconnect(path)
        await asyncio.sleep(LOCKOUT_S)
        self._locked_until = 0.0
        self._idle_status()
        try:
            await self._adv_mgr.call_register_advertisement(ADV_PATH, {})
            log("advertising again")
        except DBusError as e:
            log(f"could not resume advertising: {e.text}")

    def _touch(self, device: str) -> None:
        self._last_activity[device] = time.monotonic()

    async def _connected_devices(self) -> "list[str]":
        try:
            om = await self._bluez_interface("/", "org.freedesktop.DBus.ObjectManager")
            objects = await om.call_get_managed_objects()
        except DBusError as e:
            log(f"could not list Bluetooth devices: {e.text}")
            return []
        return [path for path, ifaces in objects.items()
                if path.startswith(ADAPTER_PATH + "/")
                and "org.bluez.Device1" in ifaces
                and ifaces["org.bluez.Device1"].get("Connected", Variant("b", False)).value]

    async def _disconnect(self, path: str) -> None:
        try:
            device = await self._bluez_interface(path, "org.bluez.Device1")
            await device.call_disconnect()
        except DBusError as e:
            log(f"could not disconnect {path}: {e.text}")

    async def _check_connections(self) -> None:
        """Disconnect a phone that went quiet or stayed too long (never mid-attempt),
        and keep BLE_CLIENT_FILE in step with whether one is connected."""
        connected = await self._connected_devices()
        now = time.monotonic()
        for path in list(self._connected_since):
            if path not in connected:
                del self._connected_since[path]
                self._last_activity.pop(path, None)
        for path in connected:
            since = self._connected_since.setdefault(path, now)
            quiet = now - max(since, self._last_activity.get(path, since))
            if self._busy:
                continue
            if quiet >= IDLE_DISCONNECT_S:
                log(f"{path} idle for {int(quiet)}s — disconnecting")
                await self._disconnect(path)
            elif now - since >= MAX_CONNECTION_S:
                log(f"{path} connected for {int(now - since)}s — disconnecting")
                await self._disconnect(path)
        try:
            if connected:
                Path(BLE_CLIENT_FILE).write_text(" ".join(connected))
            else:
                Path(BLE_CLIENT_FILE).unlink(missing_ok=True)
        except OSError:
            pass

    async def _watch_connections(self) -> None:
        while not self.done.is_set():
            await asyncio.sleep(CONNECTION_POLL_S)
            await self._check_connections()

    async def _internet_ok(self) -> bool:
        url = f"{backend.NEW_SERVER_BASE_URL.rstrip('/')}/health"

        def _get():
            with urllib.request.urlopen(url, timeout=INTERNET_CHECK_TIMEOUT) as r:
                return r.status

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _get) == 200
        except (urllib.error.URLError, OSError):
            return False

    async def _finish(self, message: str) -> None:
        self._status("done", message)
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


async def registered_as() -> "tuple[bool, str | None]":
    """(advertise?, nickname if only reconnecting). Registered and online: nothing
    to do. Registered but the hotspot is up (no network): reconnect only."""
    if not backend._has_token() or device_identity.mismatch_detected():
        return True, None
    if not await backend._ap_is_active():
        return False, None
    return True, (backend.read_wizard_registration() or {}).get("asset_name") or DEVICE_NAME


async def main() -> None:
    advertise, nickname = await registered_as()
    if not advertise:
        log("device is registered and online — not advertising")
        return
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    provisioner = Provisioner(bus, registered_as=nickname)
    try:
        await provisioner.start()
        await provisioner.done.wait()
        await asyncio.sleep(30)  # _delayed_shutdown normally stops us well before this
    finally:
        Path(BLE_CLIENT_FILE).unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
