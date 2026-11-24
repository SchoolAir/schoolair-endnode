#!/usr/bin/env python3
"""card_config.py — apply the setup file on a card's boot partition at first boot.

The boot partition is FAT, writable from any computer. Every card holds the
same image; one small file per unit says what makes it that unit. Two forms:

The school portal's (schoolair.org/install) and the SchoolAir Installer's:

    /boot/firmware/schoolair_config.json
    {
      "one_time_key": "<single-use registration token>",
      "building": "Main Campus", "room": "Room 12",
      "wifi_name": "SchoolNet", "wifi_password": "...",
      "wifi_country": "ES", "hostname": "schoolair-26B18-40ed",
      "environment": "indoor"
    }

Any name starting with schoolair_config and ending .json is read: a browser
saves a second download as "schoolair_config (1).json". The last three fields
are optional extras the installer adds; every field is optional, but a key
needs a room.

The first form, kept for cards already written with it:

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

The file holds the Wi-Fi password and the key, so it is deleted as soon as it
is read, before anything else is tried. If there are several, the newest is
used and all are deleted. What was applied, without those,
goes to /var/lib/schoolair/card-config.json.

Exit status: 0 done (or no file), 1 the file was invalid or a step failed.
"""

import asyncio
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import wizard as backend  # sets up sys.path for the app's modules
import ssh_register

BOOT_DIR = "/boot/firmware"
CARD_FILE = "schoolair.json"          # the first form
CONFIG_GLOB = "schoolair_config*.json"  # the portal's, also "schoolair_config (1).json"
RECORD_FILE = "/var/lib/schoolair/card-config.json"
SET_HOSTNAME = "/home/admin/set_hostname.sh"
UNIT_TYPE_FILE = "/etc/schoolair-unit-type"
NETWORK_WAIT_S = 120

# SchoolAir's own pattern only (set_hostname.sh: schoolair-YYMDD-XXXX, the month
# in hex): a unit stays recognisable in a network scan, so no other name is applied.
_HOSTNAME = re.compile(r"^schoolair-[0-9]{2}[1-9A-C][0-9]{2}-[0-9a-f]{4}$")


class Invalid(ValueError):
    """The setup file doesn't have the expected shape."""


def _text(value, field: str, max_len: int, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise Invalid(f"{field} is missing")
        return ""
    if not isinstance(value, str) or len(value) > max_len:
        raise Invalid(f"{field} must be text of at most {max_len} characters")
    return value


def _hostname(hn):
    if hn is None or hn == "":
        return None
    if not isinstance(hn, str) or not _HOSTNAME.match(hn):
        raise Invalid("hostname must follow SchoolAir's pattern, schoolair-YYMDD-XXXX")
    return hn


def _country(country, field: str):
    if country is None or country == "":
        return None
    if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country):
        raise Invalid(f"{field} must be 2 letters")
    return country.upper()


def _network(ssid, psk, ssid_field: str, psk_field: str) -> dict:
    ssid = _text(ssid, ssid_field, 64, required=True)
    if not 1 <= len(ssid.encode()) <= 32:
        raise Invalid(f"{ssid_field} must be 1 to 32 bytes")
    psk = _text(psk, psk_field, 64)
    if psk and not 8 <= len(psk) <= 63:
        raise Invalid(f"{psk_field} must be 8 to 63 characters")
    return {"ssid": ssid, "psk": psk}


def _environment(env, field: str):
    if env in (None, ""):
        return None
    if env not in ("indoor", "outdoor"):
        raise Invalid(f"{field} must be indoor or outdoor")
    return env


