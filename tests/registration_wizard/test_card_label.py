"""tests/registration_wizard/test_card_label.py

registration_wizard/card_label.py: the label on the boot partition that tells
the installer what a card holds.
"""

import json

import pytest

import card_label as cl


@pytest.fixture
def unit(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    status = tmp_path / "status.json"
    utype = tmp_path / "unit-type"
    utype.write_text("indoor\n")
    monkeypatch.setattr(cl, "ENV_FILE", str(env))
    monkeypatch.setattr(cl, "STATUS_FILE", str(status))
    monkeypatch.setattr(cl, "UNIT_TYPE_FILE", str(utype))
    monkeypatch.setattr(cl, "_identity", lambda: ("00000000a1b2c3d4", "K3F9Q"))
    monkeypatch.setattr(cl, "_version", lambda: "2.3.27")
    monkeypatch.setattr(cl.socket, "gethostname", lambda: "schoolair-26B18-40ed")
    return {"env": env, "status": status, "label": tmp_path / "schoolair-unit.json"}


def test_unregistered(unit):
    label = cl.build()
    assert label["schoolair"] == 1 and label["version"] == "2.3.27"
    assert label["registered"] is False and "asset" not in label
    assert label["cpu_serial"] == "00000000a1b2c3d4" and label["short_id"] == "K3F9Q"
    assert label["hostname"] == "schoolair-26B18-40ed" and label["unit_type"] == "indoor"
    assert label["card_config"] is True


def test_registered_says_as_what_without_secrets(unit):
    unit["env"].write_text("AUTH_TOKEN=tok-secret\nNEW_AUTH_TOKEN=dev-secret\n")
    unit["status"].write_text(json.dumps({"token": "tok-secret", "asset_name": "Room 12", "site": "Main",
                                          "ssid": "SchoolNet", "registered_at": "2026-11-20T10:00:00+00:00"}))
    assert cl.write(path=str(unit["label"]))
    text = unit["label"].read_text()
    label = json.loads(text)
    assert label["registered"] is True
    assert (label["asset"], label["site"]) == ("Room 12", "Main")
    for secret in ("tok-secret", "dev-secret", "SchoolNet"):
        assert secret not in text


def test_new_golden_image(unit):
    label = cl.build(new=True)
    assert label["new"] is True and label["registered"] is False
    assert "cpu_serial" not in label and "hostname" not in label


def test_not_rewritten_when_unchanged(unit):
    assert cl.write(path=str(unit["label"]))
    first = unit["label"].stat().st_mtime_ns
    unit["label"].write_text(unit["label"].read_text().replace('"updated_at": "', '"updated_at": "x'))
    before = unit["label"].read_text()
    assert cl.write(path=str(unit["label"]))
    assert unit["label"].read_text() == before  # only updated_at differed: left alone
    unit["env"].write_text("AUTH_TOKEN=t\n")
    assert cl.write(path=str(unit["label"]))
    assert json.loads(unit["label"].read_text())["registered"] is True
    assert first  # written the first time


def test_never_raises(unit, tmp_path):
    assert cl.write(path=str(tmp_path / "missing-dir" / "label.json")) is False
