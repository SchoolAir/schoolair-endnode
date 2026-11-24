#!/usr/bin/env python3
"""card_label.py — keep a label on the card's boot partition saying what it holds.

    /boot/firmware/schoolair-unit.json
    {
      "schoolair": 1,
      "version": "2.3.27",
      "hostname": "schoolair-26B18-40ed",
      "unit_type": "indoor",
      "cpu_serial": "00000000a1b2c3d4",
      "short_id": "K3F9Q",
      "card_config": true,
      "registered": true,
      "asset": "Room 12", "site": "Main Campus", "registered_at": "...",
      "wifi_networks": ["SchoolNet", "Guest"],
      "updated_at": "..."
    }

The boot partition is FAT, so the SchoolAir Installer (or anyone) can read it
from any computer without administrator rights: it tells a SchoolAir card from
an empty one or plain Raspberry Pi OS, whether it is registered, and as what,
before offering to erase it. "card_config": the unit reads schoolair_config.json
(card_config.py), so a card can be registered or given a new Wi-Fi by adding
that file instead of erasing it. The CPU serial lets the installer find the
unit on the server (within the signed-in school only).

wifi_networks: the Wi-Fi networks the unit has saved (names only; names are
broadcast anyway), most preferred first, so the installer can show them.

No secrets: no tokens, no Wi-Fi passwords. asset and site are as registered;
the dashboard may have renamed the unit since, which the installer asks the
server about.

Written at every start (schoolair-card-label.service, after card-config) and
after each registration (wizard.register_and_save, run_registration).
prepare_image.sh leaves a "new" label on the golden image.
"""

import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LABEL_FILE = "/boot/firmware/schoolair-unit.json"
APP_ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = "/home/admin/.config/schoolair/status.json"
ENV_FILE = "/home/admin/schoolair/.env"
UNIT_TYPE_FILE = "/etc/schoolair-unit-type"


def _version() -> str:
    """jobs/ingest.py's VERSION, read as text (as ble_provision does)."""
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', (APP_ROOT / "jobs" / "ingest.py").read_text(), re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _registered() -> bool:
    for line in _read(ENV_FILE).splitlines():
        if line.startswith("AUTH_TOKEN="):
            return bool(line[len("AUTH_TOKEN="):].strip())
    return False


def _status() -> dict:
    try:
        data = json.loads(_read(STATUS_FILE) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _nmcli(*args: str) -> str:
    try:
        return subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _wifi_networks() -> "list[str]":
    """Saved Wi-Fi client networks (not the setup hotspot), most preferred first."""
    found = []
    for line in _nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT-PRIORITY", "connection", "show").splitlines():
        parts = line.replace("\\:", "\0").split(":")
        if len(parts) < 3 or parts[1] != "802-11-wireless":
            continue
        name = parts[0].replace("\0", ":")
        ssid, mode = (_nmcli("-g", "802-11-wireless.ssid,802-11-wireless.mode", "connection", "show", name)
                      .strip().split("\n") + ["", ""])[:2]
        if mode.strip() == "ap" or not ssid.strip():
            continue
        try:
            prio = int(parts[2])
        except ValueError:
            prio = 0
        found.append((-prio, ssid.strip()))
    seen, out = set(), []
    for _, ssid in sorted(found):
        if ssid not in seen:
            seen.add(ssid)
            out.append(ssid)
    return out


def _identity() -> "tuple[str, str]":
    try:
        sys.path.insert(0, str(APP_ROOT))
        import device_identity
        serial = device_identity.read_cpu_serial()
        return serial, device_identity.short_id(serial)
    except Exception:
        return "", ""


def build(new: bool = False) -> dict:
    """The label. new: for the golden image (prepare_image.sh), which no Pi
    has started from yet: no hostname, identity or registration."""
    label = {
        "schoolair": 1,
        "version": _version(),
        "card_config": (APP_ROOT / "registration_wizard" / "card_config.py").exists(),
    }
    if new:
        label.update(new=True, registered=False)
    else:
        serial, short = _identity()
        unit_type = _read(UNIT_TYPE_FILE).strip()
        label.update(
            hostname=socket.gethostname(),
            unit_type=unit_type if unit_type in ("indoor", "outdoor") else None,
            cpu_serial=serial or None,
            short_id=short or None,
            registered=_registered(),
            wifi_networks=_wifi_networks(),
        )
        if label["registered"]:
            st = _status()
            label.update(asset=st.get("asset_name") or None, site=st.get("site") or None,
                         registered_at=st.get("registered_at") or None)
    label["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return label


def write(new: bool = False, path: "str | None" = None) -> bool:
    """Writes the label if it changed (ignoring updated_at): the boot partition
    is FAT, so it is written as little as possible. Never raises."""
    path = path or LABEL_FILE
    try:
        label = build(new)
        try:
            with open(path) as f:
                old = json.load(f)
            if {k: v for k, v in old.items() if k != "updated_at"} == \
               {k: v for k, v in label.items() if k != "updated_at"}:
                return True
        except (OSError, ValueError):
            pass
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(label, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[card-label] could not write {path}: {e}")
        return False


if __name__ == "__main__":
    sys.exit(0 if write(new="--new" in sys.argv[1:]) else 1)
