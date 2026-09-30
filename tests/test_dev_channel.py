"""tests/test_dev_channel.py

Dev-channel self-update (schoolair-dev-update): a unit whose
/etc/schoolair-channel says "dev" installs from the dev branch and checks it
hourly; stable units are untouched. The script runs for real here with a fake
curl (the dev branch's ingest.py) and a fake schoolair-update.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SETUP = (ROOT / "schoolair_setup.sh").read_text()


_n = 0


def _run(base, channel, installed, remote, rolled_back=(), moving=False):
    global _n
    _n += 1
    tmp_path = base / f"run{_n}"
    (tmp_path / "bin").mkdir(parents=True)
    (tmp_path / "bin/curl").write_text(f'#!/bin/sh\necho \'VERSION = "{remote}"\'\n')
    (tmp_path / "bin/schoolair-update").write_text('#!/bin/sh\necho UPDATE-RAN\n')
    for f in ("bin/curl", "bin/schoolair-update"):
        (tmp_path / f).chmod(0o755)
    (tmp_path / "ingest.py").write_text(f'VERSION = "{installed}"\n')
    if channel is not None:
        (tmp_path / "channel").write_text(channel + "\n")
    (tmp_path / "rolled-back").write_text("".join(v + "\n" for v in rolled_back))
    if moving:
        (tmp_path / "moving").touch()
    env = {**os.environ,
           "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
           "SCHOOLAIR_CHANNEL_FILE": str(tmp_path / "channel"),
           "SCHOOLAIR_ROLLED_BACK_FILE": str(tmp_path / "rolled-back"),
           "SCHOOLAIR_INSTALLED_INGEST": str(tmp_path / "ingest.py"),
           "SCHOOLAIR_MOVING_FLAG": str(tmp_path / "moving"),
           "SCHOOLAIR_UPDATE_CMD": str(tmp_path / "bin/schoolair-update")}
    r = subprocess.run(["bash", str(ROOT / "schoolair-dev-update")], env=env, capture_output=True, text=True, timeout=200)
    return r.returncode, r.stdout


def test_dev_unit_updates_when_dev_is_newer(tmp_path):
    rc, out = _run(tmp_path, "dev", "2.3.16", "2.3.17")
    assert rc == 0 and "UPDATE-RAN" in out


def test_dev_unit_leaves_a_same_or_older_version_alone(tmp_path):
    assert "UPDATE-RAN" not in _run(tmp_path, "dev", "2.3.16", "2.3.16")[1]
    assert "UPDATE-RAN" not in _run(tmp_path, "dev", "2.3.16", "2.3.9")[1]   # 9 < 16, not "9" > "1"


def test_stable_unit_never_self_updates(tmp_path):
    assert "UPDATE-RAN" not in _run(tmp_path, "stable", "2.3.16", "9.9.9")[1]
    assert "UPDATE-RAN" not in _run(tmp_path, None, "2.3.16", "9.9.9")[1]     # no channel file = stable


def test_a_rolled_back_version_is_not_retried(tmp_path):
    rc, out = _run(tmp_path, "dev", "2.3.16", "2.3.17", rolled_back=["2.3.17"])
    assert rc == 0 and "UPDATE-RAN" not in out and "rolled back" in out


def test_unreadable_versions_skip_quietly(tmp_path):
    rc, out = _run(tmp_path, "dev", "", "2.3.17")
    assert rc == 0 and "UPDATE-RAN" not in out


# ── The rest of the plumbing ──────────────────────────────────────────────────

def test_update_script_follows_the_channel():
    s = (ROOT / "schoolair-update").read_text()
    assert "/etc/schoolair-channel" in s
    assert 'export REPO_BRANCH="$BRANCH"' in s
    assert "${BRANCH}/schoolair_setup.sh" in s


def test_rollback_records_the_reverted_version():
    s = (ROOT / "schoolair_rollback.sh").read_text()
    body = s[s.index("do_restore_now() {"):s.index("do_check() {")]
    assert "to_version" in body and 'echo "$to_version" >> "$ROLLED_BACK_FILE"' in body
    assert body.index("ROLLED_BACK_FILE") < body.index("restore_from_backup")


def test_setup_installs_and_enables_the_hourly_check():
    assert 'install_with_backup "${SCHOOLAIR_DIR}/schoolair-dev-update" /usr/local/bin/schoolair-dev-update' in SETUP
    unit_loop = next(l for l in SETUP.splitlines() if l.startswith("for svc in") and "schoolair-update-watchdog.timer" in l)
    assert "schoolair-dev-update.timer" in unit_loop and "schoolair-dev-update.service" in unit_loop
    assert "systemctl enable schoolair-dev-update.timer" in SETUP


def test_dev_update_waits_for_the_flower():
    s = (ROOT / "schoolair-dev-update").read_text()
    assert "MOVING_FLAG" in s and "seq 1 150" in s
