"""schoolair_setup.sh step 2b: journald restarts only when its settings change.

A restart on every update cut the running flower off from the journal (bench
.242, 2026-11-27), so the block is run here for real in a sandbox, with stub
systemctl / systemd-tmpfiles / journalctl, for: first install, an unchanged
re-run, and a changed setting."""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = (ROOT / "schoolair_setup.sh").read_text()


def _block(tmp: Path) -> str:
    m = re.search(r"^mkdir -p /etc/systemd/journald\.conf\.d.*?^journalctl --flush.*?$", SETUP, re.S | re.M)
    assert m, "journald block not found in schoolair_setup.sh"
    return m.group(0).replace("/etc/systemd/journald.conf.d", str(tmp / "conf")) \
                     .replace("/var/log/journal", str(tmp / "log"))


def _run(tmp: Path) -> tuple:
    bin_ = tmp / "bin"
    bin_.mkdir(exist_ok=True)
    for name in ("systemctl", "systemd-tmpfiles", "journalctl"):
        f = bin_ / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp}/calls"\n')
        f.chmod(0o755)
    (tmp / "calls").write_text("")
    script = f'set -euo pipefail\n{_block(tmp)}\necho "restarted=${{JOURNALD_RESTARTED:-0}}"\n'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip(), (tmp / "calls").read_text()


def test_first_install_restarts_journald(tmp_path):
    out, calls = _run(tmp_path)
    assert out == "restarted=1"
    assert "systemctl restart systemd-journald" in calls
    assert sorted(os.listdir(tmp_path / "conf")) == ["90-schoolair.conf"]


def test_unchanged_rerun_leaves_journald_alone(tmp_path):
    _run(tmp_path)
    out, calls = _run(tmp_path)
    assert out == "restarted=0"
    assert "restart systemd-journald" not in calls
    assert "journalctl --flush" in calls                       # still flushed, harmless
    assert sorted(os.listdir(tmp_path / "conf")) == ["90-schoolair.conf"]   # no .new left


def test_changed_setting_or_old_dropin_restarts(tmp_path):
    _run(tmp_path)
    conf = tmp_path / "conf" / "90-schoolair.conf"
    conf.write_text(conf.read_text().replace("64M", "65M"))
    assert _run(tmp_path)[0] == "restarted=1"
    (tmp_path / "conf" / "00-schoolair.conf").write_text("[Journal]\n")
    assert _run(tmp_path)[0] == "restarted=1"
    assert not (tmp_path / "conf" / "00-schoolair.conf").exists()


def test_flower_restarts_after_a_journald_restart():
    assert '[ "$FLOWER_CHANGED" = 1 ] || [ "${JOURNALD_RESTARTED:-0}" = 1 ]' in SETUP
