#!/usr/bin/env python3
"""Offline tests of scripts/wp_vulnfeed.py: the version ranges, the reduction of the feed to an index, the download (what is sent, what is refused, what the failure messages may say), the checks of a
site against stand-ins for WP-CLI, and the nightly pass that tells a NEW hit once. The real feed is not reachable from here: the record shape comes from Wordfence's v3 documentation.
Run: python3 -I tests/test_wp_vulnfeed.py"""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import wp_vulnfeed as v  # noqa: E402

KEY = "k" * 40
NOW = 1_800_000_000


def record(title="Example - XSS", slug="example", kind="plugin", rng=("1.0.0", True, "1.2.3", True), patched=("1.2.4",), cve="CVE-2026-0001", score=6.5):
    return {"title": title, "cve": cve, "cvss": {"score": score, "rating": "Medium"},
            "software": [{"type": kind, "slug": slug, "patched": True, "patched_versions": list(patched),
                          "affected_versions": {"%s - %s" % (rng[0], rng[2]): {"from_version": rng[0], "from_inclusive": rng[1], "to_version": rng[2], "to_inclusive": rng[3]}}}]}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.tmp = Path(self.d.name)
        for n in ("state", "data/alpha", "data/beta", "data/gamma"):
            (self.tmp / n).mkdir(parents=True)
        (self.tmp / "key").write_text(KEY + "\n")
        self.cfg = {"key_file": str(self.tmp / "key"), "index_file": str(self.tmp / "state/index.json"), "state_dir": str(self.tmp / "state"), "feed_url": "https://feed.test/v3/production",
                    "max_age_days": 3, "tools_bin": "/opt/bin", "timeout_s": 60, "sites": {"alpha": {}, "beta": {}, "gamma": {}}, "data_dir": str(self.tmp / "data"),
                    "notify_cmd": "/opt/bin/ntfy-notify", "ntfy_topic": "wp-alerts", "host_name": "dellwp"}
        self.out, self.notes, self.requests, self.cli_calls = [], [], [], []
        self.feed = {"1": record(), "2": record("Old core bug", "wordpress", "core", ("*", True, "7.1.2", True), ("7.1.3",), "CVE-2026-0002", 9.8)}
        self.software = {}
        self.cli_fail = set()

    def tearDown(self):
        self.d.cleanup()

    def opener(self, body=None, error=None):
        def op(req, timeout=None):
            self.requests.append(req)
            if error:
                raise error
            return Resp(body if body is not None else json.dumps(self.feed).encode())
        return op

    def run_cli(self, cmd, **kw):
        site, args = cmd[1], cmd[2:]
        self.cli_calls.append(list(cmd))
        key = " ".join(a for a in args if not a.startswith("--"))
        if (site, key) in self.cli_fail or (site, "*") in self.cli_fail:
            return subprocess.CompletedProcess(cmd, 1, "", "Error: database down")
        sw = self.software.get(site)
        if sw is None:
            return subprocess.CompletedProcess(cmd, 1, "", "Error: not installed") if key == "core is-installed" else subprocess.CompletedProcess(cmd, 1, "", "no")
        if key == "core is-installed":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if key == "core version":
            return subprocess.CompletedProcess(cmd, 0, "Deprecated: noise\n%s\n" % sw["core"], "")
        kind = key.split()[0]
        rows = [{"name": n, "version": ver} for n, ver in sw[kind]]
        return subprocess.CompletedProcess(cmd, 0, "PHP Deprecated: noise line\n" + json.dumps(rows) + "\n", "")

    def go(self, *argv, **kw):
        self.out.clear()
        kw.setdefault("run", self.run_cli)
        kw.setdefault("clock", lambda: NOW)
        kw.setdefault("notify", self.notes.append)
        rc = v.main(self.cfg, list(argv), out=self.out.append, **kw)
        return rc, "\n".join(self.out)

    def update(self, **kw):
        return self.go("update", opener=self.opener(**kw))

    def installed(self, site="alpha", core="7.1.3", plugins=(("example", "1.1.0"), ("safe", "9.9")), themes=(("blocksy", "3.0"),)):
        self.software[site] = {"core": core, "plugin": list(plugins), "theme": list(themes)}


