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
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import jinja2  # noqa: E402
import yaml  # noqa: E402
import wp_site_import as imp  # noqa: E402
import wp_wpress as w  # noqa: E402
from test_wp_sites_render import ROOT, inventory_hostvars, load  # noqa: E402
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

    def kill(self):
        self.host.killed = True

    def wait(self, timeout=None):
        if self.host.wait_times_out:
            raise subprocess.TimeoutExpired("docker", timeout)
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
        self.wait_times_out, self.killed, self.timeouts, self.cli_log, self.on_chown = False, False, set(), [], None
        self.stored_urls = None
        self.runtime_files, self.runtime_done, self.stale_update_state, self.update_rows_absent = [], False, False, False
        self.backups, self.backup_n = None, 0      # a directory: wp-site-backup then leaves a snapshot in it, as the real one does
        self.wipes, self.recovered, self.stash, self.stash_tables = 0, [], None, 0

    def which(self, name):
        return self.clam if name == "clamscan" else None

    def chown(self, path, uid, gid):
        self.chowned.append((str(path), uid, gid))
        if self.on_chown:
            self.on_chown(len(self.chowned))

    def popen(self, cmd, stdin=None, stdout=None, stderr=None):
        self.calls.append(cmd)
        return FakePopen(self, stderr)

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        name = os.path.basename(cmd[0])
        if name in self.timeouts or (name == "docker" and "docker" in self.timeouts):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        if name == "docker":
            return self.docker(cmd, kw)
        if name == "wp-site-cli":
            return self.cli(cmd[2:])
        if name == "wp-site-ext":
            return self.ext(cmd[2:])
        if name == "wp-site-backup":
            rc, text = self.fail.get("backup", (0, "snapshot taken"))
            if rc == 0 and self.backups is not None:
                self.backup_n += 1
                stamp = "20261025T%06dZ" % (100000 + self.backup_n)
                snap = self.backups / cmd[1] / stamp
                snap.mkdir(parents=True)
                tag = "".join("tag=%s\n" % a[6:] for a in cmd[2:] if a.startswith("--tag="))
                (snap / "MANIFEST").write_text("site=%s\nstamp=%s\ncore=7.1.3\n%s" % (cmd[1], stamp, tag))
                (self.backups / cmd[1] / ".last-success").write_text(stamp + "\n")
                text = "OK: snapshot %s of %s (12 KiB on disk, core 7.1.3)" % (stamp, cmd[1])
            return Done(rc, text, "")
        if name == "wp-site-recover":
            rc, text = self.fail.get("recover", (0, "recovered"))
            self.recovered.append(cmd[1:])
            if rc == 0 and self.stash is not None:      # the old site comes back: its files and its tables
                shutil.rmtree(self.html / "wp-content")
                shutil.copytree(self.stash, self.html / "wp-content", symlinks=True)
                self.tables = self.stash_tables
            return Done(rc, text, "")
        if name in ("wp-clamscan", "wp-vulnfeed"):      # exit 0 clean, 1 found, 3 not set up (the defaults: nothing is set up)
            rc, text = self.fail.get("clam" if name == "wp-clamscan" else "vuln", (3, "ClamAV is not set up on this VM (no signatures)" if name == "wp-clamscan" else "no feed key"))
            return Done(rc, text, "")
        raise AssertionError("an unexpected command: %r" % (cmd,))

    def docker(self, cmd, kw):
        if cmd[1] == "run":      # wp-content reset to the image's (--replace)
            if "run" in self.fail:
                return Done(*self.fail["run"])
            self.wipes += 1
            self.wipe_script = cmd[-1]
            wc = self.html / "wp-content"
            for p in list(wc.iterdir()):
                if p.name != "uploads":
                    shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
            for p in list((wc / "uploads").iterdir()) if (wc / "uploads").is_dir() else []:
                shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
            for d in ("plugins/akismet", "themes", "languages", "uploads"):
                (wc / d).mkdir(parents=True, exist_ok=True)
            return Done(0)
        if "-i" in cmd:   # the drop of the tables
            if "drop" in self.fail:
                return Done(*self.fail["drop"])
            self.dropped = kw["input"]
            self.tables = 0
            return Done(0)
        sql = cmd[3][4:]
        if "SELECT table_name" in sql:
            return Done(0, "\n".join(self.table_names) + "\n")
        if "COUNT(*)" in sql:
            rc, text = self.fail.get("count", (0, str(self.tables)))
            return Done(rc, text + "\n", "No such container" if rc else "")
        if "option_name IN" in sql:
            if "option_names" in self.fail:
                return Done(*self.fail["option_names"])
            url = self.stored_urls if self.stored_urls is not None else "https://wptest.example.test"
            return Done(0, "home\t%s\nsiteurl\t%s\n" % (url, url))
        if "siteurl" in sql:
            return Done(0, (self.siteurl if self.tables else "") + "\n")
        raise AssertionError("an unexpected query: %s" % sql)

    def cli(self, args):
        flags = [a for a in args if a in ("--skip-plugins", "--skip-themes")]
        args = [a for a in args if a not in ("--skip-plugins", "--skip-themes")]
        self.cli_log.append((args, flags))
        for key in ("cli:" + " ".join(args[:3]), "cli:" + " ".join(args[:2])):
            if key in self.fail:
                rc, text = self.fail[key]
                return Done(rc, "", text)
        if self.runtime_files and not self.runtime_done:
            self.runtime_done = True
            for rel in self.runtime_files:
                target = self.html / "wp-content" / rel
                if rel.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                elif rel.startswith("link:"):
                    name, _, dest = rel[5:].partition("->")
                    (self.html / "wp-content" / name).symlink_to(dest)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("made by a plugin")
        if args[:3] == ["option", "delete", "_site_transient_update_plugins"]:
            if self.stale_update_state:
                self.stale_update_state = False
                return Done(0, "Success: Deleted '_site_transient_update_plugins' option.")
            return Done(1, "", "Error: Could not delete '_site_transient_update_plugins' option. Does it exist?") if self.update_rows_absent else Done(0, "Success: Deleted.")
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
        if self.stale_update_state:       # the real tool's answer when WordPress core thinks the plugin is installed because the archive's update check says so
            return Done(1, "", "Warning: %s: Plugin already installed.\nSuccess: Plugin already installed.\nWarning: The '%s' plugin could not be found.\nError: You need to specify either one or more plugin slugs to check" % (slug, slug))
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
        return [a for a, _ in self.cli_log]


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
        self.cfg = {"data_mount": str(self.data), "intake_dir": str(self.intake), "backup_dir": str(self.tmp / "backups"), "intake_quota_mb": 3072, "image_pin": "wordpress:7.1.3-php8.5-fpm", "domain": "example.test",
                    "tools_bin": "/opt/bin", "php_uid": 33, "step_timeout_s": 3600, "sites": {"wptest": {"state": "hold", "quota_mb": 2048}, "livesite": {"state": "live", "quota_mb": 2048}}}
        self.host = FakeHost(self.html)
        self.host.backups = self.tmp / "backups"
        (self.host.backups / "wptest").mkdir(parents=True)
        self.out = []

    def tearDown(self):
        self.d.cleanup()

    def put(self, data, name="friend.wpress"):
        p = self.intake / name
        p.write_bytes(data)
        return p

    def archive(self, **kw):
        return self.put(build(kw.pop("files", None) or files(), **kw))

    def run_main(self, argv, get=None, ask=None, cfg=None, stdin=None, clock=None):
        self.out.clear()
        h = self.host
        code = imp.main(cfg or self.cfg, argv, get=get or get_from(ANSWERS), run=h.run, ask=ask or (lambda prompt: ""), out=self.out.append, popen=h.popen, chown=h.chown, which=h.which, stdin=stdin,
                        **({"clock": clock} if clock else {}))
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
        self.assertNotIn(["option", "update", "siteurl", "https://wptest.example.test"], cli, "wp-config.php defines WP_SITEURL, so this would write nothing and say ok")
        self.assertIn(["option", "update", "blog_public", "0"], cli)
        self.assertIn(["transient", "delete", "--all"], cli)
        self.assertIn(["option", "delete", "rewrite_rules"], cli)
        self.assertNotIn(["rewrite", "flush"], cli, "a flush with themes skipped stores an incomplete rule set")
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
        for needle in ("IMPORT REPORT for wptest", "STEPS (", "CODE HELD BACK", "notorg", "mu-plugins", "SCAN (a heuristic", "NOT RUN: ClamAV is not set up on this VM", "wp-site-import approve wptest",
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


class StaleUpdateState(Base):
    def test_the_archives_update_checks_are_dropped_before_any_install(self):
        self.host.stale_update_state = True
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 0, text)
        self.assertIn("archive's update checks dropped", text)
        self.assertTrue((self.html / "wp-content/plugins/good").exists(), "with the stale rows gone the install really installs")
        order = []
        for c in self.host.calls:
            n = os.path.basename(c[0])
            if n == "wp-site-cli" and c[2:4] == ["option", "delete"] and c[4].startswith("_site_transient_"):
                order.append("drop")
            elif n == "wp-site-ext":
                order.append("install")
        self.assertEqual(order[:6], ["drop"] * 6)
        self.assertLess(order.index("drop"), order.index("install"))

    def test_without_the_drop_the_stand_in_reproduces_the_failure_seen_on_dellwp(self):
        self.host.stale_update_state = True
        self.host.update_rows_absent = False
        # the drop is the only thing that clears the flag in the stand-in; skip it and the install is the silent no-op of 2026-10-25
        real = imp.clear_update_state
        imp.clear_update_state = lambda site, tools: []
        try:
            self.archive()
            code, text = self.run_main(["wptest", "friend.wpress"])
        finally:
            imp.clear_update_state = real
        self.assertEqual(code, 1)
        self.assertIn("Plugin already installed", text)

    def test_all_six_rows_are_named_and_a_missing_row_is_fine(self):
        self.host.update_rows_absent = True
        text = self.import_ok()
        names = [c[4] for c in self.host.calls if os.path.basename(c[0]) == "wp-site-cli" and c[2:4] == ["option", "delete"] and c[4].startswith("_site_transient_")]
        self.assertEqual(names, ["_site_transient_update_plugins", "_site_transient_timeout_update_plugins", "_site_transient_update_themes", "_site_transient_timeout_update_themes",
                                 "_site_transient_update_core", "_site_transient_timeout_update_core"], "the literal names, not the constant compared with itself")
        self.assertIn("rows were there", text)

    def test_any_other_failure_to_drop_one_stops_the_run_with_a_way_back(self):
        self.host.fail["cli:option delete _site_transient_update_themes"] = (1, "Error: database gone")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("could not drop _site_transient_update_themes before the installs", text)
        self.assertIn("wp-site-import reset wptest", text)
        self.assertFalse([c for c in self.host.calls if os.path.basename(c[0]) == "wp-site-ext"], "nothing was installed on top of stale rows")


