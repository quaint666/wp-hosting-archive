#!/usr/bin/env python3
"""Offline tests of wp-site-import: scripts/wp_site_import.py (the logic) and templates/wp/wp_site_import.py.j2 (the wrapper that carries the repo's values).

The filesystem is real (a temporary tree standing in for /srv/wp), the archive is written in the real format (tests/test_wp_wpress.py has the writer), and the host is a stand-in (FakeHost):
docker exec for the database, the SQL load, wp-site-cli, wp-site-ext, wp-site-backup, clamscan and chown, each logging what it was given and able to fail on demand. They prove the order of the
stages, what reaches the database (the prefix swapped), what is copied and what is held back until `approve`, that every refusal changes nothing, that a failure part way stops with a way
back, that `reset` returns the site to the tree it had before the run, the ledger, the lock and the report. They do not prove that docker, MariaDB, WP-CLI or wordpress.org answer like the
stand-ins, that Elementor's commands are named as discovered, or that the intake quota project holds: the first real run on dellwp does (scripts/wp_import_rehearsal_dellwp.sh is not written yet).
Run: python3 -I tests/test_wp_import.py"""
import fcntl
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
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import jinja2  # noqa: E402
import wp_site_import as imp  # noqa: E402
import wp_wpress as w  # noqa: E402
from test_wp_sites_render import ROOT, load  # noqa: E402
from test_wp_wpress import build, get_from, meta, plugin_php, theme_css  # noqa: E402

ANSWERS = {("plugins", "good"): {"version": "1.1", "requires_php": "7.4"}, ("plugins", "litespeed-cache"): {"version": "7.9", "requires_php": "7.2"},
           ("plugins", "elementor"): {"version": "4.3", "requires_php": "7.4"}, ("themes", "blocksy"): {"version": "3.1", "requires_php": "7.0"}}
SQL = (b"-- All-in-One WP Migration SQL Dump\nDROP TABLE IF EXISTS `SERVMASK_PREFIX_options`;\nCREATE TABLE `SERVMASK_PREFIX_options` (id int) COLLATE=utf8mb4_0900_ai_ci;\n"
       b"INSERT INTO `SERVMASK_PREFIX_options` VALUES (1,'SERVMASK_PREFIX_user_roles','a:1:{}');\nINSERT INTO `SERVMASK_PREFIX_options` VALUES (2,'siteurl','http://old.local');\n")
META = dict(Plugins=["good/good.php", "litespeed-cache/litespeed-cache.php", "notorg/notorg.php"], SiteURL="http://old.local", HomeURL="http://old.local")


def files(m=None, extra=()):
    m = m or meta(**META)
    return [(".", "package.json", json.dumps(m).encode()), (".", "database.sql", SQL), (".", "index.php", b"<?php"),
            ("plugins/good", "good.php", plugin_php("1.0")), ("plugins/good", "readme.txt", b"r" * 50),
            ("plugins/litespeed-cache", "litespeed-cache.php", plugin_php("7.0")), ("plugins/notorg", "notorg.php", plugin_php("5.0")), ("plugins/notorg", "lib/x.php", b"<?php // x"),
            ("plugins/dormant", "dormant.php", plugin_php("9")), ("plugins", "index.php", b"<?php"), ("plugins/all-in-one-wp-migration", "a.php", plugin_php("7")),
            ("themes/blocksy", "style.css", theme_css("3.0")), ("themes/blocksy", "functions.php", b"<?php"), ("themes/twentytwentyfive", "style.css", theme_css("1.0")),
            ("uploads/2026/10", "a.jpg", b"J" * 300), ("mu-plugins", "m.php", b"<?php // mu"), ("languages", "x.mo", b"mo"),
            ("upgrade", "half.zip", b"z" * 400), ("cache", "c.html", b"c" * 40), ("litespeed", "l.data", b"l"), ("ai1wm-backups", "b.wpress", b"b" * 60), ("weird-dir", "q.php", b"<?php")] + list(extra)


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakePopen:
    def __init__(self, host, stderr):
        self.host, self.stderr = host, stderr
        self.stdin = self

    def write(self, data):
        if self.host.pipe_breaks:
            raise BrokenPipeError()
        self.host.sql += data

    def close(self):
        pass

    def wait(self):
        self.stderr.write(self.host.sql_err)
        if self.host.sql_rc == 0 and self.host.sql_loaded_tables is not None:
            self.host.tables = self.host.sql_loaded_tables
        return self.host.sql_rc


