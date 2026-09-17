"""tests/test_ota_rollback.py

System test for schoolair_rollback.sh — the actual bash script, invoked as
a real subprocess, not a reimplementation of its logic in Python. That
distinction matters: a Python re-implementation could silently drift from
what the script actually does and still pass. This runs the real thing.

Sandboxing, so nothing here ever touches real system paths or needs root:
  - BACKUP_ROOT / BACKUP_MANIFEST / PENDING_FILE are redirected into
    tmp_path via env vars the script reads (SCHOOLAIR_BACKUP_ROOT etc.),
    defaulting to the real production paths when unset — production
    behavior is unchanged.
  - `systemctl` is a fake executable prepended onto PATH, so
    "is a service active" and "restart"/"daemon-reload" are fully
    controlled by each test instead of depending on real systemd units
    (sen6x.service etc. don't even exist on a dev machine).
"""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROLLBACK_SCRIPT = Path(__file__).parents[1] / "schoolair_rollback.sh"

WATCHED_SERVICES = ("sen6x.service", "schoolair.service", "schoolair-netwatch.service")


def _make_fake_systemctl(
    bin_dir: Path,
    unhealthy: tuple[str, ...] = (),
    restarts: dict[str, int] | None = None,
) -> None:
    """A systemctl stand-in:
      - `is-active --quiet <svc>` fails for anything listed in `unhealthy`,
        succeeds for everything else.
      - `show <svc> -p NRestarts --value` prints `restarts[<svc>]` (0 if
        unlisted) — this is what caught the real bug: a service can be
        "active" (is-active succeeds) while its restart count has still
        climbed past the baseline, meaning it crash-looped and only
        happens to be up again at the moment it's checked.
      - every other subcommand (daemon-reload, restart, ...) always succeeds.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "systemctl"
    unhealthy_list = " ".join(unhealthy)
    restarts = restarts or {}
    restart_cases = "\n".join(
        f'    "{svc}") echo {count}; exit 0 ;;' for svc, count in restarts.items()
    )
    script.write_text(f"""#!/usr/bin/env bash
if [ "$1" = "is-active" ]; then
    svc="${{@: -1}}"
    for bad in {unhealthy_list}; do
        [ "$svc" = "$bad" ] && exit 1
    done
    exit 0
fi
if [ "$1" = "show" ] && [ "$3" = "-p" ] && [ "$4" = "NRestarts" ]; then
    svc="$2"
    case "$svc" in
{restart_cases}
    *) echo 0; exit 0 ;;
    esac
