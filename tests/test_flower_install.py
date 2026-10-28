"""tests/test_flower_install.py

How the endnode installs the wilting flower (SchoolAir/Flower-End-node):
detect_flower.sh finds the flower's driver board, schoolair_setup.sh installs
the flower bundled in flower/ (scripts/vendor_flower.sh) on indoor units with backups, and
a drop-in lets the service run only where a flower is fitted and pigpiod exists.

detect_flower.sh and schoolair-redetect are run for real with a fake `pinctrl`
and `systemctl`; the rest are static checks on the scripts, in the style of
test_setup_units.py.
"""

import hashlib
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SETUP = (ROOT / "schoolair_setup.sh").read_text()


# ── detect_flower.sh ──────────────────────────────────────────────────────────

PINS = ("17", "27", "22", "23")


def _detect(tmp_path, levels: dict | None, marker_exists: bool = False,
            flower_running: bool = False, outputs: tuple = ()):
    """Run detect_flower.sh with a fake pinctrl. `levels` maps a pin to what it
    reads with the pull-up on ("lo"/"hi"); None means no pinctrl at all."""
    marker = tmp_path / "schoolair-flower-fitted"
    if marker_exists:
        marker.touch()
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"#!/bin/sh\nexit {0 if flower_running else 3}\n")
    systemctl.chmod(0o755)
    env = {**os.environ, "FLOWER_MARKER": str(marker), "SYSTEMCTL": str(systemctl)}
    if levels is None:
        env["PINCTRL"] = str(tmp_path / "no-such-pinctrl")
    else:
        cases = "".join(
            f'  {p}) mode={"output" if p in outputs else "input"}; level={lv} ;;\n'
            for p, lv in levels.items())
        fake = tmp_path / "pinctrl"
        fake.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{tmp_path}/calls"\n'
            '[ "$1" = get ] || exit 0\n'
            'case "$2" in\n' + cases + "esac\n"
            'echo "$2: ip    pu | $level // GPIO$2 = $mode"\n')
        fake.chmod(0o755)
        env["PINCTRL"] = str(fake)
    r = subprocess.run(["bash", str(ROOT / "detect_flower.sh")], env=env,
                       capture_output=True, text=True, timeout=10)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return r.returncode, marker.exists(), calls, r.stdout


def test_driver_board_on_all_four_pins_marks_the_flower_fitted(tmp_path):
    rc, marked, calls, _ = _detect(tmp_path, dict.fromkeys(PINS, "lo"))
    assert (rc, marked) == (0, True)
    for p in PINS:
        # the pull-up goes on before the read, the pull-down after it
        up = calls.index(f"set {p} ip pu")
        assert calls[up + 1] == f"get {p}" and calls[up + 2] == f"set {p} ip pd"


def test_no_board_no_flower_and_a_stale_marker_goes(tmp_path):
    """The marker follows the hardware: a flower taken out is forgotten."""
    assert _detect(tmp_path, dict.fromkeys(PINS, "hi"), marker_exists=True)[:2] == (1, False)


def test_no_jumper_pin_is_read(tmp_path):
    """The GPIO26 strap is retired: the board itself is the evidence."""
    calls = _detect(tmp_path, dict.fromkeys(PINS, "lo"))[2]
    assert not any(c.split()[1] == "26" for c in calls)


def test_a_loose_wire_is_reported_and_changes_nothing(tmp_path):
    levels = {**dict.fromkeys(PINS, "lo"), "22": "hi"}
    rc, marked, _, out = _detect(tmp_path, levels)
    assert (rc, marked) == (2, False)
    assert "GPIO22" in out
    assert _detect(tmp_path, levels, marker_exists=True)[:2] == (0, True)


def test_pins_are_not_touched_while_the_flower_service_runs(tmp_path):
    """It holds them as outputs; an input mid-move would lose the position."""
    rc, marked, calls, _ = _detect(tmp_path, dict.fromkeys(PINS, "hi"),
                                   marker_exists=True, flower_running=True)
    assert (rc, marked) == (0, True)
    assert not any(c.startswith("set") for c in calls)


def test_pins_driven_as_outputs_are_not_probed(tmp_path):
    rc, marked, calls, _ = _detect(tmp_path, dict.fromkeys(PINS, "lo"), outputs=("27",))
    assert (rc, marked) == (2, False)
    assert not any(c.startswith("set") for c in calls)


def test_without_pinctrl_the_answer_is_unknown(tmp_path):
    assert _detect(tmp_path, None)[:2] == (2, False)
    assert _detect(tmp_path, None, marker_exists=True)[:2] == (0, True)


# ── schoolair-redetect ────────────────────────────────────────────────────────