class FakeHost:
    """What the VM would answer. `fail` maps a key to (returncode, text): 'cli:<first two words>', 'ext:<slug>', 'ext:<slug>:versioned', 'backup', 'clam'."""
    def __init__(self, site_html):
        self.html = site_html
        self.calls, self.sql, self.chowned, self.dropped = [], b"", [], ""
        self.tables, self.sql_loaded_tables, self.sql_rc, self.sql_err, self.pipe_breaks = 0, 57, 0, b"", False
        self.fail, self.elementor_active, self.elementor_help, self.clam = {}, False, "usage: wp elementor <command>\n  flush-css\n  replace-urls\n", None
        self.siteurl = "http://old.local"
        self.table_names = ["wp_options", "wp_posts"]

    def which(self, name):
        return self.clam if name == "clamscan" else None

    def chown(self, path, uid, gid):
        self.chowned.append((str(path), uid, gid))

    def popen(self, cmd, stdin=None, stdout=None, stderr=None):
        self.calls.append(cmd)
        return FakePopen(self, stderr)

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        name = os.path.basename(cmd[0])
        if name == "docker":
            return self.docker(cmd, kw)
        if name == "wp-site-cli":
            return self.cli(cmd[2:])
        if name == "wp-site-ext":
            return self.ext(cmd[2:])
        if name == "wp-site-backup":
            rc, text = self.fail.get("backup", (0, "snapshot taken"))
            return Done(rc, text, "")
        if name == "clamscan":
            rc, text = self.fail.get("clam", (0, ""))
            return Done(rc, text, "")
        raise AssertionError("an unexpected command: %r" % (cmd,))

    def docker(self, cmd, kw):
        if "-i" in cmd:   # the drop of the tables
            self.dropped = kw["input"]
            self.tables = 0
            return Done(0)
        sql = cmd[3][4:]
        if "SELECT table_name" in sql:
            return Done(0, "\n".join(self.table_names) + "\n")
        if "COUNT(*)" in sql:
            rc, text = self.fail.get("count", (0, str(self.tables)))
            return Done(rc, text + "\n", "No such container" if rc else "")
        if "siteurl" in sql:
            return Done(0, (self.siteurl if self.tables else "") + "\n")
        raise AssertionError("an unexpected query: %s" % sql)

    def cli(self, args):
        key = "cli:" + " ".join(args[:2])
        if key in self.fail:
            rc, text = self.fail[key]
            return Done(rc, "", text)
        if args[:2] == ["plugin", "is-installed"]:
            return Done(0 if (self.html / "wp-content/plugins" / args[2]).exists() else 1)
        if args[:2] == ["theme", "is-installed"]:
            return Done(0 if (self.html / "wp-content/themes" / args[2]).exists() else 1)
        if args[:2] == ["plugin", "is-active"]:
            return Done(0 if self.elementor_active else 1)
        if args[:1] == ["elementor"] and len(args) == 1:
            return Done(0, self.elementor_help)
        if args[:1] == ["search-replace"]:
            return Done(0, "Success: Made 3 replacements.")
        return Done(0, "Success: ok")

    def ext(self, args):
        kind, slug = args[0], args[2]
        versioned = any(a.startswith("--version=") for a in args)
        for key in ("ext:%s:versioned" % slug if versioned else None, "ext:%s" % slug):
            if key and key in self.fail:
                rc, text = self.fail[key]
                return Done(rc, "", text)
        d = self.html / "wp-content" / ("plugins" if kind == "plugin" else "themes") / slug
        d.mkdir(parents=True)
        (d / "main.php").write_text("<?php // %s" % slug)
        return Done(0, "Success: installed")

    def cli_calls(self):
        return [c[2:] for c in self.calls if os.path.basename(c[0]) == "wp-site-cli"]


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.tmp = Path(self.d.name)
        self.data = self.tmp / "data"
        self.intake = self.data / "intake"
        self.site_dir = self.data / "sites" / "wptest"
        self.html = self.site_dir / "html"
        for p in (self.intake, self.html / "wp-content/uploads", self.html / "wp-content/plugins", self.html / "wp-content/themes", self.html / "wp-content/languages", self.site_dir / "db"):
            p.mkdir(parents=True)
        (self.html / "wp-content/plugins/akismet").mkdir()
        (self.site_dir / "wp-config.php").write_text("<?php\n$table_prefix = 'wp_';\n")
        self.cfg = {"data_mount": str(self.data), "intake_dir": str(self.intake), "intake_quota_mb": 3072, "image_pin": "wordpress:7.1.3-php8.5-fpm", "domain": "example.test",
                    "tools_bin": "/opt/bin", "php_uid": 33, "sites": {"wptest": {"state": "hold", "quota_mb": 2048}, "livesite": {"state": "live", "quota_mb": 2048}}}
        self.host = FakeHost(self.html)
        self.out = []

    def tearDown(self):
        self.d.cleanup()

    def put(self, data, name="friend.wpress"):
        p = self.intake / name
        p.write_bytes(data)
        return p

    def archive(self, **kw):
        return self.put(build(kw.pop("files", None) or files(), **kw))

    def run_main(self, argv, get=None, ask=None, cfg=None):
        self.out.clear()
        h = self.host
        code = imp.main(cfg or self.cfg, argv, get=get or get_from(ANSWERS), run=h.run, ask=ask or (lambda prompt: ""), out=self.out.append, popen=h.popen, chown=h.chown, which=h.which)
        return code, "\n".join(self.out)

    def tree(self, root=None):
        root = root or self.tmp
        return sorted(str(p.relative_to(root)) + ("/" if p.is_dir() else "") for p in Path(root).rglob("*") if p.name != ".lock" and not p.name.endswith(".wpress"))

    def assert_refused(self, argv, needle, **kw):
        before, calls = self.tree(), len(self.host.calls)
        code, text = self.run_main(argv, **kw)
        self.assertEqual(code, 2, text)
        self.assertIn(needle, text)
        self.assertEqual(self.tree(), before, "a refusal changed something")
        return text

    def import_ok(self, **kw):
        self.archive(**kw)
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 0, text)
        return text


