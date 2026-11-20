"""tests/registration_wizard/test_card_config.py

registration_wizard/card_config.py: the setup file on the card's boot
partition (schoolair_config.json, or the earlier schoolair.json) on first boot, with the system commands, the server and the wizard helpers mocked.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import card_config as cc

PSK = "s3cret-wifi-pass"
INVITE = "https://registration.schoolair.org/#t=aaa.bbb.ccc"
CARD = {
    "version": 1,
    "hostname": "schoolair-26B18-40ed",
    "wifi": {"country": "es", "networks": [{"ssid": "SchoolNet", "psk": PSK}]},
    "register": {"asset": " Room 12 ", "site": "Main Campus", "invite": INVITE},
}
KEY = "Kx9_single-use-token"
PORTAL = {
    "one_time_key": KEY, "building": "Main Campus", "room": " Room 12 ",
    "wifi_name": "SchoolNet", "wifi_password": PSK,
    "wifi_country": "es", "hostname": "schoolair-26B18-40ed",
}
STATUS = {"tokens": [{"state": "used"}, {"state": "unused"}, {"state": "unused"}]}


# ── validate ──────────────────────────────────────────────────────────────────

def test_validate_normalises():
    cfg = cc.validate(CARD)
    assert cfg["hostname"] == "schoolair-26B18-40ed"
    assert cfg["country"] == "ES"
    assert cfg["networks"] == [{"ssid": "SchoolNet", "psk": PSK}]
    assert cfg["register"] == {"asset": "Room 12", "site": "Main Campus",
                               "invite": INVITE, "environment": None}


def test_validate_minimal():
    assert cc.validate({"version": 1}) == {"hostname": None, "country": None,
                                           "networks": [], "register": None}


@pytest.mark.parametrize("card, message", [
    ({"version": 2}, "version"),
    ([], "version"),
    ({"version": 1, "hostname": "bad host"}, "hostname"),
    ({"version": 1, "hostname": "-x"}, "hostname"),
    ({"version": 1, "hostname": "my-pi"}, "SchoolAir's pattern"),
    ({"version": 1, "hostname": "schoolair-26D18-40ed"}, "SchoolAir's pattern"),
    ({"version": 1, "wifi": {"country": "ESP"}}, "country"),
    ({"version": 1, "wifi": {"networks": [{"psk": "12345678"}]}}, "ssid is missing"),
    ({"version": 1, "wifi": {"networks": [{"ssid": "x" * 33}]}}, "1 to 32 bytes"),
    ({"version": 1, "wifi": {"networks": [{"ssid": "A", "psk": "short"}]}}, "8 to 63"),
    ({"version": 1, "register": {"site": "X"}}, "register.asset is missing"),
    ({"version": 1, "register": {"asset": "A", "environment": "space"}}, "indoor or outdoor"),
])
def test_validate_rejects(card, message):
    with pytest.raises(cc.Invalid, match=message):
        cc.validate(card)


def test_validate_error_never_echoes_the_password():
    with pytest.raises(cc.Invalid) as e:
        cc.validate({"version": 1, "wifi": {"networks": [{"ssid": "A", "psk": "tiny"}]}})
    assert "tiny" not in str(e.value)


# ── the card file ─────────────────────────────────────────────────────────────

def test_take_card_file_reads_then_deletes(tmp_path):
    f = tmp_path / "schoolair.json"
    f.write_text(json.dumps(CARD))
    assert cc.take_card_file(str(f)) == CARD
    assert not f.exists()


def test_take_card_files_absent(tmp_path):
    (tmp_path / "config.txt").write_text("x")
    assert cc.take_card_files(str(tmp_path)) is None


def test_take_card_files_deletes_even_invalid_json(tmp_path):
    f = tmp_path / "schoolair_config.json"
    f.write_text("{not json")
    with pytest.raises(ValueError):
        cc.take_card_files(str(tmp_path))
    assert not f.exists()


def test_take_card_files_reads_a_renamed_download(tmp_path):
    f = tmp_path / "schoolair_config (1).json"
    f.write_text(json.dumps(PORTAL))
    assert cc.take_card_files(str(tmp_path)) == ("schoolair_config (1).json", PORTAL)
    assert not f.exists()


def test_take_card_files_uses_the_newest_and_deletes_all(tmp_path):
    import os
    old, new, legacy = (tmp_path / n for n in
                        ("schoolair_config.json", "schoolair_config (1).json", "schoolair.json"))
    old.write_text(json.dumps({**PORTAL, "room": "Old"}))
    legacy.write_text(json.dumps(CARD))
    new.write_text(json.dumps(PORTAL))
    for age, f in ((300, old), (200, legacy), (100, new)):
        t = 1_700_000_000 - age
        os.utime(f, (t, t))
    name, data = cc.take_card_files(str(tmp_path))
    assert name == "schoolair_config (1).json" and data["room"] == " Room 12 "
    assert list(tmp_path.iterdir()) == []


# ── the portal's form ─────────────────────────────────────────────────────────

def test_validate_portal_form():
    assert cc.validate(PORTAL) == {
        "hostname": "schoolair-26B18-40ed", "country": "ES",
        "networks": [{"ssid": "SchoolNet", "psk": PSK}],
        "register": {"asset": "Room 12", "site": "Main Campus", "invite": KEY, "environment": None},
    }


def test_validate_portal_form_as_the_portal_writes_it():
    """Only the five fields the page writes; an open network has no password."""
    cfg = cc.validate({"one_time_key": KEY, "building": "", "room": "Lab",
                       "wifi_name": "Open", "wifi_password": ""})
    assert cfg["hostname"] is None and cfg["country"] is None
    assert cfg["networks"] == [{"ssid": "Open", "psk": ""}]
    assert cfg["register"]["site"] == "" and cfg["register"]["asset"] == "Lab"


def test_validate_portal_form_wifi_only():
    cfg = cc.validate({"wifi_name": "SchoolNet", "wifi_password": PSK})
    assert cfg["register"] is None and len(cfg["networks"]) == 1


@pytest.mark.parametrize("card, message", [
    ({"one_time_key": KEY}, "room is missing"),
    ({"room": "A", "environment": "space"}, "indoor or outdoor"),
    ({"wifi_name": "A", "wifi_password": "short"}, "wifi_password must be 8 to 63"),
    ({"wifi_name": "x" * 33}, "wifi_name must be 1 to 32 bytes"),
    ({"wifi_country": "ESP"}, "wifi_country"),
    ({"hostname": "bad host"}, "hostname"),
    ({"room": 12}, "room must be text"),
])
def test_validate_portal_form_rejects(card, message):
    with pytest.raises(cc.Invalid, match=message):
        cc.validate(card)


# ── Wi-Fi ─────────────────────────────────────────────────────────────────────

def test_apply_wifi_adds_profiles_first_most_preferred(monkeypatch):
    calls = []
    monkeypatch.setattr(cc, "run", lambda cmd: calls.append(cmd) or 0)
    nets = [{"ssid": "SchoolNet", "psk": PSK}, {"ssid": "Guest", "psk": ""}]
    assert cc.apply_wifi("ES", nets) is True
    assert calls[0] == ["raspi-config", "nonint", "do_wifi_country", "ES"]
    adds = [c for c in calls if c[:3] == ["nmcli", "con", "add"]]
    prio = [int(c[c.index("connection.autoconnect-priority") + 1]) for c in adds]
    assert prio[0] > prio[1]
    assert adds[0][-2:] == ["wifi-sec.psk", PSK] and "wifi-sec.psk" not in adds[1]
    assert calls[-1] == ["nmcli", "con", "up", cc.backend._profile_name("SchoolNet")]


def test_apply_wifi_reports_a_failed_profile(monkeypatch, capsys):
    monkeypatch.setattr(cc, "run", lambda cmd: 1 if cmd[:3] == ["nmcli", "con", "add"] else 0)
    assert cc.apply_wifi(None, [{"ssid": "SchoolNet", "psk": PSK}]) is False
    assert PSK not in capsys.readouterr().out


# ── the whole run ─────────────────────────────────────────────────────────────

@pytest.fixture
def card(tmp_path, monkeypatch):
    """schoolair.json on the 'boot partition'; system steps and registration mocked."""
    boot = tmp_path / "boot"
    boot.mkdir()
    f = boot / "schoolair.json"
    f.write_text(json.dumps(CARD))
    record = tmp_path / "state" / "card-config.json"
    monkeypatch.setattr(cc, "BOOT_DIR", str(boot))
    monkeypatch.setattr(cc, "RECORD_FILE", str(record))
    mocks = {
        "apply_hostname": MagicMock(return_value=True),
        "apply_wifi": MagicMock(return_value=True),
        "register": AsyncMock(return_value=(True, "")),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(cc, name, mock)
    return {"file": f, "boot": boot, "record": record, **mocks}


async def test_applies_everything_and_records_no_secrets(card, capsys):
    assert await cc.main_async() == 0
    assert not card["file"].exists()
    card["apply_hostname"].assert_called_once_with("schoolair-26B18-40ed")
    card["apply_wifi"].assert_called_once_with("ES", [{"ssid": "SchoolNet", "psk": PSK}])
    assert card["register"].call_args.args[0]["asset"] == "Room 12"

    record = card["record"].read_text()
    assert json.loads(record)["register"] == {"asset": "Room 12", "site": "Main Campus",
                                             "registered": True, "error": None}
    out = capsys.readouterr().out
    for secret in (PSK, "aaa.bbb.ccc"):
        assert secret not in record and secret not in out


async def test_applies_the_portal_form(card, capsys):
    card["file"].unlink()
    f = card["boot"] / "schoolair_config.json"
    f.write_text(json.dumps(PORTAL))
    assert await cc.main_async() == 0
    assert not f.exists()
    card["apply_hostname"].assert_called_once_with("schoolair-26B18-40ed")
    card["apply_wifi"].assert_called_once_with("ES", [{"ssid": "SchoolNet", "psk": PSK}])
    assert card["register"].call_args.args[0] == {"asset": "Room 12", "site": "Main Campus",
                                                  "invite": KEY, "environment": None}
    record = card["record"].read_text()
    assert json.loads(record)["file"] == "schoolair_config.json"
    out = capsys.readouterr().out
    for secret in (PSK, KEY):
        assert secret not in record and secret not in out


async def test_no_invite_leaves_registration_to_bluetooth(card, capsys):
    data = json.loads(json.dumps(CARD))
    del data["register"]["invite"]
    card["file"].write_text(json.dumps(data))
    assert await cc.main_async() == 0
    card["register"].assert_not_called()
    assert json.loads(card["record"].read_text())["register"]["registered"] is False
    assert "left to Bluetooth" in capsys.readouterr().out


async def test_invalid_file_is_deleted_and_nothing_applied(card):
    card["file"].write_text(json.dumps({"version": 1, "hostname": "not ok!"}))
    assert await cc.main_async() == 1
    assert not card["file"].exists()
    card["apply_hostname"].assert_not_called()
    assert "hostname" in json.loads(card["record"].read_text())["error"]


async def test_no_file_does_nothing(card):
    card["file"].unlink()
    assert await cc.main_async() == 0
    assert not card["record"].exists()


async def test_failed_registration_is_reported(card):
    card["register"].return_value = (False, "SchoolAir could not be reached")
    assert await cc.main_async() == 1
    assert json.loads(card["record"].read_text())["register"]["error"] == "SchoolAir could not be reached"


# ── registration ──────────────────────────────────────────────────────────────

@pytest.fixture
def unit(monkeypatch):
    """An unregistered unit with a SEN63C on Wi-Fi "SchoolNet"; the server reachable."""
    b = cc.backend
    mocks = {
        "_has_token": MagicMock(return_value=False),
        "_current_wifi": AsyncMock(return_value=("conn", "SchoolNet")),
        "_validate_token": AsyncMock(return_value=(True, "")),
        "register_and_save": AsyncMock(return_value=(True, "")),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(b, name, mock)
    monkeypatch.setattr(cc, "wait_for_network", AsyncMock(return_value=True))
    monkeypatch.setattr(cc.ssh_register, "detect_sensor", lambda: "SEN63C")
    mocks["invite_status"] = MagicMock(return_value=STATUS)
    monkeypatch.setattr(cc.ssh_register, "invite_status", mocks["invite_status"])
    return mocks


REG = {"asset": "Room 12", "site": "Main Campus", "invite": INVITE, "environment": None}


async def test_registers_with_the_next_unused_registration(unit):
    assert await cc.register(REG) == (True, "")
    reg, ssid, wifi = unit["register_and_save"].call_args.args
    assert reg == {"token": "bbb", "nickname": "Room 12", "site": "Main Campus",
                   "environment": "indoor", "migrate": False}
    assert ssid is None and wifi == "SchoolNet"
    assert unit["register_and_save"].call_args.kwargs == {"via": "card"}


async def test_environment_from_the_card_wins(unit):
    await cc.register({**REG, "environment": "outdoor"})
    assert unit["register_and_save"].call_args.args[0]["environment"] == "outdoor"


async def test_tries_the_next_registration_once(unit):
    unit["register_and_save"].side_effect = [(False, "taken"), (True, "")]
    assert await cc.register(REG) == (True, "")
    tokens = [c.args[0]["token"] for c in unit["register_and_save"].call_args_list]
    assert tokens == ["bbb", "ccc"]


async def test_gives_up_after_two(unit):
    unit["register_and_save"].return_value = (False, "refused")
    assert await cc.register(REG) == (False, "refused")
    assert unit["register_and_save"].call_count == 2


async def test_all_registrations_used(unit):
    unit["invite_status"].return_value = {"tokens": [{"state": "used"}] * 3}
    ok, why = await cc.register(REG)
    assert not ok and "every registration" in why
    unit["register_and_save"].assert_not_called()


async def test_already_registered_is_left_alone(unit):
    unit["_has_token"].return_value = True
    ok, why = await cc.register(REG)
    assert not ok and "already registered" in why
    unit["register_and_save"].assert_not_called()


async def test_server_unreachable(unit, monkeypatch):
    monkeypatch.setattr(cc, "wait_for_network", AsyncMock(return_value=False))
    assert await cc.register(REG) == (False, "SchoolAir could not be reached")
