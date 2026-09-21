"""tests/test_setup_units.py

Static checks on how schoolair_setup.sh treats systemd units it masks.
"""

from pathlib import Path

TEXT = (Path(__file__).parents[1] / "schoolair_setup.sh").read_text()


def test_e2scrub_units_are_stopped_before_they_are_masked():
    """Masking a running e2scrub_all.timer leaves it 'failed' at the next daemon-reload and the
    whole system 'degraded' until reboot. `mask --now` does not avoid it (verified on a device);
    only an explicit stop first does."""
    stop = TEXT.index("systemctl stop e2scrub_all.timer e2scrub_reap.service")
    mask = TEXT.index("systemctl mask e2scrub_reap.service e2scrub_all.timer")
    assert stop < mask
    assert "systemctl mask --now" not in TEXT       # (the comment in the script mentions it; the command must not appear)


def test_stale_failed_state_of_the_masked_units_is_cleared():
    mask = TEXT.index("systemctl mask e2scrub_reap.service e2scrub_all.timer")
    assert "systemctl reset-failed e2scrub_all.timer e2scrub_reap.service" in TEXT[mask:mask + 300]
