#!/usr/bin/env python3
"""Offline tests of wp-site-import, batch A: scripts/wp_site_import.py (the logic) and templates/wp/wp_site_import.py.j2 (the wrapper that carries the repo's values).

The filesystem is real (a temporary tree standing in for /srv/wp), the archive is written in the real format (tests/test_wp_wpress.py has the writer), wordpress.org and docker are stand-ins.
They prove what is unpacked and what is not (a plugin wordpress.org offers is not written; one it does not offer is), that every refusal changes nothing (no work directory, no ledger line, nothing in
the site), that a failure part way removes what the run made, the ledger, `reset` and the lock. They do not prove that docker answers like this on the VM, that the intake quota project holds, or
that the play deploys the files: the first real run on dellwp does. Run: python3 -I tests/test_wp_import.py"""
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
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
import fcntl  # noqa: E402

import jinja2  # noqa: E402
import wp_site_import as imp  # noqa: E402
import wp_wpress as w  # noqa: E402
from test_wp_sites_render import ROOT, load  # noqa: E402
from test_wp_wpress import HEADER, block, build, get_from, meta, plugin_php, theme_css  # noqa: E402

ANSWERS = {("plugins", "good"): {"version": "1.1", "requires_php": "7.4"}, ("plugins", "litespeed-cache"): {"version": "7.9", "requires_php": "7.2"},
           ("themes", "blocksy"): {"version": "3.1", "requires_php": "7.0"}}


def files(m=None, extra=()):
    m = m or meta(Plugins=["good/good.php", "litespeed-cache/litespeed-cache.php", "notorg/notorg.php"])
    return [(".", "package.json", json.dumps(m).encode()), (".", "database.sql", b"CREATE TABLE t (id int);"), (".", "index.php", b"<?php"),
            ("plugins/good", "good.php", plugin_php("1.0")), ("plugins/good", "readme.txt", b"r" * 50),
            ("plugins/litespeed-cache", "litespeed-cache.php", plugin_php("7.0")), ("plugins/notorg", "notorg.php", plugin_php("5.0")), ("plugins/notorg", "lib/x.php", b"<?php // x"),
            ("plugins/dormant", "dormant.php", plugin_php("9")), ("plugins", "index.php", b"<?php"), ("plugins/all-in-one-wp-migration", "a.php", plugin_php("7")),
            ("themes/blocksy", "style.css", theme_css("3.0")), ("themes/blocksy", "functions.php", b"<?php"), ("themes/twentytwentyfive", "style.css", theme_css("1.0")),
            ("uploads/2026/10", "a.jpg", b"J" * 300), ("mu-plugins", "m.php", b"<?php // mu"), ("languages", "x.mo", b"mo"),
            ("upgrade", "half.zip", b"z" * 400), ("cache", "c.html", b"c" * 40), ("litespeed", "l.data", b"l"), ("ai1wm-backups", "b.wpress", b"b" * 60), ("weird-dir", "q.php", b"<?php")] + list(extra)


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.tmp = Path(self.d.name)
        self.data = self.tmp / "data"
        self.intake = self.data / "intake"
        self.site_dir = self.data / "sites" / "wptest"
        for p in (self.intake, self.site_dir / "html", self.site_dir / "db"):
            p.mkdir(parents=True)
        self.cfg = {"data_mount": str(self.data), "intake_dir": str(self.intake), "intake_quota_mb": 3072, "image_pin": "wordpress:7.1.3-php8.5-fpm",
                    "sites": {"wptest": {"state": "hold", "quota_mb": 2048}, "livesite": {"state": "live", "quota_mb": 2048}}}
        self.calls, self.out, self.tables = [], [], "0"

    def tearDown(self):
        self.d.cleanup()

    def put(self, data, name="friend.wpress"):
        p = self.intake / name
        p.write_bytes(data)
        return p

    def archive(self, **kw):
        return self.put(build(kw.pop("files", None) or files(), **kw))

    def docker(self, cmd, **kw):
        self.calls.append(cmd)
        return Done(0, self.tables + "\n") if isinstance(self.tables, str) else self.tables

    def run_main(self, argv, get=None, ask=None, cfg=None):
        self.out.clear()
        code = imp.main(cfg or self.cfg, argv, get=get or get_from(ANSWERS), run=self.docker, ask=ask or (lambda prompt: ""), out=self.out.append)
        return code, "\n".join(self.out)

    def snapshot(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if p.name not in (".lock",) and not p.name.endswith(".wpress"))

    def assert_refused(self, argv, needle, **kw):
        before = self.snapshot()
        code, text = self.run_main(argv, **kw)
        self.assertEqual(code, 2, text)
        self.assertIn(needle, text)
        self.assertEqual(self.snapshot(), before, "a refusal changed something")
        return text


