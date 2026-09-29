"""tests/test_ota_security_updates.py

OTA's last step (15d) runs the OS security upgrades: _os_security_updates() in
schoolair_setup.sh. Extracts the REAL function (same approach as
tests/test_ota_detach.py) and runs it against fake nice/ionice/dpkg/apt-get/
unattended-upgrade that log their calls. The property that matters most: no
failure in here may return non-zero, since the script runs under set -e with an
EXIT trap that would roll back the (already healthy) app update.
"""

import re
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).parents[1] / "schoolair_setup.sh"
TEXT = SETUP.read_text()

# Fails when the command's name is listed in $FAIL (e.g. FAIL="apt-get").
FAKE = """#!/bin/bash
name=$(basename "$0")
echo "$name $*" >> "$CALLS"
for f in $FAIL; do [ "$f" = "$name" ] && exit 1; done
exit 0
"""
# nice/ionice just run the wrapped command (after their own options).
PASSTHROUGH_NICE = """#!/bin/bash
shift 2; exec "$@"
"""
PASSTHROUGH_IONICE = """#!/bin/bash
shift 1; exec "$@"
"""


def _function_source(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n(.*?)\n\}}\n", TEXT, re.S | re.M)
    assert m, f"{name}() not found in schoolair_setup.sh"
    return f"{name}() {{\n{m.group(1)}\n}}\n"


@pytest.fixture
def run(tmp_path):
    def _run(fail: str = "", have_uu: bool = True) -> tuple[int, list[str], str]:
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir(exist_ok=True)
        names = ["dpkg", "apt-get"] + (["unattended-upgrade"] if have_uu else [])
        for name in names:
            (fakebin / name).write_text(FAKE)
        (fakebin / "nice").write_text(PASSTHROUGH_NICE)
        (fakebin / "ionice").write_text(PASSTHROUGH_IONICE)
        for f in fakebin.iterdir():
            f.chmod(0o755)
        calls = tmp_path / "calls.log"
        calls.write_text("")
        script = tmp_path / "run.sh"
        script.write_text(
            "set -euo pipefail\n"
            'ok()   { echo "OK: $*"; }\n'
            'warn() { echo "WARN: $*"; }\n'
            + _function_source("_os_security_updates")
            + "_os_security_updates\n"
            + 'echo "REACHED_END"\n'
        )
        # Only fakebin + the bare shell utilities: a real apt must never run here.
        env = {"PATH": f"{fakebin}:/usr/bin:/bin", "CALLS": str(calls), "FAIL": fail}
        if not have_uu:
            # hide any real unattended-upgrade on the host
            env["PATH"] = f"{fakebin}:{tmp_path / 'emptybin'}"
            (tmp_path / "emptybin").mkdir(exist_ok=True)
            for tool in ("basename", "bash", "cat"):
                src = Path("/usr/bin") / tool
                if not src.exists():
                    src = Path("/bin") / tool
                (tmp_path / "emptybin" / tool).symlink_to(src)
        out = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=30)
        return out.returncode, calls.read_text().splitlines(), out.stdout
    return _run


def test_happy_path_order(run):
    rc, calls, out = run()
    assert rc == 0 and "REACHED_END" in out
    assert calls[0] == "dpkg --configure -a"
    assert calls[1].startswith("apt-get -o DPkg::Lock::Timeout=300 update")
    assert calls[2] == "unattended-upgrade -v"
    assert "OK: OS security updates applied" in out


@pytest.mark.parametrize("failing", ["dpkg", "apt-get", "unattended-upgrade"])
def test_no_failure_escapes_the_function(run, failing):
    """Under set -e a non-zero return here would fire the rollback EXIT trap."""
    rc, _, out = run(fail=failing)
    assert rc == 0 and "REACHED_END" in out
    assert "WARN:" in out


def test_failed_list_update_skips_the_upgrade(run):
    rc, calls, out = run(fail="apt-get")
    assert rc == 0
    assert not any(c.startswith("unattended-upgrade") for c in calls)


def test_installs_the_tool_when_missing(run):
    """Cards set up after unattended-upgrades was briefly dropped from the
    package list don't have it."""
    rc, calls, out = run(have_uu=False)
    assert rc == 0 and "REACHED_END" in out
    assert any(c.startswith("apt-get") and "install -y -qq unattended-upgrades" in c for c in calls)


def test_runs_last_in_update_mode_only():
    call = TEXT.index("\n    _os_security_updates\n")
    update_block = TEXT.index('if [[ "$MODE" == "--update" ]]; then\n    step "15b')
    assert update_block < call < TEXT.index("# ── 16. Verification")
    # after the rollback watchdog is armed, i.e. after the app update is settled
    assert TEXT.index("Rollback watchdog armed") < call


def test_origins_config_uses_minimal_steps():
    conf = TEXT[TEXT.index("cat > /etc/apt/apt.conf.d/50unattended-upgrades"):]
    conf = conf[:conf.index("\nEOF\n")]
    assert 'label=Debian-Security' in conf
    assert 'Unattended-Upgrade::MinimalSteps "true";' in conf
    assert 'Automatic-Reboot-Time "03:00";' in conf