class OwnSnapshot(Base):
    """The first backup the import takes is the import's own: a turned-down import takes it away, so the site's backups are as they were."""
    def snaps(self, site="wptest"):
        return sorted(p.name for p in (self.host.backups / site).iterdir() if p.is_dir())

    def reset(self):
        return self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")

    def test_reset_removes_the_snapshot_the_import_took_and_the_marker_that_names_it(self):
        self.import_ok()
        self.assertEqual(len(self.snaps()), 1)
        self.assertEqual(json.loads((self.intake / "work-wptest/PLAN.json").read_text())["snapshots"], self.snaps())
        code, text = self.reset()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.snaps(), [])
        self.assertFalse((self.host.backups / "wptest/.last-success").exists())
        self.assertIn("removed the 1 snapshot(s) this import took", text)

    def test_an_earlier_snapshot_stays_and_the_marker_goes_back_to_it(self):
        old = self.host.backups / "wptest/20260101T000000Z"
        old.mkdir()
        (old / "MANIFEST").write_text("site=wptest\nstamp=20260101T000000Z\n")
        self.import_ok()
        self.assertEqual(len(self.snaps()), 2)
        self.reset()
        self.assertEqual(self.snaps(), ["20260101T000000Z"])
        self.assertEqual((self.host.backups / "wptest/.last-success").read_text().strip(), "20260101T000000Z")

    def test_a_marker_that_names_another_snapshot_is_left_alone(self):
        old = self.host.backups / "wptest/20260101T000000Z"
        old.mkdir()
        (old / "MANIFEST").write_text("site=wptest\n")
        self.import_ok()
        (self.host.backups / "wptest/.last-success").write_text("20260101T000000Z\n")
        self.reset()
        self.assertEqual((self.host.backups / "wptest/.last-success").read_text().strip(), "20260101T000000Z")

    def test_a_failed_backup_leaves_nothing_to_remove_and_no_record(self):
        self.host.fail["backup"] = (1, "no space")
        self.archive()
        self.run_main(["wptest", "friend.wpress"])
        self.assertNotIn("snapshots", json.loads((self.intake / "work-wptest/PLAN.json").read_text()))
        code, text = self.reset()
        self.assertEqual(code, 0, text)
        self.assertNotIn("snapshot(s) this import took", text)

    def tamper(self, value):
        self.import_ok()
        p = self.intake / "work-wptest/PLAN.json"
        rec = json.loads(p.read_text())
        rec["snapshots"] = value
        p.write_text(json.dumps(rec))

    def test_a_record_cannot_name_a_path_outside_the_sites_backups(self):
        victim = self.host.backups / "other"
        victim.mkdir()
        (victim / "MANIFEST").write_text("site=wptest\n")
        self.tamper(["../other", "../../backups/wptest/..", "", None, 7, "20261025T100001Z/../.."])
        code, text = self.reset()
        self.assertEqual(code, 0, text)
        self.assertTrue(victim.is_dir())
        self.assertEqual(len(self.snaps()), 1)

    def test_a_record_with_snapshots_that_is_not_a_list_is_ignored(self):
        self.tamper("20261025T100001Z")
        code, text = self.reset()
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.snaps()), 1)

    def test_a_snapshot_of_another_site_or_a_final_one_is_never_removed(self):
        self.tamper([])
        for name, manifest in (("20261025T200001Z", "site=other\n"), ("20261025T200002Z", "site=wptest\ntag=final\n"), ("20261025T200003Z", "site=wptest\ntag=pre-update\n"),
                               ("20261025T200004Z", "")):
            d = self.host.backups / "wptest" / name
            d.mkdir()
            if manifest:
                (d / "MANIFEST").write_text(manifest)
        p = self.intake / "work-wptest/PLAN.json"
        rec = json.loads(p.read_text())
        rec["snapshots"] = ["20261025T20000%dZ" % i for i in (1, 2, 3, 4)]
        p.write_text(json.dumps(rec))
        before = self.snaps()
        self.reset()
        self.assertEqual(self.snaps(), before)

    def test_a_link_named_like_a_snapshot_is_not_followed(self):
        target = self.tmp / "precious"
        target.mkdir()
        (target / "MANIFEST").write_text("site=wptest\n")
        (self.host.backups / "wptest/20261025T100009Z").symlink_to(target)
        self.tamper(["20261025T100009Z"])
        self.reset()
        self.assertTrue((target / "MANIFEST").exists())

    def test_approve_keeps_the_snapshot_and_takes_another(self):
        self.import_ok()
        code, text = self.run_main(["approve", "wptest"])
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.snaps()), 2)