class Versions(unittest.TestCase):
    def test_ranges_with_inclusive_and_exclusive_bounds(self):
        r = ["1.0.0", True, "1.2.3", True]
        for ver, want in (("1.0.0", True), ("1.2.3", True), ("1.1", True), ("0.9.9", False), ("1.2.4", False), ("1.2.3.1", False), ("1.10", False), ("v1.2", True)):
            self.assertEqual(v.in_range(ver, r), want, ver)
        r = ["1.0.0", False, "1.2.3", False]
        for ver, want in (("1.0.0", False), ("1.2.3", False), ("1.0.1", True), ("1.2.2", True)):
            self.assertEqual(v.in_range(ver, r), want, ver)

    def test_a_star_is_unbounded_on_that_side(self):
        self.assertTrue(v.in_range("0.0.1", ["*", True, "2.0", True])); self.assertFalse(v.in_range("2.0.1", ["*", True, "2.0", True]))
        self.assertTrue(v.in_range("99", ["1.0", True, "*", True])); self.assertFalse(v.in_range("0.9", ["1.0", True, "*", True]))
        self.assertTrue(v.in_range("5", ["*", True, "*", True]))

    def test_numbers_compare_as_numbers_not_as_text(self):
        self.assertLess(v.vkey("1.9"), v.vkey("1.10")); self.assertLess(v.vkey("1.2.3"), v.vkey("1.2.10")); self.assertEqual(v.vkey("1.0"), v.vkey("1.0.0"))

    def test_a_pre_release_is_below_its_release(self):
        self.assertLess(v.vkey("1.0.0-beta1"), v.vkey("1.0.0")); self.assertLess(v.vkey("1.0-rc2"), v.vkey("1.0")); self.assertLess(v.vkey("1.0.0"), v.vkey("1.0.1-beta"))

    def test_nonsense_versions_do_not_raise(self):
        for junk in ("", "abc", "...", "1..2", None, "9" * 40):
            v.vkey(junk)
            v.in_range(junk, ["1.0", True, "2.0", True])


class Index(Base):
    def test_a_v3_record_is_reduced_to_what_a_check_needs(self):
        idx = v.build_index(self.feed)
        self.assertEqual(idx["records"], 2)
        e = idx["entries"]["plugin:example"][0]
        self.assertEqual((e["title"], e["cve"], e["score"], e["patched"], e["ranges"]), ("Example - XSS", "CVE-2026-0001", 6.5, ["1.2.4"], [["1.0.0", True, "1.2.3", True]]))
        self.assertIn("core:wordpress", idx["entries"])

    def test_a_list_of_records_works_like_an_object_of_them(self):
        self.assertEqual(v.build_index(list(self.feed.values()))["records"], 2)

    def test_what_is_not_a_usable_record_is_skipped_and_an_empty_result_is_refused(self):
        junk = [None, 5, "x", {}, {"software": "no"}, {"software": [None, {"type": "weird", "slug": "a"}, {"type": "plugin", "slug": "../x", "affected_versions": {"a": {}}},
                                                                 {"type": "plugin", "slug": "ok"}, {"type": "plugin", "slug": "ok", "affected_versions": {}}]}]
        with self.assertRaises(v.FeedError):
            v.build_index(junk)
        with self.assertRaises(v.FeedError):
            v.build_index({})
        with self.assertRaises(v.FeedError):
            v.build_index("not a feed")
        self.assertEqual(v.build_index(junk + [record()])["records"], 1)

    def test_titles_lose_their_control_characters_and_are_cut(self):
        idx = v.build_index([record(title="Bad\x1b[31m title\n" + "x" * 500)])
        t = idx["entries"]["plugin:example"][0]["title"]
        self.assertNotIn("\x1b", t); self.assertNotIn("\n", t); self.assertLessEqual(len(t), v.TITLE_MAX + 3)

    def test_odd_scores_and_cves_become_none(self):
        r = record(score="high", cve=["x"])
        e = v.build_index([r])["entries"]["plugin:example"][0]
        self.assertIsNone(e["score"]); self.assertIsNone(e["cve"])
        r = record(score=True)
        self.assertIsNone(v.build_index([r])["entries"]["plugin:example"][0]["score"], "a boolean is not a score")

    def test_matches_are_sorted_with_the_worst_first(self):
        idx = v.build_index([record("low", score=3.0), record("high", score=9.0), record("mid", score=6.0), record("other version", rng=("5.0", True, "6.0", True))])
        got = v.matches(idx, "plugin", "example", "1.1")
        self.assertEqual([e["title"] for e in got], ["high", "mid", "low"])
        self.assertEqual(v.matches(idx, "plugin", "example", "7.0"), [])
        self.assertEqual(v.matches(idx, "theme", "example", "1.1"), [], "the kind is part of the name")

    def test_the_index_file_is_replaced_atomically_and_old_ones_are_judged_by_age(self):
        v.write_index(self.cfg["index_file"], v.build_index(self.feed), lambda: NOW)
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg["index_file"]).st_mode), 0o644)
        self.assertEqual([p.name for p in (self.tmp / "state").iterdir() if p.name.startswith(".index-")], [])
        v.load_index(self.cfg["index_file"], 3, lambda: NOW + 3 * 86400 - 1)
        with self.assertRaises(v.NotRun):
            v.load_index(self.cfg["index_file"], 3, lambda: NOW + 3 * 86400 + 86400)

    def test_a_missing_or_damaged_index_is_not_run_never_none_known(self):
        for text in (None, "", "{", "[]", json.dumps({"v": 99, "entries": {}, "built": NOW}), json.dumps({"v": 1, "entries": [], "built": NOW}), json.dumps({"v": 1, "entries": {}})):
            p = Path(self.cfg["index_file"])
            p.unlink(missing_ok=True)
            if text is not None:
                p.write_text(text)
            with self.assertRaises(v.NotRun, msg=repr(text)):
                v.load_index(str(p), 3, lambda: NOW)


