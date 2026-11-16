"""tests/registration_wizard/test_ssh_register.py

registration_wizard/ssh_register.py: registering from a terminal over SSH,
with the installer's answers scripted and the server and wizard helpers mocked.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import ssh_register as sr

STATUS = {
    "org_name": "Escola X",
    "tokens": [{"state": "used"}, {"state": "unused"}, {"state": "unused"}],
    "sites": ["Annex", "Main Campus"],
    "assets": [
        {"name": "Class 12", "site": "Main Campus", "created_at": "2026-01-02T00:00:00Z"},
        {"name": "Class 11", "site": "Main Campus", "created_at": "2026-01-01T00:00:00Z"},
    ],
}
INVITE = "https://registration.schoolair.org/#t=aaa.bbb.ccc"


@pytest.fixture
def unit(monkeypatch):
    """An unregistered unit with a SEN63C, on Wi-Fi "SchoolNet"; registration succeeds."""
    b = sr.backend
    mocks = {
        "_has_token":        MagicMock(return_value=False),
        "_current_wifi":     AsyncMock(return_value=("conn", "SchoolNet")),
        "_validate_token":   AsyncMock(return_value=(True, "")),
        "register_and_save": AsyncMock(return_value=(True, "")),
        "read_wizard_registration": MagicMock(return_value={"asset_name": "Class 12"}),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(b, name, mock)
    monkeypatch.setattr(sr.device_identity, "short_id", lambda: "7F3K2")
    monkeypatch.setattr(sr.device_identity, "mismatch_detected", lambda: False)
    monkeypatch.setattr(sr, "detect_sensor", lambda: "SEN63C")
    mocks["invite_status"] = MagicMock(return_value=STATUS)
    monkeypatch.setattr(sr, "invite_status", mocks["invite_status"])
    return mocks


def answers(monkeypatch, *lines):
    """Script the installer: each ask() takes the next line (after any prefill)."""
    queue = list(lines)
    asked = []

    def ask(prompt, prefill=""):
        asked.append(prompt)
        if not queue:
            raise AssertionError(f"unexpected question: {prompt}")
        return prefill + queue.pop(0)
    monkeypatch.setattr(sr, "ask", ask)
    return asked


async def test_registers_with_a_site_and_a_keyword(unit, monkeypatch, capsys):
    #            site  keyword  rest  indoor  go
    answers(monkeypatch, "2", "1", "3", "", "")
    assert await sr.run(INVITE) == sr.EXIT_REGISTERED

    reg, ssid, wifi = unit["register_and_save"].call_args.args
    assert reg == {"token": "bbb", "nickname": "Class 3", "site": "Main Campus",
                   "environment": "indoor", "migrate": False}
    assert ssid is None and wifi == "SchoolNet"
    assert unit["register_and_save"].call_args.kwargs == {"via": "ssh"}
    out = capsys.readouterr().out
    assert "Class" in out and "e.g. Class 12" in out
    assert "Escola X · 2 of 3 registrations left" in out


async def test_a_name_in_use_at_the_site_warns_and_can_be_changed(unit, monkeypatch, capsys):
    answers(monkeypatch, "2", "class 12", "n", "Class 13", "", "")
    assert await sr.run(INVITE) == sr.EXIT_REGISTERED
    assert "“Class 12” already exists at Main Campus" in capsys.readouterr().out
    assert unit["register_and_save"].call_args.args[0]["nickname"] == "Class 13"


async def test_a_near_miss_new_site_offers_the_known_one(unit, monkeypatch, capsys):
    answers(monkeypatch, "n", "Main Campuss", "", "Lab", "", "")
    assert await sr.run(INVITE) == sr.EXIT_REGISTERED
    assert "Did you mean Main Campus?" in capsys.readouterr().out
    assert unit["register_and_save"].call_args.args[0]["site"] == "Main Campus"


async def test_no_site(unit, monkeypatch):
    answers(monkeypatch, "0", "Library", "", "")
    assert await sr.run(INVITE) == sr.EXIT_REGISTERED
    assert unit["register_and_save"].call_args.args[0]["site"] == ""


async def test_an_outdoor_choice_for_an_indoor_sensor(unit, monkeypatch):
    answers(monkeypatch, "1", "Yard", "n", "")
    await sr.run(INVITE)
    assert unit["register_and_save"].call_args.args[0]["environment"] == "outdoor"


async def test_no_sensor_must_be_confirmed(unit, monkeypatch):
    monkeypatch.setattr(sr, "detect_sensor", lambda: None)
    answers(monkeypatch, "1", "Yard", "n")
    with pytest.raises(sr.Stop):
        await sr.run(INVITE)
    unit["register_and_save"].assert_not_awaited()


async def test_an_already_registered_unit_is_left_alone_unless_asked(unit, monkeypatch):
    unit["_has_token"].return_value = True
    answers(monkeypatch, "")
    assert await sr.run(INVITE) == sr.EXIT_UNCHANGED
    unit["invite_status"].assert_not_called()


async def test_registering_again_under_its_own_name_does_not_warn(unit, monkeypatch, capsys):
    unit["_has_token"].return_value = True
    answers(monkeypatch, "y", "2", "Class 12", "", "")
    assert await sr.run(INVITE) == sr.EXIT_REGISTERED
    assert "already exists" not in capsys.readouterr().out


async def test_a_used_up_link_says_so(unit, monkeypatch, capsys):
    unit["invite_status"].return_value = {**STATUS, "tokens": [{"state": "used"}] * 3}
    answers(monkeypatch)
    assert await sr.run(INVITE) == sr.EXIT_FAILED
    assert "Every registration in this link has been used" in capsys.readouterr().out


async def test_a_registration_taken_meanwhile_retries_with_the_next(unit, monkeypatch):
    unit["register_and_save"].side_effect = [(False, "Token rejected"), (True, "")]
    answers(monkeypatch, "1", "Gym", "", "")
    assert await sr.run(INVITE) == sr.EXIT_REGISTERED
    tokens = [c.args[0]["token"] for c in unit["register_and_save"].call_args_list]
    assert tokens == ["bbb", "ccc"]


@pytest.mark.parametrize("text", [INVITE, "#t=aaa.bbb.ccc", "t=aaa.bbb.ccc", "aaa.bbb.ccc"])
def test_parse_invite(text):
    assert sr.parse_invite(text) == ["aaa", "bbb", "ccc"]