class Unpack(Base):
    def test_the_plan_alone_writes_nothing(self):
        self.archive()
        before = self.snapshot()
        code, text = self.run_main(["wptest", "friend.wpress", "--plan"])
        self.assertEqual(code, 0, text)
        self.assertIn("PLAN ONLY", text)
        self.assertEqual(self.snapshot(), before)

    def test_what_is_unpacked_and_what_is_not(self):
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 0, text)
        work = self.intake / "work-wptest"
        got = sorted(str(p.relative_to(work)) for p in work.rglob("*") if p.is_file())
        self.assertEqual(got, ["PLAN.json", "database.sql", "package.json", "wp-content/languages/x.mo", "wp-content/mu-plugins/m.php", "wp-content/plugins/notorg/lib/x.php",
                               "wp-content/plugins/notorg/notorg.php", "wp-content/uploads/2026/10/a.jpg"])
        self.assertEqual((work / "wp-content/uploads/2026/10/a.jpg").read_bytes(), b"J" * 300)
        self.assertEqual((work / "database.sql").read_bytes(), b"CREATE TABLE t (id int);")

    def test_the_plan_names_every_decision(self):
        self.archive()
        _, text = self.run_main(["wptest", "friend.wpress"])
        for needle in ("plugin good", "reinstalled from wordpress.org", "NOT on wordpress.org", "not carried over", "upgrade/ (a half-finished update)", "cache directory", "LiteSpeed cache data",
                       "the migration plugin's own backups", "plugin dormant: inactive", "theme twentytwentyfive: inactive", "unknown directory 'weird-dir'", "archive housekeeping file",
                       "mu-plugins: code that runs on every request"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"plugin\s+litespeed-cache\s+7\.0\s+not carried over")
        self.assertRegex(text, r"plugin\s+notorg\s+5\.0\s+NOT on wordpress\.org")

    def test_plan_json_and_ledger_record_what_was_done(self):
        p = self.archive()
        self.run_main(["wptest", "friend.wpress"])
        rec = json.loads((self.intake / "work-wptest/PLAN.json").read_text())
        self.assertEqual(rec["site"], "wptest")
        self.assertEqual(rec["sha256"], imp.sha256(p))
        self.assertEqual(rec["plugins"]["notorg"], "approve")
        self.assertEqual(rec["plugins"]["litespeed-cache"], "dropped")
        self.assertEqual(rec["plugins"]["good"], "reinstall")
        self.assertEqual(rec["bytes"], imp.tree_bytes(self.intake / "work-wptest") - (self.intake / "work-wptest/PLAN.json").stat().st_size)
        line = (self.intake / "ledger.log").read_text().splitlines()
        self.assertEqual(len(line), 1)
        t, site, event, digest, size, name = line[0].split("\t")
        self.assertEqual((site, event, digest, int(size), name), ("wptest", "unpacked", imp.sha256(p), p.stat().st_size, "friend.wpress"))

    def test_files_and_directories_are_root_only(self):
        self.archive()
        self.run_main(["wptest", "friend.wpress"])
        work = self.intake / "work-wptest"
        for p in work.rglob("*"):
            mode = stat.S_IMODE(p.stat().st_mode)
            self.assertEqual(mode, 0o700 if p.is_dir() else 0o600, p)
        self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)

    def test_the_site_itself_is_not_touched(self):
        self.archive()
        before = sorted(str(p) for p in self.site_dir.rglob("*"))
        self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(sorted(str(p) for p in self.site_dir.rglob("*")), before)

    def test_the_database_is_asked_as_the_site_user_inside_its_container(self):
        self.archive()
        self.run_main(["wptest", "friend.wpress", "--plan"])
        self.assertEqual(self.calls[0][:3], ["docker", "exec", "wp_wptest_db"])
        self.assertIn("MARIADB_PASSWORD", self.calls[0][-1])
        self.assertNotIn("sudo", " ".join(self.calls[0]))

    def test_a_hyphenated_site_uses_underscores_in_the_container_name(self):
        (self.data / "sites/my-shop").mkdir()
        self.cfg["sites"]["my-shop"] = {"state": "hold", "quota_mb": 2048}
        self.archive()
        self.run_main(["my-shop", "friend.wpress", "--plan"])
        self.assertEqual(self.calls[0][2], "wp_my_shop_db")

    def test_a_relative_name_means_the_intake_directory(self):
        self.archive()
        self.assertEqual(self.run_main(["wptest", str(self.intake / "friend.wpress"), "--plan"])[0], 0)