class ReplaceImport(Base):
    """--replace: a newer archive onto a site that already has data. The old site is snapshotted first (tag pre-import), wiped, and the archive imported; a deny puts the old site back through
    wp-site-recover; an approve keeps the snapshot as a pre-update one."""
    def setUp(self):
        super().setUp()
        (self.html / "wp-content/uploads/old-photo.jpg").write_bytes(b"old")
        (self.html / "wp-content/plugins/old-plugin").mkdir()
        (self.html / "wp-content/plugins/old-plugin/old.php").write_text("<?php // old\n")
        self.host.tables = 57
        self.host.stash = self.tmp / "old-wp-content"      # what wp-site-recover (a stand-in) puts back
        shutil.copytree(self.html / "wp-content", self.host.stash, symlinks=True)
        self.host.stash_tables = 57
        self.before = self.tree(self.html)

    def replace(self, **kw):
        self.archive(**kw)
        return self.run_main(["wptest", "friend.wpress", "--replace"], ask=lambda p: "wptest\n")

    def snaps(self):
        return sorted(p.name for p in (self.host.backups / "wptest").iterdir() if p.is_dir())

    def test_an_import_onto_a_site_with_data_is_refused_and_points_at_replace(self):
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2); self.assertIn("--replace", text); self.assertIn("is not empty", text)
        self.assertEqual(self.tree(self.html), self.before)

    def test_replace_on_a_site_without_data_is_refused(self):
        self.host.tables = 0
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress", "--replace"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 2); self.assertIn("nothing to replace", text)
        self.assertFalse((self.intake / "work-wptest").exists())

    def test_the_site_name_must_be_typed_and_nothing_happens_without_it(self):
        self.archive()
        for answer in ("", "no\n", "WPTEST\n", "wptest2\n"):
            code, text = self.run_main(["wptest", "friend.wpress", "--replace"], ask=lambda p, a=answer: a)
            self.assertEqual(code, 2, answer); self.assertIn("nothing was changed", text)
        code, text = self.run_main(["wptest", "friend.wpress", "--replace"])
        self.assertEqual(code, 2, "no way to ask is a no")
        self.assertEqual(self.tree(self.html), self.before)
        self.assertFalse((self.intake / "work-wptest").exists()); self.assertEqual(self.host.wipes, 0); self.assertEqual(self.snaps(), [])

    def test_the_prompt_says_what_will_happen(self):
        self.archive()
        seen = []
        self.run_main(["wptest", "friend.wpress", "--replace"], ask=lambda p: seen.append(p) or "wptest\n")
        self.assertEqual(len(seen), 1)
        for word in ("REPLACES everything of wptest", "snapshot", "deny puts it back", "Type the site name"):
            self.assertIn(word, seen[0])

    def test_the_plan_alone_asks_nothing_and_writes_nothing(self):
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress", "--replace", "--plan"], ask=lambda p: self.fail("asked"))
        self.assertEqual(code, 0, text); self.assertIn("PLAN ONLY", text)
        self.assertEqual(self.tree(self.html), self.before); self.assertEqual(self.snaps(), []); self.assertEqual(self.host.wipes, 0)

    def test_the_old_site_is_snapshotted_before_anything_is_wiped_and_the_archive_replaces_it(self):
        code, text = self.replace()
        self.assertEqual(code, 0, text)
        names = [os.path.basename(c[0]) for c in self.host.calls]
        i_backup = next(i for i, c in enumerate(self.host.calls) if os.path.basename(c[0]) == "wp-site-backup")
        self.assertEqual(self.host.calls[i_backup][1:], ["wptest", "--tag=pre-import", "--force"])
        i_drop = next(i for i, c in enumerate(self.host.calls) if c[0] == "docker" and "-i" in c)
        i_wipe = next(i for i, c in enumerate(self.host.calls) if c[0] == "docker" and c[1] == "run")
        self.assertLess(i_backup, i_drop); self.assertLess(i_drop, i_wipe)
        self.assertEqual(names.count("wp-site-backup"), 2, "the safety snapshot and the import's own first backup")
        self.assertFalse((self.html / "wp-content/uploads/old-photo.jpg").exists()); self.assertFalse((self.html / "wp-content/plugins/old-plugin").exists())
        self.assertEqual((self.html / "wp-content/uploads/2026/10/a.jpg").read_bytes(), b"J" * 300, "the archive's content is in")
        self.assertEqual(self.host.tables, 57); self.assertIn(b"SERVMASK", b"SERVMASK" + self.host.sql)
        rec = json.loads((self.intake / "work-wptest/PLAN.json").read_text())
        self.assertEqual(rec["replace"]["wiped"], True); self.assertRegex(rec["replace"]["snapshot"], r"^\d{8}T\d{6}Z$")
        self.assertIn("REPLACE: the old data of wptest is in snapshot " + rec["replace"]["snapshot"], text)
        manifest = (self.host.backups / "wptest" / rec["replace"]["snapshot"] / "MANIFEST").read_text()
        self.assertIn("tag=pre-import", manifest)

    def test_the_wipe_never_replaces_the_uploads_directory_a_running_container_has_mounted(self):
        uploads = self.html / "wp-content/uploads"
        before = uploads.stat().st_ino
        code, text = self.replace()
        self.assertEqual(code, 0, text)
        self.assertEqual(uploads.stat().st_ino, before, "the same directory: the php container's bind mount still points at it")
        sh = self.host.wipe_script
        self.assertNotIn("rm -rf /var/www/html/wp-content", sh); self.assertNotIn("rm -rf -- /var/www/html/wp-content", sh); self.assertNotRegex(sh, r"rmdir|mv ")
        self.assertIn("-mindepth 1 -maxdepth 1 ! -name uploads -exec rm -rf -- {} +", sh)
        self.assertIn("find uploads -mindepth 1 -delete", sh)
        self.assertIn("cp -a /usr/src/wordpress/wp-content/* . &&", sh, "the image's files are copied, not the image's directory attributes")
        self.assertNotRegex(sh, r"wp-content/\.\s", "cp -a src/. dst would put the image's owner and mode on wp-content itself, and root without capabilities could not write there")
        self.assertNotRegex(sh, r"chmod|chown [^&]*\s\.\s", "and nothing here changes the owner or mode of wp-content itself")
        self.assertIn("chown %d:%d uploads" % (self.cfg["php_uid"], self.cfg["php_uid"]), sh)
        self.assertIn("! -name uploads -exec chown -R 0:0", sh, "code is root's, uploads stays the php user's")
        self.assertFalse((uploads / "old-photo.jpg").exists(), "and it was emptied")

    def test_a_snapshot_that_cannot_be_taken_stops_before_anything_is_touched(self):
        self.host.fail["backup"] = (1, "no space left")
        code, text = self.replace()
        self.assertEqual(code, 2); self.assertIn("safety snapshot", text); self.assertIn("nothing was changed", text)
        self.assertEqual(self.host.wipes, 0); self.assertEqual(self.host.dropped, "")
        self.assertEqual(self.tree(self.html), self.before)
        code, text = self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 0, text); self.assertIn("was not touched", text); self.assertEqual(self.host.recovered, [])
        self.assertFalse((self.intake / "work-wptest").exists())

    def test_reset_after_a_replace_puts_the_old_site_back_and_removes_the_snapshots(self):
        code, text = self.replace()
        self.assertEqual(code, 0, text)
        stamp = json.loads((self.intake / "work-wptest/PLAN.json").read_text())["replace"]["snapshot"]
        code, text = self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.host.recovered, [["wptest", stamp, "--yes", "--no-undo"]])
        self.assertEqual(self.tree(self.html), self.before, "the site is exactly as it was")
        self.assertEqual(self.host.tables, 57)
        self.assertEqual(self.snaps(), [], "the safety snapshot and the import's own first backup are both gone")
        self.assertFalse((self.host.backups / "wptest/.last-success").exists())
        self.assertFalse((self.intake / "work-wptest").exists())
        self.assertIn("the old wptest is back from snapshot " + stamp, text)

    def test_the_manifest_undo_is_not_used_for_a_replace(self):
        self.replace()
        before_calls = len(self.host.calls)
        self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        later = self.host.calls[before_calls:]
        self.assertFalse([c for c in later if c[0] == "docker" and "-i" in c], "no table is dropped by the reset: the recover replaces them")

    def test_a_recover_that_fails_removes_nothing(self):
        self.replace()
        stamp = json.loads((self.intake / "work-wptest/PLAN.json").read_text())["replace"]["snapshot"]
        self.host.fail["recover"] = (1, "database container is not healthy")
        code, text = self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 2); self.assertIn("failed", text); self.assertIn("wp-site-recover wptest %s --yes --no-undo" % stamp, text)
        self.assertTrue((self.intake / "work-wptest").is_dir(), "the work directory stays so reset can be run again")
        self.assertIn(stamp, self.snaps(), "the old site's snapshot is the only copy: it stays")
        self.host.fail.pop("recover")
        code, text = self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 0, text); self.assertEqual(self.tree(self.html), self.before)

    def test_a_wipe_that_fails_half_way_is_undone_from_the_snapshot(self):
        self.host.fail["run"] = (1, "docker: no space left on device")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress", "--replace"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 2); self.assertIn("could not be reset", text); self.assertIn("reset wptest", text)
        self.assertEqual(self.host.tables, 0, "the tables were already dropped")
        self.host.fail.pop("run")
        code, text = self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.host.recovered), 1)
        self.assertEqual(self.tree(self.html), self.before); self.assertEqual(self.host.tables, 57); self.assertEqual(self.snaps(), [])

    def test_approve_keeps_the_old_sites_snapshot_but_as_an_ordinary_pre_update_one(self):
        self.replace()
        stamp = json.loads((self.intake / "work-wptest/PLAN.json").read_text())["replace"]["snapshot"]
        code, text = self.run_main(["approve", "wptest"])
        self.assertEqual(code, 0, text)
        manifest = (self.host.backups / "wptest" / stamp / "MANIFEST").read_text()
        self.assertIn("tag=pre-update", manifest); self.assertNotIn("tag=pre-import", manifest)
        self.assertIn("site=wptest", manifest)
        self.assertIn("now a pre-update snapshot", text)
        self.assertFalse((self.host.backups / "wptest" / stamp / ".MANIFEST.new").exists())

    def test_a_record_that_has_been_tampered_with_cannot_make_the_tool_touch_other_paths(self):
        self.replace()
        victim = self.host.backups / "other"
        victim.mkdir()
        (victim / "MANIFEST").write_text("site=wptest\ntag=pre-import\n")
        p = self.intake / "work-wptest/PLAN.json"
        for bad in ({"snapshot": "../other", "wiped": True}, {"snapshot": "20261025T1Z", "wiped": True}, "yes", ["x"], {"snapshot": 7, "wiped": True}, {"snapshot": None, "wiped": True},
                    None, {}, "", 0, [], {"snapshot": None}, {"wiped": True}, {"snapshot": None, "wiped": "yes"}, {"snapshot": None, "wiped": False, "extra": 1}):
            rec = json.loads(p.read_text()); rec["replace"] = bad; p.write_text(json.dumps(rec))
            n = len(self.host.recovered)
            code, text = self.run_main(["reset", "wptest"], ask=lambda q: "wptest\n")
            self.assertEqual(code, 2, bad); self.assertEqual(len(self.host.recovered), n, bad)
            self.assertTrue((victim / "MANIFEST").exists(), bad); self.assertTrue(p.exists(), bad)

    def test_a_drop_that_fails_after_the_wipe_began_is_undone_from_the_snapshot(self):
        self.host.fail["drop"] = (1, "ERROR 1051: could not drop")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress", "--replace"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 2)
        rec = json.loads((self.intake / "work-wptest/PLAN.json").read_text())
        self.assertTrue(rec["replace"]["wiped"], "the wipe is recorded before it starts")
        self.host.fail.pop("drop")
        code, text = self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 0, text); self.assertEqual(len(self.host.recovered), 1); self.assertEqual(self.tree(self.html), self.before)

    def test_the_intent_is_on_disk_before_the_snapshot_is_taken(self):
        orig = self.host.run

        def boom(cmd, **kw):
            if os.path.basename(cmd[0]) == "wp-site-backup":
                raise OSError("the machine went away")
            return orig(cmd, **kw)
        self.host.run = boom
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress", "--replace"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 2)
        rec = json.loads((self.intake / "work-wptest/PLAN.json").read_text())
        self.assertEqual(rec["replace"], {"snapshot": None, "wiped": False})

    def test_approve_leaves_a_snapshot_that_is_not_pre_import_exactly_as_it_is(self):
        self.replace()
        p = self.intake / "work-wptest/PLAN.json"
        rec = json.loads(p.read_text())
        mf = self.host.backups / "wptest" / rec["replace"]["snapshot"] / "MANIFEST"
        mf.write_text("site=wptest\nstamp=x\ntag=final\n")
        code, text = self.run_main(["approve", "wptest"])
        self.assertEqual(code, 0, text); self.assertIn("is not a pre-import snapshot", text)
        self.assertEqual(mf.read_text(), "site=wptest\nstamp=x\ntag=final\n")

    def test_the_old_data_does_not_count_against_the_quota_because_it_is_wiped_first(self):
        (self.html / "wp-content/uploads/big-old.bin").write_bytes(b"x" * (3 << 20))
        shutil.rmtree(self.host.stash)
        shutil.copytree(self.html / "wp-content", self.host.stash, symlinks=True)
        self.cfg["sites"]["wptest"]["quota_mb"] = 2
        code, text = self.replace()
        self.assertEqual(code, 0, text)

    def test_replace_belongs_to_the_import_command_only(self):
        for argv in (["reset", "wptest", "--replace"], ["approve", "wptest", "--replace"], ["discard", "friend.wpress", "--replace"], ["report", "wptest", "--replace"]):
            code, text = self.run_main(argv)
            self.assertEqual(code, 2, argv); self.assertIn("usage:", text)

    def test_a_second_replace_after_the_first_was_approved_works_again(self):
        self.assertEqual(self.replace()[0], 0)
        self.assertEqual(self.run_main(["approve", "wptest"])[0], 0)
        self.host.tables = 57
        (self.html / "wp-content/uploads/second.jpg").write_bytes(b"2")
        code, text = self.replace()
        self.assertEqual(code, 0, text)
        self.assertFalse((self.html / "wp-content/uploads/second.jpg").exists())
        self.assertEqual(sum(1 for n in self.snaps() if (self.host.backups / "wptest" / n / "MANIFEST").read_text().count("tag=pre-import")), 1)


