#!/usr/bin/env python3
"""Offline tests of scripts/wp_site_model.py: the field rules, the registry on disk, the allocator and the fit check.

Three things are tied to other code on purpose, because the same rule exists in more than one place until scripts/wp_sites.py retires: the fit check is run against the play's own assert
(tasks/wp_sites_common.yml, evaluated with the helpers of tests/test_wp_sites_render.py) on hand-made lists and on a few hundred random ones, and must name the same broken rules; the field rules
are run against scripts/wp_sites.py on a grid of values; the name pattern is compared with the other two copies. What they do not prove: that Ansible itself evaluates the assert as the offline
helper does (the helper is a model of it), or anything on the host. Run: python3 -I tests/test_wp_site_model.py"""
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import wp_ctl  # noqa: E402
import wp_site_model as m  # noqa: E402
import wp_sites  # noqa: E402
import test_wp_sites_render as play  # noqa: E402


def defaults():
    return play.base_vars()["wp_site_defaults"]


def limits():
    v, vm = play.base_vars(), play.dellwp_vm()
    return dict(reserved_names=v["wp_reserved_names"], vm_memory_mb=vm["memory"], data_disk_gb=vm["data_disk"], ram_reserve_mb=v["wp_vm_ram_reserve_mb"], data_reserve_mb=v["wp_data_reserve_mb"],
                backup_quota_offset=v["wp_backup_quota_offset"], scratch_quota_id=v["wp_scratch_quota_id"], archive_quota_id=v["wp_archive_quota_id"], intake_quota_id=v["wp_intake_quota_id"])


def record(name="alpha", port=9101, quota_id=1001, state="hold", **more):
    return {"schema": m.SCHEMA, "name": name, "port": port, "quota_id": quota_id, "state": state, **more}


class Fields(unittest.TestCase):
    def test_the_ranges_are_those_of_the_script_this_replaces(self):
        for key, spec in wp_sites.EDITABLE.items():
            if key == "state":
                continue
            self.assertEqual(m.EDITABLE[key], spec, key)
        self.assertEqual(set(m.EDITABLE) - set(wp_sites.EDITABLE), {"label", "note"})

    def test_the_same_verdict_as_the_script_on_a_grid_of_values(self):
        grid = ["", " ", "0", "1", "7", "383", "384", "512", "4096", "4097", "100000", "100001", "999999", "1000000", "-1", "1.5", "abc", "true", "false", "True", "auto", "notify", "freeze",
                "log", "send", "hold", " 512 ", "512\n", "٣٤٥"]
        for key in wp_sites.EDITABLE:
            if key == "state":
                continue
            for raw in grid:
                self.assertEqual(m.validate_value(key, raw), wp_sites.validate_value(key, raw), (key, raw))

    def test_a_typed_value_is_taken_as_a_string_would_be(self):
        self.assertEqual(m.validate_value("php_mem_mb", 512), (512, None))
        self.assertEqual(m.validate_value("allow_xmlrpc", True), (True, None))
        self.assertEqual(m.validate_value("allow_xmlrpc", "false"), (False, None))
        self.assertIsNotNone(m.validate_value("php_mem_mb", True)[1], "a switch is not a number")
        self.assertIsNotNone(m.validate_value("allow_xmlrpc", 1)[1], "only true or false")

    def test_label_and_note(self):
        self.assertEqual(m.validate_value("label", "x" * 100), ("x" * 100, None))
        self.assertIsNotNone(m.validate_value("label", "x" * 101)[1])
        self.assertEqual(m.validate_value("note", "n" * 500), ("n" * 500, None))
        self.assertIsNotNone(m.validate_value("note", "n" * 501)[1])
        for bad in ("a\nb", "a\x00b", "a\x7fb", "tab\there"):
            self.assertIsNotNone(m.validate_value("label", bad)[1], repr(bad))
        self.assertIsNotNone(m.validate_value("label", 5)[1], "text only")
        self.assertEqual(m.validate_value("label", "Ünïcode ok 日本")[1], None)

    def test_what_the_page_may_never_change(self):
        for key in ("name", "port", "quota_id", "state", "secret_rev", "schema", "created", "cpus", "pids_limit", "php_memory_limit", "fpm_max_children", "opcache_mb", "db_buffer_pool_mb",
                    "php_allow_functions"):
            self.assertNotIn(key, m.EDITABLE, key)
            self.assertIsNotNone(m.validate_value(key, "1")[1], key)

    def test_every_key_of_a_record_is_a_default_or_registry_only(self):
        hidden_and_fixed = set(m.FIXED_KEYS) | set(m.HIDDEN_KEYS)
        for key in set(defaults()) - {"state"}:
            self.assertIn(key, m.RECORD_KEYS, "wp_site_defaults has %s but a record may not carry it" % key)
        self.assertEqual(set(m.TEXT_KEYS) & set(defaults()), set(), "label and note are registry-only")
        self.assertTrue(set(m.HIDDEN_KEYS) <= set(defaults()), "a hidden key without a default would be undefined in a template")
        self.assertTrue(hidden_and_fixed <= m.RECORD_KEYS)