def validate(cfg) -> dict:
    """The config in a normalised form; raises Invalid. Never echoes secrets."""
    if isinstance(cfg, dict) and "version" not in cfg:
        return validate_portal(cfg)
    if not isinstance(cfg, dict) or cfg.get("version") != 1:
        raise Invalid("version must be 1")
    out = {"hostname": _hostname(cfg.get("hostname")), "country": None,
           "networks": [], "register": None}

    wifi = cfg.get("wifi") or {}
    if not isinstance(wifi, dict):
        raise Invalid("wifi must be an object")
    out["country"] = _country(wifi.get("country"), "wifi.country")
    networks = wifi.get("networks") or []
    if not isinstance(networks, list):
        raise Invalid("wifi.networks must be a list")
    for i, n in enumerate(networks):
        if not isinstance(n, dict):
            raise Invalid(f"wifi.networks[{i}] must be an object")
        out["networks"].append(_network(n.get("ssid"), n.get("psk"),
                                        f"wifi.networks[{i}].ssid", f"wifi.networks[{i}].psk"))

    reg = cfg.get("register")
    if reg is not None:
        if not isinstance(reg, dict):
            raise Invalid("register must be an object")
        out["register"] = {
            "asset": _text(reg.get("asset"), "register.asset", 100, required=True).strip(),
            "site": _text(reg.get("site"), "register.site", 100).strip(),
            "invite": _text(reg.get("invite"), "register.invite", 4096).strip(),
            "environment": _environment(reg.get("environment"), "register.environment"),
        }
    return out


def validate_portal(cfg: dict) -> dict:
    """schoolair_config.json (the portal's form) in the same normalised form."""
    out = {
        "hostname": _hostname(cfg.get("hostname")),
        "country": _country(cfg.get("wifi_country"), "wifi_country"),
        "networks": [],
        "register": None,
    }
    if cfg.get("wifi_name") not in (None, ""):
        out["networks"].append(_network(cfg.get("wifi_name"), cfg.get("wifi_password"),
                                        "wifi_name", "wifi_password"))
    key = _text(cfg.get("one_time_key"), "one_time_key", 4096).strip()
    room = _text(cfg.get("room"), "room", 100).strip()
    if key or room:
        if not room:
            raise Invalid("room is missing")
        out["register"] = {
            "asset": room,
            "site": _text(cfg.get("building"), "building", 100).strip(),
            "invite": key,
            "environment": _environment(cfg.get("environment"), "environment"),
        }
    return out


def card_files(boot_dir: "str | None" = None) -> "list[str]":
    """The setup files on the boot partition, newest last."""
    d = boot_dir or BOOT_DIR
    paths = glob.glob(os.path.join(glob.escape(d), CONFIG_GLOB))
    legacy = os.path.join(d, CARD_FILE)
    if os.path.isfile(legacy):
        paths.append(legacy)

    def age(p):
        try:
            return (os.path.getmtime(p), p)
        except OSError:
            return (0, p)
    return sorted(set(paths), key=age)


def take_card_files(boot_dir: "str | None" = None):
    """(name, parsed JSON) of the newest setup file; every one is deleted at
    once (they hold secrets). None if there is none. A file that isn't JSON
    raises ValueError, after all are deleted."""
    paths = card_files(boot_dir)
    if not paths:
        return None
    raws = {}
    for p in paths:
        try:
            raws[p] = take_card_file(p, parse=False)
        except OSError:
            pass
    newest = next((p for p in reversed(paths) if raws.get(p) is not None), None)
    if newest is None:
        return None
    return os.path.basename(newest), json.loads(raws[newest].decode("utf-8"))


def take_card_file(path: str, parse: bool = True):
    """Reads the file and deletes it at once (it holds secrets). None if absent."""
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
    return json.loads(raw.decode("utf-8")) if parse else raw


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


def why_unusable(status: dict) -> str:
    """Why none of the key's registrations can be used, from invite-status."""
    states = {t.get("state") for t in status.get("tokens", [])}
    if states <= {"invalid"}:
        return "SchoolAir doesn't know this key (from another SchoolAir server, or mistyped)"
    if states <= {"expired", "invalid"}:
        return "the key has expired: make a new one"
    return "the key has already been used"


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
            return False, message or why_unusable(status)
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
        taken = take_card_files()
    except (ValueError, UnicodeDecodeError):
        print("[card-config] the setup file is not valid JSON: ignored (and deleted)")
        write_record({**record, "error": "not valid JSON"})
        return 1
    if taken is None:
        return 0
    name, raw = taken
    record["file"] = name
    try:
        cfg = validate(raw)
    except Invalid as e:
        print(f"[card-config] {name} ignored (and deleted): {e}")
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
            print("[card-config] no key: registration is left to Bluetooth or the hotspot")
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