class TreeRestored(Base):
    def run_and_reset(self, **kw):
        before = self.tree(self.site_dir)
        self.import_ok(**kw)
        self.assertNotEqual(self.tree(self.site_dir), before)
        code, text = self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 0, text)
        return before, text

    def test_what_plugins_make_when_they_first_run_goes_too(self):
        self.host.runtime_files = ["uploads/elementor/css/global.css", "uploads/elementor/google-fonts/fonts/", "upgrade/", "uploads/.htaccess", "uploads/wpo/logs/index.php"]
        before, text = self.run_and_reset()
        self.assertEqual(self.tree(self.site_dir), before)
        self.assertIn("swept", text)

    def test_what_was_there_before_stays(self):
        (self.html / "wp-content/uploads/mine").mkdir()
        (self.html / "wp-content/uploads/mine/photo.jpg").write_bytes(b"keep")
        (self.html / "wp-content/uploads/.htaccess").write_text("")
        (self.html / "wp-content/upgrade").mkdir()
        self.host.runtime_files = ["uploads/mine/new.txt", "uploads/elementor/x.css"]
        before, _ = self.run_and_reset()
        self.assertEqual(self.tree(self.site_dir), before)
        self.assertEqual((self.html / "wp-content/uploads/mine/photo.jpg").read_bytes(), b"keep")
        self.assertTrue((self.html / "wp-content/upgrade").is_dir())

    def test_a_link_made_during_the_import_is_removed_and_never_followed(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        self.host.runtime_files = ["link:uploads/evil->%s" % outside]
        before, _ = self.run_and_reset()
        self.assertEqual(self.tree(self.site_dir), before)
        self.assertEqual((outside / "precious").read_text(), "keep")

    def test_a_tree_too_big_to_snapshot_is_not_swept_and_says_so(self):
        real = imp.TREE_LIMIT
        imp.TREE_LIMIT = 3
        try:
            self.host.runtime_files = ["uploads/elementor/x.css"]
            self.import_ok()
            self.assertIsNone(json.loads((self.intake / "work-wptest/PLAN.json").read_text())["tree_before"])
            code, text = self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        finally:
            imp.TREE_LIMIT = real
        self.assertEqual(code, 0, text)
        self.assertIn("too big to snapshot", text)
        self.assertTrue((self.html / "wp-content/uploads/elementor/x.css").exists(), "without a snapshot nothing is guessed at")
        self.assertFalse((self.html / "wp-content/plugins/good").exists(), "the manifest part still ran")

    def test_a_damaged_snapshot_in_the_record_is_ignored_not_trusted(self):
        self.host.runtime_files = ["uploads/elementor/x.css"]
        self.import_ok()
        rp = self.intake / "work-wptest/PLAN.json"
        rec = json.loads(rp.read_text())
        rec["tree_before"] = [1, 2]            # not strings: the snapshot is not trusted, so nothing is swept
        rp.write_text(json.dumps(rec))
        code, text = self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        self.assertEqual(code, 0, text)
        self.assertNotIn("swept", text)
        self.assertTrue((self.html / "wp-content/uploads/elementor/x.css").exists())

    def test_the_snapshot_is_taken_before_the_load_so_an_interrupted_load_can_be_swept(self):
        self.host.runtime_files = ["uploads/elementor/x.css"]
        before = self.tree(self.site_dir)
        self.archive()
        self.host.popen = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIsInstance(json.loads((self.intake / "work-wptest/PLAN.json").read_text())["tree_before"], list)

    def test_the_helpers_directly(self):
        root = self.tmp / "tree"
        (root / "a/b").mkdir(parents=True)
        (root / "a/f").write_text("x")
        (root / "l").symlink_to(self.tmp)
        snap = imp.snapshot_tree(root)
        self.assertEqual(snap, ["a/", "a/b/", "a/f", "l"], "directories end in a slash; a link to a directory is listed as a link")
        (root / "a/b/new").write_text("n")
        (root / "a/newdir").mkdir()
        (root / "m").symlink_to(self.tmp)
        self.assertEqual(imp.sweep_new(root, snap), 3)
        self.assertEqual(imp.snapshot_tree(root), snap)
        self.assertTrue(self.tmp.is_dir())
        self.assertIsNone(imp.snapshot_tree(root, limit=2))


class FailureDetails(Base):
    def test_the_whole_output_of_a_failed_install_is_in_the_report_and_the_record(self):
        diag = "Error: You need to specify\n--- diagnostics for 'good' (its checksum check failed):\nplugin list:\nname,status,version\nakismet,inactive,5.7.2\n--- end of diagnostics\nwp-site-ext: removed"
        self.host.fail["ext:good"] = (1, diag)
        self.host.fail["ext:good:versioned"] = (1, diag)
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1)
        self.assertIn("DETAILS OF THE FAILED STEPS", text)
        self.assertIn("    akismet,inactive,5.7.2", text)
        self.assertIn("    --- diagnostics for 'good' (its checksum check failed):", text)
        rec = json.loads((self.intake / "work-wptest/PLAN.json").read_text())
        self.assertIn("akismet,inactive,5.7.2", [s for s in rec["steps"] if s["name"] == "plugin good"][0]["detail"])
        self.assertEqual(text.count("DETAILS OF THE FAILED STEPS"), 1)

    def test_a_wpcli_step_that_fails_keeps_its_output_too(self):
        self.host.fail["cli:core update-db"] = (1, "Error: one\nError: two\nError: three")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertIn("    Error: three", text)

    def test_nothing_is_added_when_nothing_failed(self):
        self.assertNotIn("DETAILS OF THE FAILED STEPS", self.import_ok())

    def test_the_details_pass_the_terminal_filter(self):
        self.host.fail["ext:good"] = (1, "boom \x1b[2J \u202e end")
        self.host.fail["ext:good:versioned"] = (1, "boom \x1b[2J \u202e end")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        for bad in ("\x1b", "\u202e"):
            self.assertNotIn(bad, text)
            self.assertNotIn(bad, (self.intake / "work-wptest/REPORT.txt").read_text())


class StoredUrls(Base):
    def test_the_stored_rows_are_read_from_the_database_and_must_be_the_new_name(self):
        text = self.import_ok()
        self.assertRegex(text, r"ok\s+stored siteurl and home\s+both are https://wptest\.example\.test in the database")
        q = [c for c in self.host.calls if c[:3] == ["docker", "exec", "-e"] and "option_name IN" in c[3]][0]
        self.assertIn("FROM wp_options", q[3])

    def test_an_old_address_left_in_the_database_fails_the_import(self):
        self.host.stored_urls = "http://old.local"
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAILED\s+stored siteurl and home\s+FAILED: the database has .*old\.local")

    def test_a_trailing_slash_is_not_a_difference(self):
        self.host.stored_urls = "https://wptest.example.test/"
        self.archive()
        self.assertEqual(self.run_main(["wptest", "friend.wpress"])[0], 0)

    def test_missing_rows_or_an_unreadable_database_fail_it(self):
        for answer in ((0, ""), (0, "siteurl\thttps://wptest.example.test\n"), (1, "")):
            self.setUp()
            self.host.fail["option_names"] = answer
            self.archive()
            code, text = self.run_main(["wptest", "friend.wpress"])
            self.assertEqual(code, 1, answer)
            self.assertIn("stored siteurl and home", text)
            self.tearDown()


class RewriteRules(Base):
    def test_stored_rules_are_deleted_not_flushed(self):
        text = self.import_ok()
        self.assertRegex(text, r"ok\s+rewrite rules\s+deleted: WordPress builds them again on the first request")

    def test_no_stored_rules_is_not_a_failure(self):
        self.host.fail["cli:option delete rewrite_rules"] = (1, "Error: Could not delete 'rewrite_rules' option. Does it exist?")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"ok\s+rewrite rules\s+none were stored")

    def test_any_other_failure_of_it_is_one(self):
        self.host.fail["cli:option delete rewrite_rules"] = (1, "Error: database gone")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAILED\s+rewrite rules\s+FAILED: Error: database gone")


class ReportTable(Base):
    def test_a_multi_line_error_stays_on_one_line_of_the_table(self):
        self.host.fail["ext:good"] = (1, "FAILED:\nError: You need to specify\nwp-site-ext: plugin 'good' did not match")
        self.host.fail["ext:good:versioned"] = (1, "FAILED:\nError: You need to specify\nwp-site-ext: plugin 'good' did not match")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        line = [l for l in text.splitlines() if "plugin good" in l and "FAILED" in l and "STEPS" not in l][0]
        self.assertIn("Error: You need to specify wp-site-ext: plugin 'good' did not match", line)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("Error:")]), 0)


class Scan(Base):
    def test_suspicious_held_code_is_listed(self):
        fl = [f for f in files() if f[1] != "m.php"] + [("mu-plugins", "m.php", b"<?php system($_GET['c']);"), ("plugins/notorg", "evil.php", b"<?php eval(base64_decode($_POST['x']));")]
        text = self.import_ok(files=fl)
        self.assertIn("mu-plugins/m.php: system()/exec()", text)
        self.assertIn("plugins/notorg/evil.php: eval(), base64_decode()", text)
        self.assertIn("suspicious patterns in the held code: 2 file(s)", text)

    def test_a_clean_tree_has_no_hits(self):
        text = self.import_ok()
        self.assertIn("suspicious patterns in the held code: 0 file(s)", text)
        self.assertIn("suspicious patterns in uploads/: 0 file(s)", text)
        self.assertIn("PHP files under uploads/: none", text)
        self.assertNotIn("NOT COPIED because", text)

    def test_clamav_runs_through_its_tool_on_the_held_code_and_the_uploads(self):
        self.host.fail["clam"] = (0, "")
        text = self.import_ok()
        self.assertIn("ClamAV:           clean", text)
        clam = next(c for c in self.host.calls if os.path.basename(c[0]) == "wp-clamscan")
        self.assertEqual(clam[1:], [str(self.intake / "work-wptest/wp-content"), str(self.html / "wp-content/uploads")])

    def test_a_clamav_find_is_shown(self):
        self.host.fail["clam"] = (1, "/x/evil.php: Php.Trojan FOUND")
        text = self.import_ok()
        self.assertIn("FOUND: /x/evil.php: Php.Trojan FOUND", text)

    def test_a_clamav_that_is_not_set_up_is_not_run_and_is_not_clean(self):
        text = self.import_ok()
        self.assertIn("ClamAV:           NOT RUN: ClamAV is not set up on this VM", text)
        self.assertNotIn("ClamAV:           clean", text)

    def test_a_clamav_that_breaks_is_a_failure_with_its_exit_code(self):
        self.host.fail["clam"] = (2, "ERROR: Can't open file")
        text = self.import_ok()
        self.assertIn("ClamAV:           FAILED to run (exit 2): ERROR: Can't open file", text)

    def test_the_vulnerability_check_asks_the_tool_about_this_site(self):
        self.host.fail["vuln"] = (0, "")
        text = self.import_ok()
        self.assertIn("known vulnerabilities: none known", text)
        v = next(c for c in self.host.calls if os.path.basename(c[0]) == "wp-vulnfeed")
        self.assertEqual(v[1:], ["check", "wptest"])

    def test_a_known_vulnerability_is_shown_with_what_the_tool_said(self):
        self.host.fail["vuln"] = (1, "plugin elementor 4.2.3: Elementor <= 4.2.4 - Stored XSS (CVE-2026-1111, CVSS 6.4, fixed in 4.2.5)")
        text = self.import_ok()
        self.assertIn("known vulnerabilities: FOUND: plugin elementor 4.2.3", text)
        self.assertIn("CVE-2026-1111", text)

    def test_several_findings_become_one_line_with_a_count_and_the_start_of_each(self):
        self.host.fail["vuln"] = (1, "\n".join("plugin woocommerce 11.0.1: Finding number %d - %s (CVE-2026-%04d, CVSS 7.5, fixed in 11.2.0)" % (i, "x" * 200, i) for i in range(1, 6)))
        text = self.import_ok()
        line = next(l for l in text.splitlines() if l.startswith("  known vulnerabilities:"))
        self.assertIn("FOUND: 5 finding(s): plugin woocommerce 11.0.1: Finding number 1", line)
        self.assertIn("| plugin woocommerce 11.0.1: Finding number 3", line); self.assertNotIn("Finding number 4", line); self.assertIn("... 2 more", line)

    def test_the_report_says_where_the_time_went(self):
        ticks = iter(range(0, 100000, 1))
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"], clock=lambda: next(ticks))
        self.assertEqual(code, 0, text)
        tm = json.loads((self.intake / "work-wptest/PLAN.json").read_text())["timing"]
        self.assertEqual(set(tm), {"unpack", "load", "after_load", "total", "slowest"})
        self.assertGreater(tm["total"], 0); self.assertEqual(len(tm["slowest"]), 8, "an import runs far more than eight commands: the eight slowest are kept")
        self.assertEqual((tm["unpack"], tm["load"]), (1.0, 1.0), "with a clock that ticks once per reading, a span with no command inside it lasts one tick: each is measured over its own span only")
        self.assertEqual(tm["slowest"], sorted(tm["slowest"], reverse=True), "slowest first")
        line = next(l for l in text.splitlines() if l.startswith("TIMING (seconds)"))
        self.assertIn("total %s" % tm["total"], line); self.assertIn("unpack %s" % tm["unpack"], line); self.assertIn("database load %s" % tm["load"], line)
        rows = text.split(line, 1)[1].split("\n\n", 1)[0].strip().splitlines()
        self.assertEqual(len(rows), len(tm["slowest"]))
        self.assertTrue(all(re.match(r"^\s*\d+\.\d  \S", r) for r in rows), rows)

    def test_the_timing_does_not_change_what_a_command_returns_or_when_it_fails(self):
        self.host.fail["backup"] = (1, "no space")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"], clock=iter(range(0, 100000)).__next__)
        self.assertEqual(code, 1); self.assertRegex(text, r"FAILED\s+first backup"); self.assertIn("TIMING (seconds)", text)

    def test_a_feed_that_is_not_set_up_says_so(self):
        text = self.import_ok()
        self.assertIn("known vulnerabilities: NOT RUN: no feed key", text)

    def test_a_feed_that_breaks_is_a_failure_with_its_exit_code(self):
        self.host.fail["vuln"] = (2, "the feed answered 429")
        text = self.import_ok()
        self.assertIn("known vulnerabilities: FAILED to run (exit 2): the feed answered 429", text)

    def test_a_scan_tool_that_hangs_or_is_missing_does_not_stop_the_import(self):
        orig = self.host.run

        def run(cmd, **kw):
            n = os.path.basename(cmd[0])
            if n == "wp-clamscan":
                raise FileNotFoundError(cmd[0])
            if n == "wp-vulnfeed":
                raise subprocess.TimeoutExpired(cmd, 1)
            return orig(cmd, **kw)
        self.host.run = run
        text = self.import_ok()
        self.assertIn("ClamAV:           NOT RUN: wp-clamscan is not installed", text)
        self.assertIn("known vulnerabilities: FAILED to run (exit 124): timed out", text)

    def test_checksum_failures_are_shown(self):
        self.host.fail["cli:core verify-checksums"] = (1, "Error: File should not exist: wp-content/evil.php")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertIn("core checksums:   FAILED", text)