class Download(Base):
    def test_the_key_goes_in_the_bearer_header_and_nowhere_else(self):
        rc, text = self.update()
        self.assertEqual(rc, 0, text)
        req = self.requests[0]
        self.assertEqual(req.full_url, self.cfg["feed_url"]); self.assertEqual(req.get_header("Authorization"), "Bearer " + KEY)
        self.assertNotIn(KEY, text); self.assertNotIn(KEY, req.full_url)
        self.assertIn("feed updated", text)
        self.assertEqual(json.loads(Path(self.cfg["index_file"]).read_text())["records"], 2)

    def test_no_temporary_feed_file_is_left_behind_on_success_or_failure(self):
        self.update(); self.update(body=b"not json")
        self.assertEqual([p.name for p in (self.tmp / "state").iterdir() if p.name.startswith(".feed-")], [])

    def test_no_key_and_a_bad_key_are_not_run(self):
        (self.tmp / "key").unlink()
        rc, text = self.update()
        self.assertEqual(rc, 3); self.assertIn("NOT RUN: there is no feed key", text); self.assertEqual(self.requests, [])
        (self.tmp / "key").write_text("short\n")
        rc, text = self.update()
        self.assertEqual(rc, 3); self.assertIn("does not look like a key", text); self.assertEqual(self.requests, [])
        (self.tmp / "key").write_text("has spaces in it which is not a key at all\n")
        self.assertEqual(self.update()[0], 3)

    def test_each_refusal_has_a_plain_message_without_the_key_and_keeps_the_old_index(self):
        self.update()
        before = Path(self.cfg["index_file"]).read_text()
        for code, word in ((401, "the key was refused"), (403, "the key was refused"), (429, "rate limited"), (410, "switched off"), (500, "the feed answered")):
            err = urllib.error.HTTPError(self.cfg["feed_url"], code, "x", {}, io.BytesIO(b"body with " + KEY.encode()))
            rc, text = self.update(error=err)
            self.assertEqual(rc, 2, code); self.assertIn(word, text, code); self.assertIn("HTTP %d" % code, text); self.assertNotIn(KEY, text)
        rc, text = self.update(error=urllib.error.URLError("name not resolved"))
        self.assertEqual(rc, 2); self.assertIn("could not be reached", text)
        self.assertEqual(Path(self.cfg["index_file"]).read_text(), before, "a failed update never touches the index")

    def test_a_feed_that_is_not_json_or_has_nothing_usable_keeps_the_old_index(self):
        self.update()
        before = Path(self.cfg["index_file"]).read_text()
        for body in (b"<html>maintenance</html>", b"{}", b"[]", b'{"1": {"software": []}}', b""):
            rc, text = self.update(body=body)
            self.assertEqual(rc, 2, body); self.assertEqual(Path(self.cfg["index_file"]).read_text(), before, body)

    def test_a_feed_over_the_size_limit_is_refused(self):
        old = v.MAX_FEED_BYTES
        v.MAX_FEED_BYTES = 100
        try:
            rc, text = self.update()
        finally:
            v.MAX_FEED_BYTES = old
        self.assertEqual(rc, 2); self.assertIn("larger than", text)

    def test_the_download_will_not_overwrite_or_follow_a_link(self):
        target = self.tmp / "victim"
        target.write_text("keep")
        link = self.tmp / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            v.download(self.cfg["feed_url"], KEY, str(link), self.opener())
        self.assertEqual(target.read_text(), "keep")
        dest = self.tmp / "exists"
        dest.write_text("x")
        with self.assertRaises(OSError):
            v.download(self.cfg["feed_url"], KEY, str(dest), self.opener())

    def test_the_downloaded_file_is_private(self):
        dest = self.tmp / "feed"
        v.download(self.cfg["feed_url"], KEY, str(dest), self.opener())
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)


