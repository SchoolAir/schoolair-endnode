"""pigpio_watch.py: the passive logger of who connects to pigpiod."""

from pathlib import Path

import pigpio_watch as pw

ROOT = Path(__file__).resolve().parent.parent

# Real shape of `ss -Htnp '( dport = :8888 )'` as root on a flower Pi: the
# flower service's two sockets, one pigs call, one half-closed line to skip.
SS = """\
ESTAB 0 0 [::1]:56574 [::1]:8888 users:(("python3",pid=18711,fd=3))
ESTAB 0 0 [::1]:56578 [::1]:8888 users:(("python3",pid=18711,fd=4))
ESTAB 0 0 127.0.0.1:40122 127.0.0.1:8888 users:(("pigs",pid=20001,fd=3))
TIME-WAIT 0 0 [::1]:56000 [::1]:8888
"""


def test_parse_ss_reads_clients_and_owners():
    c = pw.parse_ss(SS)
    assert set(c) == {"[::1]:56574", "[::1]:56578", "127.0.0.1:40122"}
    assert c["[::1]:56574"] == [(18711, "python3")]
    assert c["127.0.0.1:40122"] == [(20001, "pigs")]


def test_parse_ss_skips_pigpiods_own_end_and_other_ports():
    text = ('ESTAB 0 0 [::1]:8888 [::1]:56574 users:(("pigpiod",pid=216,fd=5))\n'
            'ESTAB 0 0 192.168.6.242:22 192.168.6.244:57293 users:(("sshd",pid=1,fd=4))\n')
    assert pw.parse_ss(text) == {}


def test_parse_ss_without_process_info_still_counts_the_connection():
    c = pw.parse_ss("ESTAB 0 0 [::1]:1 [::1]:8888\n")
    assert c == {"[::1]:1": []}
    assert pw.describe([], {}) == "process unknown"


def test_changes_logs_opened_and_closed_with_command_line():
    before = pw.parse_ss(SS)
    after = {k: v for k, v in before.items() if k != "127.0.0.1:40122"}
    after["[::1]:60000"] = [(30000, "python3")]
    seen = {20001: "pigs wvtx 1", 30000: "python3 flower_service.py --nod"}
    lines = pw.changes(before, after, seen)
    assert lines == ["+ [::1]:60000 pid 30000 python3 flower_service.py --nod",
                     "- 127.0.0.1:40122 pid 20001 pigs wvtx 1"]


def test_no_change_no_log():
    c = pw.parse_ss(SS)
    assert pw.changes(c, dict(c), {}) == []


def test_cmdline_of_a_gone_process_is_empty():
    assert pw.cmdline(2 ** 22 + 12345) == ""


def test_service_is_passive_root_and_flower_only():
    svc = (ROOT / "deploy" / "schoolair-pigpio-watch.service").read_text()
    assert "ConditionPathExists=/etc/schoolair-flower-fitted" in svc
    assert "User=root" in svc
    assert "pigpio_watch.py" in svc
    assert "import pigpio" not in (ROOT / "pigpio_watch.py").read_text()   # never a client


def test_setup_installs_enables_restarts_and_checks_it():
    setup = (ROOT / "schoolair_setup.sh").read_text()
    assert "schoolair-pigpio-watch.service; do" in setup
    assert "systemctl enable schoolair-pigpio-watch.service" in setup
    assert "systemctl restart schoolair-pigpio-watch.service" in setup
    assert 'chk "schoolair-pigpio-watch enabled"' in setup
