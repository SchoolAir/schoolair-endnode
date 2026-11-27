#!/usr/bin/env python3
"""pigpio_watch.py: write down every program that connects to pigpiod.

Why: twice on 2026-11-26 (22:55 and 23:13) the flower stopped mid-move because
pigpio reported a wave that was not the flower's ("wave 1"), then homed for no
reason. Either another program talked to pigpiod at that moment, or pigpio's
own report was wrong. This service settles the first question: it logs, to the
journal, each connection to pigpiod as it opens and closes, with the program's
PID and command line. Then after the next event:

    journalctl -u schoolair-pigpio-watch --since "<time of the abort> -1 min"

shows who was connected. The flower service itself holds two connections
(pigpio's Python library opens a command and a notification socket), so a
quiet log after it starts is the normal state.

It is NOT a pigpio client: it only reads the kernel's socket table with `ss`,
once a second, so it cannot disturb a move. It runs as root because only root
can see which process owns another user's socket. Flower units only.
"""

import re
import subprocess
import sys
import time

PIGPIO_PORT = 8888
POLL_S = 1.0

# One ss line, client side of a connection to pigpiod, e.g.
#   ESTAB 0 0 [::1]:56574 [::1]:8888 users:(("python3",pid=18711,fd=3))
_LINE = re.compile(r"(?P<state>\S+)\s+\d+\s+\d+\s+(?P<local>\S+)\s+(?P<peer>\S+)\s*(?P<users>users:.*)?$")
_PID = re.compile(r'\("(?P<name>[^"]*)",pid=(?P<pid>\d+)')


def parse_ss(text: str, port: int = PIGPIO_PORT) -> dict:
    """The client connections to pigpiod in `ss -Htnp` output, as
    {client_address: [(pid, name), ...]}. Lines for pigpiod's own end (local
    port 8888) are skipped: they only name pigpiod."""
    clients = {}
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m or m.group("state") != "ESTAB" or not m.group("peer").endswith(f":{port}"):
            continue
        owners = [(int(p.group("pid")), p.group("name")) for p in _PID.finditer(m.group("users") or "")]
        clients[m.group("local")] = owners
    return clients


def cmdline(pid: int) -> str:
    """The process's full command line, or "" once it has gone."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def describe(owners: list, seen_cmd: dict) -> str:
    if not owners:
        return "process unknown"
    return ", ".join(f"pid {pid} {seen_cmd.get(pid) or name}" for pid, name in owners)


def changes(before: dict, after: dict, seen_cmd: dict) -> list:
    """Log lines for connections that opened (+) or closed (-)."""
    lines = []
    for addr in sorted(after.keys() - before.keys()):
        lines.append(f"+ {addr} {describe(after[addr], seen_cmd)}")
    for addr in sorted(before.keys() - after.keys()):
        lines.append(f"- {addr} {describe(before[addr], seen_cmd)}")
    return lines


def snapshot() -> dict:
    out = subprocess.run(["ss", "-Htnp", f"( dport = :{PIGPIO_PORT} )"],
                         capture_output=True, text=True, timeout=5).stdout
    return parse_ss(out)


def main() -> int:
    print(f"[pigpio-watch] logging connections to pigpiod (port {PIGPIO_PORT}) every {POLL_S:.0f} s")
    current, seen_cmd = {}, {}
    while True:
        try:
            now = snapshot()
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[pigpio-watch] ss failed: {e}")
            time.sleep(10)
            continue
        for owners in now.values():
            for pid, _ in owners:
                if pid not in seen_cmd:
                    seen_cmd[pid] = cmdline(pid)  # read while it still runs
        lines = changes(current, now, seen_cmd)
        for line in lines:
            print(f"[pigpio-watch] {line}")
        if lines:
            print(f"[pigpio-watch] now {len(now)} connection(s)")
        live = {pid for owners in now.values() for pid, _ in owners}
        seen_cmd = {pid: c for pid, c in seen_cmd.items() if pid in live}
        current = now
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
