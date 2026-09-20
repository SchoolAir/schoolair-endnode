"""setup.py

Device commissioning and registration.

Run manually to register a device:   python -m setup

(main.py does not import this module: an unregistered or unreachable device
is handled by jobs.ingest — readings queue locally, and the startup connectivity
ping validates the token against the primary server in the background.)
"""

import os
import re
import json
import uuid
import httpx
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
ENV_PATH = Path(".env")
HTTP_TIMEOUT = 10

# ----------------------- Helpers -----------------------

def print_banner():
    print("""
┌─────────────────────────────┐
│          SchoolAir          │
│     Device Registration     │
└─────────────────────────────┘
""")


def get_mac_address() -> str:
    """Return MAC address of eth0, wlan0, or the first available interface."""
    for iface in ("eth0", "wlan0"):
        try:
            return Path(f"/sys/class/net/{iface}/address").read_text().strip()
        except OSError:
            pass
    # Fall back to any non-loopback interface
    for iface_path in Path("/sys/class/net").iterdir():
        if iface_path.name == "lo":
            continue
        try:
            return (iface_path / "address").read_text().strip()
        except OSError:
            pass
    raise RuntimeError("Unable to determine a real MAC address")


def get_cpu_serial() -> str:
    """Return the Pi's CPU serial from /proc/cpuinfo, or 'unknown'."""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("Serial"):
                return line.split(":")[1].strip()
    except OSError:
        pass
    return "unknown"


def write_env_token(token: str):
    """Update AUTH_TOKEN in .env, or append it if not present."""
    content = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    if re.search(r"^AUTH_TOKEN=.*$", content, re.MULTILINE):
        content = re.sub(r"^AUTH_TOKEN=.*$", f"AUTH_TOKEN={token}", content, flags=re.MULTILINE)
    else:
        content += f"\nAUTH_TOKEN={token}\n"
    ENV_PATH.write_text(content)
    
# ----------------------- Registration flow -----------------------

def prompt_asset() -> tuple[int | None, dict | None]:
    """
    Ask whether the asset already exists.
    - If yes: user enters the asset_id directly (server handles assignment).
    - If no:  collect name + type for the server to create.

    Returns (asset_id, new_asset_payload) — one will always be None.
    """
    import questionary  # lazy: heavy (prompt_toolkit), interactive-only

    asset_exists = questionary.confirm(
        "Does this asset already exist on the server?"
    ).ask()

    if asset_exists:
        asset_id = int(questionary.text(
            "Asset ID:",
            validate=lambda v: v.isdigit() or "Please enter a valid numeric ID"
        ).ask())
        return asset_id, None
    else:
        asset_name = questionary.text(
            "Asset name (e.g. 'Classroom 3B', 'Main Entrance'):"
        ).ask()
        asset_type = questionary.select(
            "Asset type (e.g. 'indoor', 'outdoor'):",
            choices=["indoor", "outdoor"]
        ).ask()
        return None, {"nickname": asset_name, "type": asset_type}


def run_registration():
    print_banner()
    mac_address = get_mac_address()

    import questionary  # lazy: heavy (prompt_toolkit), interactive-only

    org_token   = questionary.text("Organisation Token:").ask()
    username    = questionary.text("Teacher Username:").ask()
    password    = questionary.password("Teacher Password:").ask()
    device_name = questionary.text("Device Nickname (e.g. 'pi-mini-2'):").ask()

    asset_id, new_asset = prompt_asset()

    auth_headers = {
        "Authorization": f"Bearer {org_token}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        res = client.post(
            f"{SERVER_URL}/aqc/v1/register",
            headers=auth_headers,
            json={
                "mac_address": mac_address,
                "cpu_serial":  get_cpu_serial(),
                "nickname":    device_name,
                "username":    username,
                "password":    password,
                "asset_id":    asset_id,
                "new_asset":   new_asset,
            },
        )

        if not res.is_success:
            raise RuntimeError(res.json().get("error", "Registration failed"))

        data = res.json()
        print(f"\n{data.get('message', 'Registered successfully!')}\n")

        write_env_token(data["auth_token"])
        load_dotenv(override=True)


# ----------------------- Manual entry point -----------------------

if __name__ == "__main__":
    try:
        run_registration()
    except RuntimeError as e:
        print(f"Registration failed: {e}")
        raise SystemExit(1)
