"""tests/test_ota_detach.py

The automatic OTA runs inside schoolair.service's cgroup (the app spawns
`sudo schoolair-update`), and the update restarts that very service, so systemd
killed the update partway (found live: no health check, no rollback watchdog
armed, netwatch not restarted). schoolair_setup.sh now re-runs `--update` as a
transient systemd unit. See the `schoolair-detach` block in the script.

The block is bash that only misbehaves on a device, so these tests extract the
REAL block from the script and run it against fake `systemd-run` / `journalctl`
binaries: the exact argument set, exit-code propagation, the fallbacks, and that
the live-output follower is cleaned up.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).parents[1] / "schoolair_setup.sh"
TEXT = SETUP.read_text()

FAKE_SYSTEMD_RUN = r"""#!/bin/bash
echo "$*" >> "$FAKE_LOG"
envs=(); cmd=()
for a in "$@"; do
  case "$a" in
    --setenv=*) envs+=("${a#--setenv=}") ;;
  esac
  if [[ ${#cmd[@]} -gt 0 || "$a" != --* ]]; then cmd+=("$a"); fi
done
if [[ "${cmd[0]}" == true ]]; then exit "${FAKE_PROBE_RC:-0}"; fi   # the "can we make transient units" probe
exec env "${envs[@]}" "${cmd[@]}"
"""

FAKE_JOURNALCTL = r"""#!/bin/bash
echo $$ > "$FAKE_JOURNALCTL_PID"
exec sleep 30
"""


def _block() -> str:
    m = re.search(r"# >>> schoolair-detach\n(.*?)# <<< schoolair-detach", TEXT, re.S)
    assert m, "schoolair-detach block markers missing from schoolair_setup.sh"
    return m.group(1)


@pytest.fixture
def harness(tmp_path):
    """Returns run(mode, **env) -> (CompletedProcess, fake_systemd_run_calls)."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "systemd-run").write_text(FAKE_SYSTEMD_RUN)
    (fakebin / "journalctl").write_text(FAKE_JOURNALCTL)
    for f in fakebin.iterdir():
        f.chmod(0o755)
    sysdir = tmp_path / "run-systemd-system"
    sysdir.mkdir()

    script = tmp_path / "harness.sh"
    # The block checks the real /run/systemd/system; point it at a temp dir so the
    # tests do not depend on the machine running them being booted with systemd.
    block = _block().replace("/run/systemd/system", str(sysdir))
    script.write_text(
        "set -euo pipefail\n"
        'MODE="${1:-setup}"\n'
        + block
        + 'echo "PAYLOAD RAN detached=${SCHOOLAIR_OTA_DETACHED:-no} admin=${ADMIN_USER:-} branch=${REPO_BRANCH:-}"\n'
        'exit "${PAYLOAD_RC:-0}"\n'
    )
    log = tmp_path / "systemd-run.log"
    jpid = tmp_path / "journalctl.pid"

    def run(mode="--update", path_prefix=None, **env):
        full_env = {
            "PATH": f"{path_prefix or fakebin}:/usr/bin:/bin",
            "FAKE_LOG": str(log),
            "FAKE_JOURNALCTL_PID": str(jpid),
            **env,
        }
        proc = subprocess.run(["bash", str(script), mode], env=full_env, capture_output=True, text=True, timeout=60)
        calls = log.read_text().splitlines() if log.exists() else []
        return proc, calls

    run.jpid = jpid
    run.tmp = tmp_path
    return run


def _payload_runs(proc):
    return proc.stdout.count("PAYLOAD RAN")


def test_update_reruns_itself_as_a_transient_unit_with_the_right_arguments(harness):
    proc, calls = harness("--update")
    assert proc.returncode == 0, proc.stderr
    real = [c for c in calls if not c.rstrip().endswith(" true")]           # ignore the probe
    assert len(real) == 1
    args = real[0]
    for wanted in ("--wait", "--collect", "--setenv=SCHOOLAIR_OTA_DETACHED=1", "/usr/bin/env bash", " --update"):
        assert wanted in args, args
    assert re.search(r"--unit=schoolair-ota-\d{8}-\d{6}\b", args)
    # --pipe would SIGPIPE the update the moment the app that started it is killed
    assert "--pipe" not in args
    assert _payload_runs(proc) == 1
    assert "detached=1" in proc.stdout


def test_a_probe_of_transient_unit_support_runs_first(harness):
    _, calls = harness("--update")
    assert calls[0].rstrip().endswith(" true")


def test_the_updates_exit_status_is_propagated(harness):
    proc, _ = harness("--update", PAYLOAD_RC="7")
    assert proc.returncode == 7                       # the app / canary script see the real result
    assert _payload_runs(proc) == 1


def test_the_detached_unit_does_not_detach_again(harness):
    proc, calls = harness("--update", SCHOOLAIR_OTA_DETACHED="1")
    assert calls == []                                # no systemd-run at all
    assert _payload_runs(proc) == 1
    assert "detached=1" in proc.stdout


def test_a_first_time_setup_is_never_detached(harness):
    proc, calls = harness("setup")
    assert calls == []
    assert _payload_runs(proc) == 1
    assert "detached=no" in proc.stdout


def test_admin_user_and_branch_reach_the_detached_unit(harness):
    proc, calls = harness("--update", ADMIN_USER="pi", REPO_BRANCH="dev")
    assert "--setenv=ADMIN_USER=pi" in "\n".join(calls)
    assert "--setenv=REPO_BRANCH=dev" in "\n".join(calls)
    assert "admin=pi branch=dev" in proc.stdout       # a clean unit environment must not lose them


def test_without_systemd_run_the_update_runs_in_place(harness):
    tools = harness.tmp / "tools"
    tools.mkdir()
    for name in ("env", "date", "readlink", "sleep", "kill", "bash"):
        (tools / name).symlink_to(shutil.which(name))
    (tools / "journalctl").write_text(FAKE_JOURNALCTL)
    (tools / "journalctl").chmod(0o755)
    proc = subprocess.run(
        ["bash", str(harness.tmp / "harness.sh"), "--update"],
        env={"PATH": str(tools), "FAKE_LOG": str(harness.tmp / "x"), "FAKE_JOURNALCTL_PID": str(harness.jpid)},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert _payload_runs(proc) == 1 and "detached=no" in proc.stdout


def test_if_transient_units_cannot_be_created_the_update_still_runs_in_place(harness):
    proc, calls = harness("--update", FAKE_PROBE_RC="1")
    assert proc.returncode == 0, proc.stderr
    assert "updating in place" in proc.stderr          # says so instead of silently degrading
    assert _payload_runs(proc) == 1 and "detached=no" in proc.stdout
    assert len(calls) == 1                             # only the probe; no second attempt


def test_the_live_output_follower_is_cleaned_up(harness):
    proc, _ = harness("--update")
    assert proc.returncode == 0
    pid = int(harness.jpid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                # journalctl -f must not be left running


def test_detach_block_comes_before_anything_with_side_effects():
    """Before log redirection and the failure trap: it re-execs, so nothing may have run yet."""
    marker_end = TEXT.index("# <<< schoolair-detach")
    assert marker_end < TEXT.index('exec > >(tee -a "$LOG_FILE")')
    assert marker_end < TEXT.index("trap _rollback_on_failure EXIT")
    assert TEXT.index("# >>> schoolair-detach") > TEXT.index('MODE="${1:-setup}"')


def test_script_is_still_valid_bash():
    subprocess.run(["bash", "-n", str(SETUP)], check=True)
