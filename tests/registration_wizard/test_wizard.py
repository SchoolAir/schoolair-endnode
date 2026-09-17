"""tests/registration_wizard/test_wizard.py

Unit tests for registration_wizard/wizard.py: _has_token (reads AUTH_TOKEN
from the telemetry .env), the _idle_watchdog early-exit paths, and the
run_registration / index() "keep Wi-Fi creds on registration-only failure"
retry flow.

The conftest.py in this directory adds registration_wizard/ to sys.path
so that wizard.py's bare `from config import ...` resolves correctly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import wizard


# ── _has_token ─────────────────────────────────────────────────────────────────

def test_has_token_true_when_env_file_has_token(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SERVER_URL=http://example.com\nAUTH_TOKEN=abc123\n")
    monkeypatch.setattr(wizard, "PI_MAIN_ENV_PATH", str(env))
    assert wizard._has_token() is True


def test_has_token_false_when_token_value_is_empty(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AUTH_TOKEN=\n")
    monkeypatch.setattr(wizard, "PI_MAIN_ENV_PATH", str(env))
    assert wizard._has_token() is False


def test_has_token_false_when_token_is_only_whitespace(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AUTH_TOKEN=   \n")
    monkeypatch.setattr(wizard, "PI_MAIN_ENV_PATH", str(env))
    assert wizard._has_token() is False


def test_has_token_false_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "PI_MAIN_ENV_PATH", str(tmp_path / "missing.env"))
    assert wizard._has_token() is False


def test_has_token_true_with_no_trailing_newline(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AUTH_TOKEN=tok456")
    monkeypatch.setattr(wizard, "PI_MAIN_ENV_PATH", str(env))
    assert wizard._has_token() is True


# ── _idle_watchdog early exits ─────────────────────────────────────────────────

async def test_idle_watchdog_returns_immediately_in_ap_mode():
    """In AP mode the wizard self-shuts after registration; watchdog steps aside."""
    with patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("wizard._ap_is_active", new=AsyncMock(return_value=True)):
        await wizard._idle_watchdog()   # must return without entering the while loop


async def test_idle_watchdog_returns_immediately_without_token():
    """On LAN without a token (first registration), watchdog must stay out of the way."""
    with patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("wizard._ap_is_active", new=AsyncMock(return_value=False)), \
         patch("wizard._has_token", return_value=False):
        await wizard._idle_watchdog()   # must return without entering the while loop


# ── run_registration / index(): keep Wi-Fi creds on registration-only failure ──
#
# Wi-Fi + token are proven good by the time registration is attempted, so a
# pure registration failure should keep the network instead of erasing it —
# see wizard._last_good_wifi. These tests exercise the exact deployed
# run_registration/index() code with the nmcli/HTTP-calling helpers mocked
# out, rather than a live device: hitting the real failure branches on a
# single-radio Pi actually deactivates its client Wi-Fi connection (AP mode
# takes the only radio), so this is the only way to test the failure paths
# without severing SSH access to the device under test.

@pytest.fixture
def net(monkeypatch, tmp_path):
    """Mock every nmcli/HTTP-touching helper run_registration calls, and
    reset the module-level session/state dicts so tests don't leak into
    each other."""
    monkeypatch.setattr(wizard, "_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(wizard, "_setup_client_profile", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(wizard, "_wait_for_ip", AsyncMock(return_value=True))
    monkeypatch.setattr(wizard, "_validate_token", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(wizard, "_post_heartbeat", AsyncMock(return_value=(True, "", "dev-tok")))
    monkeypatch.setattr(wizard, "_commit_wifi_profile",
                         AsyncMock(return_value="school-air-saved-TestNet"))
    monkeypatch.setattr(wizard, "_revert_to_ap", AsyncMock())
    monkeypatch.setattr(wizard, "_delayed_shutdown", AsyncMock())
    monkeypatch.setattr(wizard, "write_status", MagicMock())
    monkeypatch.setattr(wizard, "write_error", MagicMock())
    monkeypatch.setattr(wizard, "_write_auth_token", MagicMock())
    monkeypatch.setattr(wizard, "_write_new_auth_token", MagicMock())
    monkeypatch.setattr(wizard, "_get_mac_address", MagicMock(return_value="AA:BB:CC"))
    monkeypatch.setattr(wizard, "_get_cpu_serial", MagicMock(return_value="1234"))
    monkeypatch.setattr(wizard, "STAGING_FILE", str(tmp_path / "staging.json"))
    monkeypatch.setattr(wizard, "wifi_sessions", {})
    monkeypatch.setattr(wizard, "_last_good_wifi", {})
    monkeypatch.setattr(wizard, "reg_state", {"state": "idle", "message": "", "redirect": ""})
    monkeypatch.setattr(wizard, "_connection_in_progress", True)
    return wizard


def _new_setup_session(ssid="TestNet", password="pw", token="TOK123",
                        site="Site A", asset="Asset A", retry_profile=None):
    sess_tok = wizard._new_session("setup", ssid=ssid, password=password)
    sess = wizard.wifi_sessions[sess_tok]
    sess["token"] = token
    sess["site"] = site
    sess["asset"] = asset
    sess["step"] = 1
    if retry_profile:
        sess["retry_profile"] = retry_profile
    return sess_tok


async def test_registration_failure_keeps_wifi_and_remembers_profile(net):
    net._post_heartbeat = AsyncMock(return_value=(False, "site name already exists", ""))
    sess_tok = _new_setup_session()

    await wizard.run_registration(sess_tok)

    # Session torn down and AP reopened, same as any other failure...
    assert sess_tok not in wizard.wifi_sessions
    assert wizard._connection_in_progress is False
    wizard._revert_to_ap.assert_awaited_once()
    # ...but the Wi-Fi profile was committed (not deleted) and remembered.
    wizard._commit_wifi_profile.assert_awaited_once_with("TestNet")
    assert wizard._last_good_wifi == {"ssid": "TestNet", "profile": "school-air-saved-TestNet"}
    assert wizard.reg_state["state"] == "error"
    assert "kept" in wizard.reg_state["message"].lower()
    assert "site name already exists" in wizard.reg_state["message"]


async def test_token_rejected_still_erases_wifi_creds(net):
    """Unlike a registration failure, a bad token still wipes the network —
    only the specific 'wifi+token good, registration failed' case is spared."""
    net._validate_token = AsyncMock(return_value=(False, "Token rejected by SchoolAir Cloud"))
    sess_tok = _new_setup_session()

    await wizard.run_registration(sess_tok)

    assert sess_tok not in wizard.wifi_sessions
    wizard._revert_to_ap.assert_awaited_once()
    wizard._commit_wifi_profile.assert_not_awaited()
    assert wizard._last_good_wifi == {}
    # TEMP_PROFILE delete attempted (no-op if it doesn't exist — real assertion
    # is just that the "keep creds" path was NOT taken).
    net._cmd.assert_any_call(f'nmcli con delete "{wizard.TEMP_PROFILE}" 2>/dev/null; true')


async def test_index_ap_mode_skips_step1_when_wifi_already_good(net):
    net._ap_is_active = AsyncMock(return_value=True)
    wizard._last_good_wifi.update({"ssid": "TestNet", "profile": "school-air-saved-TestNet"})
    wizard.reg_state.update({"state": "error", "message": "device registration failed: boom"})

    request = MagicMock()
    request.headers.get.return_value = ""
    request.args.get.return_value = None  # no ?reset=1

    resp = await wizard.index(request)

    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("/step2?s=")
    sess_tok = location.split("s=", 1)[1]
    sess = wizard.wifi_sessions[sess_tok]
    assert sess["ssid"] == "TestNet"
    assert sess["retry_profile"] == "school-air-saved-TestNet"
    assert sess["step"] == 1


async def test_index_reset_param_discards_saved_network(net):
    net._ap_is_active = AsyncMock(return_value=True)
    wizard._last_good_wifi.update({"ssid": "TestNet", "profile": "school-air-saved-TestNet"})

    request = MagicMock()
    request.headers.get.return_value = ""
    request.args.get.side_effect = lambda key, *a: "1" if key == "reset" else None

    resp = await wizard.index(request)

    # Falls through to the normal Step 1 form, not another redirect.
    assert resp.status_code == 200
    assert wizard._last_good_wifi == {}
    net._cmd.assert_any_call('nmcli con delete "school-air-saved-TestNet" 2>/dev/null; true')


async def test_retry_profile_reused_directly_without_full_wifi_setup(net):
    """The Step-2-only retry must bring up the already-committed profile
    directly, not repeat _setup_client_profile/TEMP_PROFILE from scratch."""
    sess_tok = _new_setup_session(retry_profile="school-air-saved-TestNet")

    await wizard.run_registration(sess_tok)

    wizard._setup_client_profile.assert_not_awaited()
    net._cmd.assert_any_call('nmcli con up "school-air-saved-TestNet"')
    # Already committed — _commit_wifi_profile should not be re-invoked with
    # the TEMP_PROFILE rename dance.
    assert wizard._last_good_wifi == {}  # cleared on full success
    assert wizard.reg_state["state"] == "success"


async def test_retry_profile_connect_failure_erases_creds(net):
    """If the previously-good network stops working on retry, that's a real
    credentials problem again — erase it instead of looping forever."""
    async def fake_cmd(cmd):
        if cmd == 'nmcli con up "school-air-saved-TestNet"':
            return 1, "", "no network with that name"
        return 0, "", ""
    net._cmd = AsyncMock(side_effect=fake_cmd)
    wizard._last_good_wifi.update({"ssid": "TestNet", "profile": "school-air-saved-TestNet"})
    sess_tok = _new_setup_session(retry_profile="school-air-saved-TestNet")

    await wizard.run_registration(sess_tok)

    assert wizard._last_good_wifi == {}
    wizard._revert_to_ap.assert_awaited_once()
    net._cmd.assert_any_call('nmcli con delete "school-air-saved-TestNet" 2>/dev/null; true')
