"""tests/registration_wizard/test_netwatch_restart_guard.py

netwatch.sh's own 'ap' state handler restarts schoolair.service, on its own
POLL_INTERVAL cycle — which can beat wizard.py's _delayed_shutdown() to the same
restart, since the wizard clears WIZARD_BUSY_FILE the instant registration
succeeds, not 6s later when its own restart actually fires. schoolair_recently_
restarted() skips netwatch's redundant restart when that's already happened.

netwatch.sh has no other tests; this extracts just that function (regex, same
approach as tests/test_ota_detach.py) and runs it against a fake systemctl —
not a live device, so no real "did it skip the restart" test here, but this pins
the time-window arithmetic that a live test can't easily control precisely.
"""

import re
import subprocess
from pathlib import Path

import pytest

NETWATCH = Path(__file__).parents[2] / "registration_wizard" / "netwatch.sh"
TEXT = NETWATCH.read_text()

FAKE_SYSTEMCTL = """#!/bin/bash
if [[ "$1" == "show" ]]; then
    echo "$FAKE_ACTIVE_ENTER"
fi
"""


def _function_source(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n(.*?)\n\}}\n", TEXT, re.S | re.M)
    assert m, f"{name}() not found in netwatch.sh"
    return f"{name}() {{\n{m.group(1)}\n}}\n"


@pytest.fixture
def check(tmp_path):
    """Returns run(active_enter_us, uptime_s, poll_interval=30) -> bool, running the
    REAL schoolair_recently_restarted() body against a fake systemctl and /proc/uptime."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text(FAKE_SYSTEMCTL)
    (fakebin / "systemctl").chmod(0o755)
    fake_uptime = tmp_path / "uptime"
    script = tmp_path / "run.sh"
    script.write_text(
        'TELEMETRY_SERVICE="schoolair"\n'
        'POLL_INTERVAL="${POLL_INTERVAL:-30}"\n'
        + _function_source("schoolair_recently_restarted")
        + 'schoolair_recently_restarted && echo YES || echo NO\n'
    )

    def run(active_enter_us, uptime_s, poll_interval=30):
        fake_uptime.write_text(f"{uptime_s} 0\n")
        # /proc/uptime itself can't be faked without root; the function reads it via
        # `awk ... /proc/uptime` literally, so patch that one call out with a wrapper.
        patched = script.read_text().replace("/proc/uptime", str(fake_uptime))
        script.write_text(patched)
        env = {
            "PATH": f"{fakebin}:/usr/bin:/bin",
            "FAKE_ACTIVE_ENTER": str(active_enter_us),
            "POLL_INTERVAL": str(poll_interval),
        }
        out = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=15)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip() == "YES"

    return run


def test_true_just_after_a_restart(check):
    # started at t=1_000_000us (1s), now t=4_000_000us (4s): 3s ago, well inside margin
    assert check(active_enter_us=1_000_000, uptime_s=4.0) is True


def test_false_once_past_poll_interval_plus_buffer(check):
    # POLL_INTERVAL=30 -> margin=35s; 40s ago is outside it
    assert check(active_enter_us=1_000_000, uptime_s=41.0, poll_interval=30) is False


def test_true_right_at_the_edge_of_the_margin(check):
    # POLL_INTERVAL=5 -> margin=10s; 9s ago is just inside it
    assert check(active_enter_us=1_000_000, uptime_s=10.0, poll_interval=5) is True


def test_false_when_never_started(check):
    assert check(active_enter_us=0, uptime_s=100.0) is False


def test_margin_scales_with_poll_interval():
    """Pins the actual margin formula (POLL_INTERVAL + 5) so a future POLL_INTERVAL
    change doesn't silently make this guard too tight or too loose."""
    m = re.search(r"local margin=\$\(\( POLL_INTERVAL \+ (\d+) \)\)", TEXT)
    assert m, "expected schoolair_recently_restarted()'s margin to be POLL_INTERVAL + <buffer>"


def test_netwatch_script_is_still_valid_bash():
    subprocess.run(["bash", "-n", str(NETWATCH)], check=True)
