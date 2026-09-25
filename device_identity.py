"""device_identity.py

Binds this uSD card to the Pi it was registered on.

The registration wizard saves the Pi's CPU serial and MAC address into .env
(DEVICE_CPU_SERIAL / DEVICE_MAC) next to the auth tokens. Every time
schoolair.service starts (boot or restart), main.py calls enforce(), which
compares them with the hardware it's actually running on. On a mismatch —
e.g. the cards of two endnodes were swapped — main.py refuses to run and
leaves MISMATCH_FILE behind, which makes netwatch.sh hold the device in AP
mode with the wizard up, so it can be re-registered on the spot. A successful
registration saves the new identity and removes MISMATCH_FILE.

Stdlib-only: imported both by main.py (venv) and by the wizard (system python).
"""

import os
import re
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"

# Under schoolair.service's RuntimeDirectory (tmpfs, owned by admin, kept across
# service restarts) — so it can never survive a reboot stale: every boot starts
# clean and enforce() re-decides.
MISMATCH_FILE = "/run/schoolair/identity-mismatch"

# main.py's exit status on a mismatch. schoolair.service lists it in
# RestartPreventExitStatus so systemd doesn't restart it every 10s for nothing.
EXIT_MISMATCH = 78  # EX_CONFIG

SERIAL_KEY = "DEVICE_CPU_SERIAL"
MAC_KEY    = "DEVICE_MAC"
UNKNOWN    = "unknown"


def read_cpu_serial() -> str:
    """The Pi's CPU serial, read live from /proc/cpuinfo."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":", 1)[1].strip().lower()
    except OSError:
        pass
    return UNKNOWN


def read_mac() -> str:
    """The onboard Wi-Fi MAC, read live from sysfs.

    wlan0 first: it's the onboard radio on every endnode, whereas an eth0 can
    come and go with a USB Ethernet dongle — which would otherwise look like a
    different Pi.
    """
    for iface in ("wlan0", "eth0"):
        try:
            return Path(f"/sys/class/net/{iface}/address").read_text().strip().lower()
        except OSError:
            pass
    return UNKNOWN


def _read_env(path: Path) -> dict:
    env = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    except OSError:
        pass
    return env


def _write_env_keys(path: Path, values: dict) -> None:
    """Set keys in .env in place (appending missing ones). Written to a temp
    file then renamed, so a power cut mid-write can't truncate the tokens."""
    try:
        content = path.read_text()
    except FileNotFoundError:
        content = ""
    for key, value in values.items():
        pattern = rf"^{re.escape(key)}=.*$"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += f"{key}={value}\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content)
    try:
        st = path.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    os.replace(tmp, path)


def save(env_path: Path = ENV_PATH) -> tuple[str, str]:
    """Record the current hardware as this card's identity (called by the
    wizard after a successful registration). Returns (serial, mac)."""
    serial, mac = read_cpu_serial(), read_mac()
    _write_env_keys(Path(env_path), {SERIAL_KEY: serial, MAC_KEY: mac})
    return serial, mac


def clear_mismatch() -> None:
    try:
        os.remove(MISMATCH_FILE)
    except FileNotFoundError:
        pass


def mismatch_detected() -> bool:
    return os.path.exists(MISMATCH_FILE)


def _flag_mismatch(detail: str) -> None:
    try:
        with open(MISMATCH_FILE, "w") as f:
            f.write(detail + "\n")
    except OSError as e:
        print(f"[identity] WARNING: could not write {MISMATCH_FILE}: {e}")


def enforce(env_path: Path = ENV_PATH) -> bool:
    """Compare the saved identity with the live hardware.

    Returns True if schoolair may run, False on a mismatch (after flagging it
    for netwatch.sh). Cases:
      - not registered (no token)         → nothing to protect; run.
      - registered, no saved identity     → a card registered before this
        check existed: trust-on-first-use, record the current hardware; run.
      - saved identity matches hardware   → run.
      - anything else                     → mismatch; don't run.
    """
    env = _read_env(Path(env_path))
    registered = bool(env.get("NEW_AUTH_TOKEN") or env.get("AUTH_TOKEN"))
    saved_serial = env.get(SERIAL_KEY, "").lower()
    saved_mac    = env.get(MAC_KEY, "").lower()
    serial, mac  = read_cpu_serial(), read_mac()

    if not registered:
        clear_mismatch()
        return True

    if not saved_serial and not saved_mac:
        if serial == UNKNOWN or mac == UNKNOWN:
            print(f"[identity] WARNING: can't read hardware identity "
                  f"(serial={serial} mac={mac}) — not recording it yet")
        else:
            _write_env_keys(Path(env_path), {SERIAL_KEY: serial, MAC_KEY: mac})
            print(f"[identity] No saved identity — recorded this Pi "
                  f"(serial={serial} mac={mac})")
        clear_mismatch()
        return True

    if saved_serial == serial and saved_mac == mac:
        clear_mismatch()
        return True

    detail = (f"saved serial={saved_serial or '-'} mac={saved_mac or '-'} / "
              f"this Pi serial={serial} mac={mac}")
    print(f"[identity] MISMATCH — this card was registered on a different Pi ({detail}). "
          f"Refusing to run; re-register this device via the wizard (AP mode).")
    _flag_mismatch(detail)
    return False