class Check(Base):
    def setUp(self):
        super().setUp()
        self.update()
        self.installed()

    def test_a_site_with_an_affected_plugin_reports_it_and_exits_1(self):
        rc, text = self.go("check", "alpha")
        self.assertEqual(rc, 1, text)
        self.assertEqual(text, "plugin example 1.1.0: Example - XSS (CVE-2026-0001, CVSS 6.5, fixed in 1.2.4)")

    def test_core_is_checked_too_and_the_worst_comes_first(self):
        self.installed(core="7.1.1")
        rc, text = self.go("check", "alpha")
        self.assertEqual(rc, 1)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("core wordpress 7.1.1: Old core bug"), lines)
        self.assertEqual(len(lines), 2)

    def test_a_clean_site_exits_0_and_says_what_it_checked(self):
        self.installed(core="7.1.3", plugins=(("example", "1.2.4"),))
        rc, text = self.go("check", "alpha")
        self.assertEqual(rc, 0); self.assertIn("3 plugins, themes and core checked against 2 records; none known", text)

    def test_one_plugin_version_is_checked_before_it_is_installed_anywhere(self):
        rc, text = self.go("check-plugin", "example", "1.1.0")
        self.assertEqual((rc, text), (1, "plugin example 1.1.0: Example - XSS (CVE-2026-0001, CVSS 6.5, fixed in 1.2.4)"))
        rc, text = self.go("check-plugin", "example", "1.2.4")
        self.assertEqual(rc, 0); self.assertIn("plugin example 1.2.4 checked against 2 records; none known", text)
        self.assertEqual(self.go("check-plugin", "never-heard-of", "1.0")[0], 0)
        self.assertEqual(self.cli_calls, [], "it asks no site")

    def test_a_theme_version_is_checked_the_same_way_and_a_plugin_record_is_not_a_theme_record(self):
        rc, text = self.go("check-theme", "example", "1.1.0")
        self.assertEqual(rc, 0, "the index has a plugin called example, not a theme")
        self.assertIn("theme example 1.1.0 checked against 2 records; none known", text)
        for slug, version in (("../x", "1.0"), ("Example", "1.0"), ("example", ""), ("example", "1.0\n")):
            self.assertEqual(self.go("check-theme", slug, version)[0], 2, (slug, version))
        self.assertEqual(self.go("check-theme", "example")[0], 2)
        (self.tmp / "state/index.json").unlink()
        rc, text = self.go("check-theme", "example", "1.1.0")
        self.assertEqual(rc, 3); self.assertIn("NOT RUN", text)

    def test_check_plugin_refuses_a_slug_or_version_that_is_not_one_and_is_not_run_without_a_fresh_index(self):
        for slug, version in (("../x", "1.0"), ("Example", "1.0"), ("example", ""), ("example", "x1"), ("example", "1.0\n"), ("", "1.0"), ("a b", "1.0")):
            rc, text = self.go("check-plugin", slug, version)
            self.assertEqual(rc, 2, (slug, version, text))
        self.assertEqual(self.go("check-plugin", "example")[0], 2)
        (self.tmp / "state/index.json").unlink()
        rc, text = self.go("check-plugin", "example", "1.1.0")
        self.assertEqual(rc, 3); self.assertIn("NOT RUN", text)

    def test_a_site_without_wordpress_has_nothing_to_check(self):
        rc, text = self.go("check", "beta")
        self.assertEqual(rc, 0); self.assertIn("no WordPress yet", text)

    def test_every_wp_cli_call_skips_plugins_and_themes(self):
        self.go("check", "alpha")
        self.assertGreaterEqual(len(self.cli_calls), 4)
        for c in self.cli_calls:
            self.assertIn("--skip-plugins", c); self.assertIn("--skip-themes", c); self.assertEqual(c[0], "/opt/bin/wp-site-cli")

    def test_a_site_that_is_not_known_is_refused_before_anything_runs(self):
        self.cli_calls.clear()
        for name in ("nosuch", "../etc", "", "Alpha", "alpha; id"):
            rc, text = self.go("check", name)
            self.assertEqual(rc, 2, name)
        self.assertEqual(self.cli_calls, [])

    def test_wp_cli_that_fails_or_says_nonsense_is_an_error_not_a_pass(self):
        self.cli_fail = {("alpha", "plugin list")}
        rc, text = self.go("check", "alpha")
        self.assertEqual(rc, 2); self.assertIn("plugin list could not be read", text)
        self.cli_fail = {("alpha", "core version")}
        self.assertEqual(self.go("check", "alpha")[0], 2)
        self.cli_fail = set()
        self.software["alpha"]["core"] = "not a version"
        self.assertEqual(self.go("check", "alpha")[0], 2)

    def test_names_that_are_not_slugs_in_the_list_are_ignored_not_trusted(self):
        self.installed(plugins=(("../evil", "1.0"), ("example", "1.1.0")))
        rc, text = self.go("check", "alpha")
        self.assertEqual(rc, 1); self.assertNotIn("evil", text)
        found = v.software_of("alpha", self.cfg, self.run_cli)
        self.assertNotIn("../evil", [s for _, s, _ in found]); self.assertIn(("plugin", "example", "1.1.0"), found)

    def test_an_old_index_is_not_run_and_says_why(self):
        rc, text = self.go("check", "alpha", clock=lambda: NOW + 10 * 86400)
        self.assertEqual(rc, 3); self.assertIn("NOT RUN: the vulnerability index is 10 days old", text)

    def test_a_timeout_is_an_error(self):
        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        rc, text = self.go("check", "alpha", run=slow)
        self.assertEqual(rc, 2); self.assertIn("did not answer in time", text)

    def test_the_command_line_is_checked(self):
        for argv in ((), ("check",), ("check", "a", "b"), ("update", "x"), ("what",), ("check-all", "x")):
            rc, text = self.go(*argv)
            self.assertEqual(rc, 2, argv); self.assertIn("usage:", text)


