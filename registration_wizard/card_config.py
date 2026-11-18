#!/usr/bin/env python3
"""card_config.py — apply the installer's schoolair.json on a unit's first boot.

The SchoolAir installer writes the same image to every card and adds one file
per unit to the boot partition (FAT, writable from any computer):

    /boot/firmware/schoolair.json
    {
      "version": 1,
      "hostname": "schoolair-26B18-40ed",
      "wifi": {"country": "ES", "networks": [{"ssid": "SchoolNet", "psk": "..."}]},
      "register": {"asset": "Room 12", "site": "Main Campus",
                   "invite": "<invite link>", "environment": "indoor"}
    }

Every field but "version" is optional. schoolair-card-config.service runs this
after schoolair-first-boot (the unit type is known by then) and before the
launcher, so a unit that registers here starts in normal operation, and one that
doesn't falls back to the setup hotspot and Bluetooth as before.

The file holds the Wi-Fi password and the invite link, so it is deleted as soon
as it is read, before anything else is tried. What was applied, without those,
goes to /var/lib/schoolair/card-config.json.

Exit status: 0 done (or no file), 1 the file was invalid or a step failed.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import wizard as backend  # sets up sys.path for the app's modules
import ssh_register

CARD_FILE = "/boot/firmware/schoolair.json"
RECORD_FILE = "/var/lib/schoolair/card-config.json"
SET_HOSTNAME = "/home/admin/set_hostname.sh"
UNIT_TYPE_FILE = "/etc/schoolair-unit-type"
NETWORK_WAIT_S = 120

_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class Invalid(ValueError):
    """schoolair.json doesn't have the expected shape."""


def _text(value, field: str, max_len: int, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise Invalid(f"{field} is missing")
        return ""
    if not isinstance(value, str) or len(value) > max_len:
        raise Invalid(f"{field} must be text of at most {max_len} characters")
    return value


def validate(cfg) -> dict:
    """The config in a normalised form; raises Invalid. Never echoes secrets."""
    if not isinstance(cfg, dict) or cfg.get("version") != 1:
        raise Invalid("version must be 1")
    out = {"hostname": None, "country": None, "networks": [], "register": None}

    hn = cfg.get("hostname")
    if hn is not None:
        if not isinstance(hn, str) or not _HOSTNAME.match(hn):
            raise Invalid("hostname is not a valid hostname")
        out["hostname"] = hn

    wifi = cfg.get("wifi") or {}
    if not isinstance(wifi, dict):
        raise Invalid("wifi must be an object")
    country = wifi.get("country")
    if country is not None:
        if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country):
            raise Invalid("wifi.country must be 2 letters")
        out["country"] = country.upper()
    networks = wifi.get("networks") or []
    if not isinstance(networks, list):
        raise Invalid("wifi.networks must be a list")
    for i, n in enumerate(networks):
        if not isinstance(n, dict):
            raise Invalid(f"wifi.networks[{i}] must be an object")
        ssid = _text(n.get("ssid"), f"wifi.networks[{i}].ssid", 64, required=True)
        if not 1 <= len(ssid.encode()) <= 32:
            raise Invalid(f"wifi.networks[{i}].ssid must be 1 to 32 bytes")
        psk = _text(n.get("psk"), f"wifi.networks[{i}].psk", 64)
        if psk and not 8 <= len(psk) <= 63:
            raise Invalid(f"wifi.networks[{i}].psk must be 8 to 63 characters")
        out["networks"].append({"ssid": ssid, "psk": psk})

    reg = cfg.get("register")
    if reg is not None:
        if not isinstance(reg, dict):
            raise Invalid("register must be an object")
        env = reg.get("environment")
        if env not in (None, "indoor", "outdoor"):
            raise Invalid("register.environment must be indoor or outdoor")
        out["register"] = {
            "asset": _text(reg.get("asset"), "register.asset", 100, required=True).strip(),
            "site": _text(reg.get("site"), "register.site", 100).strip(),
            "invite": _text(reg.get("invite"), "register.invite", 4096).strip(),
            "environment": env,
        }
    return out


def take_card_file(path: "str | None" = None):
    """Reads the file and deletes it at once (it holds secrets). None if absent."""
    path = path or CARD_FILE
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    try:
        with open(path, "r+b") as f:  # blank it first: FAT keeps deleted data around
            f.write(b"\0" * len(raw))
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    os.remove(path)
    os.sync()
    return json.loads(raw.decode("utf-8"))


def run(cmd: list) -> int:
    """Runs a command; never prints its arguments (they may hold a password)."""
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE).returncode


def apply_hostname(hn: str) -> bool:
    return run(["bash", SET_HOSTNAME, hn]) == 0


