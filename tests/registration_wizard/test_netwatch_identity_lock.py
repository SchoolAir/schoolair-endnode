"""tests/registration_wizard/test_netwatch_identity_lock.py

When main.py finds the card was registered on a different Pi it leaves
device_identity.MISMATCH_FILE behind; netwatch.sh must then bring up the AP +
wizard even though a perfectly good Wi-Fi uplink is present, and hold it there
(never closing the AP on "uplink detected").

Runs the REAL netwatch.sh main loop for a few seconds against fake nmcli /
systemctl / iw that log every call, with a 1s poll interval.
"""

import subprocess
from pathlib import Path

NETWATCH = Path(__file__).parents[2] / "registration_wizard" / "netwatch.sh"

# A client Wi-Fi uplink is always active, the AP is never reported active.
FAKE_NMCLI = """#!/bin/bash
echo "nmcli $*" >> "$CALLS"
if [[ "$*" == *"con show --active"* ]]; then
    echo "HomeWifi:802-11-wireless:activated"
fi
exit 0
"""
FAKE_LOGGER = """#!/bin/bash
echo "$(basename "$0") $*" >> "$CALLS"
exit 0
"""


def _run(tmp_path, flag_present: bool) -> tuple[str, str]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "nmcli").write_text(FAKE_NMCLI)
    for name in ("systemctl", "iw", "iptables"):
        (fakebin / name).write_text(FAKE_LOGGER)
    for f in fakebin.iterdir():
        f.chmod(0o755)
    flag = tmp_path / "identity-mismatch"
    if flag_present:
        flag.write_text("saved serial=a / this Pi serial=b\n")
    calls = tmp_path / "calls.log"
    calls.touch()
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "CALLS": str(calls),
        "NETWATCH_POLL": "1",
        "NETWATCH_IDENTITY_FILE": str(flag),
    }
    try:
        out = subprocess.run(["bash", str(NETWATCH)], env=env, capture_output=True,
                             text=True, timeout=7)
        stdout = out.stdout
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    return stdout, calls.read_text()


def test_mismatch_forces_ap_despite_uplink_and_holds_it(tmp_path):
    log, calls = _run(tmp_path, flag_present=True)
    assert "forcing AP mode" in log
    assert "nmcli con up SchoolAir_AP" in calls
    assert "systemctl start schoolair-wizard" in calls
    # Held: the uplink never makes netwatch close the AP or restart schoolair.
    assert "closing AP" not in log
    assert "nmcli con down SchoolAir_AP" not in calls
    assert "systemctl restart schoolair" not in calls


def test_no_flag_leaves_online_device_alone(tmp_path):
    log, calls = _run(tmp_path, flag_present=False)
    assert "Initial state: online" in log
    assert "con up SchoolAir_AP" not in calls
    assert "start schoolair-wizard" not in calls
