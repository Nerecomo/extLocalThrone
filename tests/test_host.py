"""Tests for the native host: input validation and Throne DB edits on fixtures.

Run: python -m unittest discover -s tests -v   (from the project root)
"""

import io
import json
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

import throne_db  # noqa: E402
import throne_host  # noqa: E402
import throne_proc  # noqa: E402
from throne_db import ThroneError, normalize_entry  # noqa: E402

SCHEMA = os.path.join(ROOT, "tests", "fixtures", "schema_1_2_2.sql")


def make_db(path, rules=None, current=8, raw=False, extra_profiles=()):
    """A Throne 1.2.2-shaped DB. rules: list of dicts for profile `current`."""
    con = sqlite3.connect(path)
    con.executescript(open(SCHEMA, encoding="utf-8").read())
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO settings VALUES ('current_route_id', ?)", (str(current),))
    con.execute("INSERT INTO route_profiles (id, name, default_outbound_id, is_raw) VALUES (?, 'Main', -2, ?)",
                (current, 1 if raw else 0))
    for pid in extra_profiles:
        con.execute("INSERT INTO route_profiles (id, name) VALUES (?, 'Other')", (pid,))
        con.execute("INSERT INTO route_rules (route_profile_id, rule_order, name, type, outbound_id, action, "
                    "domain_suffix_json) VALUES (?, 0, 'Simple Address Proxy', 1, -1, 'route', '[\"other.com\"]')",
                    (pid,))
    for order, r in enumerate(rules or []):
        con.execute(
            "INSERT INTO route_rules (route_profile_id, rule_order, name, type, outbound_id, action, protocol, "
            "domain_suffix_json, ip_cidr_json, domain_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (current, order, r.get("name", "r"), r.get("type", 1), r.get("outbound", -1), r.get("action", "route"),
             r.get("protocol", ""), json.dumps(r.get("suffix", [])), json.dumps(r.get("cidr", [])),
             json.dumps(r.get("domain", []))))
    con.commit()
    con.close()


def rule_lists(path, profile=8):
    con = sqlite3.connect(path)
    rows = con.execute("SELECT rule_order, type, outbound_id, domain_suffix_json, ip_cidr_json FROM route_rules "
                       "WHERE route_profile_id=? ORDER BY rule_order", (profile,)).fetchall()
    con.close()
    return [(o, t, out, json.loads(s or "[]"), json.loads(c or "[]")) for o, t, out, s, c in rows]


DNS = {"name": "dns-hijack", "type": 0, "outbound": -2, "action": "hijack-dns", "protocol": "dns"}
BYPASS = {"name": "Simple Address Bypass", "type": 2, "outbound": -2, "suffix": [".local"]}
PROXY = {"name": "Simple Address Proxy", "type": 1, "outbound": -1, "suffix": ["openai.com", "x.com"],
         "cidr": ["1.2.3.4"]}