class Registry(Base):
    ROWS = "alpha hold 1001 3072 2001 3072 notify\nbeta live 1002 3072 2002 3072 auto\n"

    def asks(self, rc=0, out=ROWS, err=""):
        calls = []

        def run(cmd, **kw):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, rc, out, err)
        return run, calls

    def test_the_sites_are_those_the_registry_lists_not_a_baked_in_table(self):
        del self.cfg["sites"]
        run, calls = self.asks()
        self.assertEqual(v.registry_sites(self.cfg, run), {"alpha": {}, "beta": {}})
        self.assertEqual(calls, [["/opt/bin/wp-site-provision", "rows"]])

    def test_a_site_the_registry_does_not_list_is_not_a_site(self):
        del self.cfg["sites"]
        run, _ = self.asks()
        rc, text = self.go("check", "gamma", run=run)
        self.assertEqual(rc, 2)
        self.assertIn("is not a site this host knows", text)

    def test_a_registry_that_cannot_be_read_is_an_error_and_never_no_sites(self):
        del self.cfg["sites"]
        for command in (("check", "alpha"), ("check-all",)):
            run, _ = self.asks(rc=1, out="", err="/srv/wp/sites/x/site.json: not valid")
            rc, text = self.go(*command, run=run)
            self.assertEqual(rc, 2, command)
            self.assertIn("cannot read the site registry", text)
            self.assertIn("not valid", text)

    def test_check_all_checks_exactly_the_registrys_sites(self):
        del self.cfg["sites"]
        self.update()
        self.installed("alpha"); self.installed("beta", plugins=(("safe", "1.0"),), core="7.1.3")
        run, calls = self.asks()
        cli = self.run_cli
        seen = []

        def both(cmd, **kw):
            if cmd[1:2] == ["rows"]:
                return run(cmd, **kw)
            seen.append(cmd[1])
            return cli(cmd, **kw)
        rc, _ = self.go("check-all", run=both)
        self.assertEqual(sorted(set(seen)), ["alpha", "beta"], "gamma is not in the registry: it is not checked")
        self.assertEqual(len(calls), 1)

    def test_updating_the_feed_does_not_need_the_registry(self):
        del self.cfg["sites"]
        run, calls = self.asks(rc=1)
        rc, _ = self.go("update", run=run, opener=self.opener())
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])