class Plan(Base):
    def test_the_plan_alone_writes_nothing_and_loads_nothing(self):
        self.archive()
        before = self.tree()
        code, text = self.run_main(["wptest", "friend.wpress", "--plan"])
        self.assertEqual(code, 0, text)
        self.assertIn("PLAN ONLY", text)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.host.sql, b"")
        self.assertFalse([c for c in self.host.calls if os.path.basename(c[0]) in ("wp-site-cli", "wp-site-ext", "wp-site-backup")])

    def test_the_plan_names_every_decision(self):
        self.archive()
        _, text = self.run_main(["wptest", "friend.wpress", "--plan"])
        for needle in ("plugin good", "reinstalled from wordpress.org", "NOT on wordpress.org", "not carried over", "upgrade/ (a half-finished update)", "cache directory", "LiteSpeed cache data",
                       "the migration plugin's own backups", "plugin dormant: inactive", "theme twentytwentyfive: inactive", "unknown directory 'weird-dir'", "archive housekeeping file",
                       "mu-plugins: code that runs on every request"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"plugin\s+litespeed-cache\s+7\.0\s+not carried over")
        self.assertRegex(text, r"plugin\s+notorg\s+5\.0\s+NOT on wordpress\.org")

    def test_the_database_is_asked_as_the_site_user_inside_its_container(self):
        self.archive()
        self.run_main(["wptest", "friend.wpress", "--plan"])
        c = self.host.calls[0]
        self.assertEqual((c[:2], c[2], c[4]), (["docker", "exec"], "-e", "wp_wptest_db"))
        self.assertIn("MARIADB_PASSWORD", c[-1])

    def test_a_hyphenated_site_uses_underscores_in_the_container_name(self):
        (self.data / "sites/my-shop").mkdir()
        (self.data / "sites/my-shop/wp-config.php").write_text("<?php\n$table_prefix = 'wp_';\n")
        self.cfg["sites"]["my-shop"] = {"state": "hold", "quota_mb": 2048}
        self.archive()
        self.run_main(["my-shop", "friend.wpress", "--plan"])
        self.assertEqual(self.host.calls[0][4], "wp_my_shop_db")

    def test_a_relative_name_means_the_intake_directory(self):
        self.archive()
        self.assertEqual(self.run_main(["wptest", str(self.intake / "friend.wpress"), "--plan"])[0], 0)


