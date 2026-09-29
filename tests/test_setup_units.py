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


# ── nginx doesn't log every request to the SD card ───────────────────────────

def test_nginx_does_not_log_every_request():
    block = TEXT[TEXT.index("cat > /etc/nginx/sites-available/default"):TEXT.index("NGINXEOF\nln -sf")]
    assert block.count("server {") == 2
    assert block.count("access_log off;") == 2


# ── Automatic apt runs (security updates ship through OTA instead) ───────────

def test_unattended_upgrades_not_installed_or_configured_on():
    install = TEXT.split("apt-get install -y", 1)[1].split("\n    ok ", 1)[0]
    assert "unattended-upgrades" not in install
    assert 'Unattended-Upgrade "1"' not in TEXT
    assert 'Update-Package-Lists "1"' not in TEXT


def test_apt_timers_are_stopped_before_they_are_masked():
    stop = TEXT.index("systemctl stop apt-daily.timer apt-daily-upgrade.timer")
    mask = TEXT.index("systemctl mask apt-daily.timer apt-daily-upgrade.timer")
    reset = TEXT.index("systemctl reset-failed apt-daily.timer apt-daily-upgrade.timer")
    assert stop < mask < reset


def test_apt_disable_runs_in_update_mode_too():
    """Existing devices only ever get it through OTA (--update), so it must
    sit in 2b (runs in both modes), not in a setup-only block."""
    at = TEXT.index("systemctl mask apt-daily.timer apt-daily-upgrade.timer")
    assert TEXT.index("# ── 2b. SD card longevity") < at < TEXT.index("# ── 3.")


def test_apt_upgrade_service_is_never_stopped():
    """Stopping it could interrupt a dpkg run in progress mid-OTA."""
    assert "stop apt-daily-upgrade.service" not in TEXT


def test_pigpiod_restarts_only_when_its_drop_in_changed():
    """A pigpiod restart also restarts schoolair-flower (PartOf) and costs a re-home
    if the flower is moving, so an update that doesn't touch pigpiod leaves it alone."""
    assert 'cmp -s "${DEPLOY_DIR}/pigpiod-early.conf"' in TEXT
    assert TEXT.index("PIGPIOD_CONF_CHANGED=1") < TEXT.index("install_with_backup \"${DEPLOY_DIR}/pigpiod-early.conf\"")
    restart = TEXT.index("systemctl restart pigpiod.service")
    guard = TEXT.rindex('if [ "$PIGPIOD_CONF_CHANGED" = 1 ]', 0, restart)
    assert "wait_for_flower_move" in TEXT[guard:restart]   # see test_flower_install.py
    assert TEXT.count("systemctl restart pigpiod") == 1