class Refusals(Base):
    def test_unknown_site_and_bad_names(self):
        self.archive()
        self.assert_refused(["nosuch", "friend.wpress"], "is not in wp_sites")
        self.assert_refused(["../x", "friend.wpress"], "is not a site name")
        self.assert_refused(["WPTEST", "friend.wpress"], "is not a site name")

    def test_a_site_that_is_not_on_hold(self):
        self.archive()
        (self.data / "sites/livesite").mkdir()
        self.assert_refused(["livesite", "friend.wpress"], "an import goes into a site on hold")
        self.cfg["sites"]["wptest"]["state"] = "stopped"
        self.assert_refused(["wptest", "friend.wpress"], "stopped")

    def test_a_stopped_flag_and_a_missing_directory(self):
        self.archive()
        (self.site_dir / "stopped.flag").write_text("")
        self.assert_refused(["wptest", "friend.wpress"], "is stopped")
        (self.site_dir / "stopped.flag").unlink()
        self.cfg["sites"]["ghost"] = {"state": "hold", "quota_mb": 2048}
        self.assert_refused(["ghost", "friend.wpress"], "not a real directory")

    def test_a_database_that_is_not_empty(self):
        self.archive()
        self.tables = "57"
        self.assert_refused(["wptest", "friend.wpress"], "is not empty (57 tables)")

    def test_a_database_that_cannot_be_read_is_not_taken_for_empty(self):
        self.archive()
        for answer in (Done(1, "", "No such container: wp_wptest_db"), Done(0, "", ""), Done(0, "ERROR\n", "")):
            self.tables = answer
            self.assert_refused(["wptest", "friend.wpress"], "could not read the database")

    def test_files_outside_the_intake_directory_or_of_the_wrong_kind(self):
        self.archive()
        other = self.tmp / "elsewhere.wpress"
        other.write_bytes(build(files()))
        self.assert_refused(["wptest", str(other)], "not directly inside")
        (self.intake / "link.wpress").symlink_to(other)
        self.assert_refused(["wptest", "link.wpress"], "not a regular file")
        self.put(b"x", "notes.txt")
        self.assert_refused(["wptest", "notes.txt"], "not a plain .wpress file name")
        self.assert_refused(["wptest", "missing.wpress"], "not a regular file")
        self.put(b"x", "a b.wpress")
        self.assert_refused(["wptest", "a b.wpress"], "not a plain .wpress file name")

    def test_a_path_that_climbs_out_of_intake(self):
        self.archive()
        sub = self.intake / "sub"
        sub.mkdir()
        (sub / "f.wpress").write_bytes(build(files()))
        self.assert_refused(["wptest", str(sub / "f.wpress")], "not directly inside")
        self.assert_refused(["wptest", str(self.intake / ".." / "intake" / "sub" / "f.wpress")], "not directly inside")

    def test_a_work_directory_left_behind(self):
        self.archive()
        (self.intake / "work-wptest").mkdir()
        self.assert_refused(["wptest", "friend.wpress"], "wp-site-import reset wptest")

    def test_an_archive_the_reader_rejects(self):
        cases = {"newer core": (files(meta(WordPress={"Version": "7.2.0"}, Plugins=[])), "newer than the sites'"),
                 "unsafe path": (files(extra=[("plugins/../../etc", "x.php", b"x")]), "unsafe path"),
                 "multisite": (files(extra=[(".", "multisite.json", b"{}")]), "multisite")}
        for label, (fl, needle) in cases.items():
            self.archive(files=fl)
            self.assert_refused(["wptest", "friend.wpress"], needle)

    def test_a_truncated_archive(self):
        self.archive(tail="none", truncate=300)
        self.assert_refused(["wptest", "friend.wpress"], "truncated")

    def test_a_plugin_that_needs_a_newer_php(self):
        self.archive()
        answers = dict(ANSWERS); answers[("plugins", "good")] = {"version": "1.1", "requires_php": "8.6"}
        self.assert_refused(["wptest", "friend.wpress"], "needs PHP 8.6", get=get_from(answers))

    def test_wordpress_org_unreachable_is_not_judged(self):
        self.archive()

        def down(url):
            raise urllib.error.URLError("no route")
        self.assert_refused(["wptest", "friend.wpress"], "NOT JUDGED", get=down)

    def test_not_enough_room_in_the_intake_quota(self):
        self.archive()
        self.cfg["intake_quota_mb"] = 0
        self.assert_refused(["wptest", "friend.wpress"], "intake quota has")
        self.cfg["intake_quota_mb"] = 3072
        (self.intake / "bulk.dat").write_bytes(b"0")
        os.truncate(self.intake / "bulk.dat", 3072 * imp.MIB - 100)
        self.assert_refused(["wptest", "friend.wpress"], "intake quota has")

    def test_not_enough_room_in_the_site(self):
        self.archive()
        (self.site_dir / "db/big").write_bytes(b"0")
        os.truncate(self.site_dir / "db/big", 2048 * imp.MIB - 10)
        self.assert_refused(["wptest", "friend.wpress"], "left of its quota")

    def test_a_second_import_at_the_same_time(self):
        self.archive()
        fd = os.open(self.intake / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assert_refused(["wptest", "friend.wpress"], "another import is running")
        finally:
            os.close(fd)

    def test_usage(self):
        for argv in ([], ["wptest"], ["a", "b", "c"], ["reset"], ["reset", "a", "b"]):
            code, text = self.run_main(argv)
            self.assertEqual(code, 2)
            self.assertIn("usage", text)


class Failures(Base):
    def test_a_duplicate_path_removes_the_work_directory(self):
        self.archive(files=files(extra=[("uploads/2026/10", "a.jpg", b"second")]))
        before = self.snapshot()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("twice", text)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.intake / "ledger.log").exists())

    def test_a_file_that_is_also_a_directory_is_refused(self):
        self.archive(files=files(extra=[("uploads/2026/10/a.jpg", "inner.txt", b"x")]))
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertFalse((self.intake / "work-wptest").exists())

    def test_an_archive_that_ends_inside_a_file_leaves_nothing(self):
        data = build(files())
        cut = data.index(b"J" * 300) + 100
        self.put(data[:cut])
        before = self.snapshot()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertEqual(self.snapshot(), before)

    def test_a_copy_that_does_not_add_up_is_removed(self):
        # the planned bytes must equal the written bytes; force a short write behind extract's back
        real = imp.extract

        def short(a, keep, work):
            real(a, keep, work)
            f = next(p for p in work.rglob("*.jpg"))
            os.truncate(f, 10)
        imp.extract = short
        try:
            self.archive()
            code, text = self.run_main(["wptest", "friend.wpress"])
        finally:
            imp.extract = real
        self.assertEqual(code, 2)
        self.assertIn("internal check failed", text)
        self.assertFalse((self.intake / "work-wptest").exists())
        self.assertFalse((self.intake / "ledger.log").exists())

    def test_extract_never_writes_outside_the_work_directory(self):
        self.archive()
        with w.Archive(self.intake / "friend.wpress") as a:
            e = a.entries[0]
            for dest in ("../escape", "wp-content/../../escape", "/abs/escape"):
                with self.assertRaises(imp.Refuse):
                    imp.extract(a, [(e[0], e[1], e[2], e[3], dest)], self.intake / "work-x")
        self.assertFalse((self.intake / "work-x").exists())
        self.assertFalse((self.intake / "escape").exists())