STAMP = "20261028T120000Z"


class Names(unittest.TestCase):
    def test_the_pattern_is_the_other_copies_pattern(self):
        self.assertEqual(m.NAME_RE.pattern, wp_sites.NAME_RE.pattern)
        self.assertEqual(m.NAME_RE.pattern, wp_ctl.NAME_RE.pattern)

    def test_the_rules(self):
        res, taken = ["hub", "vps-hub", "www"], {"alpha"}
        ok = lambda n: m.check_name(n, res, taken)  # noqa: E731
        for good in ("ab", "a1", "my-shop", "a" + "b" * 22 + "c", "x9"):
            self.assertIsNone(ok(good), good)
        for bad in ("", "a", "1ab", "-ab", "ab-", "a--b", "Ab", "a_b", "a.b", "a b", "a" * 25, "ab\n", "日本"):
            self.assertIsNotNone(ok(bad), repr(bad))
        self.assertIn("reserved", ok("vps-hub"))
        self.assertIsNone(ok("gone"), "the name of a closed site may be used again: only existing sites hold a name")
        self.assertIn("already exists", ok("alpha"))
        self.assertIsNotNone(ok(None))
        self.assertIsNotNone(ok(5))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data, self.tomb = Path(self.tmp.name, "sites"), Path(self.tmp.name, "tombstones")
        self.data.mkdir()
        self.reg = m.Registry(self.data, self.tomb, defaults())

    def site(self, name, **kw):
        (self.data / name).mkdir(exist_ok=True)
        rec = record(name, **kw)
        self.reg.write_site(rec)
        return rec

    def test_a_record_survives_a_round_trip_and_is_world_readable_not_writable(self):
        rec = self.site("alpha", label="Alpha", php_mem_mb=640)
        self.assertEqual(self.reg.read_site("alpha"), rec)
        self.assertEqual(os.stat(self.reg.site_file("alpha")).st_mode & 0o777, 0o644)
        self.assertEqual(sorted(p.name for p in (self.data / "alpha").iterdir()), ["site.json"], "no temp file is left")

    def test_the_file_is_stable_json_ending_in_a_newline(self):
        self.site("alpha")
        text = self.reg.site_file("alpha").read_text()
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n")

    def test_a_failed_write_keeps_the_old_record_and_leaves_no_temp_file(self):
        self.site("alpha", php_mem_mb=640)
        real = os.replace
        os.replace = lambda *a: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with self.assertRaises(OSError):
                self.reg.write_site(record("alpha", php_mem_mb=900))
        finally:
            os.replace = real
        self.assertEqual(self.reg.read_site("alpha")["php_mem_mb"], 640)
        self.assertEqual(sorted(p.name for p in (self.data / "alpha").iterdir()), ["site.json"])

    def test_the_data_is_flushed_to_disk_before_it_replaces_the_old_record(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        os.fsync = lambda fd: (order.append("fsync"), real_fsync(fd))[1]
        os.replace = lambda a, b: (order.append("replace"), real_replace(a, b))[1]
        try:
            self.site("alpha")
        finally:
            os.fsync, os.replace = real_fsync, real_replace
        self.assertEqual(order, ["fsync", "replace"])

    def test_a_bad_record_is_not_written(self):
        (self.data / "alpha").mkdir()
        for bad in (record("alpha", php_mem_mb=10), record("alpha", mail_mode="x"), record("alpha", surprise=1), record("alpha", port="9101"), record("alpha", port=True),
                    record("alpha", state="paused"), record("Alpha"), record("alpha", secret_rev="1"), {**record("alpha"), "schema": 2}, record("alpha", cpus=1)):
            with self.assertRaises(m.RegistryError, msg=str(bad)):
                self.reg.write_site(bad)
        self.assertFalse(self.reg.site_file("alpha").exists())

    def test_closed_is_a_tombstone_not_a_site_record(self):
        (self.data / "alpha").mkdir()
        with self.assertRaises(m.Refuse):
            self.reg.write_site(record("alpha", state="closed"))

    def test_corrupt_records_stop_the_reader(self):
        for name, text in (("a", "{not json"), ("b", "[]"), ("c", json.dumps({**record("zzz")})), ("d", json.dumps({**record("d"), "extra": 1})), ("e", "")):
            (self.data / name).mkdir()
            (self.data / name / "site.json").write_text(text)
            with self.assertRaises(m.RegistryError, msg=name):
                self.reg.read_site(name)

    def test_a_link_or_a_missing_file_is_not_a_record(self):
        (self.data / "alpha").mkdir()
        self.assertIsNone(self.reg.read_site("alpha"))
        real = Path(self.tmp.name, "elsewhere.json")
        real.write_text(json.dumps(record("alpha")))
        os.symlink(real, self.data / "alpha" / "site.json")
        self.assertIsNone(self.reg.read_site("alpha"), "a link is not a record (the same rule as require_site)")
        os.symlink(self.data / "alpha", self.data / "linked")
        self.assertEqual(self.reg.sites(), [], "a linked directory is not a site")

    def test_sites_lists_records_by_name_and_skips_directories_without_one(self):
        self.site("bravo", port=9102, quota_id=1002)
        self.site("alpha")
        (self.data / "plain").mkdir()
        (self.data / "file.txt").write_text("x")
        self.assertEqual([r["name"] for r in self.reg.sites()], ["alpha", "bravo"])
        self.assertEqual(m.Registry(Path(self.tmp.name, "none"), self.tomb, defaults()).sites(), [])

    def test_a_tombstone_outlives_the_site_directory(self):
        self.site("alpha")
        self.reg.write_tombstone(record("alpha", state="hold", created="2026-10-28"), STAMP)
        import shutil
        shutil.rmtree(self.data / "alpha")
        self.assertEqual(self.reg.sites(), [])
        tomb = self.reg.tombstones()
        self.assertEqual([(t["name"], t["state"], t["port"], t["quota_id"]) for t in tomb], [("alpha", "closed", 9101, 1001)])
        self.assertEqual(os.stat(self.tomb).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(self.reg.tombstone_file("alpha", STAMP)).st_mode & 0o777, 0o644)
        self.assertEqual(self.reg.tombstone_file("alpha", STAMP).name, "alpha.%s.json" % STAMP)

    def test_a_tombstone_must_be_closed_and_named_like_its_file(self):
        self.tomb.mkdir()
        (self.tomb / "alpha.json").write_text(json.dumps(record("alpha", state="live")))
        with self.assertRaises(m.RegistryError):
            self.reg.tombstones()
        (self.tomb / "alpha.json").write_text(json.dumps(record("other", state="closed")))
        with self.assertRaises(m.RegistryError):
            self.reg.tombstones()
        (self.tomb / "alpha.json").unlink()
        for odd in ("Alpha.json", "alpha.2026.json", "alpha.%s.txt.json" % STAMP, "alpha.%s.json.bak.json" % STAMP):
            (self.tomb / odd).write_text(json.dumps(record("alpha", state="closed")))
            with self.assertRaises(m.RegistryError, msg=odd):
                self.reg.tombstones()
            (self.tomb / odd).unlink()

    def test_a_name_may_have_several_tombstones_and_the_old_unstamped_file_still_counts(self):
        self.tomb.mkdir()
        (self.tomb / "alpha.json").write_text(json.dumps(record("alpha", state="closed")))
        self.reg.write_tombstone(record("alpha", port=9105, quota_id=1005), "20261101T000000Z")
        self.reg.write_tombstone(record("alpha", port=9106, quota_id=1006), "20261102T000000Z")
        self.assertEqual([p.name for p in self.reg.tombstone_files("alpha")], ["alpha.json", "alpha.20261101T000000Z.json", "alpha.20261102T000000Z.json"])
        self.assertEqual(self.reg.tombstone_files("alph"), [], "a name that is a prefix of another is not that name")
        self.assertEqual(sorted(t["port"] for t in self.reg.tombstones()), [9101, 9105, 9106])
        with self.assertRaises(m.RegistryError):
            self.reg.write_tombstone(record("alpha", port=9105, quota_id=1005), "20261101T000000Z")

    def test_a_linked_tombstone_is_not_a_tombstone(self):
        self.tomb.mkdir()
        (self.tomb / "real").write_text("{}")
        os.symlink(self.tomb / "real", self.tomb / "alpha.json")
        self.assertEqual(self.reg.tombstone_files("alpha"), [])


class Allocation(unittest.TestCase):
    def test_a_first_site_gets_the_first_numbers(self):
        self.assertEqual(m.next_numbers([]), (9101, 1001), "the same numbers scripts/wp_sites.py gives")

    def test_above_everything_ever_recorded(self):
        sites = [{"name": "a", "port": 9105, "quota_id": 1003}, {"name": "b", "port": 9102, "quota_id": 1009, "state": "closed"}]
        self.assertEqual(m.next_numbers(sites), (9106, 1010))

    def test_the_registry_the_tombstones_and_the_repo_are_one_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = m.Registry(Path(tmp, "sites"), Path(tmp, "tombstones"), defaults())
            (Path(tmp, "sites") / "alpha").mkdir(parents=True)
            reg.write_site(record("alpha", port=9111, quota_id=1011))
            reg.write_tombstone(record("beta", port=9112, quota_id=1012), STAMP)
            known = [{"name": "old", "port": 9109, "quota_id": 1009, "state": "closed"}]
            every = m.everything(reg, known)
            self.assertEqual([s["name"] for s in every], ["alpha", "beta", "old"])
            self.assertEqual(m.next_numbers(every), (9113, 1013))

    def test_the_same_site_in_two_places_is_one_entry_and_closed_stays_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = m.Registry(Path(tmp, "sites"), Path(tmp, "tombstones"), defaults())
            reg.write_tombstone(record("beta", port=9112, quota_id=1012), STAMP)
            every = m.everything(reg, [{"name": "beta", "port": 9112, "quota_id": 1012, "state": "live"}])
            self.assertEqual([(s["name"], s["state"]) for s in every], [("beta", "closed")])

    def test_numbers_are_never_shared_by_two_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = m.Registry(Path(tmp, "sites"), Path(tmp, "tombstones"), defaults())
            reg.write_tombstone(record("beta", port=9112, quota_id=1012), STAMP)
            for other in ({"name": "gamma", "port": 9112, "quota_id": 1012}, {"name": "beta", "port": 9999, "quota_id": 1012}, {"name": "beta", "port": 9112, "quota_id": 1999}, {"name": "gamma", "port": 9112, "quota_id": 1999}):
                with self.assertRaises(m.RegistryError, msg=str(other)):
                    m.everything(reg, [dict(other, state="closed")])

    def test_a_name_that_was_closed_may_have_a_new_site_with_new_numbers_and_both_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = m.Registry(Path(tmp, "sites"), Path(tmp, "tombstones"), defaults())
            (Path(tmp, "sites") / "beta").mkdir(parents=True)
            reg.write_tombstone(record("beta", port=9112, quota_id=1012), STAMP)
            reg.write_site(record("beta", port=9113, quota_id=1013))
            every = m.everything(reg)
            self.assertEqual([(s["name"], s["port"], s["state"]) for s in every], [("beta", 9112, "closed"), ("beta", 9113, "hold")])
            self.assertEqual(m.next_numbers(every), (9114, 1014))


class Transitions(unittest.TestCase):
    ALLOWED = {("hold", "live"): ["record", "apply"], ("hold", "stopped"): ["backup", "record", "apply"], ("live", "hold"): ["record", "apply"], ("live", "stopped"): ["backup", "record", "apply"],
               ("stopped", "hold"): ["record", "apply"]}

    def test_every_pair_of_states_is_decided(self):
        for current in m.STATES:
            for target in ("hold", "live", "stopped"):
                with self.subTest(current=current, target=target):
                    if current == target and current != "closed":
                        self.assertEqual(m.plan_state(current, target), [])
                    elif (current, target) in self.ALLOWED:
                        self.assertEqual(m.plan_state(current, target), self.ALLOWED[(current, target)])
                    else:
                        with self.assertRaises(m.Refuse):
                            m.plan_state(current, target)

    def test_a_site_is_stopped_only_after_a_backup_and_a_stopped_site_never_goes_straight_to_live(self):
        for current in ("hold", "live"):
            self.assertEqual(m.plan_state(current, "stopped")[0], "backup")
        for current in ("hold", "live", "stopped"):
            for target in ("hold", "live"):
                self.assertNotIn("backup", m.plan_state(current, target) if (current, target) in self.ALLOWED or current == target else [])
        with self.assertRaises(m.Refuse) as e:
            m.plan_state("stopped", "live")
        self.assertIn("hold first", str(e.exception))

    def test_closed_is_terminal_and_is_not_a_target(self):
        for target in ("hold", "live", "stopped", "closed"):
            with self.assertRaises(m.Refuse) as e:
                m.plan_state("closed", target)
            if target != "closed":
                self.assertIn("the site is closed", str(e.exception), "the reason names the tombstone, not a move that is merely not in the table")
        for current in ("hold", "live", "stopped"):
            with self.assertRaises(m.Refuse) as e:
                m.plan_state(current, "closed")
            self.assertIn("deleting", str(e.exception))

    def test_odd_input_is_refused_not_guessed(self):
        for current, target in (("paused", "live"), ("hold", "LIVE"), ("hold", ""), ("hold", None), (None, "live"), ("hold", ["live"])):
            with self.subTest(current=current, target=target), self.assertRaises(m.Refuse):
                m.plan_state(current, target)

    def test_the_table_and_the_states_agree(self):
        self.assertEqual(set(m.TRANSITIONS), set(m.STATES))
        for current, targets in m.TRANSITIONS.items():
            self.assertTrue(set(targets) <= set(m.STATES) - {"closed", current}, current)

    def test_the_dispatcher_offers_exactly_the_keys_and_states_the_model_knows(self):
        self.assertEqual(set(wp_ctl.SET_KEYS), set(m.EDITABLE), "the dispatcher's list of editable keys is the model's")
        self.assertEqual(set(wp_ctl.STATES), set(m.STATES) - {"closed"})
        self.assertLessEqual(max(spec[1] for spec in m.EDITABLE.values() if spec[0] == "text"), wp_ctl.SET_VALUE_MAX)


class Fit(unittest.TestCase):
    """The fit check against the play's assert, expression by expression."""

    @classmethod
    def setUpClass(cls):
        cls.v, cls.that, cls.defaults, cls.limits = play.base_vars(), play.assertions(), defaults(), limits()

    def ansible(self, sites):
        bad = play.failing(self.that, self.v, sites)
        return sorted(self.that.index(expr) for expr in bad)

    def mine(self, sites):
        return m.broken_rules([m.merged(self.defaults, s) for s in sites], self.limits)

    def same(self, sites, msg=""):
        expect = self.ansible(sites)
        exist = [s["name"] for s in sites if s.get("state", "hold") != "closed"]
        if 4 in expect and len(set(exist)) == len(exist):
            expect.remove(4)             # the one deliberate difference: the play counts a closed site's name as taken, the model does not (a closed name may be used again). The play's copy goes with the play's per-site code
        self.assertEqual(self.mine(sites), expect, msg or str(sites))

    def test_a_closed_name_does_not_break_the_uniqueness_rule_but_two_that_exist_do(self):
        a = self.site("alpha", 1)
        self.assertEqual(self.mine([a, {**self.site("alpha", 2), "state": "closed"}]), [])
        self.assertEqual(self.mine([a, self.site("alpha", 2)]), [4])
        self.assertEqual(self.mine([a, {**self.site("alpha", 2), "state": "stopped"}]), [4], "a stopped site still exists")
        self.assertEqual(self.mine([a, {**self.site("alpha", 2), "port": a["port"], "state": "closed"}]), [5], "ports are never shared, closed or not")

    def test_there_is_a_rule_for_each_expression_in_the_same_order(self):
        self.assertEqual(len(m.RULES), len(self.that))

    def test_the_real_list_and_the_fixture_fit(self):
        self.same(self.v["real_wp_sites"])
        self.same(self.v["wp_sites"])
        self.assertEqual(self.mine(self.v["wp_sites"]), [])

    def site(self, name, n, **more):
        return {"name": name, "port": 9100 + n, "quota_id": 1000 + n, "state": "hold", "update_policy": "notify", **more}

    def test_hand_made_breakages_name_the_same_rule(self):
        a, b = self.site("alpha", 1), self.site("bravo", 2)
        cases = {
            "fits": [a, b],
            "duplicate name": [a, {**b, "name": "alpha"}],
            "duplicate port": [a, {**b, "port": a["port"]}],
            "duplicate quota": [a, {**b, "quota_id": a["quota_id"]}],
            "low port": [{**a, "port": 80}],
            "port 1024": [{**a, "port": 1024}],
            "port 1025": [{**a, "port": 1025}],
            "low quota": [{**a, "quota_id": 999}],
            "quota 1000": [{**a, "quota_id": 1000}],
            "reserved": [{**a, "name": "hub"}, b],
            "vps-hub is reserved": [{**a, "name": "vps-hub"}],
            "bad name": [{**a, "name": "A"}],
            "one letter": [{**a, "name": "a"}],
            "24 letters": [{**a, "name": "a" * 24}],
            "25 letters": [{**a, "name": "a" * 25}],
            "trailing hyphen": [{**a, "name": "abc-"}],
            "bad state": [{**a, "state": "paused"}],
            "bad policy": [{**a, "update_policy": "always"}],
            "closed": [{**a, "state": "closed"}, b],
            "too much memory": [a, {**b, "php_mem_mb": 99999}],
            "stopped memory does not count": [a, {**b, "php_mem_mb": 99999, "state": "stopped"}],
            "closed memory does not count": [a, {**b, "php_mem_mb": 99999, "state": "closed"}],
            "too much disk": [a, {**b, "quota_mb": 999999}],
            "stopped disk counts": [a, {**b, "quota_mb": 999999, "state": "stopped"}],
            "closed disk does not count": [a, {**b, "quota_mb": 999999, "state": "closed"}],
            "wide spread of ids": [a, {**b, "quota_id": 1000 + self.v["wp_backup_quota_offset"] + 5}],
            "id at the scratch project": [{**a, "quota_id": self.v["wp_scratch_quota_id"] - self.v["wp_backup_quota_offset"]}],
            "id just below it": [{**a, "quota_id": self.v["wp_scratch_quota_id"] - self.v["wp_backup_quota_offset"] - 1}],
            "id at the archive project": [{**a, "quota_id": self.v["wp_archive_quota_id"] - self.v["wp_backup_quota_offset"]}],
            "id at the intake project": [{**a, "quota_id": self.v["wp_intake_quota_id"] - self.v["wp_backup_quota_offset"]}],
        }
        for label, sites in cases.items():
            self.assertEqual(self.mine(sites), self.ansible(sites), label)

    def test_the_limits_themselves(self):
        a = self.site("alpha", 1)
        for key, value in (("archive_quota_id", self.limits["scratch_quota_id"]), ("intake_quota_id", self.limits["scratch_quota_id"]), ("intake_quota_id", self.limits["archive_quota_id"]),
                           ("backup_quota_offset", 0), ("backup_quota_offset", 1), ("ram_reserve_mb", 10 ** 6), ("data_reserve_mb", 10 ** 7)):
            lim = {**self.limits, key: value}
            v = {**self.v, {"archive_quota_id": "wp_archive_quota_id", "intake_quota_id": "wp_intake_quota_id", "backup_quota_offset": "wp_backup_quota_offset", "ram_reserve_mb": "wp_vm_ram_reserve_mb",
                            "data_reserve_mb": "wp_data_reserve_mb"}[key]: value}
            theirs = sorted(self.that.index(e) for e in play.failing(self.that, v, [a]))
            self.assertEqual(m.broken_rules([m.merged(self.defaults, a)], lim), theirs, (key, value))

    def test_random_lists_name_the_same_rules(self):
        rng = random.Random(20261028)
        names = ["alpha", "bravo", "charlie", "hub", "vps-hub", "wg", "x", "ok-1", "a" * 24, "Bad", "abc-"]
        for _ in range(400):
            sites = []
            for i in range(rng.randint(1, 5)):
                s = {"name": rng.choice(names), "port": rng.choice([80, 1024, 1025, 9101, 9102, 9103, 9104, 9105]), "quota_id": rng.choice([5, 999, 1000, 1001, 1002, 1003, 1004, 2000, 2999, 3500, 4999, 5000, 5001]),
                     "state": rng.choice(["hold", "live", "stopped", "closed", "hold", "live", "paused"]), "update_policy": rng.choice(["auto", "notify", "freeze", "notify", "bad"])}
                if rng.random() < 0.3:
                    s["php_mem_mb"] = rng.choice([384, 4096, 9000])
                if rng.random() < 0.3:
                    s["db_mem_mb"] = rng.choice([192, 2000, 9000])
                if rng.random() < 0.3:
                    s["quota_mb"] = rng.choice([512, 50000, 400000])
                if rng.random() < 0.3:
                    s["backup_quota_mb"] = rng.choice([512, 50000, 400000])
                sites.append(s)
            self.same(sites)

    def test_an_empty_list_breaks_the_first_rule_in_both(self):
        # Only the first rule is compared: the play's later expressions raise on the largest id of nothing, which the offline helper counts as failing. The tool never asks about an empty list
        # (the site being created is always in it).
        self.assertIn(0, self.mine([]))
        self.assertIn(0, self.ansible([]))

    def test_a_site_without_its_numbers_is_refused(self):
        sites = [{"name": "alpha", "state": "hold", "update_policy": "notify"}]
        self.assertIn(1, self.mine(sites))
        self.assertIn(1, self.ansible(sites))

    def test_the_report_is_in_words(self):
        self.assertEqual(m.fit_report([m.merged(self.defaults, self.site("hub", 1))], self.limits), ["no name is reserved"])
        self.assertEqual(m.fit_report([m.merged(self.defaults, self.site("alpha", 1))], self.limits), [])


if __name__ == "__main__":
    unittest.main()