class Pipeline(Base):
    def test_what_is_unpacked_in_the_work_directory(self):
        self.import_ok()
        work = self.intake / "work-wptest"
        got = sorted(str(p.relative_to(work)) for p in work.rglob("*") if p.is_file())
        self.assertEqual(got, ["PLAN.json", "REPORT.txt", "database.sql", "package.json", "wp-content/languages/x.mo", "wp-content/mu-plugins/m.php", "wp-content/plugins/notorg/lib/x.php",
                               "wp-content/plugins/notorg/notorg.php", "wp-content/uploads/2026/10/a.jpg"])
        for p in work.rglob("*"):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700 if p.is_dir() else 0o600, p)

    def test_the_sql_reaches_the_database_with_the_sites_prefix(self):
        self.import_ok()
        sql = self.host.sql
        self.assertTrue(sql.startswith(imp.PRELUDE))
        self.assertNotIn(b"SERVMASK_PREFIX_", sql)
        self.assertIn(b"`wp_options`", sql)
        self.assertIn(b"'wp_user_roles'", sql)
        self.assertNotIn(b"utf8mb4_0900_ai_ci", sql)
        self.assertIn(b"utf8mb4_unicode_520_ci", sql)
        load = next(c for c in self.host.calls if c[:3] == ["docker", "exec", "-i"])
        self.assertEqual(load[3], "wp_wptest_db")

    def test_the_prefix_comes_from_the_sites_wp_config(self):
        (self.site_dir / "wp-config.php").write_text("<?php\n$table_prefix = 'xy7_';\n")
        self.import_ok()
        self.assertIn(b"`xy7_options`", self.host.sql)
        self.assertNotIn(b"`wp_options`", self.host.sql)

    def test_data_is_copied_with_the_right_owner_and_code_is_held_back(self):
        self.import_ok()
        up = self.html / "wp-content/uploads/2026/10/a.jpg"
        self.assertEqual(up.read_bytes(), b"J" * 300)
        self.assertEqual(stat.S_IMODE(up.stat().st_mode), 0o644)
        self.assertIn((str(up), 33, 33), self.host.chowned)
        self.assertIn((str(self.html / "wp-content/uploads/2026"), 33, 33), self.host.chowned)
        lang = self.html / "wp-content/languages/x.mo"
        self.assertTrue(lang.exists())
        self.assertIn((str(lang), 0, 0), self.host.chowned)
        self.assertFalse((self.html / "wp-content/plugins/notorg").exists(), "a plugin wordpress.org does not offer must wait for approval")
        self.assertFalse((self.html / "wp-content/mu-plugins").exists(), "mu-plugins must wait for approval")

    def test_plugins_and_themes_wordpress_org_offers_are_installed_at_the_archive_version(self):
        self.import_ok()
        ext = [c[2:] for c in self.host.calls if os.path.basename(c[0]) == "wp-site-ext"]
        self.assertIn(["plugin", "install", "good", "--version=1.0"], ext)
        self.assertIn(["theme", "install", "blocksy", "--version=3.0"], ext)
        self.assertNotIn("litespeed-cache", " ".join(" ".join(c) for c in ext))
        self.assertNotIn("notorg", " ".join(" ".join(c) for c in ext))
        self.assertTrue((self.html / "wp-content/plugins/good/main.php").exists())

    def test_the_order_of_the_stages(self):
        self.import_ok()
        kinds = []
        for c in self.host.calls:
            n = os.path.basename(c[0])
            tag = {"wp-site-ext": "ext", "wp-site-backup": "backup"}.get(n)
            if n == "wp-site-cli":
                tag = "cli:" + c[2]
            elif n == "docker" and "-i" in c:
                tag = "load"
            elif n == "docker":
                tag = "query"
            if tag and (not kinds or kinds[-1] != tag):
                kinds.append(tag)
        first = {k: i for i, k in reversed(list(enumerate(kinds)))}
        self.assertLess(first["query"], first["load"])
        self.assertLess(first["load"], first["ext"])
        self.assertLess(first["ext"], first["cli:core"])
        self.assertLess(first["cli:core"], kinds.index("backup"))
        self.assertEqual(kinds[-1], "backup")

    def test_the_wpcli_steps_and_their_arguments(self):
        self.import_ok()
        cli = self.host.cli_calls()
        self.assertIn(["core", "update-db"], cli)
        self.assertIn(["option", "update", "siteurl", "https://wptest.example.test"], cli)
        self.assertIn(["option", "update", "home", "https://wptest.example.test"], cli)
        self.assertIn(["option", "update", "blog_public", "0"], cli)
        self.assertIn(["transient", "delete", "--all"], cli)
        self.assertIn(["rewrite", "flush"], cli)
        sr = [c for c in cli if c[0] == "search-replace"]
        self.assertEqual([c[1:3] for c in sr], [["http://old.local", "https://wptest.example.test"], ["https://old.local", "https://wptest.example.test"], ["//old.local", "//wptest.example.test"],
                                                ["http:\\/\\/old.local", "https:\\/\\/wptest.example.test"], ["https:\\/\\/old.local", "https:\\/\\/wptest.example.test"],
                                                ["\\/\\/old.local", "\\/\\/wptest.example.test"]])
        for c in sr:
            self.assertIn("--all-tables-with-prefix", c)

    def test_no_search_replace_when_the_old_address_is_the_new_one(self):
        m = meta(**dict(META, SiteURL="https://wptest.example.test", HomeURL="https://wptest.example.test"))
        self.import_ok(files=files(m))
        self.assertFalse([c for c in self.host.cli_calls() if c[0] == "search-replace"])

    def test_an_old_address_with_odd_characters_is_never_passed_on(self):
        m = meta(**dict(META, SiteURL="http://x.local;touch /tmp/x", HomeURL="http://old.local"))
        self.archive(files=files(m))
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1, text)
        self.assertNotIn("touch", " ".join(" ".join(c) for c in self.host.calls))
        self.assertIn("will not pass on", text)

    def test_an_old_address_that_looks_like_an_option_is_never_passed_on(self):
        m = meta(**dict(META, SiteURL="http://--skip-plugins", HomeURL="http://old.local"))
        self.archive(files=files(m))
        self.run_main(["wptest", "friend.wpress"])
        self.assertFalse([c for c in self.host.cli_calls() if c[:1] == ["search-replace"] and c[1] in ("--skip-plugins", "http://--skip-plugins")])

    def test_elementor_commands_are_discovered_not_assumed(self):
        m = meta(**dict(META, Plugins=["elementor/elementor.php"]))
        fl = [f for f in files(m) if not f[0].startswith("plugins/good")] + [("plugins/elementor", "elementor.php", plugin_php("4.2"))]
        self.host.elementor_active = True
        self.import_ok(files=fl)
        cli = self.host.cli_calls()
        self.assertIn(["elementor", "replace-urls", "http://old.local", "https://wptest.example.test"], cli)
        self.assertIn(["elementor", "replace-urls", "https://old.local", "https://wptest.example.test"], cli)
        self.assertIn(["elementor", "flush-css"], cli)

    def test_elementor_with_underscore_names(self):
        m = meta(**dict(META, Plugins=["elementor/elementor.php"]))
        fl = [f for f in files(m) if not f[0].startswith("plugins/good")] + [("plugins/elementor", "elementor.php", plugin_php("4.2"))]
        self.host.elementor_active, self.host.elementor_help = True, "  replace_urls\n  flush_css\n"
        self.import_ok(files=fl)
        self.assertIn(["elementor", "replace_urls", "http://old.local", "https://wptest.example.test"], self.host.cli_calls())

    def test_elementor_active_without_a_replace_command_is_a_failed_step(self):
        m = meta(**dict(META, Plugins=["elementor/elementor.php"]))
        fl = [f for f in files(m) if not f[0].startswith("plugins/good")] + [("plugins/elementor", "elementor.php", plugin_php("4.2"))]
        self.host.elementor_active, self.host.elementor_help = True, "usage: wp elementor <command>\n  library\n"
        self.archive(files=fl)
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1)
        self.assertIn("lists no replace command", text)

    def test_elementor_not_active_means_no_elementor_call(self):
        self.import_ok()
        self.assertFalse([c for c in self.host.cli_calls() if c[:1] == ["elementor"]])

    def test_the_record_and_ledger_say_what_was_done(self):
        p = self.archive()
        self.run_main(["wptest", "friend.wpress"])
        work = self.intake / "work-wptest"
        rec = json.loads((work / "PLAN.json").read_text())
        self.assertEqual((rec["stage"], rec["db_loaded"], rec["sha256"]), ("reported", True, imp.sha256(p)))
        self.assertEqual((rec["plugins"]["notorg"], rec["plugins"]["litespeed-cache"], rec["plugins"]["good"]), ("approve", "dropped", "reinstall"))
        self.assertEqual(rec["installed"]["plugin"], ["good"])
        self.assertIn("wp-content/uploads/2026/10/a.jpg", rec["copied"])
        events = [l.split("\t")[2] for l in (self.intake / "ledger.log").read_text().splitlines()]
        self.assertEqual(events, ["unpacked", "loaded", "reported"])

    def test_the_report(self):
        text = self.import_ok()
        for needle in ("IMPORT REPORT for wptest", "STEPS (", "CODE HELD BACK", "notorg", "mu-plugins", "SCAN (a heuristic", "NOT RUN: ClamAV is not installed", "wp-site-import approve wptest",
                       "wp-site-import reset wptest"):
            self.assertIn(needle, text)
        r = self.intake / "work-wptest/REPORT.txt"
        self.assertEqual(stat.S_IMODE(r.stat().st_mode), 0o600)
        self.assertIn("IMPORT REPORT", r.read_text())

    def test_the_first_backup_is_taken_last(self):
        self.import_ok()
        last = self.host.calls[-1]
        self.assertEqual(os.path.basename(last[0]), "wp-site-backup")
        self.assertEqual(last[1], "wptest")

    def test_the_site_stays_on_hold_and_nothing_goes_live(self):
        self.import_ok()
        self.assertEqual(self.cfg["sites"]["wptest"]["state"], "hold")
        self.assertFalse([c for c in self.host.calls if "live" in c])