class Reset(Base):
    def unpacked(self):
        self.archive()
        self.run_main(["wptest", "friend.wpress"])

    def test_the_right_name_removes_the_work_directory_and_keeps_the_file(self):
        self.unpacked()
        code, text = self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(code, 0, text)
        self.assertFalse((self.intake / "work-wptest").exists())
        self.assertTrue((self.intake / "friend.wpress").exists())
        self.assertEqual((self.intake / "ledger.log").read_text().splitlines()[-1].split("\t")[1:3], ["wptest", "reset"])

    def test_a_wrong_name_or_none_removes_nothing(self):
        self.unpacked()
        for answer in ("wptes\n", "\n", "WPTEST\n"):
            self.assert_refused(["reset", "wptest"], "nothing was removed", ask=lambda prompt, a=answer: a)

    def test_nothing_to_reset_and_bad_names(self):
        self.assert_refused(["reset", "wptest"], "nothing to reset")
        self.assert_refused(["reset", "../intake"], "is not a site name")

    def test_a_link_is_not_followed(self):
        target = self.tmp / "precious"
        target.mkdir()
        (target / "keep").write_text("k")
        (self.intake / "work-wptest").symlink_to(target)
        self.assert_refused(["reset", "wptest"], "nothing to reset", ask=lambda prompt: "wptest\n")
        self.assertTrue((target / "keep").exists())

    def test_after_a_reset_the_import_can_be_run_again(self):
        self.unpacked()
        self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(self.run_main(["wptest", "friend.wpress"])[0], 0)


