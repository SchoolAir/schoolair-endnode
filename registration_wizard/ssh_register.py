#!/usr/bin/env python3
"""ssh_register.py — register this unit from a terminal, using an invite link.

The setup script the registration page offers for download (run on the
installer's computer) installs SchoolAir on a fresh Pi over SSH and then runs
this, over the same session:

    sudo SCHOOLAIR_INVITE='<the link, or its #t=… part>' python3 ssh_register.py

It asks what the page asks — site, asset name, indoor/outdoor — with the same
help: the org's sites to pick from (and "did you mean" for a near miss), name
keywords from the names in use (asset_names.py), a warning when the name
already exists at that site. Then it registers over the network the unit is
already on, saving the result exactly as a Bluetooth registration does
(wizard.register_and_save), with the next unused registration of the link.

Exit status: 0 registered, 3 nothing done (already registered, kept as is),
1 not registered (an error, or the installer stopped).
"""

import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request

import wizard as backend  # sets up sys.path for the app's modules
import device_identity
import hardware_record
from asset_names import (existing_asset, known_site, same_name, similar_site,
                         suggest_keywords, tidy)

INTENDED_ENVIRONMENT = {"SEN63C": "indoor", "SEN65": "outdoor"}  # as the page's protocol.ts
EXIT_REGISTERED, EXIT_FAILED, EXIT_UNCHANGED = 0, 1, 3
HTTP_TIMEOUT = 15

BOLD, DIM, YELLOW, RED, GREEN, OFF = (("\033[1m", "\033[2m", "\033[33m", "\033[31m", "\033[32m", "\033[0m")
                                      if sys.stdout.isatty() else ("",) * 6)


class Stop(Exception):
    """The installer ended it (Ctrl-C / Ctrl-D, or answered no)."""


# ── Input ─────────────────────────────────────────────────────────────────────

def ask(prompt: str, prefill: str = "") -> str:
    """A line from the installer; `prefill` is already typed in, to edit."""
    try:
        import readline
    except ImportError:
        readline = None
    if readline and prefill:
        readline.set_pre_input_hook(lambda: (readline.insert_text(prefill), readline.redisplay()))
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise Stop()
    finally:
        if readline and prefill:
            readline.set_pre_input_hook()


def yes(prompt: str, default: bool) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        a = ask(f"{prompt} {hint} ").strip().lower()
        if not a:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False


def warn(text: str) -> None:
    print(f"{YELLOW}{text}{OFF}")


# ── Server ────────────────────────────────────────────────────────────────────

def parse_invite(text: str) -> "list[str]":
    """The tokens in an invite link, its fragment, or the bare token list."""
    frag = text.split("#", 1)[1] if "#" in text else text
    frag = re.sub(r"^t=", "", frag.strip())
    return [t for t in frag.split(".") if t]


