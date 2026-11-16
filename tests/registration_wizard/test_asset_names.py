"""tests/registration_wizard/test_asset_names.py

The same cases as the registration page's assetNames.test.ts: the Python port
must suggest what the page suggests.
"""

from datetime import datetime, timedelta, timezone

import pytest

import asset_names as an

_clock = [0]


def at(site, *names):
    out = []
    for name in names:
        _clock[0] += 1
        t = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=_clock[0])
        out.append({"name": name, "site": site, "created_at": t.isoformat().replace("+00:00", "Z")})
    return out


@pytest.mark.parametrize("name,expected", [
    ("Class 3A", ["Class"]),
    ("Sala de professors", ["Sala", "professors"]),
    ("Aula del 2n B", ["Aula"]),
    ("Sala d'actes", ["Sala", "actes"]),
    ("101", []),
])
def test_words_keep_letter_words_of_three_or_more(name, expected):
    assert list(an.words(name).values()) == expected


def test_a_word_counts_once_per_name_ignoring_case():
    assert list(an.words("Class class 2")) == ["class"]


def test_no_second_word_that_points_back_at_the_same_assets():
    picked = an.pick_keywords(at("S", "Class A 3", "Class A 2", "Class B 2"), set(), 3)
    assert picked == [("Class", "Class B 2")]


def test_covers_each_asset_once_most_common_first():
    assets = at("S", "Class 1A", "Class 1B", "Class 2A", "Science Lab", "Computer Lab", "Library", "Lab Class")
    assert [k for k, _ in an.pick_keywords(assets, set(), 3)] == ["Class", "Lab", "Library"]


def test_ties_go_to_the_most_recent():
    assert an.pick_keywords(at("S", "Gym", "Library"), set(), 1)[0][0] == "Library"


def test_shows_the_spelling_used_most():
    assert an.pick_keywords(at("S", "class 1", "Class 2", "Class 3"), set(), 1)[0][0] == "Class"


def test_leaves_out_assets_with_a_word_already_taken():
    assert [k for k, _ in an.pick_keywords(at("S", "Class Lab 1", "Lab 2"), {"class"}, 3)] == ["Lab"]


def test_three_from_the_site_when_it_has_them():
    assets = at("North", "Class 1", "Room 2", "Gym") + at("South", "Aula 1", "Aula 2")
    assert sorted(k for k, _ in an.suggest_keywords(assets, "north")) == ["Class", "Gym", "Room"]


def test_fills_from_the_org_then_ours():
    assets = at("North", "Aula 1", "Aula 2") + at("South", "Aula 3", "Aula Gym", "Despatx")
    got = an.suggest_keywords(assets, "North")
    assert [k for k, _ in got] == ["Aula", "Despatx", "Class"]
    assert got[0][1] == "Aula 2"
    assert got[2] == an.DEFAULT_SUGGESTIONS[0]


def test_ours_without_words_already_suggested():
    assert [k for k, _ in an.suggest_keywords(at("S", "Class 4", "Room 5"), "S")] == ["Room", "Class", "Library"]


def test_only_ours_for_a_new_org():
    assert an.suggest_keywords([], "Main") == an.DEFAULT_SUGGESTIONS[:3]


def test_no_site_is_its_own_group():
    assets = at(None, "Library") + at("S", "Class 1")
    assert an.suggest_keywords(assets, None)[0][0] == "Library"


def test_existing_asset_matches_name_and_site():
    assets = at("Main Campus", "Class 12") + at(None, "Library")
    assert an.existing_asset(assets, " class  12", "main campus")["name"] == "Class 12"
    assert an.existing_asset(assets, "Class 12", "Annex") is None
    assert an.existing_asset(assets, "Library", None)["name"] == "Library"
    assert an.existing_asset(assets, "Library", "Main Campus") is None


def test_sites_known_and_similar():
    sites = ["Main Campus", "Annex"]
    assert an.known_site(sites, " main  campus ") == "Main Campus"
    assert an.similar_site(sites, "Main Campuss") == "Main Campus"
    assert an.similar_site(sites, "Anex") == "Annex"
    assert an.similar_site(sites, "North Campus") is None
    assert an.similar_site(sites, "Main Campus") is None
