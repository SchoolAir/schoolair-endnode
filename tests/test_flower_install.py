"""tests/test_flower_install.py

How the endnode installs the wilting flower (SchoolAir/Flower-End-node):
detect_flower.sh reads the dock's strap, schoolair_setup.sh installs the flower
bundled in flower/ (scripts/vendor_flower.sh) on indoor units with backups, and
a drop-in lets the service run only where a flower is fitted and pigpiod exists.

detect_flower.sh is run for real with a fake `pinctrl`; the rest are static
checks on the scripts, in the style of test_setup_units.py.
"""

import hashlib
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SETUP = (ROOT / "schoolair_setup.sh").read_text()


# ── detect_flower.sh ──────────────────────────────────────────────────────────

def _detect(tmp_path, level: str | None, marker_exists: bool = False):
    """Run detect_flower.sh with a fake pinctrl reporting `level` ("lo"/"hi"),
    or with no pinctrl at all when level is None."""
    marker = tmp_path / "schoolair-flower-fitted"
    if marker_exists:
        marker.touch()
    env = {**os.environ, "FLOWER_MARKER": str(marker)}
    if level is None:
        env["PINCTRL"] = str(tmp_path / "no-such-pinctrl")
    else:
        fake = tmp_path / "pinctrl"
        fake.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{tmp_path}/calls"\n'
            f'[ "$1" = get ] && echo "$2: ip    pu | {level} // GPIO$2 = input"\n'
            "exit 0\n")
        fake.chmod(0o755)
        env["PINCTRL"] = str(fake)
    r = subprocess.run(["bash", str(ROOT / "detect_flower.sh")], env=env,
                       capture_output=True, text=True, timeout=10)
    return r.returncode, marker.exists(), tmp_path


def test_strap_to_ground_marks_the_flower_fitted(tmp_path):
    rc, marked, d = _detect(tmp_path, "lo")
    assert (rc, marked) == (0, True)
    # the pull-up must be switched on before reading, or an open pin floats
    assert (d / "calls").read_text().splitlines()[0] == "set 26 ip pu"


def test_no_strap_no_flower(tmp_path):
    assert _detect(tmp_path, "hi")[:2] == (1, False)


def test_a_hand_set_marker_is_kept_without_a_strap(tmp_path):
    """The bench unit has no dock harness: it is marked by hand."""
    assert _detect(tmp_path, "hi", marker_exists=True)[:2] == (0, True)


def test_without_pinctrl_the_answer_is_unknown(tmp_path):
    assert _detect(tmp_path, None)[:2] == (2, False)


# ── schoolair_setup.sh ────────────────────────────────────────────────────────

def _flower_block() -> str:
    start = SETUP.index("# ── Wilting flower (indoor units)")
    return SETUP[start:SETUP.index("# An earlier revision drove GPIO24", start)]


def _source():
    lines = (ROOT / "flower/SOURCE").read_text().splitlines()
    commit = next(l.split()[1] for l in lines if l.startswith("commit "))
    sums = {l.split()[2]: l.split()[1] for l in lines if l.startswith("sha256 ")}
    return commit, sums


def test_bundled_flower_records_a_full_commit_sha():
    commit, _ = _source()
    assert re.fullmatch(r"[0-9a-f]{40}", commit)


def test_bundled_flower_files_match_their_recorded_checksums():
    """A hand edit in flower/ must not drift silently away from the pinned commit:
    change Flower-End-node and re-run scripts/vendor_flower.sh instead."""
    _, sums = _source()
    assert set(sums) == {"flower_service.py", "step.py", "schoolair-flower.service",
                         "calibration.example.json"}
    for name, expected in sums.items():
        assert hashlib.sha256((ROOT / "flower" / name).read_bytes()).hexdigest() == expected, name


def test_bundled_flower_scripts_are_executable():
    for name in ("flower_service.py", "step.py"):
        assert os.access(ROOT / "flower" / name, os.X_OK), name


def test_installing_the_flower_needs_no_network():
    """Flower-End-node is private and units have no GitHub credentials."""
    block = _flower_block()
    assert "git " not in block and "curl" not in block
    assert 'FLOWER_SRC="${SCHOOLAIR_DIR}/flower"' in block


def test_flower_is_installed_on_indoor_units_only_and_after_detection():
    block = _flower_block()
    assert "grep -qs indoor /etc/schoolair-unit-type" in block
    assert block.index("detect_flower.sh") < block.index("install_with_backup")


def test_flower_files_go_through_the_backup_helper():
    """So an OTA rollback restores the previous flower with everything else."""
    block = _flower_block()
    for dst in ('"${FLOWER_DIR}/${f}"', '"${FLOWER_DIR}/.installed-ref"',
                "/etc/systemd/system/schoolair-flower.service",
                "/etc/systemd/system/schoolair-flower.service.d/"):
        line = next(l for l in block.splitlines() if dst in l and not l.strip().startswith(("#", "mkdir", "if", "chmod")))
        assert line.strip().startswith("install_with_backup"), line


def test_calibration_is_created_but_never_replaced():
    block = _flower_block()
    assert 'if [ ! -f "${FLOWER_DIR}/calibration.json" ]; then' in block


def test_a_flower_problem_never_fails_the_update():
    assert "die " not in _flower_block()


def test_same_pin_is_not_reinstalled():
    """No needless restarts of the flower on updates that don't change it."""
    assert '.installed-ref" 2>/dev/null)" = "$FLOWER_REF" ]' in _flower_block()


def test_update_restarts_the_flower_only_when_it_changed_and_not_mid_move():
    restart = SETUP.index("systemctl try-restart schoolair-flower.service")
    guard = SETUP.rindex('if [ "$FLOWER_CHANGED" = 1 ]', 0, restart)
    assert "wait_for_flower_move" in SETUP[guard:restart]


def test_wait_for_flower_move_outlasts_a_blind_home():
    body = SETUP[SETUP.index("wait_for_flower_move() {"):]
    body = body[:body.index("\n}\n")]
    assert "/run/schoolair-flower/moving" in body
    assert int(re.search(r"seq 1 (\d+)", body).group(1)) >= 150


# ── Around the service ────────────────────────────────────────────────────────

def test_drop_in_requires_the_strap_and_pigpiod():
    conf = (ROOT / "deploy/schoolair-flower-fitted.conf").read_text()
    assert "ConditionPathExists=/etc/schoolair-flower-fitted" in conf
    assert "ConditionPathExists=/var/lib/schoolair-pigpio-installed" in conf


def test_pigpio_setup_starts_the_flower_once_pigpiod_is_there():
    unit = (ROOT / "deploy/schoolair-pigpio-setup.service").read_text()
    assert unit.index("touch /var/lib/schoolair-pigpio-installed") < unit.index("systemctl start schoolair-flower.service")


def test_golden_images_do_not_carry_the_marker():
    assert "rm -f /etc/schoolair-flower-fitted" in (ROOT / "prepare_image.sh").read_text()


def test_first_boot_detects_the_flower_after_the_unit_type():
    fb = (ROOT / "first_boot.sh").read_text()
    assert fb.rindex("configure_unit_type") < fb.index("/home/admin/schoolair/detect_flower.sh ||")