class StepFailures(Base):
    def test_a_version_wordpress_org_no_longer_offers_gets_the_newest_and_the_report_says_why(self):
        answers = dict(ANSWERS)
        answers[("plugins", "good")] = {"version": "1.1", "requires_php": "7.4", "versions": {"1.1": "u", "trunk": "u"}}      # the archive has 1.0: not in the list
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"], get=get_from(answers))
        self.assertEqual(code, 0, text)
        self.assertIn("version 1.0 is no longer offered by wordpress.org: the newest was installed", text)
        ext = [c[2:] for c in self.host.calls if os.path.basename(c[0]) == "wp-site-ext" and c[2] == "plugin"]
        self.assertEqual(ext, [["plugin", "install", "good"]], "no versioned attempt for a version that is known to be gone")
        self.assertTrue((self.html / "wp-content/plugins/good").exists())

    def test_a_versioned_install_that_fails_for_any_other_reason_is_a_failed_step_not_a_substitution(self):
        self.host.fail["ext:good:versioned"] = (1, "Error: You need to specify either one or more plugin slugs to check\n--- diagnostics for 'good' (its checksum check failed):")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1, text)
        self.assertRegex(text, r"FAILED\s+plugin good\s+FAILED: Error: You need to specify")
        self.assertNotIn("no longer offered", text)
        self.assertNotIn("the newest was installed", text)
        ext = [c[2:] for c in self.host.calls if os.path.basename(c[0]) == "wp-site-ext" and c[2] == "plugin"]
        self.assertEqual(ext, [["plugin", "install", "good", "--version=1.0"]], "one attempt, at the archive's version; nothing else is installed in its place")
        self.assertFalse((self.html / "wp-content/plugins/good").exists())
        self.assertIn("--- diagnostics for 'good'", text, "the command's own output is in the details")

    def test_a_version_string_that_is_not_a_version_is_not_passed_to_the_installer_and_is_not_called_gone(self):
        fl = [f for f in files() if f[1] != "good.php"] + [("plugins/good", "good.php", plugin_php("1.0 beta; x"))]
        self.archive(files=fl)
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 0, text)
        self.assertIn("the newest was installed (the archive's version could not be read)", text)
        self.assertNotIn("no longer offered", text)
        ext = [c[2:] for c in self.host.calls if os.path.basename(c[0]) == "wp-site-ext" and c[2] == "plugin"]
        self.assertEqual(ext, [["plugin", "install", "good"]])

    def test_an_archive_whose_version_cannot_be_read_gets_the_newest_and_says_so(self):
        fl = [f for f in files() if f[1] != "good.php"] + [("plugins/good", "good.php", b"<?php // no header")]
        self.archive(files=fl)
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertIn("the newest was installed (the archive's version could not be read)", text)

    def test_an_install_that_fails_is_a_failed_step_and_the_run_goes_on(self):
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
        self.assertIn(["option", "delete", "rewrite_rules"], self.host.cli_calls())
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
        self.assertTrue(rec["db_loaded"], "set before the load: a stopped load has still put tables in")
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