class CheckAll(Base):
    def setUp(self):
        super().setUp()
        self.update()
        self.installed("alpha"); self.installed("beta", plugins=(("safe", "1.0"),), core="7.1.3")

    def test_a_new_hit_is_told_once_and_a_known_one_is_not_told_again(self):
        rc, text = self.go("check-all")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.notes), 1)
        self.assertTrue(self.notes[0].startswith("alpha: 1 new known vulnerability: plugin example 1.1.0"), self.notes)
        self.go("check-all"); self.go("check-all")
        self.assertEqual(len(self.notes), 1)

    def test_a_vulnerability_that_goes_away_and_comes_back_is_told_again(self):
        self.go("check-all")
        self.software["alpha"]["plugin"] = [("example", "1.2.4")]
        self.go("check-all"); self.assertEqual(len(self.notes), 1)
        self.software["alpha"]["plugin"] = [("example", "1.1.0")]
        self.go("check-all"); self.assertEqual(len(self.notes), 2)

    def test_a_second_hit_is_told_without_repeating_the_first(self):
        self.go("check-all")
        self.installed("alpha", core="7.1.1")
        self.go("check-all")
        self.assertEqual(len(self.notes), 2)
        self.assertIn("core wordpress 7.1.1", self.notes[1]); self.assertNotIn("plugin example", self.notes[1])

    def test_sites_without_wordpress_or_stopped_are_skipped_and_say_nothing(self):
        self.go("check-all")
        (self.tmp / "data/beta/stopped.flag").write_text("")
        self.software["gamma"] = None
        self.cli_calls.clear()
        rc, text = self.go("check-all")
        self.assertEqual(rc, 0)
        self.assertNotIn("beta", [c[1] for c in self.cli_calls])
        self.assertEqual(text.count("gamma"), 0)
        self.assertFalse((self.tmp / "state/seen-gamma.json").exists(), "a site without WordPress gets no marker")

    def test_one_site_that_cannot_be_read_does_not_stop_the_others_and_the_run_fails(self):
        self.cli_fail = {("alpha", "plugin list")}
        self.installed("gamma", core="7.1.1", plugins=())
        rc, text = self.go("check-all")
        self.assertEqual(rc, 2); self.assertIn("the plugin list could not be read", text)
        self.assertTrue(any(n.startswith("gamma:") for n in self.notes), "the site after the broken one was still checked")

    def test_an_old_index_stops_the_whole_pass_as_not_run(self):
        rc, text = self.go("check-all", clock=lambda: NOW + 99 * 86400)
        self.assertEqual(rc, 3); self.assertEqual(self.notes, [])

    def test_the_default_notification_goes_through_ntfy_notify_with_the_host_name(self):
        calls = []

        def run(cmd, **kw):
            if cmd[0] == "/opt/bin/ntfy-notify":
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return self.run_cli(cmd, **kw)
        rc, _ = v.main(self.cfg, ["check-all"], out=self.out.append, run=run, clock=lambda: NOW), None
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["/opt/bin/ntfy-notify", "wp-alerts"]); self.assertTrue(calls[0][2].startswith("dellwp alpha:"), calls[0])

    def test_a_notify_that_fails_does_not_fail_the_pass(self):
        def run(cmd, **kw):
            if cmd[0] == "/opt/bin/ntfy-notify":
                raise OSError("no such command")
            return self.run_cli(cmd, **kw)
        rc = v.main(self.cfg, ["check-all"], out=self.out.append, run=run, clock=lambda: NOW)
        self.assertEqual(rc, 0); self.assertIn("could not notify", "\n".join(self.out))

    def test_many_hits_are_summarised_in_one_message(self):
        self.feed = {str(i): record("Bug %d" % i, score=float(i)) for i in range(1, 8)}
        self.update()
        self.go("check-all")
        self.assertEqual(len(self.notes), 1); self.assertIn("7 new known vulnerabilities", self.notes[0]); self.assertTrue(self.notes[0].endswith("..."))

    def test_the_seen_marker_is_a_file_per_site_in_the_state_directory(self):
        self.go("check-all")
        self.assertTrue((self.tmp / "state/seen-alpha.json").is_file())
        self.assertEqual(json.loads((self.tmp / "state/seen-beta.json").read_text()), [])


if __name__ == "__main__":
    unittest.main()