fi
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Returns a helper object exposing paths + a run() method that invokes
    the real script with everything redirected into tmp_path."""
    backup_root = tmp_path / "backup-root"
    pending_file = tmp_path / "pending.json"
    bin_dir = tmp_path / "fakebin"
    _make_fake_systemctl(bin_dir)  # default: everything healthy

    env = os.environ.copy()
    env["SCHOOLAIR_BACKUP_ROOT"] = str(backup_root)
    env["SCHOOLAIR_BACKUP_MANIFEST"] = str(backup_root) + ".manifest"
    env["SCHOOLAIR_PENDING_FILE"] = str(pending_file)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    class Sandbox:
        def __init__(self):
            self.tmp_path = tmp_path
            self.backup_root = backup_root
            self.manifest = Path(str(backup_root) + ".manifest")
            self.pending_file = pending_file
            self.env = env

        def set_unhealthy(self, *services):
            _make_fake_systemctl(bin_dir, unhealthy=services)

        def set_restarts(self, **counts):
            """e.g. set_restarts(**{"schoolair.service": 3}) — simulates a
            service that's currently is-active (crashed, auto-restarted,
            and happens to be up again) but has restarted since the
            baseline was recorded."""
            _make_fake_systemctl(bin_dir, restarts=counts)

        def back_up(self, real_path: Path, content: str):
            """Simulates what install_with_backup() in schoolair_setup.sh
            does: snapshot real_path's *current* content under
            backup_root (mirrored by absolute path) and record it in the
            manifest, before the caller goes on to overwrite real_path
            with new (possibly broken) content."""
            mirrored = self.backup_root / str(real_path).lstrip("/")
            mirrored.parent.mkdir(parents=True, exist_ok=True)
            mirrored.write_text(content)
            with self.manifest.open("a") as f:
                f.write(f"{real_path}\n")

        def write_pending(self, deadline_offset_seconds: float, restart_baseline: dict | None = None):
            self.pending_file.parent.mkdir(parents=True, exist_ok=True)
            self.pending_file.write_text(json.dumps({
                "from_version": "1.0.0",
                "to_version": "1.0.1",
                "deadline": time.time() + deadline_offset_seconds,
                "restart_baseline": restart_baseline or {},
            }))

        def run(self, *args) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["bash", str(ROLLBACK_SCRIPT), *args],
                env=self.env, capture_output=True, text=True, timeout=10,
            )

    return Sandbox()


# ── --restore-now ────────────────────────────────────────────────────────────

def test_restore_now_restores_backed_up_file_content(sandbox):
    target = sandbox.tmp_path / "app" / "version.txt"
    target.parent.mkdir(parents=True)
    sandbox.back_up(target, "GOOD (pre-update)")
    target.write_text("BROKEN (post-update)")  # simulates the bad update's result

    result = sandbox.run("--restore-now")

    assert result.returncode == 0, result.stderr
    assert target.read_text() == "GOOD (pre-update)"


def test_restore_now_restores_a_backed_up_directory(sandbox):
    target = sandbox.tmp_path / "app" / "schoolair"
    target.mkdir(parents=True)
    (target / "main.py").write_text("old code")
    # Directories need their own backup path (mirror the whole tree,
    # matching what install_dir_with_backup() does in schoolair_setup.sh) —
    # sandbox.back_up() only handles single files.
    mirrored = sandbox.backup_root / str(target).lstrip("/")
    mirrored.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copytree(target, mirrored)
    with sandbox.manifest.open("a") as f:
        f.write(f"{target}\n")

    (target / "main.py").write_text("BROKEN new code")
    (target / "new_file_from_bad_update.py").write_text("should be removed on restore")

    result = sandbox.run("--restore-now")

    assert result.returncode == 0, result.stderr
    assert (target / "main.py").read_text() == "old code"
    assert not (target / "new_file_from_bad_update.py").exists()


def test_restore_now_clears_pending_file(sandbox):
    sandbox.write_pending(deadline_offset_seconds=3600)
    result = sandbox.run("--restore-now")
    assert result.returncode == 0, result.stderr
    assert not sandbox.pending_file.exists()


def test_restore_now_is_safe_with_no_manifest(sandbox):
    """Nothing was ever backed up (e.g. failure before the first install_*
    call) — must not error out."""
    result = sandbox.run("--restore-now")
    assert result.returncode == 0, result.stderr


# ── --check ───────────────────────────────────────────────────────────────────

def test_check_does_nothing_when_no_update_pending(sandbox):
    target = sandbox.tmp_path / "app" / "version.txt"
    target.parent.mkdir(parents=True)
    sandbox.back_up(target, "GOOD")
    target.write_text("CURRENT")

    result = sandbox.run("--check")

    assert result.returncode == 0, result.stderr
    assert target.read_text() == "CURRENT"  # untouched — nothing to confirm/revert


def test_check_waits_when_healthy_and_within_deadline(sandbox):
    target = sandbox.tmp_path / "app" / "version.txt"
    target.parent.mkdir(parents=True)
    sandbox.back_up(target, "GOOD")
    target.write_text("CURRENT")
    sandbox.write_pending(deadline_offset_seconds=3600)  # far in the future

    result = sandbox.run("--check")

    assert result.returncode == 0, result.stderr
    assert sandbox.pending_file.exists(), "should still be waiting for confirmation"
    assert target.read_text() == "CURRENT"


def test_check_rolls_back_when_restart_count_exceeds_baseline_even_if_active(sandbox):
    """The exact bug found in a real live-fire test on .169: a Type=simple
    service reports "active" the instant its process is spawned, even if
    it crashes moments later (e.g. partway through imports). A service
    crash-looping on a ~10s Restart= cycle can land squarely in that
    "active" window at the exact instant a single is-active check samples
    it — is-active alone said everything was fine while the service was
    actually crash-looping for real. Comparing against the restart-count
    baseline recorded at update time is what actually catches it."""
    target = sandbox.tmp_path / "app" / "version.txt"
    target.parent.mkdir(parents=True)
    sandbox.back_up(target, "GOOD")
    target.write_text("CURRENT")
    sandbox.write_pending(
        deadline_offset_seconds=3600,
        restart_baseline={"schoolair.service": 0},
    )
    # systemctl reports it as active (no service listed in set_unhealthy)
    # AND with a restart count that has climbed past the baseline —
    # exactly what a crash-looping-but-currently-up service looks like.
    sandbox.set_restarts(**{"schoolair.service": 3})

    result = sandbox.run("--check")

    assert result.returncode == 0, result.stderr
    assert target.read_text() == "GOOD", "restart-count regression should have triggered rollback"
    assert not sandbox.pending_file.exists()


def test_check_waits_when_restart_count_matches_baseline(sandbox):
    """No false positive: a service that legitimately had prior restarts
    before the update (reflected in its own baseline) shouldn't trigger a
    rollback just for staying at that same count."""
    target = sandbox.tmp_path / "app" / "version.txt"
    target.parent.mkdir(parents=True)
    sandbox.back_up(target, "GOOD")
    target.write_text("CURRENT")
    sandbox.write_pending(
        deadline_offset_seconds=3600,
        restart_baseline={"schoolair.service": 2},
    )
    sandbox.set_restarts(**{"schoolair.service": 2})  # unchanged since baseline

    result = sandbox.run("--check")

    assert result.returncode == 0, result.stderr
    assert sandbox.pending_file.exists(), "should still be waiting — nothing actually changed"
    assert target.read_text() == "CURRENT"


def test_check_rolls_back_immediately_when_a_service_is_unhealthy(sandbox):
    """The case that matters most: don't wait for the deadline if it's
    already obvious something's wrong."""
    target = sandbox.tmp_path / "app" / "version.txt"
    target.parent.mkdir(parents=True)
    sandbox.back_up(target, "GOOD")
    target.write_text("CURRENT")
    sandbox.write_pending(deadline_offset_seconds=3600)  # deadline far away —
    sandbox.set_unhealthy("schoolair.service")           # health check should still catch it

    result = sandbox.run("--check")

    assert result.returncode == 0, result.stderr
    assert target.read_text() == "GOOD", "should have rolled back despite the far-off deadline"
    assert not sandbox.pending_file.exists()