def apply_wifi(country, networks) -> bool:
    ok = True
    if country:
        ok &= run(["raspi-config", "nonint", "do_wifi_country", country]) == 0
        run(["rfkill", "unblock", "wifi"])
    # First listed = most preferred.
    for priority, n in zip(range(len(networks) + 10, 10, -1), networks):
        name = backend._profile_name(n["ssid"])
        run(["nmcli", "con", "delete", name])
        cmd = ["nmcli", "con", "add", "type", "wifi", "ifname", backend.AP_INTERFACE,
               "con-name", name, "ssid", n["ssid"],
               "connection.autoconnect", "yes", "connection.autoconnect-priority", str(priority),
               "ipv4.method", "auto", "ipv6.method", "ignore"]
        if n["psk"]:
            cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", n["psk"]]
        if run(cmd) != 0:
            print(f"[card-config] could not add Wi-Fi network {n['ssid']!r}")
            ok = False
    if networks:
        run(["nmcli", "con", "up", backend._profile_name(networks[0]["ssid"])])
    return ok


def environment_for(reg: dict) -> str:
    if reg.get("environment"):
        return reg["environment"]
    intended = ssh_register.INTENDED_ENVIRONMENT.get(ssh_register.detect_sensor() or "")
    if intended:
        return intended
    try:
        with open(UNIT_TYPE_FILE) as f:
            kind = f.read().strip()
        if kind in ("indoor", "outdoor"):
            return kind
    except OSError:
        pass
    return "indoor"


async def wait_for_network(timeout: int = NETWORK_WAIT_S) -> bool:
    for _ in range(timeout // 5):
        try:
            ssh_register.invite_status([])  # any answer means SchoolAir is reachable
            return True
        except RuntimeError as e:
            if "could not reach" not in str(e):
                return True  # it answered, with an error about the empty list
        await asyncio.sleep(5)
    return False


async def register(reg: dict) -> "tuple[bool, str]":
    """Registers with the invite link's next unused registration, as ssh_register does."""
    tokens = ssh_register.parse_invite(reg["invite"])
    if not tokens:
        return False, "the invite link holds no registrations"
    if backend._has_token():
        return False, "already registered: left as it is"
    if not await wait_for_network():
        return False, "SchoolAir could not be reached"
    _, current_wifi = await backend._current_wifi()
    environment = environment_for(reg)
    tried: "set[str]" = set()
    message = ""
    for _ in range(2):  # another unit may take a registration meanwhile: try the next once
        status = ssh_register.invite_status(tokens)
        token = ssh_register.next_unused(tokens, status, tried)
        if token is None:
            return False, message or "every registration in the invite link has been used"
        tried.add(token)
        valid, vmsg = await backend._validate_token(token)
        if not valid:
            message = backend._friendly_error(vmsg)
            continue
        ok, message = await backend.register_and_save(
            {"token": token, "nickname": reg["asset"], "site": reg["site"],
             "environment": environment, "migrate": False},
            None, current_wifi or None, via="card")
        if ok:
            return True, ""
    return False, message


def write_record(record: dict) -> None:
    os.makedirs(os.path.dirname(RECORD_FILE), exist_ok=True)
    with open(RECORD_FILE, "w") as f:
        json.dump(record, f, indent=2)


async def main_async() -> int:
    record = {"applied_at": datetime.now(timezone.utc).isoformat()}
    try:
        raw = take_card_file()
    except (ValueError, UnicodeDecodeError):
        print("[card-config] schoolair.json is not valid JSON: ignored (and deleted)")
        write_record({**record, "error": "not valid JSON"})
        return 1
    if raw is None:
        return 0
    try:
        cfg = validate(raw)
    except Invalid as e:
        print(f"[card-config] schoolair.json ignored (and deleted): {e}")
        write_record({**record, "error": str(e)})
        return 1

    ok = True
    if cfg["hostname"]:
        done = apply_hostname(cfg["hostname"])
        print(f"[card-config] hostname {cfg['hostname']}: {'set' if done else 'FAILED'}")
        record["hostname"] = cfg["hostname"]
        ok &= done
    if cfg["country"] or cfg["networks"]:
        done = apply_wifi(cfg["country"], cfg["networks"])
        print(f"[card-config] Wi-Fi ({len(cfg['networks'])} network(s)): {'added' if done else 'FAILED'}")
        record["wifi"] = {"country": cfg["country"], "ssids": [n["ssid"] for n in cfg["networks"]]}
        ok &= done
    reg = cfg["register"]
    if reg:
        record["register"] = {"asset": reg["asset"], "site": reg["site"]}
        if reg["invite"]:
            registered, why = await register(reg)
            print(f"[card-config] registration as {reg['asset']!r}: "
                  f"{'done' if registered else 'not done: ' + why}")
            record["register"].update(registered=registered, error=why or None)
            ok &= registered
        else:
            print("[card-config] no invite link: registration is left to Bluetooth or the hotspot")
            record["register"]["registered"] = False
    write_record(record)
    return 0 if ok else 1


def main() -> int:
    if os.geteuid() != 0:
        print("Run as root.")
        return 1
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