class Scan(Base):
    def test_php_in_uploads_and_suspicious_code_are_listed(self):
        fl = files(extra=[("uploads/2026", "shell.php", b"<?php eval(base64_decode($_POST['x']));"), ("uploads/2026", "ok.txt", b"hi")])
        fl = [f for f in fl if f[1] != "m.php"] + [("mu-plugins", "m.php", b"<?php system($_GET['c']);")]
        text = self.import_ok(files=fl)
        self.assertIn("2026/shell.php", text)
        self.assertIn("uploads/2026/shell.php: eval(), base64_decode()", text)
        self.assertIn("mu-plugins/m.php: system()/exec()", text)
        self.assertNotIn("ok.txt", text)

    def test_a_clean_tree_has_no_hits(self):
        text = self.import_ok()
        self.assertIn("suspicious patterns in the held code: 0 file(s)", text)
        self.assertIn("suspicious patterns in uploads/: 0 file(s)", text)
        self.assertIn("PHP files under uploads/: none", text)

    def test_clamav_runs_when_it_is_there_and_a_find_is_reported(self):
        self.host.clam = "/usr/bin/clamscan"
        text = self.import_ok()
        self.assertIn("ClamAV:           clean", text)
        clam = next(c for c in self.host.calls if os.path.basename(c[0]) == "clamscan")
        self.assertIn(str(self.intake / "work-wptest/wp-content"), clam)
        self.assertEqual(clam[1:4], ["-r", "--infected", "--no-summary"])

    def test_a_clamav_find_is_shown(self):
        self.host.clam = "/usr/bin/clamscan"
        self.host.fail["clam"] = (1, "/x/evil.php: Php.Trojan FOUND")
        text = self.import_ok()
        self.assertIn("FOUND: /x/evil.php: Php.Trojan FOUND", text)

    def test_checksum_failures_are_shown(self):
        self.host.fail["cli:core verify-checksums"] = (1, "Error: File should not exist: wp-content/evil.php")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertIn("core checksums:   FAILED", text)