def test_check_rolls_back_once_deadline_has_passed(sandbox):
    target = sandbox.tmp_path / "app" / "version.txt"
    target.parent.mkdir(parents=True)
    sandbox.back_up(target, "GOOD")
    target.write_text("CURRENT")
    sandbox.write_pending(deadline_offset_seconds=-10)  # already expired

    result = sandbox.run("--check")

    assert result.returncode == 0, result.stderr
    assert target.read_text() == "GOOD"
    assert not sandbox.pending_file.exists()


def test_check_all_watched_services_healthy_matches_real_service_names(sandbox):
    """Sanity check that the fake systemctl's service-name matching lines
    up with what the script actually watches, so the other tests aren't
    accidentally passing for the wrong reason."""
    for svc in WATCHED_SERVICES:
        sandbox.set_unhealthy(svc)
        target = sandbox.tmp_path / f"app-{svc}" / "version.txt"
        target.parent.mkdir(parents=True)
        sandbox.back_up(target, "GOOD")
        target.write_text("CURRENT")
        sandbox.write_pending(deadline_offset_seconds=3600)

        result = sandbox.run("--check")

        assert result.returncode == 0, result.stderr
        assert target.read_text() == "GOOD", f"{svc} unhealthy should have triggered rollback"
        sandbox.set_unhealthy()  # reset for next iteration
