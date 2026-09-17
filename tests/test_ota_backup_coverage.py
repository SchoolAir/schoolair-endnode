"""tests/test_ota_backup_coverage.py

Enforces that schoolair_setup.sh never writes to a path the OTA rollback
mechanism protects except through install_with_backup() /
install_dir_with_backup() (see schoolair_setup.sh's own comment above those
functions, and schoolair_rollback.sh). A direct cp/install to one of these
paths bypasses the backup-before-replace guarantee the rollback watchdog
depends on — silently reintroducing the exact "broken update, no way back"
failure mode the mechanism exists to prevent.

This is a regex-based heuristic over the raw script text, not a real shell
parser — good enough to catch "someone added a plain cp instead of using
the helper," not a guarantee against every way bash can write a file.
"""

import re
from pathlib import Path

SETUP_SCRIPT = Path(__file__).parents[1] / "schoolair_setup.sh"

HELPER_NAMES = ("install_with_backup", "install_dir_with_backup")

PROTECTED_PATH_PATTERNS = [
    r"\$SCHOOLAIR_DIR",
    r"\$\{SCHOOLAIR_DIR\}",
    r"\$ADMIN_HOME",
    r"\$\{ADMIN_HOME\}",
    r"/etc/systemd/system/",
    r"/usr/local/bin/schoolair-update",
    r"\$I2C_DIR",
    r"\$\{I2C_DIR\}",
]


def _strip_helper_function_bodies(text: str) -> str:
    """Remove install_with_backup()/install_dir_with_backup()'s own bodies —
    they're the only place allowed to cp directly, that's their entire job."""
    for name in HELPER_NAMES:
        text = re.sub(
            rf"^{name}\(\)\s*\{{.*?^\}}\s*$",
            "",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
    return text


def test_no_direct_cp_to_protected_paths_outside_backup_helpers():
    text = SETUP_SCRIPT.read_text()
    text = _strip_helper_function_bodies(text)

    violations = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not re.match(r"^(cp|install)\b", stripped):
            continue
        # .env preservation is a different, intentional mechanism (keeping
        # the device's own local file across an update, not installing new
        # content that came from the update) — not what this test guards.
        if ".env" in line:
            continue
        if any(re.search(pat, line) for pat in PROTECTED_PATH_PATTERNS):
            violations.append(f"line {lineno}: {stripped}")

    assert not violations, (
        "Direct cp/install to a rollback-protected path found outside "
        "install_with_backup()/install_dir_with_backup() — use one of "
        "those helpers so a broken update can still be restored:\n"
        + "\n".join(violations)
    )


def test_backup_helpers_exist_and_are_called():
    text = SETUP_SCRIPT.read_text()
    for name in HELPER_NAMES:
        assert f"{name}()" in text, f"{name}() definition missing from schoolair_setup.sh"
        assert text.count(name) >= 2, (
            f"{name} is defined but never called — nothing is actually "
            "protected by it"
        )