def _redetect(tmp_path, *args, unit="indoor", flower_running=True, detect_rc=0):
    sa = tmp_path / "schoolair"
    sa.mkdir()
    (sa / "detect_flower.sh").write_text(f'echo detect >> "{tmp_path}/log"\nexit {detect_rc}\n')
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{tmp_path}/log"\n'
        f'[ "$1" = is-active ] && exit {0 if flower_running else 3}\n'
        "exit 0\n")
    systemctl.chmod(0o755)
    (tmp_path / "unit-type").write_text(unit)
    done = tmp_path / "lib" / "redetect-done"
    env = {**os.environ, "SCHOOLAIR_DIR": str(sa), "DONE_FILE": str(done),
           "UNIT_TYPE_FILE": str(tmp_path / "unit-type"), "SYSTEMCTL": str(systemctl),
           "FLOWER_MOVING_FLAG": str(tmp_path / "moving")}
    r = subprocess.run(["bash", str(ROOT / "schoolair-redetect"), *args], env=env,
                       capture_output=True, text=True, timeout=10)
    log = (tmp_path / "log").read_text().splitlines() if (tmp_path / "log").exists() else []
    return r.returncode, log, done.read_text().strip() if done.exists() else None


def test_redetect_frees_the_pins_probes_then_restarts_the_flower(tmp_path):
    rc, log, done = _redetect(tmp_path, "req-42")
    assert rc == 0 and done == "req-42"
    assert log.index("stop schoolair-flower.service") < log.index("detect") \
        < log.index("start schoolair-flower.service")


def test_redetect_leaves_a_removed_flower_stopped(tmp_path):
    log = _redetect(tmp_path, detect_rc=1)[1]
    assert "stop schoolair-flower.service" in log
    assert "start schoolair-flower.service" not in log


def test_redetect_on_an_outdoor_unit_only_records_the_request(tmp_path):
    rc, log, done = _redetect(tmp_path, "r1", unit="outdoor")
    assert (rc, done) == (0, "r1")
    assert "detect" not in log


def test_redetect_refuses_a_request_id_it_cannot_safely_echo(tmp_path):
    rc, log, done = _redetect(tmp_path, "x; rm -rf /")
    assert rc == 2 and done is None and log == []


def test_redetect_is_installed_root_owned_and_allowed_by_sudoers():
    assert 'install_with_backup "${SCHOOLAIR_DIR}/schoolair-redetect" /usr/local/bin/schoolair-redetect' in SETUP
    assert "chown root:root /usr/local/bin/schoolair-redetect" in SETUP
    assert "NOPASSWD: /usr/local/bin/schoolair-redetect" in SETUP
    assert os.access(ROOT / "schoolair-redetect", os.X_OK)


# ── Detection at every boot ───────────────────────────────────────────────────

def test_boot_detection_runs_before_the_flower_and_is_installed():
    unit = (ROOT / "deploy/schoolair-detect-hardware.service").read_text()
    assert "Before=schoolair-flower.service" in unit
    assert "detect_flower.sh" in unit
    assert "SuccessExitStatus=1 2" in unit
    # the LED starts at sysinit and must not wait for it
    assert "schoolair-led" not in unit.split("[Service]")[0].split("Before=")[1].splitlines()[0]
    assert "schoolair-detect-hardware.service" in SETUP[SETUP.index("for svc in sen6x.service"):]
    assert "systemctl enable schoolair-detect-hardware.service" in SETUP


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
    restart = SETUP.index("systemctl restart schoolair-flower.service")
    guard = SETUP.rindex('if [ "$FLOWER_CHANGED" = 1 ]', 0, restart)
    assert "wait_for_flower_move" in SETUP[guard:restart]


def test_update_starts_a_newly_installed_flower():
    """try-restart skips a unit that isn't running, so the update that first
    installed the flower left it stopped until the next reboot."""
    assert "try-restart schoolair-flower" not in SETUP


def test_wait_for_flower_move_outlasts_a_blind_home():
    body = SETUP[SETUP.index("wait_for_flower_move() {"):]
    body = body[:body.index("\n}\n")]
    assert "/run/schoolair-flower/moving" in body
    assert int(re.search(r"seq 1 (\d+)", body).group(1)) >= 150


# ── Around the service ────────────────────────────────────────────────────────

def test_drop_in_requires_the_marker_and_pigpiod():
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
    assert fb.index("/home/admin/schoolair/detect_unit_type.sh") < fb.index("/home/admin/schoolair/detect_flower.sh ||")


def test_the_led_waits_for_pigpiod_instead_of_crash_looping():
    """Before pigpiod is installed the LED is skipped, then started by the installer."""
    led = (ROOT / "deploy/schoolair-led.service").read_text()
    assert "ConditionPathExists=/var/lib/schoolair-pigpio-installed" in led
    unit = (ROOT / "deploy/schoolair-pigpio-setup.service").read_text()
    assert unit.index("touch /var/lib/schoolair-pigpio-installed") < unit.index("systemctl start schoolair-led.service")
