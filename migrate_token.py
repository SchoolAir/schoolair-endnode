#!/usr/bin/env python3
"""migrate_token.py

One-time migration helper: reads the existing device auth token from the
old install_files locations and writes it to pi-main's .env file.

Run once on the Pi before starting schoolair.service for the first time.
Safe to re-run — it updates AUTH_TOKEN in place without touching other vars.
"""

import json
import re
import sys
from pathlib import Path

from atomic_file import write_atomic

ENV_PATH = Path(__file__).parent / ".env"

# New wizard format (non-LEGACY registrations)
STATUS_JSON = Path("/home/admin/.config/schoolair/status.json")
# Legacy format (LEGACY registrations and Node-RED devices)
DEVICE_TOKEN_JSON = Path("/home/admin/.device_token")


def find_token() -> tuple[str, str]:
    """Return (token, source_description). Token is '' if none found."""
    if STATUS_JSON.exists():
        try:
            data = json.loads(STATUS_JSON.read_text())
            token = data.get("token", "").strip()
            if token:
                return token, str(STATUS_JSON)
        except (json.JSONDecodeError, OSError):
            pass

    if DEVICE_TOKEN_JSON.exists():
        try:
            data = json.loads(DEVICE_TOKEN_JSON.read_text())
            token = data.get("token", "").strip()
            if token:
                return token, str(DEVICE_TOKEN_JSON)
        except (json.JSONDecodeError, OSError):
            pass

    return "", ""


def write_token(token: str) -> None:
    content = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    if re.search(r"^AUTH_TOKEN=.*$", content, re.MULTILINE):
        content = re.sub(r"^AUTH_TOKEN=.*$", f"AUTH_TOKEN={token}", content, flags=re.MULTILINE)
    else:
        content += f"\nAUTH_TOKEN={token}\n"
    write_atomic(ENV_PATH, content)


if __name__ == "__main__":
    token, source = find_token()

    if not token:
        print("No existing token found in:")
        print(f"  {STATUS_JSON}")
        print(f"  {DEVICE_TOKEN_JSON}")
        print("The device will need to be registered via the wizard on first boot.")
        sys.exit(1)

    write_token(token)
    print(f"Migrated token from {source}")
    print(f"AUTH_TOKEN written to {ENV_PATH}")