class StepFailures(Base):
    def test_a_version_that_is_not_offered_falls_back_to_the_newest_and_says_so(self):
        self.host.fail["ext:good:versioned"] = (1, "Error: Version not found")
        text = self.import_ok()
        self.assertIn("version 1.0 is not offered any more: the newest was installed", text)
        self.assertTrue((self.html / "wp-content/plugins/good").exists())

    def test_an_install_that_fails_both_ways_is_a_failed_step_and_the_run_goes_on(self):
        self.host.fail["ext:good"] = (1, "Error: checksums do not match")
        self.host.fail["ext:good:versioned"] = (1, "Error: checksums do not match")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAILED\s+plugin good")
        self.assertEqual(json.loads((self.intake / "work-wptest/PLAN.json").read_text())["installed"]["plugin"], [])

    def test_a_wpcli_step_that_fails_is_recorded_and_the_run_goes_on(self):
        self.host.fail["cli:core update-db"] = (1, "Error: database upgrade failed")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAILED\s+core update-db")
        self.assertIn(["rewrite", "flush"], self.host.cli_calls())
        self.assertEqual(os.path.basename(self.host.calls[-1][0]), "wp-site-backup")

    def test_a_plugin_already_in_the_image_is_left_alone(self):
        m = meta(**dict(META, Plugins=["akismet/akismet.php"]))
        fl = [f for f in files(m) if not f[0].startswith("plugins/good")] + [("plugins/akismet", "akismet.php", plugin_php("5.0"))]
        answers = dict(ANSWERS); answers[("plugins", "akismet")] = {"version": "5.1", "requires_php": "7.4"}
        self.archive(files=fl)
        code, text = self.run_main(["wptest", "friend.wpress"], get=get_from(answers))
        self.assertEqual(code, 0, text)
        self.assertIn("already in the image", text)
        self.assertFalse([c for c in self.host.calls if os.path.basename(c[0]) == "wp-site-ext" and c[2] == "plugin"])
        self.assertEqual(json.loads((self.intake / "work-wptest/PLAN.json").read_text())["installed"]["plugin"], [])

    def test_a_failed_backup_is_a_failed_step(self):
        self.host.fail["backup"] = (1, "no space")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAILED\s+first backup")

    def test_a_load_that_mariadb_rejects_stops_with_its_message_and_a_way_back(self):
        self.host.sql_rc, self.host.sql_err = 1, b"ERROR 1064 (42000) at line 3: You have an error in your SQL syntax"
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("ERROR 1064", text)
        self.assertIn("wp-site-import reset wptest", text)
        rec = json.loads((self.intake / "work-wptest/PLAN.json").read_text())
        self.assertFalse(rec["db_loaded"])
        self.assertEqual(rec["stage"], "unpacked")
        self.assertFalse([c for c in self.host.calls if os.path.basename(c[0]) in ("wp-site-ext", "wp-site-cli", "wp-site-backup")])
        self.assertEqual([l.split("\t")[2] for l in (self.intake / "ledger.log").read_text().splitlines()], ["unpacked"])

    def test_a_mariadb_that_dies_early_still_reports_why(self):
        self.host.pipe_breaks, self.host.sql_rc, self.host.sql_err = True, 1, b"ERROR 2002: can't connect"
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("can't connect", text)

    def test_a_load_that_ends_without_tables_is_not_taken_for_success(self):
        self.host.sql_loaded_tables = 0
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("treat it as failed", text)

    def test_a_load_without_a_siteurl_is_not_taken_for_success(self):
        self.host.siteurl = ""
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("no siteurl", text)


class Approve(Base):
    def reported(self, **kw):
        self.import_ok(**kw)
        self.host.calls.clear()

    def test_approve_copies_the_held_code_in_removes_the_intake_file_and_keeps_the_site_on_hold(self):
        self.reported()
        code, text = self.run_main(["approve", "wptest"])
        self.assertEqual(code, 0, text)
        self.assertEqual((self.html / "wp-content/plugins/notorg/notorg.php").read_bytes(), plugin_php("5.0"))
        self.assertTrue((self.html / "wp-content/plugins/notorg/lib/x.php").exists())
        self.assertEqual((self.html / "wp-content/mu-plugins/m.php").read_bytes(), b"<?php // mu")
        for p in (self.html / "wp-content/plugins/notorg", self.html / "wp-content/mu-plugins"):
            for f in p.rglob("*"):
                self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o755 if f.is_dir() else 0o644)
                self.assertIn((str(f), 0, 0), self.host.chowned)
        self.assertFalse((self.intake / "work-wptest").exists())
        self.assertFalse((self.intake / "friend.wpress").exists())
        self.assertEqual((self.intake / "ledger.log").read_text().splitlines()[-1].split("\t")[2], "approved")
        self.assertEqual(os.path.basename(self.host.calls[-1][0]), "wp-site-backup")
        self.assertFalse([c for c in self.host.calls if "live" in c])

    def test_reject_leaves_a_plugin_out(self):
        self.reported()
        code, text = self.run_main(["approve", "wptest", "--reject", "notorg"])
        self.assertEqual(code, 0, text)
        self.assertFalse((self.html / "wp-content/plugins/notorg").exists())
        self.assertTrue((self.html / "wp-content/mu-plugins/m.php").exists())
        self.assertIn("rejected notorg", text)

    def test_reject_of_something_that_is_not_held_changes_nothing(self):
        self.reported()
        self.assert_refused(["approve", "wptest", "--reject", "good"], "not held back")
        self.assertTrue((self.intake / "work-wptest").exists())

    def test_approve_refuses_while_a_step_failed_unless_told(self):
        self.host.fail["cli:core update-db"] = (1, "Error: no")
        self.archive()
        self.run_main(["wptest", "friend.wpress"])
        self.assert_refused(["approve", "wptest"], "--accept-failures")
        code, text = self.run_main(["approve", "wptest", "--accept-failures"])
        self.assertEqual(code, 0, text)

    def test_approve_without_an_import_or_at_the_wrong_stage(self):
        self.assert_refused(["approve", "wptest"], "no unapproved import")
        self.archive()
        self.host.sql_rc, self.host.sql_err = 1, b"ERROR"
        self.run_main(["wptest", "friend.wpress"])
        self.assert_refused(["approve", "wptest"], "stage 'unpacked'")

    def test_approve_refuses_to_overwrite_what_is_there_and_copies_nothing(self):
        self.reported()
        (self.html / "wp-content/mu-plugins").mkdir()
        (self.html / "wp-content/mu-plugins/other.php").write_text("<?php")
        self.assert_refused(["approve", "wptest"], "already exists")
        self.assertFalse((self.html / "wp-content/plugins/notorg").exists())

    def test_a_second_approve_finds_nothing_to_approve(self):
        self.reported()
        self.run_main(["approve", "wptest"])
        self.assert_refused(["approve", "wptest"], "no unapproved import")

    def test_approve_on_a_site_not_on_hold(self):
        self.reported()
        self.cfg["sites"]["wptest"]["state"] = "live"
        self.assert_refused(["approve", "wptest"], "on hold")


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
        self.host.tables = 57
        self.assert_refused(["wptest", "friend.wpress"], "is not empty (57 tables)")

    def test_a_database_that_cannot_be_read_is_not_taken_for_empty(self):
        self.archive()
        self.host.fail["count"] = (1, "")
        self.assert_refused(["wptest", "friend.wpress"], "could not read the database")
        self.host.fail["count"] = (0, "ERROR")
        self.assert_refused(["wptest", "friend.wpress"], "could not read the database")
        self.host.fail["count"] = (0, "")
        self.assert_refused(["wptest", "friend.wpress"], "could not read the database")

    def test_a_site_without_a_readable_table_prefix(self):
        self.archive()
        (self.site_dir / "wp-config.php").write_text("<?php\n")
        self.assert_refused(["wptest", "friend.wpress"], "table_prefix")

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
        for argv in ([], ["wptest"], ["a", "b", "c"], ["reset"], ["reset", "a", "b"], ["approve"], ["wptest", "f.wpress", "--accept-failures"], ["wptest", "f.wpress", "--reject", "x"]):
            code, text = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("usage", text)


