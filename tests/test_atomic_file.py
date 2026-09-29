"""tests/test_atomic_file.py

write_atomic() must leave either the old content or the new one on disk —
never a truncated file — and keep the file's owner/mode (.env holds tokens).
"""

import os
import re
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from atomic_file import write_atomic

ROOT = Path(__file__).parents[1]


def test_creates_new_file(tmp_path):
    p = tmp_path / "settings.json"
    write_atomic(p, "{}\n")
    assert p.read_text() == "{}\n"


def test_replaces_existing_and_leaves_no_temp_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("AUTH_TOKEN=old\n")
    write_atomic(p, "AUTH_TOKEN=new\n")
    assert p.read_text() == "AUTH_TOKEN=new\n"
    assert sorted(x.name for x in tmp_path.iterdir()) == [".env"]


def test_keeps_the_existing_mode(tmp_path):
    p = tmp_path / ".env"
    p.write_text("AUTH_TOKEN=old\n")
    p.chmod(0o600)
    write_atomic(p, "AUTH_TOKEN=new\n")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_failure_before_rename_keeps_old_content(tmp_path):
    """The power-cut case: the new content never made it — old one intact,
    no temp file left behind."""
    p = tmp_path / ".env"
    p.write_text("AUTH_TOKEN=old\n")
    with patch("atomic_file.os.replace", side_effect=OSError("power cut")):
        with pytest.raises(OSError):
            write_atomic(p, "AUTH_TOKEN=new\n")
    assert p.read_text() == "AUTH_TOKEN=old\n"
    assert sorted(x.name for x in tmp_path.iterdir()) == [".env"]


def test_failure_while_writing_keeps_old_content(tmp_path):
    p = tmp_path / "criteria.json"
    p.write_text("[1]\n")
    with patch("atomic_file.os.fsync", side_effect=OSError("I/O error")):
        with pytest.raises(OSError):
            write_atomic(p, "[2]\n")
    assert p.read_text() == "[1]\n"


def test_fsyncs_the_data_before_renaming(tmp_path):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    with patch("atomic_file.os.fsync", side_effect=lambda fd: (order.append("fsync"), real_fsync(fd))), \
         patch("atomic_file.os.replace", side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))):
        write_atomic(tmp_path / "x", "data")
    assert order[:2] == ["fsync", "replace"]      # file data first, then the rename
    assert order[2:] == ["fsync"]                  # then the directory entry


@pytest.mark.parametrize("path", [
    "jobs/ingest.py", "registration_wizard/wizard.py", "device_identity.py",
    "setup.py", "migrate_token.py",
])
def test_no_in_place_writes_of_persistent_files(path):
    """Guard: persistent files go through write_atomic. What's left in these
    modules writing with open(..., "w") must be a /run (tmpfs) file."""
    src = (ROOT / path).read_text()
    assert ".write_text(" not in src
    for target in re.findall(r'open\(([^,]+), "w"\)', src):
        assert target.strip() in {"LED_STATE_FILE", "WIZARD_BUSY_FILE", "MISMATCH_FILE"}, target