class Registry(Base):
    ROWS = "wptest hold 1001 2048 2001 512 notify\nshop live 1002 4096 2002 512 auto\n"

    def asks(self, rc=0, out=ROWS, err="", boom=None):
        calls = []

        def run(cmd, **kw):
            if cmd[1:] == ["rows"]:
                calls.append(list(cmd))
                if boom:
                    raise boom
                return subprocess.CompletedProcess(cmd, rc, out, err)
            return self.host.run(cmd, **kw)
        return run, calls

    def go(self, argv, **kw):
        self.cfg.pop("sites", None)
        run, calls = self.asks(**kw)
        code = imp.main(self.cfg, argv, run=run, out=self.out.append)
        return code, "\n".join(self.out), calls

    def test_the_states_and_quotas_come_from_the_registry(self):
        del self.cfg["sites"]
        run, calls = self.asks()
        self.assertEqual(imp.registry_sites(self.cfg, run), {"wptest": {"state": "hold", "quota_mb": 2048}, "shop": {"state": "live", "quota_mb": 4096}})
        self.assertEqual(calls, [["/opt/bin/wp-site-provision", "rows"]])

    def test_an_import_into_a_site_that_is_live_in_the_registry_is_refused(self):
        code, text, calls = self.go(["shop", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("shop is live", text)
        self.assertIn("wp-ctl ensure-state site=shop state=hold", text)
        self.assertEqual(len(calls), 1)

    def test_a_registry_that_cannot_be_read_refuses_everything_and_says_why(self):
        for kw in ({"rc": 1, "out": "", "err": "site.json: not valid"}, {"boom": FileNotFoundError(2, "No such file")}, {"out": "wptest hold 1001\n"}, {"out": "wptest hold 1001 x 2001 512 notify\n"}):
            self.out.clear()
            code, text, _ = self.go(["wptest", "friend.wpress"], **kw)
            self.assertEqual(code, 2, kw)
            self.assertIn("cannot read the site registry", text, kw)


class Refusals(Base):
    def test_unknown_site_and_bad_names(self):
        self.archive()
        self.assert_refused(["nosuch", "friend.wpress"], "has no registry record")
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


class Receive(Base):
    def send(self, name, data, **kw):
        return self.run_main(["receive", name], stdin=io.BytesIO(data), **kw)

    def test_a_file_arrives_whole_with_its_hash_and_nothing_else_is_left(self):
        data = os.urandom(3 * imp.CHUNK + 17)
        code, text = self.send("friend.wpress", data)
        self.assertEqual(code, 0, text)
        import hashlib
        self.assertEqual(text, "received %s %d friend.wpress" % (hashlib.sha256(data).hexdigest(), len(data)))
        f = self.intake / "friend.wpress"
        self.assertEqual(f.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.intake.iterdir() if p.name != ".lock"), ["friend.wpress", "ledger.log"])
        rec = (self.intake / "ledger.log").read_text().splitlines()[-1].split("\t")
        self.assertEqual(rec[2:], ["received", hashlib.sha256(data).hexdigest(), str(len(data)), "friend.wpress"])

    def test_a_name_that_is_not_plain_is_refused(self):
        for name in ("../x.wpress", "a/b.wpress", "x.txt", ".hidden.wpress", "a b.wpress", "", "x.wpress\n", "x\x1b[2J.wpress"):
            before = self.tree()
            code, text = self.send(name, b"data")
            self.assertEqual(code, 2, repr(name))
            self.assertEqual(self.tree(), before)

    def test_it_never_replaces_a_file_that_is_there(self):
        self.put(b"mine")
        code, text = self.send("friend.wpress", b"theirs")
        self.assertEqual(code, 2)
        self.assertIn("wp-site-import discard friend.wpress", text)
        self.assertEqual((self.intake / "friend.wpress").read_bytes(), b"mine")

    def test_a_file_larger_than_the_free_space_is_stopped_and_nothing_is_kept(self):
        self.cfg["intake_quota_mb"] = 1
        before = self.tree()
        code, text = self.send("friend.wpress", b"x" * (imp.MIB + 1))
        self.assertEqual(code, 2)
        self.assertIn("larger than", text)
        self.assertEqual(self.tree(), before)
        self.assertEqual(list(self.intake.glob(".recv-*")), [], "no partial file is kept")
        self.assertFalse((self.intake / "friend.wpress").exists())

    def test_the_free_space_counts_what_is_already_there(self):
        self.cfg["intake_quota_mb"] = 1
        (self.intake / "bulk.dat").write_bytes(b"0" * (imp.MIB - 100))
        self.assertEqual(self.send("friend.wpress", b"x" * 99)[0], 0)
        (self.intake / "friend.wpress").unlink()
        self.assertEqual(self.send("friend.wpress", b"x" * 101)[0], 2)

    def test_nothing_received_is_an_error(self):
        before = self.tree()
        code, text = self.send("friend.wpress", b"")
        self.assertEqual(code, 2)
        self.assertIn("nothing was received", text)
        self.assertEqual(self.tree(), before)
        self.assertEqual(list(self.intake.glob(".recv-*")), [])

    def test_a_stream_that_breaks_leaves_no_partial_file(self):
        class Breaks:
            def __init__(self, exc):
                self.n, self.exc = 0, exc

            def read(self, size):
                self.n += 1
                if self.n == 2:
                    raise self.exc
                return b"x" * 1000
        for exc in (KeyboardInterrupt(), OSError("connection reset")):
            before = self.tree()
            code, text = self.run_main(["receive", "friend.wpress"], stdin=Breaks(exc))
            self.assertIn(code, (2, 130))
            self.assertEqual(self.tree(), before, repr(exc))
            self.assertEqual(list(self.intake.glob(".recv-*")), [], "the partial file is removed when the stream breaks: %r" % exc)
            self.assertFalse((self.intake / "friend.wpress").exists())

    def test_a_stale_partial_from_an_earlier_try_is_replaced_not_followed(self):
        (self.intake / ".recv-friend.wpress").write_bytes(b"old")
        self.assertEqual(self.send("friend.wpress", b"new")[0], 0)
        self.assertEqual((self.intake / "friend.wpress").read_bytes(), b"new")
        outside = self.tmp / "victim"
        outside.write_text("keep")
        (self.intake / "x.wpress").unlink() if (self.intake / "x.wpress").exists() else None
        (self.intake / ".recv-x.wpress").symlink_to(outside)
        code, _ = self.send("x.wpress", b"data")
        self.assertEqual(code, 2)
        self.assertEqual(outside.read_text(), "keep")

    def test_a_received_file_can_be_imported(self):
        self.send("friend.wpress", build(files()))
        self.assertEqual(self.run_main(["wptest", "friend.wpress", "--plan"])[0], 0)

    def test_usage(self):
        for argv in (["receive"], ["receive", "a", "b"], ["discard"], ["report"]):
            self.assertEqual(self.run_main(argv, stdin=io.BytesIO(b""))[0], 2, argv)


class Discard(Base):
    def test_if_present_is_silent_when_there_is_nothing_to_remove(self):
        code, text = self.run_main(["discard", "--if-present", "friend.wpress"])
        self.assertEqual((code, text), (0, ""))
        self.assertFalse((self.intake / "ledger.log").exists(), "nothing happened, nothing is recorded")

    def test_if_present_still_removes_a_file_that_is_there_and_still_refuses_an_import_in_progress(self):
        self.put(b"x")
        self.assertEqual(self.run_main(["discard", "--if-present", "friend.wpress"])[0], 0)
        self.assertFalse((self.intake / "friend.wpress").exists())
        self.import_ok()
        self.assert_refused(["discard", "--if-present", "friend.wpress"], "approve or reset that first")

    def test_if_present_does_not_excuse_a_link_or_a_bad_name(self):
        outside = self.tmp / "victim.wpress"
        outside.write_text("keep")
        (self.intake / "link.wpress").symlink_to(outside)
        self.assert_refused(["discard", "--if-present", "link.wpress"], "not a file")
        self.assert_refused(["discard", "--if-present", "../x.wpress"], "plain .wpress")
        self.assertTrue(outside.exists())

    def test_the_flag_belongs_to_discard_alone(self):
        for argv in (["reset", "wptest", "--if-present"], ["wptest", "friend.wpress", "--if-present"], ["approve", "wptest", "--if-present"], ["report", "wptest", "--if-present"]):
            self.assertEqual(self.run_main(argv)[0], 2, argv)

    def test_removes_the_file_and_says_so_in_the_ledger(self):
        self.put(b"x")
        code, text = self.run_main(["discard", "friend.wpress"])
        self.assertEqual(code, 0, text)
        self.assertFalse((self.intake / "friend.wpress").exists())
        self.assertEqual((self.intake / "ledger.log").read_text().splitlines()[-1].split("\t")[2:3], ["discarded"])

    def test_refuses_the_file_of_an_import_in_progress(self):
        self.import_ok()
        self.assert_refused(["discard", "friend.wpress"], "approve or reset that first")
        self.assertTrue((self.intake / "friend.wpress").exists())

    def test_after_a_reset_it_can_be_discarded(self):
        self.import_ok()
        self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(self.run_main(["discard", "friend.wpress"])[0], 0)

    def test_refuses_names_that_are_not_plain_and_things_that_are_not_files(self):
        self.assert_refused(["discard", "../x.wpress"], "plain .wpress")
        self.assert_refused(["discard", "missing.wpress"], "not a file")
        outside = self.tmp / "victim.wpress"
        outside.write_text("keep")
        (self.intake / "link.wpress").symlink_to(outside)
        self.assert_refused(["discard", "link.wpress"], "not a file")
        self.assertTrue(outside.exists())

    def test_a_damaged_record_of_another_site_does_not_block_it(self):
        (self.intake / "work-other").mkdir()
        (self.intake / "work-other/PLAN.json").write_text("{not json")
        self.put(b"x")
        self.assertEqual(self.run_main(["discard", "friend.wpress"])[0], 0)


class ListCommand(Base):
    """`list`: one JSON line for the control app. It reads and takes no lock."""

    def listing(self):
        code, text = self.run_main(["list"])
        self.assertEqual(code, 0, text)
        self.assertEqual(len(text.splitlines()), 1, "one line")
        return json.loads(text)

    def test_an_empty_intake_lists_nothing(self):
        self.assertEqual(self.listing(), {"files": [], "imports": []})

    def test_a_file_is_listed_with_its_size_and_time_and_no_site(self):
        self.put(b"abcdef", "friend.wpress")
        got = self.listing()
        (f,) = got["files"]
        self.assertEqual((f["name"], f["bytes"], f["site"]), ("friend.wpress", 6, None))
        self.assertIsInstance(f["mtime"], int)
        self.assertEqual(got["imports"], [])

    def test_a_file_that_is_not_a_plain_wpress_file_is_not_listed(self):
        self.put(b"x", "ok.wpress")
        self.put(b"x", "notes.txt")
        self.put(b"x", ".recv-half.wpress")
        outside = self.tmp / "victim.wpress"
        outside.write_text("secret")
        (self.intake / "link.wpress").symlink_to(outside)
        (self.intake / "dir.wpress").mkdir()
        self.assertEqual([f["name"] for f in self.listing()["files"]], ["ok.wpress"])

    def test_an_import_that_waits_for_a_decision_is_listed_with_its_file_and_why_it_is_not_clean(self):
        self.import_ok()
        got = self.listing()
        (imp_,) = got["imports"]
        self.assertEqual((imp_["site"], imp_["file"], imp_["stage"]), ("wptest", "friend.wpress", "reported"))
        self.assertIsInstance(imp_["problems"], list)
        rec = json.loads((self.intake / "work-wptest/PLAN.json").read_text())
        self.assertEqual(imp_["problems"], imp.unclean_reasons(rec), "the same reasons the phone notification gives")
        self.assertEqual([f["site"] for f in got["files"]], ["wptest"], "the file knows which site it was imported into")

    def test_a_known_vulnerability_in_the_scan_shows_as_a_problem_of_the_import(self):
        self.import_ok()
        plan = self.intake / "work-wptest/PLAN.json"
        rec = json.loads(plan.read_text())
        rec["scan"]["vuln"] = "FOUND: 7 finding(s): core wordpress 7.1.3: WordPress Core 4.7.0 - 7.1.3"
        plan.write_text(json.dumps(rec))
        (imp_,) = self.listing()["imports"]
        self.assertEqual(imp_["stage"], "reported")
        self.assertTrue(any(p.startswith("known vulnerabilities: FOUND: 7 finding(s)") for p in imp_["problems"]), imp_["problems"])

    def test_an_unfinished_unpack_is_listed_as_unfinished_with_no_problems(self):
        self.put(b"x")
        (self.intake / "work-wptest").mkdir()
        (self.intake / "work-wptest/PLAN.json").write_text(json.dumps({"file": "friend.wpress", "stage": "unpacked"}))
        (imp_,) = self.listing()["imports"]
        self.assertEqual((imp_["stage"], imp_["file"], imp_["problems"]), ("unfinished", "friend.wpress", []))

    def test_a_record_that_cannot_be_read_does_not_stop_the_listing(self):
        (self.intake / "work-other").mkdir()
        (self.intake / "work-other/PLAN.json").write_text("{not json")
        (self.intake / "work-third").mkdir()
        (self.intake / "work-third/PLAN.json").write_text("[1, 2]")
        got = self.listing()
        self.assertEqual(sorted((i["site"], i["stage"], i["file"]) for i in got["imports"]), [("other", "unfinished", None), ("third", "unfinished", None)])

    def test_a_work_directory_that_is_not_a_site_name_or_is_a_link_is_not_listed(self):
        (self.intake / "work-Bad_Name").mkdir()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.intake / "work-linked").symlink_to(elsewhere)
        self.assertEqual(self.listing()["imports"], [])

    def test_it_answers_while_an_import_holds_the_lock(self):
        import fcntl
        fd = os.open(self.intake / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.run_main(["wptest", "friend.wpress"])[0], 2, "another import is refused")
        self.assertEqual(self.listing(), {"files": [], "imports": []})

    def test_it_changes_nothing_and_a_flag_or_a_second_word_is_a_usage_error(self):
        self.put(b"x")
        before = self.tree()
        self.listing()
        self.assertEqual(self.tree(), before)
        for argv in (["list", "--plan"], ["list", "--accept-failures"], ["list", "--if-present"], ["list", "--replace"], ["list", "--reject", "a"], ["list", "wptest"], ["list", "friend.wpress"]):
            self.assertEqual(self.run_main(argv)[0], 2, argv)

    def test_a_name_with_odd_characters_cannot_break_the_json(self):
        self.put(b"x", "a-b_c.1.wpress")
        got = self.listing()
        self.assertEqual(got["files"][0]["name"], "a-b_c.1.wpress")


class ReportCommand(Base):
    def test_prints_the_report_again(self):
        self.import_ok()
        code, text = self.run_main(["report", "wptest"])
        self.assertEqual(code, 0)
        self.assertIn("IMPORT REPORT for wptest", text)
        self.assertEqual(text.strip(), (self.intake / "work-wptest/REPORT.txt").read_text().strip())

    def test_nothing_to_report_and_bad_names(self):
        self.assert_refused(["report", "wptest"], "no import is waiting")
        self.assert_refused(["report", "../x"], "not a site name")

    def test_a_link_is_not_followed(self):
        outside = self.tmp / "secret.txt"
        outside.write_text("topsecret")
        (self.intake / "work-wptest").mkdir()
        (self.intake / "work-wptest/REPORT.txt").symlink_to(outside)
        code, text = self.run_main(["report", "wptest"])
        self.assertEqual(code, 2)
        self.assertNotIn("topsecret", text)