class Failures(Base):
    def test_a_duplicate_path_removes_the_work_directory(self):
        self.archive(files=files(extra=[("uploads/2026/10", "a.jpg", b"second")]))
        before = self.tree()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("twice", text)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.intake / "ledger.log").exists())
        self.assertEqual(self.host.sql, b"")

    def test_a_file_that_is_also_a_directory_is_refused(self):
        self.archive(files=files(extra=[("uploads/2026/10/a.jpg", "inner.txt", b"x")]))
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertFalse((self.intake / "work-wptest").exists())

    def test_an_archive_that_ends_inside_a_file_leaves_nothing(self):
        data = build(files())
        self.put(data[:data.index(b"J" * 300) + 100])
        before = self.tree()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertEqual(self.tree(), before)

    def test_extract_never_writes_outside_the_work_directory(self):
        self.archive()
        with w.Archive(self.intake / "friend.wpress") as a:
            e = a.entries[0]
            for dest in ("../escape", "wp-content/../../escape", "/abs/escape"):
                with self.assertRaises(imp.Refuse):
                    imp.extract(a, [(e[0], e[1], e[2], e[3], dest)], self.intake / "work-x")
        self.assertFalse((self.intake / "work-x").exists())
        self.assertFalse((self.intake / "escape").exists())

    def test_a_copy_that_does_not_add_up_is_removed(self):
        real = imp.extract

        def short(a, keep, work):
            real(a, keep, work)
            os.truncate(next(p for p in work.rglob("*.jpg")), 10)
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

    def test_copying_never_overwrites_a_file_in_the_site(self):
        (self.html / "wp-content/uploads/2026/10").mkdir(parents=True)
        (self.html / "wp-content/uploads/2026/10/a.jpg").write_bytes(b"mine")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", text)
        self.assertEqual((self.html / "wp-content/uploads/2026/10/a.jpg").read_bytes(), b"mine")
        self.assertIn("wp-site-import reset wptest", text)


class Names(Base):
    def test_a_plugin_name_that_is_a_path_is_refused_at_the_plan(self):
        for entry in ("../../etc/x.php", "../x.php", "./x.php", "..", "a b/x.php"):
            m = meta(**dict(META, Plugins=[entry]))
            self.archive(files=files(m))
            before = self.tree()
            code, text = self.run_main(["wptest", "friend.wpress", "--plan"])
            self.assertEqual(code, 2, entry)
            self.assertEqual(self.tree(), before)

    def test_a_theme_name_that_is_a_path_is_refused_at_the_plan(self):
        m = meta(**dict(META, Template="../..", Stylesheet="../.."))
        self.archive(files=files(m))
        code, text = self.run_main(["wptest", "friend.wpress", "--plan"])
        self.assertEqual(code, 2)
        self.assertIn("not a plain folder name", text)

    def test_a_tampered_record_cannot_make_approve_copy_outside_the_plugin_directory(self):
        self.import_ok()
        rp = self.intake / "work-wptest/PLAN.json"
        rec = json.loads(rp.read_text())
        rec["plugins"][".."] = "approve"
        rp.write_text(json.dumps(rec))
        before = self.tree(self.html)
        self.assert_refused(["approve", "wptest"], "not a plain folder name")
        self.assertEqual(self.tree(self.html), before)

    def test_a_tampered_record_cannot_make_reset_remove_outside_the_plugin_directory(self):
        self.import_ok()
        victim = self.html / "wp-content/precious"
        victim.mkdir()
        (victim / "keep").write_text("k")
        rp = self.intake / "work-wptest/PLAN.json"
        rec = json.loads(rp.read_text())
        rec["installed"]["plugin"] += ["../precious", "..", "/etc"]
        rp.write_text(json.dumps(rec))
        code, _ = self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(code, 0)
        self.assertTrue((victim / "keep").exists())
        self.assertTrue((self.html / "wp-content").is_dir())


