"""tests/test_ota_device_config.py

An OTA update copies the new repo over ~/schoolair. config/settings.json and
config/criteria.json are tracked in the repo as defaults, but on a device they
hold its own state: the upload offset (drain_jitter_seconds), ntp_clock_corrected,
the server-pushed active window and alert criteria. Found on the bench unit's
2.3.12 update: the offset went from 45 s to a fresh random 93 s.

The deploy part of schoolair_setup.sh step 3 is run for real here, with the
script's own install_dir_with_backup(), in a scratch directory.
"""

import json
import re
import subprocess
from pathlib import Path

SETUP = (Path(__file__).parents[1] / "schoolair_setup.sh").read_text()


def _snippet() -> str:
    helper = re.search(r"^install_dir_with_backup\(\) \{.*?^\}", SETUP, re.M | re.S).group(0)
    deploy = SETUP[SETUP.index('DEVICE_CONFIG_KEEP="$(mktemp -d)"'):
                   SETUP.index('rm -rf "$DEVICE_CONFIG_KEEP"') + len('rm -rf "$DEVICE_CONFIG_KEEP"')]
    return f"set -e\nok() {{ :; }}\n{helper}\n{deploy}\n"


def _update(tmp_path, device_files: dict[str, str]):
    repo, app, backup = tmp_path / "repo", tmp_path / "app", tmp_path / "backup"
    (repo / "config").mkdir(parents=True)
    (repo / "config/settings.json").write_text('{"active_window": {"start": "07:00", "end": "16:00"}}')
    (repo / "config/criteria.json").write_text("[]")
    (repo / ".env.example").write_text("AUTH_TOKEN=\n")
    for rel, text in device_files.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text(text)
    env = {"SCHOOLAIR_DIR": str(app), "REPO_DIR": str(repo),
           "BACKUP_ROOT": str(backup), "BACKUP_MANIFEST": str(tmp_path / "manifest"),
           "PATH": "/usr/bin:/bin"}
    subprocess.run(["bash", "-c", _snippet()], env=env, check=True, timeout=20)
    return app, backup


def test_update_keeps_the_devices_settings_and_criteria(tmp_path):
    settings = {"active_window": {"start": "06:00", "end": "14:00"},
                "drain_jitter_seconds": 45, "ntp_clock_corrected": True}
    criteria = [{"metric": "co2", "threshold": "1000", "condition": "above", "severity": "warning"}]
    app, _ = _update(tmp_path, {".env": "AUTH_TOKEN=x\n",
                                "config/settings.json": json.dumps(settings),
                                "config/criteria.json": json.dumps(criteria)})
    assert json.loads((app / "config/settings.json").read_text()) == settings
    assert json.loads((app / "config/criteria.json").read_text()) == criteria
    assert (app / ".env").read_text() == "AUTH_TOKEN=x\n"


def test_the_backup_still_holds_them_for_a_rollback(tmp_path):
    app, backup = _update(tmp_path, {".env": "", "config/settings.json": '{"drain_jitter_seconds": 45}'})
    kept = backup / str(app).lstrip("/") / "config/settings.json"
    assert json.loads(kept.read_text()) == {"drain_jitter_seconds": 45}


def test_a_fresh_install_gets_the_repo_defaults(tmp_path):
    app, _ = _update(tmp_path, {})
    assert json.loads((app / "config/settings.json").read_text()) == {"active_window": {"start": "07:00", "end": "16:00"}}
    assert (app / ".env").read_text() == "AUTH_TOKEN=\n"