class Prompt(unittest.TestCase):
    def test_the_question_is_flushed_before_the_tool_waits_for_the_answer(self):
        import io
        events = []

        class Err(io.StringIO):
            def write(self, text):
                events.append(("write", text))
                return super().write(text)

            def flush(self):
                events.append(("flush", None))

        tty = Path(tempfile.mkdtemp()) / "tty"
        tty.write_text("wptest\n")
        real_err, old = sys.stderr, os.environ.get("WP_SITE_TTY")
        sys.stderr, os.environ["WP_SITE_TTY"] = Err(), str(tty)
        try:
            answer = imp.tty_ask("Type the site name: ")
        finally:
            sys.stderr = real_err
            os.environ.pop("WP_SITE_TTY") if old is None else os.environ.__setitem__("WP_SITE_TTY", old)
        self.assertEqual(answer, "wptest\n")
        self.assertEqual(events, [("write", "Type the site name: "), ("flush", None)], "a prompt without a newline stays in a line-buffered stderr until it is flushed")


class Names(Base):
    def test_a_trailing_newline_does_not_pass_any_name_check(self):
        # in Python's re, $ also matches before a final newline; every check here must refuse it
        for rx, good in ((imp.SITE_RE, "wptest"), (imp.FILE_RE, "x.wpress"), (imp.SLUG_RE, "good"), (imp.NAME_RE, "good.php"), (imp.VERSION_RE, "1.0"), (imp.HOST_RE, "a.local"), (w.NAME_PART, "good")):
            self.assertTrue(rx.match(good), good)
            self.assertIsNone(rx.match(good + "\n"), (rx.pattern, good))
        for site in ("wptest\n", "wptest\n\n"):
            self.assert_refused([site, "friend.wpress"], "not a site name")
        self.assert_refused(["reset", "wptest\n"], "not a site name")
        self.assert_refused(["report", "wptest\n"], "not a site name")

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
        victim = self.html / "wp-content/precious"        # it exists before the import: what is new since then is swept, which is not what this test is about
        victim.mkdir()
        (victim / "keep").write_text("k")
        self.import_ok()
        rp = self.intake / "work-wptest/PLAN.json"
        rec = json.loads(rp.read_text())
        rec["installed"]["plugin"] += ["../precious", "..", "/etc"]
        rp.write_text(json.dumps(rec))
        code, _ = self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(code, 0)
        self.assertTrue((victim / "keep").exists())
        self.assertTrue((self.html / "wp-content").is_dir())


class Hostile(Base):
    """What a crafted archive can do to the run. The database is the archive's, so it names what WordPress will include; none of that may run before the operator approves."""
    def test_no_command_loads_plugin_or_theme_code_unless_it_must(self):
        self.host.elementor_active = True
        m = meta(**dict(META, Plugins=["elementor/elementor.php"]))
        self.import_ok(files=[f for f in files(m) if not f[0].startswith("plugins/good")] + [("plugins/elementor", "elementor.php", plugin_php("4.2"))])
        self.assertTrue(self.host.cli_log)
        for args, flags in self.host.cli_log:
            self.assertIn("--skip-themes", flags, args)
            loads_plugins = args[:1] == ["elementor"]
            self.assertEqual("--skip-plugins" in flags, not loads_plugins, args)

    def test_the_options_that_name_code_are_reset_before_anything_else_runs(self):
        self.import_ok()
        first = self.host.cli_log[:3]
        self.assertEqual([a[:3] for a, _ in first], [["option", "update", "active_plugins"], ["option", "update", "template"], ["option", "update", "stylesheet"]])
        self.assertEqual(json.loads(first[0][0][3]), ["good/good.php", "notorg/notorg.php"], "the dropped plugin is out; the list is package.json's, not the database's")
        self.assertEqual(first[0][0][4], "--format=json")
        self.assertEqual((first[1][0][3], first[2][0][3]), ("blocksy", "blocksy"))
        calls = [c for c in self.host.calls if os.path.basename(c[0]) in ("wp-site-cli", "wp-site-ext")]
        self.assertEqual(calls[0][2:5], ["option", "update", "active_plugins"], "nothing, not even is-installed, may run first")

    def test_a_child_theme_keeps_its_parent(self):
        m = meta(**dict(META, Template="parent", Stylesheet="child"))
        self.import_ok(files=files(m) + [("themes/parent", "style.css", theme_css("1")), ("themes/child", "style.css", theme_css("1"))])
        opts = {a[2]: a[3] for a, _ in self.host.cli_log if a[:2] == ["option", "update"]}
        self.assertEqual((opts["template"], opts["stylesheet"]), ("parent", "child"))

    def test_when_the_options_cannot_be_reset_nothing_else_runs(self):
        self.host.fail["cli:option update"] = (1, "Error: database gone")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("before running anything else", text)
        self.assertFalse([c for c in self.host.calls if os.path.basename(c[0]) in ("wp-site-ext", "wp-site-backup")])
        self.assertEqual(len(self.host.cli_log), 1)
        self.assertEqual(list((self.html / "wp-content/uploads").iterdir()), [], "no data is copied before the options are safe")

    def test_php_and_hidden_files_under_uploads_are_not_copied(self):
        bad = [("uploads/a", "shell.php", b"<?php"), ("uploads/a", "SHELL.PHP", b"<?php"), ("uploads/a", "x.php5", b"<?php"), ("uploads/a", "x.phtml", b"<?php"), ("uploads/a", "x.phar", b"p"),
               ("uploads/a", "x.php.jpg", b"<?php"), ("uploads/a", "x.inc", b"<?php"), ("uploads", ".htaccess", b"x"), ("uploads", ".user.ini", b"auto_prepend_file=x"),
               ("uploads/.hidden", "y.jpg", b"j"), ("uploads/a", ".x.jpg", b"j")]
        text = self.import_ok(files=files(extra=bad + [("uploads/a", "ok.jpg", b"fine"), ("uploads/a", "phpinfo.txt", b"t"), ("uploads/a", "my.phpfile.jpg", b"j")]))
        got = sorted(str(p.relative_to(self.html / "wp-content/uploads")) for p in (self.html / "wp-content/uploads").rglob("*") if p.is_file())
        self.assertEqual(got, ["2026/10/a.jpg", "a/my.phpfile.jpg", "a/ok.jpg", "a/phpinfo.txt"])
        self.assertIn("NOT COPIED because PHP or the web server would act on it", text)
        self.assertIn("%d file(s)  %s" % (len(bad), imp.UPLOADS_BLOCKED), text)

    def test_only_translations_are_copied_from_languages(self):
        langs = [("languages", n, b"x") for n in ("de_DE.mo", "de_DE.po", "de_DE.json", "de_DE.l10n.php", "evil.php", ".hidden.mo", "readme.txt")]
        text = self.import_ok(files=files(extra=langs))
        got = sorted(p.name for p in (self.html / "wp-content/languages").iterdir())
        self.assertEqual(got, ["de_DE.json", "de_DE.mo", "de_DE.po", "x.mo"])
        self.assertIn(imp.LANG_BLOCKED, text)

    def test_the_old_address_regex_cannot_hang(self):
        t = time.time()
        self.assertEqual(imp.old_hosts({"SiteURL": "http://a" + "/" * 400000 + "b", "HomeURL": "http://" + "a" * 300, "InternalSiteURL": "//ok.local//", "InternalHomeURL": 5}), ["ok.local"])
        self.assertLess(time.time() - t, 1.0)

    def test_the_length_limit_applies_to_the_address_itself(self):
        self.assertEqual(imp.old_hosts({"SiteURL": "http://" + "a" * imp.HOST_MAX}), ["a" * imp.HOST_MAX])
        self.assertEqual(imp.old_hosts({"SiteURL": "http://" + "a" * (imp.HOST_MAX + 1)}), [])
        self.assertEqual(imp.old_hosts({"SiteURL": "https://" + "a" * (imp.HOST_MAX + 1)}), [])

    def test_the_scan_reads_only_the_first_bytes_of_a_file(self):
        real = imp.SCAN_BYTES
        imp.SCAN_BYTES = 100
        try:
            fl = [f for f in files() if f[1] != "m.php"] + [("mu-plugins", "m.php", b"<?php // " + b"x" * 200 + b" eval($a);"), ("mu-plugins", "n.php", b"<?php eval($a); " + b"x" * 200)]
            text = self.import_ok(files=fl)
        finally:
            imp.SCAN_BYTES = real
        self.assertIn("mu-plugins/n.php: eval()", text)
        self.assertNotIn("mu-plugins/m.php", text)

    def test_an_error_text_that_reaches_the_terminal_through_a_refusal_is_filtered_too(self):
        self.host.fail["cli:option update"] = (1, "Error: \x1b[2J\x1b]0;pwned\x07 \u202e")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("before running anything else", text)
        for bad in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(bad, text)

    def test_old_hosts_forms(self):
        self.assertEqual(imp.old_hosts({"SiteURL": "https://a.local/blog/", "HomeURL": "http://a.local/blog", "InternalSiteURL": "//b.local:8080"}), ["a.local/blog", "b.local:8080"])
        self.assertEqual(imp.old_hosts({"SiteURL": "ftp://x", "HomeURL": "", "InternalSiteURL": None}), [])

    def test_text_from_a_command_cannot_move_the_operators_terminal(self):
        self.host.fail["cli:core update-db"] = (1, "Error: \x1b[2J\x1b]0;pwned\x07 \x85 \u202e done")
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1)
        for bad in ("\x1b", "\x07", "\x85", "\u202e"):
            self.assertNotIn(bad, text)
            self.assertNotIn(bad, (self.intake / "work-wptest/REPORT.txt").read_text())
        self.assertIn("[2J", text)

    def test_a_tampered_record_cannot_make_approve_delete_a_file_outside_the_intake(self):
        self.import_ok()
        outside = self.tmp / "precious.wpress"
        outside.write_text("keep")
        rp = self.intake / "work-wptest/PLAN.json"
        rec = json.loads(rp.read_text())
        rec["file"] = "../../precious.wpress"
        rp.write_text(json.dumps(rec))
        self.run_main(["approve", "wptest"])
        self.assertTrue(outside.exists())

    def test_names_that_are_not_plain_are_refused_by_the_reader_before_the_tool_goes_on(self):
        for entry in ("../../etc/x.php", "a/b/c.php", ".."):
            m = meta(**dict(META, Plugins=[entry]))
            self.archive(files=files(m))
            before = self.tree()
            self.assertEqual(self.run_main(["wptest", "friend.wpress", "--plan"])[0], 2, entry)
            self.assertEqual(self.tree(), before)


