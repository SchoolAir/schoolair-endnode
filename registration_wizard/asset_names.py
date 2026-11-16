"""asset_names.py — name suggestions for a new asset, from the names the org
already uses. A port of the registration page's src/register/assetNames.ts
(schoolair-registration): keep the two in step.

Keywords: the most common word among the site's asset names; then drop every
asset that has it and take the most common word among the rest, and so on.
So "Class A 3", "Class A 2", "Class B 2" give "Class" alone: "A" and "2"
would only point back at the same assets. Fewer than three from the site →
the same over the whole org, leaving out assets that have a word already
chosen; still fewer → our defaults, minus the words already chosen.

A word is a run of letters, at least 3 long, with no digits ("3A", "101" and
"B" name a room within a kind, they aren't the kind), and not a filler word.
Each asset counts once per word, ignoring case. Ties go to the word with the
most recently added asset; its name is the example.
"""

import re
from datetime import datetime

SUGGESTION_COUNT = 3

# Ours, for an org without enough names of its own: (keyword, example).
DEFAULT_SUGGESTIONS = [
    ("Class",   "Class 1A"),
    ("Room",    "Room 101"),
    ("Library", "Library"),
    ("Lab",     "Science Lab"),
    ("Gym",     "Gym"),
    ("Office",  "Office"),
]

# English, Spanish and Catalan filler of 3+ letters (shorter words are dropped anyway).
FILLER = {
    "the", "and", "for", "with", "from",
    "del", "las", "los", "una", "uno", "con", "por", "para",
    "les", "els", "dels", "amb", "per",
}

_WORD = re.compile(r"[^\W_]+")  # letters and digits (Unicode), like \p{L}\p{N}


def tidy(name: str) -> str:
    """As sent to the server: trimmed, single spaces."""
    return " ".join(name.split())


def normalize(name: str) -> str:
    return tidy(name).lower()


def same_name(a: str, b: str) -> bool:
    """Same name, ignoring case and surrounding/repeated spaces."""
    return normalize(a) == normalize(b)


def same_site(a: "str | None", b: "str | None") -> bool:
    if a is None or b is None:
        return a is None and b is None
    return same_name(a, b)


def words(name: str) -> "dict[str, str]":
    """The keyword-worthy words of a name, lower case → as written, once each."""
    out: "dict[str, str]" = {}
    for w in _WORD.findall(name):
        if len(w) < 3 or any(c.isdigit() for c in w):
            continue
        key = w.lower()
        if key not in FILLER and key not in out:
            out[key] = w
    return out


def _time(asset: dict) -> float:
    try:
        return datetime.fromisoformat(str(asset.get("created_at", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def pick_keywords(assets: "list[dict]", taken: "set[str]", limit: int) -> "list[tuple[str, str]]":
    """Up to `limit` (keyword, example) from `assets` ({name, site, created_at}),
    skipping assets that have one of `taken` (lower case)."""
    left = [{"name": a["name"], "at": _time(a), "words": words(a["name"])} for a in assets]
    left = [a for a in left if not (a["words"].keys() & taken)]
    picked: "list[tuple[str, str]]" = []

    while len(picked) < limit:
        tally: "dict[str, dict]" = {}
        for a in left:
            for key, spelled in a["words"].items():
                t = tally.setdefault(key, {"count": 0, "newest": -1.0, "example": a["name"], "spellings": {}})
                t["count"] += 1
                if a["at"] > t["newest"]:
                    t["newest"], t["example"] = a["at"], a["name"]
                t["spellings"][spelled] = t["spellings"].get(spelled, 0) + 1
        if not tally:
            break
        key, best = sorted(tally.items(), key=lambda kv: (-kv[1]["count"], -kv[1]["newest"], kv[0]))[0]
        spelling = sorted(best["spellings"].items(), key=lambda kv: -kv[1])[0][0]
        picked.append((spelling, best["example"]))
        left = [a for a in left if key not in a["words"]]
    return picked


def suggest_keywords(assets: "list[dict]", site: "str | None", limit: int = SUGGESTION_COUNT) -> "list[tuple[str, str]]":
    """Keywords for a new asset at `site` (None: no site): the site's, then the org's, then ours."""
    picked = pick_keywords([a for a in assets if same_site(a.get("site"), site)], set(), limit)
    if len(picked) < limit:
        picked += pick_keywords(assets, {k.lower() for k, _ in picked}, limit - len(picked))
    if len(picked) < limit:
        taken = {k.lower() for k, _ in picked}
        picked += [d for d in DEFAULT_SUGGESTIONS if d[0].lower() not in taken][: limit - len(picked)]
    return picked


def existing_asset(assets: "list[dict]", name: str, site: "str | None") -> "dict | None":
    """The asset of that name at that site, if any (a unit registered with it replaces that asset's unit)."""
    if not name.strip():
        return None
    return next((a for a in assets if same_name(a["name"], name) and same_site(a.get("site"), site)), None)


def known_site(sites: "list[str]", name: str) -> "str | None":
    """The site already known by that name (ignoring case and spaces), if any."""
    return next((s for s in sites if same_name(s, name)), None)


def similar_site(sites: "list[str]", name: str) -> "str | None":
    """A known site the typed name is probably a typo of: at most 2 edits away (1 for short names)."""
    typed = normalize(name)
    if not typed or known_site(sites, name):
        return None
    limit = 1 if len(typed) <= 5 else 2
    best = None
    for s in sites:
        d = _edit_distance(normalize(s), typed)
        if d <= limit and (best is None or d < best[1]):
            best = (s, d)
    return best[0] if best else None


def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        row = [i]
        for j in range(1, len(b) + 1):
            row.append(min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1])))
        prev = row
    return prev[len(b)]
