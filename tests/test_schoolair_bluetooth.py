"""schoolair-bluetooth, run for real with a fake systemctl."""
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(tmp_path, *args, start_rc=0):
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/log"\nexit {start_rc}\n')
    systemctl.chmod(0o755)
    done, window = tmp_path / "lib" / "bluetooth-done", tmp_path / "ble-window"
    env = {**os.environ, "DONE_FILE": str(done), "WINDOW_FILE": str(window), "SYSTEMCTL": str(systemctl)}
    r = subprocess.run(["bash", str(ROOT / "schoolair-bluetooth"), *args], env=env,
                       capture_output=True, text=True, timeout=10)
    log = (tmp_path / "log").read_text().splitlines() if (tmp_path / "log").exists() else []
    read = lambda p: p.read_text().strip() if p.exists() else None
    return r.returncode, log, read(done), read(window)


def test_opens_the_window_starts_the_service_and_reports_done(tmp_path):
    before = time.time()
    rc, log, done, window = _run(tmp_path, "15", "bt-42")
    assert rc == 0 and log == ["start schoolair-ble.service"] and done == "bt-42"
    assert before + 15 * 60 - 1 <= int(window) <= time.time() + 15 * 60


def test_asking_again_moves_the_end(tmp_path):
    _run(tmp_path, "5")
    _, _, _, first = _run(tmp_path, "30")
    assert int(first) >= time.time() + 29 * 60


@pytest.mark.parametrize("args", [(), ("0",), ("61",), ("abc",), ("15", "../x"), ("15", "a" * 65)])
def test_refuses_bad_arguments(tmp_path, args):
    rc, log, done, window = _run(tmp_path, *args)
    assert rc == 2 and log == [] and done is None and window is None


def test_a_service_that_wont_start_leaves_no_window_and_nothing_done(tmp_path):
    rc, _, done, window = _run(tmp_path, "15", "bt-43", start_rc=1)
    assert rc == 1 and done is None and window is None