class Interrupted(Base):
    def test_ctrl_c_during_the_load_leaves_a_run_that_reset_can_clean(self):
        self.host.pipe_breaks = False

        def stop(*a, **k):
            raise KeyboardInterrupt()
        real = self.host.popen
        self.host.popen = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("interrupted", text)
        self.assertIn("wp-site-import reset wptest", text)
        rec = json.loads((self.intake / "work-wptest/PLAN.json").read_text())
        self.assertTrue(rec["db_loaded"])
        self.host.tables = 5
        self.assertEqual(self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")[0], 0)
        self.assertEqual(self.host.tables, 0)

    def test_ctrl_c_in_the_middle_of_a_copy_leaves_a_manifest_that_removes_every_file(self):
        before = self.tree(self.site_dir)

        def stop(n):
            if n == 3:
                raise KeyboardInterrupt()
        self.host.on_chown = stop
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertNotEqual(self.tree(self.site_dir), before, "the copy had started")
        self.host.on_chown = None
        self.assertEqual(self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")[0], 0)
        self.assertEqual(self.tree(self.site_dir), before)

    def test_ctrl_c_in_the_middle_of_an_approve_leaves_a_manifest_that_removes_every_file(self):
        self.import_ok()
        before = self.tree(self.site_dir)
        count = len(self.host.chowned)

        def stop(n):
            if n == count + 3:
                raise KeyboardInterrupt()
        self.host.on_chown = stop
        code, text = self.run_main(["approve", "wptest"])
        self.assertEqual(code, 130)
        self.assertIn("interrupted", text)
        self.assertNotEqual(self.tree(self.site_dir), before, "the copy had started")
        self.assertTrue((self.intake / "work-wptest").exists(), "nothing is deleted when the approval did not finish")
        self.assertTrue((self.intake / "friend.wpress").exists())
        self.host.on_chown = None
        self.assertEqual(self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")[0], 0)
        self.assertNotIn("wp-content/plugins/notorg", " ".join(self.tree(self.site_dir)))
        self.assertNotIn("mu-plugins", " ".join(self.tree(self.site_dir)))

    def test_ctrl_c_with_nothing_written_says_so_and_exits_130(self):
        self.archive()
        self.host.run = lambda cmd, **kw: (_ for _ in ()).throw(KeyboardInterrupt())
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 130)

    def test_the_manifest_is_saved_every_200_files_not_only_at_the_end(self):
        many = [("uploads/many", "f%03d.jpg" % i, b"j") for i in range(450)]
        saved = []
        real = imp.save_record
        imp.save_record = lambda work, record: (saved.append(len(record["copied"])), real(work, record))[1]
        try:
            self.import_ok(files=files(extra=many))
        finally:
            imp.save_record = real
        self.assertTrue(any(n in (200, 400) for n in saved), saved)


def _interrupt_at(n, approve):
    """Ctrl-C at the n-th chown (every directory and file the copy makes passes one), then reset: the site must be exactly as it was before the copy started."""
    def test(self):
        before = self.tree(self.site_dir)       # reset undoes the whole run, so this is the site before the import, also after an interrupted approve
        if approve:
            self.import_ok()
        start = len(self.host.chowned)

        def stop(k):
            if k == start + n:
                raise KeyboardInterrupt()
        self.host.on_chown = stop
        if approve:
            code, _ = self.run_main(["approve", "wptest"])
        else:
            self.archive()
            code, _ = self.run_main(["wptest", "friend.wpress"])
        self.host.on_chown = None
        if code in (0, 1):
            return      # fewer than n files: nothing was interrupted
        self.assertIn(code, (2, 130))
        self.assertEqual(self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")[0], 0)
        self.assertEqual(self.tree(self.site_dir), before, "interrupted at chown %d" % n)
    return test


for _n in range(1, 13):
    setattr(Interrupted, "test_ctrl_c_during_the_import_copy_at_chown_%02d_leaves_nothing_behind" % _n, _interrupt_at(_n, False))
    setattr(Interrupted, "test_ctrl_c_during_the_approve_copy_at_chown_%02d_leaves_nothing_behind" % _n, _interrupt_at(_n, True))


class Timeouts(Base):
    def test_a_step_that_hangs_is_a_failed_step_not_a_hang(self):
        self.host.timeouts = {"wp-site-ext"}
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAILED\s+plugin good\s+FAILED: timed out after 3600 s")

    def test_a_database_that_does_not_answer_is_a_refusal(self):
        self.host.timeouts = {"docker"}
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("did not answer within 3600 s", text)

    def test_a_load_that_never_finishes_is_killed(self):
        self.host.wait_times_out = True
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 2)
        self.assertIn("did not finish within 3600 s", text)
        self.assertTrue(self.host.killed)

    def test_every_command_is_given_the_timeout(self):
        seen = []
        real = self.host.run
        self.host.run = lambda cmd, **kw: (seen.append(kw.get("timeout")), real(cmd, **kw))[1]
        self.import_ok()
        self.assertTrue(seen)
        self.assertEqual(set(seen), {3600})


class Quota(Base):
    def test_the_database_grows_on_disk_so_the_site_needs_room_for_more_than_the_dump(self):
        big = [(".", "database.sql", SQL + b"-- " + b"x" * (1024 * 1024) + b"\n")]
        fl = [f for f in files() if f[1] != "database.sql"] + big
        self.archive(files=fl)
        self.cfg["sites"]["wptest"]["quota_mb"] = 2
        self.assert_refused(["wptest", "friend.wpress"], "left of its quota")
        self.cfg["sites"]["wptest"]["quota_mb"] = 4
        self.assertEqual(self.run_main(["wptest", "friend.wpress", "--plan"])[0], 0)


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

    def test_reset_after_a_failed_load_drops_whatever_the_load_left(self):
        self.host.sql_rc, self.host.sql_err = 1, b"ERROR"
        self.archive()
        self.run_main(["wptest", "friend.wpress"])
        self.host.tables = 12
        code, text = self.run_main(["reset", "wptest"], ask=lambda prompt: "wptest\n")
        self.assertEqual(code, 0, text)
        self.assertIn("`wp_options`", self.host.dropped)
        self.assertEqual(self.host.tables, 0)

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
        v = {"hostvars": inventory_hostvars()}          # wp_ctl_panel_from refers to the control app's VM
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
        self.assertNotIn("sites", cfg, "the sites are asked of the registry at every run, nothing is baked in")
        self.assertEqual((cfg["intake_dir"], cfg["backup_dir"], cfg["intake_quota_mb"], cfg["image_pin"], cfg["data_mount"]), (v["wp_intake_dir"], v["wp_backup_dir"], v["wp_intake_quota_mb"], v["wp_image_php"], v["wp_data_mount"]))
        self.assertEqual((cfg["backup_dir"], cfg["domain"], cfg["tools_bin"], cfg["php_uid"], cfg["step_timeout_s"]), (v["wp_backup_dir"], v["wp_domain"], v["wp_tools_bin"], v["wp_php_uid"], v["wp_import_step_timeout_s"]))
        self.assertEqual(cfg["approvals"], {"key_file": v["wp_approvals_dir"] + "/key", "approver_file": v["wp_approvals_dir"] + "/approver", "ntfy_conf": v["wp_ntfy_conf"], "netrc": v["wp_ntfy_netrc"],
                                            "reply_topic": v["wp_approvals_reply_topic"], "ttl_s": v["wp_approvals_ttl_s"]})
        self.assertIsInstance(cfg["php_uid"], int)
        self.assertIn('sys.path.insert(0, "%s")' % v["wp_tools_lib"], text)

    def test_the_rendered_config_runs_the_real_tool(self):
        text, v = self.render(wp_sites=[{"name": "wptest", "port": 9101, "quota_id": 1001, "state": "hold"}])
        self.assertEqual(w.pin(self.config(text)["image_pin"]), w.ours(ROOT))


    def test_the_real_wrapper_runs_end_to_end_as_a_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib, intake = d / "lib", d / "data/intake"
            lib.mkdir(); intake.mkdir(parents=True); (d / "data/sites/wptest").mkdir(parents=True)
            (d / "bin").mkdir()
            stand_in = d / "bin/wp-site-provision"
            stand_in.write_text("#!/bin/sh\necho 'wptest hold 1001 2048 2001 512 notify'\n")
            stand_in.chmod(0o755)
            text, _ = self.render(wp_tools_bin=str(d / "bin"), wp_tools_lib=str(lib), wp_data_mount=str(d / "data"), wp_intake_dir=str(intake))
            deployed = [i for t in yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text()) if t.get("name") == "Stage the Python modules of wp-site-import" for i in t["loop"]]
            self.assertEqual(sorted(deployed), ["wp_approvals.py", "wp_ctl.py", "wp_site_import.py", "wp_site_model.py", "wp_site_provision.py", "wp_site_renderer.py", "wp_vulnfeed.py", "wp_wpress.py"],
                         "the play must copy every module the tool imports (wp_vulnfeed.py is wp-vulnfeed's, wp_ctl.py is wp-ctl's, and wp_site_model.py, wp_site_renderer.py and wp_site_provision.py are wp-site-provision's, not its)")
            for m in deployed:
                (lib / m).write_text((ROOT / "scripts" / m).read_text())
            tool = d / "wp-site-import"
            tool.write_text(text.replace("#!/usr/bin/python3 -I", "#!%s -I" % sys.executable))
            tool.chmod(0o755)
            r = subprocess.run([str(tool)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("usage: wp-site-import", r.stdout)
            r = subprocess.run([str(tool), "nosuch", "x.wpress"], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)
            self.assertIn("has no registry record", r.stdout)
            stand_in.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
            r = subprocess.run([str(tool), "wptest", "x.wpress"], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)
            self.assertIn("cannot read the site registry", r.stdout)


class Classify(unittest.TestCase):
    def test_every_branch(self):
        pl, th = {"a": "reinstall", "b": "approve", "c": "dropped"}, {"t": "reinstall", "u": "approve"}
        cases = [(("package.json", "."), "keep"), (("database.sql", "."), "keep"), (("index.php", "."), "skip"), (("x", "upgrade/y"), "skip"), (("x", "cache"), "skip"),
                 (("f.php", "plugins/a"), "skip"), (("f.php", "plugins/b/sub"), "keep"), (("f.php", "plugins/c"), "skip"), (("f.php", "plugins/zz"), "skip"), (("one.php", "plugins"), "skip"),
                 (("s.css", "themes/t"), "skip"), (("s.css", "themes/u"), "keep"), (("s.css", "themes/other"), "skip"), (("f", "uploads/2026"), "keep"), (("f", "mu-plugins"), "keep"),
                 (("f.mo", "languages"), "keep"), (("f.php", "languages"), "skip"), (("f.l10n.php", "languages"), "skip"), (("f.php", "uploads"), "skip"), (("f", "elsewhere"), "skip")]
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