class Wrapper(unittest.TestCase):
    def render(self, **over):
        v = {}
        v.update(load("vars/wp.yml")); v.update(load("host_vars/dellwp/vars.yml"))
        e = jinja2.Environment(undefined=jinja2.StrictUndefined)
        e.filters["regex_search"] = lambda value, pattern: (__import__("re").search(pattern, value) or [None])[0]
        for _ in range(3):
            for key, val in list(v.items()):
                if isinstance(val, str) and "{{" in val:
                    v[key] = e.from_string(val).render(**v)
        v.update(over)
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        return env.get_template("wp_site_import.py.j2").render(**v), v

    def config(self, text):
        ns = {}
        src = text.split("import wp_site_import  # noqa: E402\n")[1].split("sys.exit(")[0]
        exec(src, ns)
        return ns["CONFIG"]

    def test_the_rendered_wrapper_is_valid_python_with_the_repos_values(self):
        text, v = self.render(wp_sites=[{"name": "alpha", "port": 9201, "quota_id": 1201, "state": "hold", "quota_mb": 4096}, {"name": "beta", "port": 9202, "quota_id": 1202},
                                        {"name": "gone", "port": 9203, "quota_id": 1203, "state": "closed"}])
        compile(text, "wp-site-import", "exec")
        self.assertTrue(text.startswith("#!/usr/bin/python3 -I\n"))
        cfg = self.config(text)
        self.assertEqual(cfg["sites"], {"alpha": {"state": "hold", "quota_mb": 4096}, "beta": {"state": v["wp_site_defaults"]["state"], "quota_mb": v["wp_site_defaults"]["quota_mb"]}})
        self.assertEqual((cfg["intake_dir"], cfg["intake_quota_mb"], cfg["image_pin"], cfg["data_mount"]), (v["wp_intake_dir"], v["wp_intake_quota_mb"], v["wp_image_php"], v["wp_data_mount"]))
        self.assertIn('sys.path.insert(0, "%s")' % v["wp_tools_lib"], text)

    def test_the_rendered_config_runs_the_real_tool(self):
        text, v = self.render(wp_sites=[{"name": "wptest", "port": 9101, "quota_id": 1001, "state": "hold"}])
        cfg = self.config(text)
        self.assertEqual(w.pin(cfg["image_pin"]), w.ours(ROOT))

    def test_no_site_means_an_empty_but_valid_table(self):
        text, _ = self.render(wp_sites=[])
        self.assertEqual(self.config(text)["sites"], {})

    def test_the_real_wrapper_runs_end_to_end_as_a_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib, intake = d / "lib", d / "data/intake"
            lib.mkdir(); intake.mkdir(parents=True); (d / "data/sites/wptest").mkdir(parents=True)
            text, _ = self.render(wp_sites=[{"name": "wptest", "port": 9101, "quota_id": 1001, "state": "hold"}], wp_tools_lib=str(lib), wp_data_mount=str(d / "data"), wp_intake_dir=str(intake))
            for m in ("wp_wpress.py", "wp_site_import.py"):
                (lib / m).write_text((ROOT / "scripts" / m).read_text())
            tool = d / "wp-site-import"
            tool.write_text(text.replace("#!/usr/bin/python3 -I", "#!%s -I" % sys.executable))
            tool.chmod(0o755)
            r = subprocess.run([str(tool)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("usage: wp-site-import", r.stdout)
            r = subprocess.run([str(tool), "nosuch", "x.wpress"], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)
            self.assertIn("is not in wp_sites", r.stdout)


class Classify(unittest.TestCase):
    def test_every_branch(self):
        pl, th = {"a": "reinstall", "b": "approve", "c": "dropped"}, {"t": "reinstall", "u": "approve"}
        cases = [(("package.json", "."), "keep"), (("database.sql", "."), "keep"), (("index.php", "."), "skip"), (("x", "upgrade/y"), "skip"), (("x", "cache"), "skip"),
                 (("f.php", "plugins/a"), "skip"), (("f.php", "plugins/b/sub"), "keep"), (("f.php", "plugins/c"), "skip"), (("f.php", "plugins/zz"), "skip"), (("one.php", "plugins"), "skip"),
                 (("s.css", "themes/t"), "skip"), (("s.css", "themes/u"), "keep"), (("s.css", "themes/other"), "skip"), (("f", "uploads/2026"), "keep"), (("f", "mu-plugins"), "keep"),
                 (("f", "languages"), "keep"), (("f", "elsewhere"), "skip")]
        for (name, path), want in cases:
            self.assertEqual(imp.classify(name, path, pl, th)[0], want, (name, path))

    def test_the_destinations_sit_under_wp_content(self):
        self.assertEqual(imp.classify("f.php", "plugins/b/sub", {"b": "approve"}, {}), ("keep", "wp-content/plugins/b/sub/f.php"))
        self.assertEqual(imp.classify("database.sql", ".", {}, {}), ("keep", "database.sql"))

    def test_the_cache_plugin_and_the_migration_plugin_are_dropped_by_name(self):
        self.assertTrue(imp.dropped_plugin("litespeed-cache"))
        self.assertTrue(imp.dropped_plugin("all-in-one-wp-migration"))
        self.assertTrue(imp.dropped_plugin("all-in-one-wp-migration-unlimited-extension"))
        self.assertFalse(imp.dropped_plugin("litespeed"))
        self.assertFalse(imp.dropped_plugin("wp-optimize"))


if __name__ == "__main__":
    unittest.main()
