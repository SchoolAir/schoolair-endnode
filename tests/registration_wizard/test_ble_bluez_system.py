"""tests/registration_wizard/test_ble_bluez_system.py

System test: ble_provision's GATT application and Improv advertisement against
the real BlueZ on this machine (system D-Bus, adapter hci0). The wizard's
network and cloud helpers are stubbed, so nothing touches Wi-Fi.

Opt-in, since it advertises from the local adapter and renames it for a few
seconds (the old name is put back). Run as root: the advertisement goes to the
kernel through btmgmt (see Advertiser in ble_provision.py):
    sudo SCHOOLAIR_BLE_SYSTEM_TEST=1 python3 -m pytest tests/registration_wizard/test_ble_bluez_system.py

What it proves: BlueZ accepts the services, characteristics and flags
(RegisterApplication validates them through our ObjectManager), the kernel
accepts the advertisement with the Improv service data, and it follows state changes —
re-advertising while provisioning, stopping once provisioned. It cannot pair
or connect: that needs a second device (a phone).
"""

import asyncio
import os
import re
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("SCHOOLAIR_BLE_SYSTEM_TEST") != "1",
                                reason="set SCHOOLAIR_BLE_SYSTEM_TEST=1 to run against the local BlueZ")

dbus_next = pytest.importorskip("dbus_next")

from dbus_next import BusType, Variant  # noqa: E402
from dbus_next.aio import MessageBus     # noqa: E402

import ble_provision  # noqa: E402
import improv         # noqa: E402

ADAPTER = "org.bluez.Adapter1"


async def _active_instances(props) -> int:
    """Advertising instances in the kernel ("Instances list with N item(s)")."""
    proc = await asyncio.create_subprocess_exec("btmgmt", "advinfo", stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    return int(re.search(r"Instances list with (\d+)", out.decode()).group(1))


async def test_gatt_app_and_advertisement_register_with_bluez(monkeypatch):
    monkeypatch.setattr(ble_provision.backend, "_cmd", AsyncMock(return_value=(0, "", "")))  # no rfkill
    # Becoming the default agent would take over the desktop session's pairing prompts.
    monkeypatch.setattr(ble_provision.Provisioner, "_register_agent", AsyncMock())

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    introspection = await bus.introspect("org.bluez", ble_provision.ADAPTER_PATH)
    adapter_obj = bus.get_proxy_object("org.bluez", ble_provision.ADAPTER_PATH, introspection)
    props = adapter_obj.get_interface("org.freedesktop.DBus.Properties")
    old_alias    = (await props.call_get(ADAPTER, "Alias")).value
    old_pairable = (await props.call_get(ADAPTER, "Pairable")).value
    before = await _active_instances(props)

    prov = ble_provision.Provisioner(bus)
    try:
        await prov.start()  # raises if BlueZ rejects the application or the kernel the advertisement

        assert (await props.call_get(ADAPTER, "Alias")).value == ble_provision.DEVICE_NAME
        assert await _active_instances(props) == before + 1

        prov._set_state(improv.STATE_PROVISIONING)
        await asyncio.sleep(1)
        assert await _active_instances(props) == before + 1  # re-advertised with the new state

        # Guards: the device list comes from BlueZ's object tree; a lockout stops
        # advertising and resumes it. (Disconnecting is stubbed: it would drop
        # this laptop's own Bluetooth devices.)
        devices = await prov._connected_devices()
        assert all(d.startswith(ble_provision.ADAPTER_PATH + "/dev_") for d in devices)
        monkeypatch.setattr(ble_provision, "LOCKOUT_S", 2)
        monkeypatch.setattr(prov, "_disconnect", AsyncMock())
        lock = asyncio.get_running_loop().create_task(prov._lock())
        await asyncio.sleep(3)  # 2 s to let the phone read "locked", then locked
        assert await _active_instances(props) == before
        await lock
        assert await _active_instances(props) == before + 1

        prov._set_state(improv.STATE_PROVISIONED)
        await asyncio.sleep(1)
        assert await _active_instances(props) == before  # provisioned: no longer advertising
    finally:
        await prov.advertiser.stop()
        gatt_mgr = adapter_obj.get_interface("org.bluez.GattManager1")
        try:
            await gatt_mgr.call_unregister_application(ble_provision.APP_PATH)
        except dbus_next.DBusError:
            pass
        await props.call_set(ADAPTER, "Alias", Variant("s", old_alias))
        await props.call_set(ADAPTER, "Pairable", Variant("b", old_pairable))
        bus.disconnect()