class NormalizeTests(unittest.TestCase):
    def test_domains(self):
        self.assertEqual(normalize_entry("Example.CO.uk"), ("domain", "example.co.uk"))
        self.assertEqual(normalize_entry("  sub.example.com.  "), ("domain", "sub.example.com"))
        self.assertEqual(normalize_entry("xn--80ak6aa92e.com"), ("domain", "xn--80ak6aa92e.com"))
        self.assertEqual(normalize_entry("b-cdn.net"), ("domain", "b-cdn.net"))

    def test_wildcard(self):
        self.assertEqual(normalize_entry("*.example.com"), ("domain", ".example.com"))
        self.assertEqual(normalize_entry(".example.com"), ("domain", ".example.com"))
        self.assertEqual(throne_db.display("domain", ".example.com"), "*.example.com")

    def test_ips_and_subnets(self):
        self.assertEqual(normalize_entry("10.0.0.0/8"), ("ip", "10.0.0.0/8"))
        self.assertEqual(normalize_entry("10.1.2.3/8"), ("ip", "10.0.0.0/8"))
        self.assertEqual(normalize_entry("192.168.0.148"), ("ip", "192.168.0.148"))
        self.assertEqual(normalize_entry("2001:DB8::/32"), ("ip", "2001:db8::/32"))
        self.assertEqual(normalize_entry("::1"), ("ip", "::1"))

    def test_rejects(self):
        for bad in ["", "   ", "localhost", "exa mple.com", "пример.рф", "-bad.com", "bad-.com", "a..b.com",
                    "http://x.com", "x.com/path", "x.com;rm", "*.*.x.com", "x.*.com", "1.2.3", "300.1.1.1",
                    "10.0.0.0/33", "a" * 64 + ".com", "x.123", "\"x.com\"", None, 42, "gggg::1"]:
            with self.subTest(bad=bad), self.assertRaises(ThroneError):
                normalize_entry(bad)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "throne.db")
        self.backups = os.path.join(self.tmp.name, "backups")

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_to_end_of_proxy_rule(self):
        make_db(self.db, [DNS, BYPASS, PROXY], extra_profiles=[4])
        r = throne_db.modify(self.db, self.backups, add=["example.co.uk"])
        self.assertEqual(r["added"], ["example.co.uk"])
        self.assertTrue(r["changed"])
        rules = rule_lists(self.db)
        self.assertEqual(rules[2][3], ["openai.com", "x.com", "example.co.uk"])
        self.assertEqual(rules[1][3], [".local"], "bypass rule untouched")
        self.assertEqual(rule_lists(self.db, 4)[0][3], ["other.com"], "inactive profile untouched")
        self.assertEqual(r["entries"], ["openai.com", "x.com", "example.co.uk", "1.2.3.4"])

    def test_no_duplicates(self):
        make_db(self.db, [DNS, PROXY])
        r = throne_db.modify(self.db, self.backups, add=["X.com"])
        self.assertEqual(r["exists"], ["x.com"])
        self.assertFalse(r["changed"])
        self.assertIsNone(r["backup"])
        self.assertEqual(rule_lists(self.db)[1][3], ["openai.com", "x.com"])

    def test_subdomain_already_covered(self):
        make_db(self.db, [DNS, PROXY])
        r = throne_db.modify(self.db, self.backups, add=["api.openai.com"])
        self.assertEqual(r["covered"], {"api.openai.com": "openai.com"})
        self.assertFalse(r["changed"])

    def test_remove(self):
        make_db(self.db, [DNS, PROXY])
        r = throne_db.modify(self.db, self.backups, remove=["x.com"])
        self.assertEqual(r["removed"], ["x.com"])
        self.assertEqual(rule_lists(self.db)[1][3], ["openai.com"])
        r = throne_db.modify(self.db, self.backups, remove=["x.com"])
        self.assertEqual(r["missing"], ["x.com"])
        self.assertFalse(r["changed"])

    def test_wildcard_and_subnet_go_to_right_fields(self):
        make_db(self.db, [DNS, PROXY])
        throne_db.modify(self.db, self.backups, add=["*.cdn.example", "10.0.0.0/8", "2001:db8::/32"])
        _, _, _, suffix, cidr = rule_lists(self.db)[1]
        self.assertEqual(suffix, ["openai.com", "x.com", ".cdn.example"])
        self.assertEqual(cidr, ["1.2.3.4", "10.0.0.0/8", "2001:db8::/32"])
        r = throne_db.list_entries(self.db)
        self.assertIn("*.cdn.example", r["entries"])
        throne_db.modify(self.db, self.backups, remove=["*.cdn.example", "10.0.0.0/8"])
        self.assertEqual(rule_lists(self.db)[1][3:], (["openai.com", "x.com"], ["1.2.3.4", "2001:db8::/32"]))

    def test_creates_rule_when_missing(self):
        make_db(self.db, [DNS, BYPASS])
        r = throne_db.modify(self.db, self.backups, add=["example.com"])
        self.assertTrue(r["changed"])
        rules = rule_lists(self.db)
        self.assertEqual(len(rules), 3)
        self.assertEqual(rules[2], (2, 1, -1, ["example.com"], []))
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT name, action, domain_json, process_name_json, protocol FROM route_rules "
                          "WHERE route_profile_id=8 AND rule_order=2").fetchone()
        con.close()
        self.assertEqual(row, ("Simple Address Proxy", "route", "[]", "[]", ""))

    def test_ignores_non_proxy_and_inverted_rules(self):
        direct_with_domains = {"type": 1, "outbound": -2, "suffix": ["direct.com"]}
        make_db(self.db, [DNS, direct_with_domains, {"type": 0, "outbound": -1, "suffix": ["custom.com"]}, PROXY])
        throne_db.modify(self.db, self.backups, add=["new.com"])
        rules = rule_lists(self.db)
        self.assertEqual(rules[1][3], ["direct.com"])
        self.assertEqual(rules[2][3], ["custom.com"], "simple proxy rule preferred over custom")
        self.assertEqual(rules[3][3], ["openai.com", "x.com", "new.com"])

    def test_raw_profile_refused(self):
        make_db(self.db, [DNS], raw=True)
        with self.assertRaises(ThroneError) as cm:
            throne_db.modify(self.db, self.backups, add=["a.com"])
        self.assertEqual(cm.exception.code, "config-error")

    def test_missing_active_profile(self):
        make_db(self.db, [DNS, PROXY])
        con = sqlite3.connect(self.db)
        con.execute("UPDATE settings SET value='99' WHERE key='current_route_id'")
        con.commit()
        con.close()
        with self.assertRaises(ThroneError):
            throne_db.list_entries(self.db)

    def test_missing_db(self):
        with self.assertRaises(ThroneError) as cm:
            throne_db.list_entries(os.path.join(self.tmp.name, "nope.db"))
        self.assertEqual(cm.exception.code, "config-error")

    def test_invalid_input_writes_nothing(self):
        make_db(self.db, [DNS, PROXY])
        before = open(self.db, "rb").read()
        with self.assertRaises(ThroneError):
            throne_db.modify(self.db, self.backups, add=["good.com", "bad domain"])
        self.assertEqual(rule_lists(self.db)[1][3], ["openai.com", "x.com"])
        self.assertFalse(os.path.exists(self.backups))
        self.assertEqual(before, open(self.db, "rb").read())

    def test_backup_is_complete_and_rotated(self):
        make_db(self.db, [DNS, PROXY])
        r = throne_db.modify(self.db, self.backups, add=["a1.com"])
        con = sqlite3.connect(r["backup"])
        self.assertEqual(json.loads(con.execute("SELECT domain_suffix_json FROM route_rules WHERE rule_order=1")
                                    .fetchone()[0]), ["openai.com", "x.com"], "backup holds the state before")
        con.close()
        for i in range(5):
            throne_db.backup(self.db, self.backups, keep=3)
        self.assertEqual(len(os.listdir(self.backups)), 3)

    def test_locked_db_reports_config_error(self):
        make_db(self.db, [DNS, PROXY])
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")  # e.g. Throne in the middle of a write
        old = throne_db.BUSY_TIMEOUT_MS
        throne_db.BUSY_TIMEOUT_MS = 200
        try:
            with self.assertRaises(ThroneError) as cm:
                throne_db.modify(self.db, self.backups, add=["z.com"])
            self.assertEqual(cm.exception.code, "config-error")
        finally:
            throne_db.BUSY_TIMEOUT_MS = old
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(rule_lists(self.db)[1][3], ["openai.com", "x.com"])

    def test_concurrent_writers_lose_nothing(self):
        make_db(self.db, [DNS, PROXY])
        errors = []

        def worker(i):
            try:
                throne_db.modify(self.db, self.backups, add=[f"site{i}.com"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        suffix = rule_lists(self.db)[1][3]
        done = {f"site{i}.com" for i in range(8)} & set(suffix)
        # A writer may be told to retry ("changed during write"), but nothing may be silently lost.
        self.assertEqual(len(done) + len(errors), 8)
        self.assertEqual(len(suffix), len(set(suffix)))

    def test_writer_killed_mid_transaction_leaves_db_intact(self):
        make_db(self.db, [DNS, PROXY])
        con = sqlite3.connect(self.db, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE route_rules SET domain_suffix_json='garbage' WHERE rule_order=1")
        con.close()  # closed without COMMIT ~ process died mid-write
        self.assertEqual(rule_lists(self.db)[1][3], ["openai.com", "x.com"])
        check = sqlite3.connect(self.db)
        self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        check.close()


class HostProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.throne = os.path.join(self.tmp.name, "Throne")
        os.makedirs(os.path.join(self.throne, "config"))
        make_db(os.path.join(self.throne, "config", "throne.db"), [DNS, PROXY])
        self.data = os.path.join(self.tmp.name, "data")
        self.cfg = {"throneDir": self.throne}

    def tearDown(self):
        self.tmp.cleanup()

    def test_message_framing_roundtrip(self):
        buf = io.BytesIO()
        throne_host.write_message(buf, {"cmd": "add", "value": "пример"})
        buf.seek(0)
        self.assertEqual(throne_host.read_message(buf), {"cmd": "add", "value": "пример"})
        self.assertIsNone(throne_host.read_message(io.BytesIO(b"")))
        self.assertEqual(struct.unpack("<I", buf.getvalue()[:4])[0], len(buf.getvalue()) - 4)

    def test_add_list_remove_flow(self):
        # The fixture "Throne" folder has no running processes: changes are saved, restart is skipped.
        r = throne_host.handle({"cmd": "add", "value": "example.org"}, self.cfg, self.data)
        self.assertEqual(r["added"], ["example.org"])
        self.assertEqual(r["restart"], {"restarted": False, "reason": "throne-off"})
        self.assertFalse(r["throneRunning"])
        r = throne_host.handle({"cmd": "add", "value": "example.org"}, self.cfg, self.data)
        self.assertEqual(r["exists"], ["example.org"])
        self.assertEqual(r["restart"]["reason"], "unchanged")
        r = throne_host.handle({"cmd": "list"}, self.cfg, self.data)
        self.assertEqual(r["entries"][-2:], ["example.org", "1.2.3.4"])
        self.assertEqual(r["profile"]["name"], "Main")
        r = throne_host.handle({"cmd": "remove", "value": "example.org"}, self.cfg, self.data)
        self.assertEqual(r["removed"], ["example.org"])
        r = throne_host.handle({"cmd": "import", "values": ["a.com", "b.com", "x.com"]}, self.cfg, self.data)
        self.assertEqual((r["added"], r["exists"]), (["a.com", "b.com"], ["x.com"]))

    def test_unknown_command(self):
        with self.assertRaises(ThroneError):
            throne_host.handle({"cmd": "rm -rf"}, self.cfg, self.data)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.procs = {"Throne.exe": [10], "ThroneCore.exe": [20]}
        self.killed = []
        self.slept = []
        self.clock = [1000.0]
        self._orig = (throne_proc.find_processes, throne_proc._terminate)

        def find(name, _dir=None):
            return list(self.procs.get(name, []))

        def term(pid):
            self.killed.append(pid)
            self.procs["ThroneCore.exe"] = [pid + 1]  # Throne starts a fresh core
            return True

        throne_proc.find_processes, throne_proc._terminate = find, term

    def tearDown(self):
        throne_proc.find_processes, throne_proc._terminate = self._orig
        self.tmp.cleanup()

    def sleep(self, s):
        self.slept.append(s)
        self.clock[0] += s

    def restart(self):
        return throne_proc.restart_core("X", self.state, sleep=self.sleep, now=lambda: self.clock[0])

    def test_restart_kills_core_and_sees_new_one(self):
        self.assertEqual(self.restart()["restarted"], True)
        self.assertEqual(self.killed, [20])

    def test_restarts_are_spaced(self):
        self.restart()
        self.clock[0] += 3
        self.restart()
        self.assertAlmostEqual(max(self.slept), throne_proc.MIN_RESTART_INTERVAL - 3)
        self.assertEqual(len(self.killed), 2)

    def test_throne_closed(self):
        self.procs = {}
        self.assertEqual(self.restart(), {"restarted": False, "reason": "throne-off"})
        self.assertEqual(self.killed, [])

    def test_proxy_not_started(self):
        self.procs = {"Throne.exe": [10]}
        self.assertEqual(self.restart()["reason"], "core-off")


if __name__ == "__main__":
    unittest.main()