def invite_status(tokens: "list[str]") -> dict:
    url = f"{backend.NEW_SERVER_BASE_URL.rstrip('/')}/aqc/v1/invite-status"
    req = urllib.request.Request(url, data=json.dumps({"tokens": tokens}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error")
        except Exception:
            msg = None
        raise RuntimeError(msg or f"SchoolAir answered HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"could not reach SchoolAir: {getattr(e, 'reason', e)}")


def next_unused(tokens: "list[str]", status: dict, skip: "set[str]") -> "str | None":
    return next((t for t, s in zip(tokens, status["tokens"]) if s["state"] == "unused" and t not in skip), None)


# ── The unit ──────────────────────────────────────────────────────────────────

def detect_sensor() -> "str | None":
    """As ble_provision._detect_sensor: the hardware record, else a check now (recorded)."""
    known, model = hardware_record.sen6x_model(hardware_record.load())
    if known:
        return model
    try:
        model, conclusive = hardware_record.check_sen6x()
        hardware_record.update(hardware_record.sen6x_observations(model, conclusive))
        return model
    except OSError as e:
        print(f"{DIM}(could not check the sensor: {e}){OFF}")
        return None


# ── Questions ─────────────────────────────────────────────────────────────────

def ask_site(sites: "list[str]") -> "str | None":
    print(f"\n{BOLD}Site{OFF}")
    if not sites:
        return ask_new_site([], optional=True)
    for i, s in enumerate(sites, 1):
        print(f"  {i:>2}) {s}")
    print("   n) New site")
    print("   0) No site")
    default = "1" if len(sites) == 1 else ""
    while True:
        a = ask(f"Choose{f' [{default}]' if default else ''}: ").strip() or default
        if a == "0":
            return None
        if a.lower() == "n":
            site = ask_new_site(sites, optional=False)
            if site is not None:
                return site
            continue
        if a.isdigit() and 1 <= int(a) <= len(sites):
            return sites[int(a) - 1]
        # A name typed straight in.
        if a and known_site(sites, a):
            return known_site(sites, a)
        print("Type the number of a site, n for a new one, or 0 for none.")


def ask_new_site(sites: "list[str]", optional: bool) -> "str | None":
    """A new site's name; None when left empty (no site if optional, back to the list if not)."""
    while True:
        name = tidy(ask("Site name (Enter for none): " if optional else "New site name (Enter to go back): "))
        if not name:
            return None
        known = known_site(sites, name)
        if known:
            print(f"That’s the existing site {BOLD}{known}{OFF}.")
            return known
        near = similar_site(sites, name)
        if near:
            warn(f"Did you mean {near}? A new site keeps its units apart from that one.")
            if yes(f"Use {near}?", True):
                return near
            if not yes(f'Create the new site "{name}"?', False):
                continue
        return name


def ask_name(assets: "list[dict]", site: "str | None", own_name: "str | None") -> str:
    suggestions = suggest_keywords(assets, site)
    print(f"\n{BOLD}Asset name{OFF} (usually the room it's in)")
    print("Names in use start with:" if assets else "For example:")
    for i, (keyword, example) in enumerate(suggestions, 1):
        print(f"  {i}) {keyword:<12}{DIM}{'' if same_name(keyword, example) else 'e.g. ' + example}{OFF}")
    where = f" at {site}" if site else " (with no site)"
    while True:
        a = ask("Type the name, or a number to start with that word: ").strip()
        if a.isdigit() and 1 <= int(a) <= len(suggestions):
            a = ask("Asset name: ", prefill=suggestions[int(a) - 1][0] + " ")
        name = tidy(a)
        if not name:
            continue
        clash = existing_asset(assets, name, site)
        if clash and not (own_name and same_name(clash["name"], own_name)):
            warn(f"“{clash['name']}” already exists{where}. Registering this unit with that name replaces "
                 "the unit there: that one stops reporting, and this one continues its readings.")
            if not yes("Use that name anyway?", False):
                continue
        return name


def ask_environment(sensor: "str | None") -> str:
    print(f"\n{BOLD}Where it’s used{OFF}")
    if sensor is None:
        warn("No SEN6x sensor was detected on this unit. It won’t measure air quality "
             "until one is fitted and working.")
        if not yes("Register it anyway?", False):
            raise Stop()
        while True:
            a = ask("Indoor or outdoor? [i/o]: ").strip().lower()
            if a in ("i", "indoor"):
                return "indoor"
            if a in ("o", "outdoor"):
                return "outdoor"
    intended = INTENDED_ENVIRONMENT.get(sensor, "indoor")
    other = "outdoor" if intended == "indoor" else "indoor"
    print(f"Detected a {sensor} sensor: this is an {intended} unit.")
    return intended if yes(f"Use it {intended}s, as intended?", True) else other


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(invite: str) -> int:
    tokens = parse_invite(invite)
    if not tokens:
        print(f"{RED}No invite link given (SCHOOLAIR_INVITE).{OFF}")
        return EXIT_FAILED

    unit = f"SchoolAir-{device_identity.short_id()}"
    print(f"\n{BOLD}Register {unit}{OFF}  {DIM}({os.uname().nodename}){OFF}")

    own_name = None
    if backend._has_token() and not device_identity.mismatch_detected():
        own_name = (backend.read_wizard_registration() or {}).get("asset_name")
        warn(f"This unit is already registered{f' as “{own_name}”' if own_name else ''}.")
        if not yes("Register it again? That uses a registration from your link and replaces its current one.", False):
            print("Left as it is.")
            return EXIT_UNCHANGED

    status = invite_status(tokens)
    token = next_unused(tokens, status, set())
    if token is None:
        print(f"{RED}Every registration in this link has been used, or it has expired. "
              f"Ask your SchoolAir admin for a new link.{OFF}")
        return EXIT_FAILED
    left = sum(1 for s in status["tokens"] if s["state"] == "unused")
    print(f"{status.get('org_name') or 'Your organisation'} · {left} of {len(tokens)} registrations left")

    sites, assets = status.get("sites") or [], status.get("assets") or []
    site = ask_site(sites)
    name = ask_name(assets, site, own_name)
    environment = ask_environment(detect_sensor())

    print(f"\nRegister {BOLD}{name}{OFF}{f' at {site}' if site else ''}, {environment}.")
    if not yes("Go ahead?", True):
        raise Stop()

    _, current_wifi = await backend._current_wifi()
    tried: "set[str]" = set()
    while True:
        print("Registering with SchoolAir…")
        reg = {"token": token, "nickname": name, "site": site or "", "environment": environment,
               "migrate": False}
        valid, vmsg = await backend._validate_token(token)
        ok, message = (await backend.register_and_save(reg, None, current_wifi or None, via="ssh")
                       if valid else (False, backend._friendly_error(vmsg)))
        if ok:
            print(f"{GREEN}{BOLD}{name} is registered.{OFF} It will start sending readings within a few minutes "
                  "of its restart.")
            return EXIT_REGISTERED
        # Another unit may have taken that registration meanwhile: try the next one once more.
        tried.add(token)
        token = next_unused(tokens, invite_status(tokens), tried)
        if token is None or len(tried) >= 2:
            print(f"{RED}Not registered: {message}{OFF}")
            return EXIT_FAILED


def main() -> int:
    if os.geteuid() != 0:
        print("Run with sudo.")
        return EXIT_FAILED
    try:
        return asyncio.run(run(os.environ.get("SCHOOLAIR_INVITE", "")))
    except Stop:
        print("Not registered.")
        return EXIT_FAILED
    except RuntimeError as e:
        print(f"{RED}Not registered: {e}{OFF}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