class Reset(Base):
    def test_reset_after_a_complete_run_returns_the_site_to_its_earlier_tree(self):
        before = self.tree(self.site_dir)
        self.import_ok()
        self.assertNotEqual(self.tree(self.site_dir), before)
        code, text = self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.tree(self.site_dir), before)
        self.assertIn("`wp_options`", self.host.dropped)
        self.assertIn("`wp_posts`", self.host.dropped)
        self.assertTrue(self.host.dropped.startswith("SET FOREIGN_KEY_CHECKS=0;"))
        self.assertFalse((self.intake / "work-wptest").exists())
        self.assertTrue((self.intake / "friend.wpress").exists())
        self.assertEqual((self.intake / "ledger.log").read_text().splitlines()[-1].split("\t")[1:3], ["wptest", "reset"])
        self.assertTrue((self.html / "wp-content/plugins/akismet").exists(), "something the run did not install must stay")

    def test_reset_after_a_failed_load_does_not_touch_the_database(self):
        self.host.sql_rc, self.host.sql_err = 1, b"ERROR"
        self.archive()
        self.run_main(["wptest", "friend.wpress"])
        self.host.calls.clear()
        code, text = self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.host.dropped, "")
        self.assertFalse([c for c in self.host.calls if c[:2] == ["docker", "exec"]])

    def test_reset_after_a_half_finished_copy_removes_only_what_was_made(self):
        (self.html / "wp-content/uploads/2026/10").mkdir(parents=True)
        (self.html / "wp-content/uploads/2026/10/a.jpg").write_bytes(b"mine")
        before = self.tree(self.site_dir)
        self.archive()
        self.run_main(["wptest", "friend.wpress"])
        code, text = self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.tree(self.site_dir), before)
        self.assertEqual((self.html / "wp-content/uploads/2026/10/a.jpg").read_bytes(), b"mine")

    def test_a_wrong_name_or_none_removes_nothing(self):
        self.import_ok()
        for answer in ("wptes\n", "\n", "WPTEST\n"):
            self.assert_refused(["reset", "wptest"], "nothing was removed", ask=lambda prompt, a=answer: a)
        self.assertEqual(self.host.dropped, "")

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

    def test_a_work_directory_without_a_record_is_just_removed(self):
        (self.intake / "work-wptest").mkdir()
        (self.intake / "work-wptest/junk").write_text("x")
        code, _ = self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(code, 0)
        self.assertFalse((self.intake / "work-wptest").exists())

    def test_a_table_name_that_cannot_be_put_in_a_statement_stops_the_drop(self):
        self.import_ok()
        self.host.table_names = ["wp_options", "wp_x`; DROP DATABASE wordpress; --"]
        self.assert_refused(["reset", "wptest"], "drop the tables by hand", ask=lambda prompt: "wptest\n")

    def test_after_a_reset_the_import_can_be_run_again(self):
        self.import_ok()
        self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.host.sql = b""
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 0, text)


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
        exec(text.split("import wp_site_import  # noqa: E402\n")[1].split("sys.exit(")[0], ns)
        return ns["CONFIG"]

    def test_the_rendered_wrapper_is_valid_python_with_the_repos_values(self):
        text, v = self.render(wp_sites=[{"name": "alpha", "port": 9201, "quota_id": 1201, "state": "hold", "quota_mb": 4096}, {"name": "beta", "port": 9202, "quota_id": 1202},
                                        {"name": "gone", "port": 9203, "quota_id": 1203, "state": "closed"}])
        compile(text, "wp-site-import", "exec")
        self.assertTrue(text.startswith("#!/usr/bin/python3 -I\n"))
        cfg = self.config(text)
        self.assertEqual(cfg["sites"], {"alpha": {"state": "hold", "quota_mb": 4096}, "beta": {"state": v["wp_site_defaults"]["state"], "quota_mb": v["wp_site_defaults"]["quota_mb"]}})
        self.assertEqual((cfg["intake_dir"], cfg["intake_quota_mb"], cfg["image_pin"], cfg["data_mount"]), (v["wp_intake_dir"], v["wp_intake_quota_mb"], v["wp_image_php"], v["wp_data_mount"]))
        self.assertEqual((cfg["domain"], cfg["tools_bin"], cfg["php_uid"]), (v["wp_domain"], v["wp_tools_bin"], v["wp_php_uid"]))
        self.assertIsInstance(cfg["php_uid"], int)
        self.assertIn('sys.path.insert(0, "%s")' % v["wp_tools_lib"], text)

    def test_the_rendered_config_runs_the_real_tool(self):
        text, v = self.render(wp_sites=[{"name": "wptest", "port": 9101, "quota_id": 1001, "state": "hold"}])
        self.assertEqual(w.pin(self.config(text)["image_pin"]), w.ours(ROOT))

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

    def test_old_hosts_are_read_from_the_four_url_fields_once_each(self):
        m = {"SiteURL": "http://a.local/", "HomeURL": "https://a.local", "InternalSiteURL": "http://b.local:8080", "InternalHomeURL": ""}
        self.assertEqual(imp.old_hosts(m), ["a.local", "b.local:8080"])


if __name__ == "__main__":
    unittest.main()
