#!/usr/bin/env python3
"""Offline tests of the backups: templates/wp/wp_site_backup.sh.j2 (the nightly job), wp_backup_check.sh.j2 (the freshness check) and wp_site_restore.sh.j2 (the drill).

The backup test uses the real rsync and the real filesystem, so hard links between snapshots are really checked (inodes), and the retention rule is compared with a second,
independent implementation. docker, ntfy-notify and the quota tools are stand-ins that record what they are given. They prove the logic, the failure paths and the cleanup, and no
more: not that mariadb-dump prints what the checks look for, not that the throwaway database starts, not that WP-CLI boots a restored site. scripts/wp_backup_rehearsal_dellwp.sh
does those against the real thing. Run: python3 -I tests/test_wp_backup.py"""
import datetime
import gzip
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import jinja2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wp_sites_render import ROOT, base_vars  # noqa: E402

DUMP_OK = "-- MariaDB dump\nCREATE TABLE `wp_options` (id int);\nINSERT INTO `wp_options` VALUES (1);\n-- Dump completed on 2026-10-24 20:15:00\n"
DOCKER_BACKUP = r'''#!/bin/bash
echo "$*" >> "$SHIM/docker.calls"
case "$1" in
  exec) case "$*" in *information_schema*) [ -f "$SHIM/tables_fail" ] && exit 1; cat "$SHIM/tables" 2>/dev/null || echo 57; exit 0 ;; esac
        echo "dump flag=$([ -e "$SITEDIR/maintenance.flag" ] && echo up || echo down)" >> "$SHIM/order"; [ -f "$SHIM/dump_fail" ] && exit 1; cat "$SHIM/dump.sql"; exit 0 ;;
esac
exit 0
'''
DATE = r'''#!/bin/bash
if [ "${1:-}" = -u ] && [ "${2:-}" = +%Y%m%dT%H%M%SZ ] && [ -n "${FAKE_STAMP:-}" ]; then echo "$FAKE_STAMP"; else exec /usr/bin/date "$@"; fi
'''
NTFY = '#!/bin/bash\necho "$*" >> "$SHIM/ntfy.log"\n'
DOCKER_COUNT = r'''#!/bin/bash
[ -f "$SHIM/tables_fail" ] && exit 1
cat "$SHIM/tables" 2>/dev/null || echo 0
'''
DOCKER_RESTORE = r'''#!/bin/bash
echo "$*" >> "$SHIM/docker.calls"
args=" $* "
# SHIM_NOISE: a plugin that prints PHP notices to stdout before WP-CLI's answer (Elementor does, on PHP 8.5)
say() { if [ -n "${SHIM_WPFAIL:-}" ] && [[ " $args " == *" $SHIM_WPFAIL "* ]]; then echo "partial output"; echo "Error: Database connection error" >&2; exit 3; fi
  if [ -n "${SHIM_NOANSWER:-}" ] && [[ " $args " == *" $SHIM_NOANSWER "* ]]; then printf 'Deprecated: only a notice\n'; return 0; fi
  [ -z "${SHIM_NOISE:-}" ] || printf 'PHP Deprecated:  Creation of dynamic property in /var/www/html/x.php on line 3\nDeprecated: 12 items skipped\n99\n'; echo "$1"; }
case "$1" in
  network) exit 0 ;;
  rm) exit 0 ;;
  exec)
    case "$args" in
      *healthcheck.sh*) [ -f "$SHIM/db_never_healthy" ] && exit 1; exit 0 ;;
      *" -i "*) cat >> "$SHIM/loaded.sql"; exit "${SHIM_LOAD_RC:-0}" ;;
    esac ;;
  run)
    case "$args" in
      *" --entrypoint sh "*) exit 0 ;;
      *" -d "*) echo containerid; exit 0 ;;
      *" --entrypoint wp "*)
        case "$args" in
          *" is-installed "*) [ -f "$SHIM/wp_not_installed" ] && exit 1; exit 0 ;;
          *" siteurl "*) say "https://wptest.example.com"; exit 0 ;;
          *" user "*) say "${SHIM_USERS:-2}"; exit 0 ;;
          *" post "*) say 7; exit 0 ;;
          *" plugin "*) say 1; exit 0 ;;
        esac ;;
    esac ;;
esac
exit 0
'''


def gz(text, path):
    with gzip.open(path, "wt") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wpbackup-"))
        self.v = base_vars()
        data = self.tmp / "data"
        self.site = data / "sites" / "wptest"
        (self.site / "html/wp-content/uploads").mkdir(parents=True)
        (self.site / "html/wp-content/plugins/p").mkdir(parents=True)
        (self.site / "html/wp-includes").mkdir(parents=True)
        (self.site / "html/wp-includes/version.php").write_text("<?php\n$wp_version = '7.1.3';\n")
        (self.site / "wp-config.php").write_text("<?php\n")
        (self.site / "site.json").write_text("{}")           # a site exists when it has a registry record (templates/wp/wp_tools_common.sh.j2 require_site)
        (self.site / ".env").write_text("MARIADB_USER=wp\n")
        self.backups = data / "backups"
        (self.backups / "wptest").mkdir(parents=True)
        (data / "scratch").mkdir()
        for d in ("bin", "lib", "shim", "state"):
            (self.tmp / d).mkdir()
        self.ctx = dict(self.v, wp_tools_bin=str(self.tmp / "bin"), wp_tools_lib=str(self.tmp / "lib"), wp_data_mount=str(data), wp_backup_dir=str(self.backups),
                        wp_freeze_file=str(self.tmp / "freeze"), wp_disk_state_dir=str(self.tmp / "state"), inventory_hostname="dellwp")
        self.env_j = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        self.install("wp_tools_common.sh.j2", "lib/common.sh")
        # wp-site-provision rows, the one reader of the registry: it prints what the test puts in $SHIM/rows, or fails when $SHIM/rows_fail exists
        prov = self.tmp / "bin" / "wp-site-provision"
        prov.write_text('#!/bin/bash\n[ "$1" = rows ] || exit 64\n[ -f "$SHIM/rows_fail" ] && { echo "wp-site-provision: the registry is unreadable" >&2; exit 1; }\ncat "$SHIM/rows" 2>/dev/null\nexit 0\n')
        prov.chmod(0o755)
        d = self.v["wp_site_defaults"]
        self.rows("wptest hold 1001 %d 2001 %d %s" % (d["quota_mb"], d["backup_quota_mb"], d["update_policy"]))
        self.env = dict(os.environ, PATH="%s:%s" % (self.tmp / "shim", os.environ["PATH"]), SHIM=str(self.tmp / "shim"))
        self.put("dump.sql", DUMP_OK)
        for name, body in (("ntfy-notify", NTFY), ("date", DATE), ("chattr", "#!/bin/bash\nexit 0\n"), ("chown", "#!/bin/bash\nexit 0\n"),
                           ("setquota", '#!/bin/bash\necho "setquota $*" >> "$SHIM/quota.calls"\n')):
            self.shim(name, body)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def shim(self, name, body):
        f = self.tmp / "shim" / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def rows(self, *lines):
        """The lines wp-site-provision rows prints: name state quota_id quota_mb backup_quota_id backup_quota_mb update_policy."""
        self.put("rows", "".join(l + "\n" for l in lines))

    def put(self, name, text):
        (self.tmp / "shim" / name).write_text(text)

    def install(self, template, dest, **over):
        out = self.tmp / dest
        out.write_text(self.env_j.get_template(template).render(**dict(self.ctx, **over)))
        out.chmod(0o755)
        return out

    def run_script(self, script, *args, **env):
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=dict(self.env, **env), timeout=120)

    def log(self, name):
        p = self.tmp / "shim" / name
        return p.read_text() if p.exists() else ""

    def snapshots(self, site="wptest"):
        return sorted(p.name for p in (self.backups / site).iterdir() if re.fullmatch(r"\d{8}T\d{6}Z", p.name))


class Backup(Base):
    def setUp(self):
        super().setUp()
        self.shim("docker", DOCKER_BACKUP)
        self.script = self.install("wp_site_backup.sh.j2", "bin/wp-site-backup")
        self.env["SITEDIR"] = str(self.site)
        self.flag = self.site / "maintenance.flag"
        (self.site / "html/wp-content/uploads/a.jpg").write_text("a" * 5000)
        (self.site / "html/wp-content/plugins/p/p.php").write_text("<?php // plugin\n")

    def backup(self, stamp, *args, **env):
        return self.run_script(self.script, "wptest", *args, FAKE_STAMP=stamp, **env)

    def tagged(self, *stamps):
        for st in stamps:
            d = self.backups / "wptest" / st
            (d / "files").mkdir(parents=True); (d / "db.sql.gz").write_text("x"); (d / "MANIFEST").write_text("site=wptest\nstamp=%s\ntag=pre-update\n" % st)

    def test_a_first_backup_is_a_complete_checked_private_snapshot(self):
        (self.site / "html/wp-content/cache").mkdir(); (self.site / "html/wp-content/cache/c.html").write_text("x")
        r = self.backup("20261024T201500Z")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        snap = self.backups / "wptest" / "20261024T201500Z"
        self.assertEqual(self.snapshots(), ["20261024T201500Z"])
        with gzip.open(snap / "db.sql.gz", "rt") as f:
            self.assertIn("CREATE TABLE `wp_options`", f.read())
        self.assertTrue((snap / "files/uploads/a.jpg").is_file())
        self.assertTrue((snap / "files/plugins/p/p.php").is_file())
        self.assertFalse((snap / "files/cache").exists(), "caches are not worth keeping")
        man = dict(l.split("=", 1) for l in (snap / "MANIFEST").read_text().splitlines())
        self.assertEqual((man["site"], man["stamp"], man["core"]), ("wptest", "20261024T201500Z", "7.1.3"))
        self.assertEqual((self.backups / "wptest/.last-success").read_text().strip(), "20261024T201500Z")
        self.assertEqual(stat.S_IMODE(snap.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((snap / "db.sql.gz").stat().st_mode), 0o600)
        self.assertEqual([p.name for p in (self.backups / "wptest").iterdir() if p.name.startswith(".partial")], [])

    def test_the_dump_is_taken_as_the_sites_own_user_inside_its_container_with_the_password_not_on_the_host(self):
        self.backup("20261024T201500Z")
        call = self.log("docker.calls")
        self.assertIn("exec wp_wptest_db sh -c", call)
        self.assertIn("--single-transaction", call)
        self.assertIn('MYSQL_PWD="$MARIADB_PASSWORD"', call, "the password comes from the container's own environment")

    def test_later_snapshots_hard_link_what_did_not_change(self):
        self.assertEqual(self.backup("20261023T201500Z").returncode, 0)
        (self.site / "html/wp-content/uploads/b.jpg").write_text("b" * 100)
        (self.site / "html/wp-content/plugins/p/p.php").write_text("<?php // changed\n")
        self.assertEqual(self.backup("20261024T201500Z").returncode, 0)
        one, two = (self.backups / "wptest" / s / "files" for s in ("20261023T201500Z", "20261024T201500Z"))
        same = lambda rel: (one / rel).stat().st_ino == (two / rel).stat().st_ino
        self.assertTrue(same("uploads/a.jpg"), "an unchanged file must be a hard link, not a copy")
        self.assertEqual((two / "uploads/a.jpg").stat().st_nlink, 3 if (self.site / "html/wp-content/uploads/a.jpg").stat().st_dev != two.stat().st_dev else 2)
        self.assertFalse(same("plugins/p/p.php"), "a changed file must be new")
        self.assertEqual((two / "plugins/p/p.php").read_text(), "<?php // changed\n")
        self.assertEqual((one / "plugins/p/p.php").read_text(), "<?php // plugin\n", "the old snapshot keeps the old content")
        self.assertTrue((two / "uploads/b.jpg").is_file())
        self.assertFalse((one / "uploads/b.jpg").exists())

    def test_a_failed_dump_leaves_nothing_behind_and_the_last_success_alone(self):
        self.assertEqual(self.backup("20261023T201500Z").returncode, 0)
        self.put("dump_fail", "")
        r = self.backup("20261024T201500Z")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mariadb-dump failed", r.stderr)
        self.assertEqual(self.snapshots(), ["20261023T201500Z"])
        self.assertEqual([p.name for p in (self.backups / "wptest").iterdir() if p.name.startswith(".partial")], [])
        self.assertEqual((self.backups / "wptest/.last-success").read_text().strip(), "20261023T201500Z")

    def test_a_truncated_dump_and_an_empty_database_are_refused(self):
        self.put("dump.sql", DUMP_OK.replace("-- Dump completed on 2026-10-24 20:15:00\n", ""))
        r = self.backup("20261024T201500Z")
        self.assertNotEqual(r.returncode, 0); self.assertIn("completion marker", r.stderr)
        self.put("dump.sql", "-- MariaDB dump\n-- Dump completed on 2026-10-24 20:15:00\n")
        r = self.backup("20261024T201600Z")
        self.assertNotEqual(r.returncode, 0); self.assertIn("wp_options", r.stderr)
        self.assertEqual(self.snapshots(), [])
        self.assertFalse((self.backups / "wptest/.last-success").exists())

    # --- a site with no tables at all: a FINAL backup always; the nightly job only if it never had a real snapshot
    EMPTY_DUMP = "-- MariaDB dump\n-- Dump completed on 2026-10-24 20:15:00\n"

    def empty_site(self, tables="0"):
        self.put("dump.sql", self.EMPTY_DUMP)
        self.put("tables", tables)

    def test_a_site_that_never_had_data_can_take_an_empty_final_snapshot_and_says_so(self):
        self.empty_site()
        r = self.backup("20261024T201500Z", "--tag=final")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("an empty final snapshot is taken (empty=yes)", r.stdout)
        snap = self.backups / "wptest/20261024T201500Z"
        man = dict(l.split("=", 1) for l in (snap / "MANIFEST").read_text().splitlines())
        self.assertEqual((man["tag"], man["empty"]), ("final", "yes"))
        with gzip.open(snap / "db.sql.gz", "rt") as f:
            self.assertNotIn("CREATE TABLE", f.read())

    def test_a_normal_snapshot_has_no_empty_mark(self):
        self.backup("20261024T201500Z", "--tag=final")
        self.assertNotIn("empty", (self.backups / "wptest/20261024T201500Z/MANIFEST").read_text())

    def test_a_pre_update_backup_of_an_empty_site_is_still_refused(self):
        self.empty_site()
        r = self.backup("20261024T200200Z", "--tag=pre-update")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no wp_options table", r.stderr)
        self.assertEqual(self.snapshots(), [])

    def test_the_nightly_job_on_a_site_waiting_for_an_import_is_a_quiet_success_and_takes_nothing(self):
        self.empty_site()
        for args in ((), ("--force",)):
            r = self.backup("20261024T20%02d00Z" % (len(args) + 1), *args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("waiting for an import): nothing to back up", r.stdout)
        self.assertEqual(self.snapshots(), [])
        self.assertFalse((self.backups / "wptest" / ".last-success").exists(), "a skipped night is not a good backup")
        self.assertEqual([p.name for p in (self.backups / "wptest").iterdir()], [], "no partial directory is left either")

    def test_the_nightly_job_on_a_site_that_had_real_backups_and_lost_its_tables_still_fails(self):
        self.assertEqual(self.backup("20261023T201500Z").returncode, 0)
        self.empty_site()
        r = self.backup("20261024T201500Z")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no wp_options table", r.stderr)
        self.assertEqual(self.snapshots(), ["20261023T201500Z"])

    def test_a_nightly_job_that_cannot_count_the_tables_fails(self):
        self.empty_site()
        self.put("tables_fail", "1")
        r = self.backup("20261024T201500Z")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not count the tables", r.stderr)

    def test_tables_without_wp_options_is_damage_not_emptiness(self):
        self.put("dump.sql", "-- MariaDB dump\nCREATE TABLE `wp_posts` (id int);\n-- Dump completed on 2026-10-24 20:15:00\n")
        self.put("tables", "12")
        r = self.backup("20261024T201500Z", "--tag=final")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no wp_options table", r.stderr)
        self.assertEqual(self.snapshots(), [])

    def test_a_site_that_had_a_real_snapshot_and_lost_its_tables_still_gets_an_empty_final_one(self):
        self.assertEqual(self.backup("20261023T201500Z").returncode, 0)
        self.empty_site()
        r = self.backup("20261024T201500Z", "--tag=final")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("an empty final snapshot is taken (empty=yes)", r.stdout)
        self.assertIn("1 older real snapshot(s) stay on disk", r.stdout)
        self.assertEqual(self.snapshots(), ["20261023T201500Z", "20261024T201500Z"], "the real backup is untouched")

    def test_an_empty_final_snapshot_of_a_site_with_no_real_ones_does_not_mention_older_ones(self):
        self.empty_site()
        r = self.backup("20261024T201500Z", "--tag=final")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("older real", r.stdout)

    def test_a_snapshot_without_a_manifest_counts_as_real_so_the_rule_errs_on_keeping_data(self):
        d = self.backups / "wptest/20261023T201500Z"
        (d / "files").mkdir(parents=True); (d / "db.sql.gz").write_text("x")
        self.empty_site()
        self.assertNotEqual(self.backup("20261024T201500Z").returncode, 0)

    def test_an_earlier_empty_snapshot_does_not_count_as_real(self):
        self.empty_site()
        self.assertEqual(self.backup("20261023T201500Z", "--tag=final").returncode, 0)
        self.assertEqual(self.backup("20261024T201500Z", "--tag=final").returncode, 0)

    def test_when_the_tables_cannot_be_counted_nothing_is_taken(self):
        self.empty_site()
        self.put("tables_fail", "")
        r = self.backup("20261024T201500Z", "--tag=final")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not count the tables", r.stderr)
        self.assertEqual(self.snapshots(), [])

    def test_a_count_that_is_not_zero_is_not_empty(self):
        for answer in ("1", "57", "", "zero", "0 "):
            self.empty_site(answer)
            r = self.backup("20261024T201500Z", "--tag=final")
            self.assertNotEqual(r.returncode, 0, repr(answer))

    def test_a_same_second_collision_is_refused_and_does_not_nest_a_snapshot(self):
        self.assertEqual(self.backup("20261024T201500Z").returncode, 0)
        r = self.backup("20261024T201500Z")
        self.assertNotEqual(r.returncode, 0); self.assertIn("already exists", r.stderr)
        self.assertEqual(self.snapshots(), ["20261024T201500Z"])
        self.assertFalse(any(p.is_dir() and re.fullmatch(r"\d{8}T\d{6}Z", p.name) for p in (self.backups / "wptest/20261024T201500Z").iterdir()), "nothing nested inside")

    # --- retention: the newest 7, plus the newest of each of the 3 ISO weeks before the oldest of those
    @staticmethod
    def expected_keep(stamps, daily, weekly):
        iso = lambda s: datetime.datetime.strptime(s[:8], "%Y%m%d").isocalendar()[:2]
        s = sorted(stamps, reverse=True)
        keep = set(s[:daily]); seen = {iso(s[:daily][-1])}; n = 0
        for x in s[daily:]:
            if iso(x) in seen or n >= weekly:
                continue
            keep.add(x); seen.add(iso(x)); n += 1
        return keep

    def fabricate(self, days_back, end=datetime.date(2026, 10, 23)):
        names = []
        for i in range(days_back):
            d = end - datetime.timedelta(days=i)
            name = d.strftime("%Y%m%dT201500Z")
            (self.backups / "wptest" / name / "files").mkdir(parents=True)
            (self.backups / "wptest" / name / "db.sql.gz").write_text("x")
            names.append(name)
        return names

    def test_retention_keeps_seven_daily_and_three_weekly_exactly_as_an_independent_implementation_does(self):
        old = self.fabricate(45)
        (self.backups / "wptest" / "notes").mkdir(); (self.backups / "wptest" / "20260101").mkdir(); (self.backups / "wptest" / "20260101T000000Z.keep").mkdir()
        (self.backups / "wptest" / "important.txt").write_text("keep me")
        r = self.backup("20261024T201500Z")      # a Saturday
        self.assertEqual(r.returncode, 0, r.stderr)
        want = self.expected_keep(old + ["20261024T201500Z"], 7, 3)
        self.assertEqual(set(self.snapshots()), want)
        # worked by hand: 18..24 Oct are the seven daily. The oldest of them, Sun 18 Oct, is in ISO week 42 (Mon 12 .. Sun 18), so that week is covered and 12..17 Oct go; the
        # weeklies are then the newest of weeks 41, 40 and 39: Sun 11 Oct, Sun 4 Oct, Sun 27 Sep.
        hand = {"202610%02dT201500Z" % d for d in (24, 23, 22, 21, 20, 19, 18, 11, 4)} | {"20260927T201500Z"}
        self.assertEqual(set(self.snapshots()), hand)
        for stray in ("notes", "20260101", "20260101T000000Z.keep", "important.txt"):
            self.assertTrue((self.backups / "wptest" / stray).exists(), stray + " is not a snapshot and must never be deleted")

    def test_a_pre_import_snapshot_is_tagged_and_is_never_pruned(self):
        r = self.backup("20261001T020000Z", "--tag=pre-import")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tag=pre-import", (self.backups / "wptest" / "20261001T020000Z" / "MANIFEST").read_text())
        self.assertIn("OK: snapshot 20261001T020000Z of wptest", r.stdout, "wp-site-import reads the stamp from this line")
        for day in range(2, 30):
            self.backup("202610%02dT031500Z" % day)
            self.backup("202610%02dT041500Z" % day, "--tag=pre-update")
        snaps = self.snapshots()
        self.assertIn("20261001T020000Z", snaps, "the old site of an import that is still waiting must survive any number of nightly runs and pre-update snapshots")
        self.assertLess(len(snaps), 20, "the other snapshots are still pruned")

    def test_a_pre_import_backup_of_an_empty_site_is_refused(self):
        self.empty_site()
        r = self.backup("20261024T200300Z", "--tag=pre-import")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no wp_options table", r.stderr)
        self.assertEqual(self.snapshots(), [])

    def test_a_pre_update_snapshot_is_tagged_and_is_pruned_on_its_own(self):
        r = self.backup("20261024T020000Z", "--tag=pre-update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tag=pre-update", (self.backups / "wptest" / "20261024T020000Z" / "MANIFEST").read_text())
        self.assertIn("OK: snapshot 20261024T020000Z of wptest", r.stdout, "the update job reads the stamp from this line")
        self.backup("20261025T031500Z")
        self.assertNotIn("tag=", (self.backups / "wptest" / "20261025T031500Z" / "MANIFEST").read_text())
        # the nightly ones are seven, the tagged are two, and neither pushes the other out
        old = self.fabricate(12)
        self.tagged("20261020T020000Z", "20261021T020000Z", "20261022T020000Z", "20261023T020000Z")
        self.assertEqual(self.backup("20261026T031500Z").returncode, 0)
        left = set(self.snapshots())
        self.assertEqual({s for s in left if s.endswith("T020000Z")}, {"20261023T020000Z", "20261024T020000Z"}, "the newest two pre-update snapshots, whichever nights they are from")
        plain = {s for s in left if not s.endswith("T020000Z")}
        self.assertEqual(plain, self.expected_keep([s for s in old + ["20261025T031500Z", "20261026T031500Z"]], 7, 3), "tagged ones take no daily or weekly place")

    # --- the final backup of a site that is about to stop
    def dump_flag(self):
        return [l for l in self.log("order").splitlines() if l.startswith("dump")]

    def test_a_final_backup_quiets_the_site_for_the_dump_and_leaves_it_quiet(self):
        r = self.backup("20261024T201500Z", "--tag=final")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.dump_flag(), ["dump flag=up"], "the flag is up when the database is dumped")
        self.assertTrue(self.flag.exists(), "and it stays up: nothing may write between this snapshot and the stop")
        self.assertIn("stays so", r.stdout); self.assertIn("tag=final", (self.backups / "wptest" / "20261024T201500Z" / "MANIFEST").read_text())
        self.assertIn("OK: snapshot 20261024T201500Z of wptest", r.stdout)

    def test_a_failed_final_backup_brings_the_site_back(self):
        self.put("dump_fail", "")
        r = self.backup("20261024T201500Z", "--tag=final")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.dump_flag(), ["dump flag=up"]); self.assertFalse(self.flag.exists(), "a failed backup must not leave a site offline")
        self.assertEqual(self.snapshots(), [])

    def test_someone_elses_maintenance_flag_is_never_touched(self):
        self.flag.write_text("")   # the update job's, say
        self.assertEqual(self.backup("20261024T201500Z", "--tag=final").returncode, 0)
        self.assertTrue(self.flag.exists(), "left as found on success")
        self.put("dump_fail", "")
        self.assertNotEqual(self.backup("20261025T201500Z", "--tag=final").returncode, 0)
        self.assertTrue(self.flag.exists(), "left as found on failure")

    def test_other_backups_never_touch_the_flag(self):
        for n, args in enumerate(((), ("--tag=pre-update",), ("--force",))):
            self.assertEqual(self.backup("2026102%dT201500Z" % (1 + n), *args).returncode, 0)
            self.assertFalse(self.flag.exists(), args); self.assertEqual(self.dump_flag()[-1], "dump flag=down")

    def test_a_final_backup_of_a_stopped_or_frozen_site_raises_no_flag(self):
        (self.site / "stopped.flag").write_text("")
        self.assertEqual(self.backup("20261024T201500Z", "--tag=final").returncode, 2); self.assertFalse(self.flag.exists())
        (self.site / "stopped.flag").unlink(); (self.tmp / "freeze").write_text("")
        r = self.backup("20261025T201500Z", "--tag=final")
        self.assertEqual(r.returncode, 0); self.assertIn("frozen", r.stdout); self.assertFalse(self.flag.exists(), "skipped as a success, nothing touched")
        r = self.backup("20261026T201500Z", "--tag=final", "--force")
        self.assertEqual(r.returncode, 0); self.assertTrue(self.flag.exists(), "--force backs up anyway, and quiets the site")

    def test_final_snapshots_are_kept_on_their_own_and_nothing_else_is_pushed_out(self):
        old = self.fabricate(12)
        self.tagged("20261020T020000Z", "20261021T020000Z", "20261022T020000Z")   # pre-update, as before
        for st in ("20261001T030000Z", "20261015T030000Z", "20261023T030000Z"):
            d = self.backups / "wptest" / st
            (d / "files").mkdir(parents=True); (d / "db.sql.gz").write_text("x"); (d / "MANIFEST").write_text("site=wptest\nstamp=%s\ntag=final\n" % st)
        self.assertEqual(self.backup("20261026T031500Z").returncode, 0)
        left = set(self.snapshots())
        self.assertEqual({s for s in left if s.endswith("T030000Z")}, {"20261023T030000Z"}, "the newest final snapshot only")
        self.assertEqual({s for s in left if s.endswith("T020000Z")}, {"20261021T020000Z", "20261022T020000Z"}, "the pre-update ones keep their own rule")
        plain = {s for s in left if s.endswith("T201500Z") or s.endswith("T031500Z")}
        self.assertEqual(plain, self.expected_keep(old + ["20261026T031500Z"], 7, 3), "tagged ones take no daily or weekly place")

    def test_an_unknown_tag_is_refused(self):
        for bad in ("--tag=finl", "--tag=final;id", "--tag=Final"):
            self.assertEqual(self.backup("20261024T201500Z", bad).returncode, 2, bad)
        self.assertEqual(self.snapshots(), [])

    def test_a_first_ever_snapshot_that_is_tagged_prunes_without_error(self):
        r = self.backup("20261024T020000Z", "--tag=pre-update")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(self.snapshots(), ["20261024T020000Z"])

    def test_a_tagged_backup_under_a_freeze_prunes_nothing_and_other_tags_are_refused(self):
        self.tagged("20261020T020000Z", "20261021T020000Z", "20261022T020000Z")
        (self.tmp / "freeze").write_text("")
        self.assertEqual(self.backup("20261024T020000Z", "--tag=pre-update", "--force").returncode, 0)
        self.assertEqual(len([s for s in self.snapshots() if s.endswith("T020000Z")]), 4)
        for bad in ("--tag=other", "--tag=", "--tag", "pre-update"):
            self.assertEqual(self.backup("20261025T031500Z", bad).returncode, 2, bad)

    def test_a_stopped_site_is_refused_and_touches_nothing(self):
        (self.site / "stopped.flag").write_text("")
        r = self.backup("20261024T201500Z")
        self.assertEqual(r.returncode, 2); self.assertIn("is stopped", r.stderr); self.assertIn("wp-ctl ensure-state site=wptest state=hold", r.stderr)
        self.assertEqual(self.log("docker.calls"), ""); self.assertEqual(self.snapshots(), [])

    def test_retention_with_few_snapshots_deletes_nothing(self):
        old = self.fabricate(5)
        self.assertEqual(self.backup("20261024T201500Z").returncode, 0)
        self.assertEqual(set(self.snapshots()), set(old + ["20261024T201500Z"]))

    def test_a_frozen_site_is_skipped_as_a_success_and_nothing_is_touched(self):
        old = self.fabricate(45)
        for flag in (self.tmp / "freeze", self.site / "freeze"):
            flag.write_text("")
            r = self.backup("20261024T201500Z")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("is frozen", r.stdout)
            self.assertEqual(set(self.snapshots()), set(old), "no new snapshot, nothing pruned")
            self.assertEqual(self.log("docker.calls"), "", "the database must not even be touched")
            flag.unlink()
        self.assertEqual(self.backup("20261024T201500Z").returncode, 0)
        self.assertIn("20261024T201500Z", self.snapshots(), "unfrozen, the backup runs")

    def test_force_backs_up_a_frozen_site_but_still_prunes_nothing(self):
        old = self.fabricate(45)
        (self.tmp / "freeze").write_text("")
        r = self.run_script(self.script, "wptest", "--force", FAKE_STAMP="20261024T201500Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing is pruned", r.stdout)
        self.assertEqual(set(self.snapshots()), set(old + ["20261024T201500Z"]))
        (self.tmp / "freeze").unlink()
        (self.site / "freeze").write_text("")
        r = self.run_script(self.script, "wptest", "--force", FAKE_STAMP="20261024T201600Z")
        self.assertEqual(len(self.snapshots()), 47, "the site's own flag pauses pruning too")

    def test_force_on_an_unfrozen_site_prunes_as_usual_and_other_arguments_are_refused(self):
        self.fabricate(45)
        self.assertEqual(self.run_script(self.script, "wptest", "--force", FAKE_STAMP="20261024T201500Z").returncode, 0)
        self.assertEqual(len(self.snapshots()), 10)
        for bad in ("--forcefully", "now", "-f"):
            self.assertEqual(self.run_script(self.script, "wptest", bad, FAKE_STAMP="20261024T201700Z").returncode, 2, bad)

    def test_bad_names_never_reach_docker(self):
        for bad in ("nosuch", "../x", "a;id", "", "wptest extra"):
            r = self.run_script(self.script, *([bad] if bad else []))
            self.assertEqual(r.returncode, 2, bad)
        self.assertEqual(self.log("docker.calls"), "")

    # --- how it is scheduled
    def test_every_site_is_backed_up_every_night_hold_or_live_and_a_failure_alerts(self):
        # which state has the timer on (hold and live yes, stopped no) is the provisioning tool's: tests/test_wp_site_provision.py (test_stopped, test_a_timer_that_is_enabled_but_not_running_is_started)
        svc = self.env_j.get_template("wp_backup.service.j2").render(**self.ctx)
        self.assertIn("OnFailure=wp-job-failed@%N.service", svc)
        self.assertIn("ExecStart=%s/wp-site-backup %%i" % self.ctx["wp_tools_bin"], svc)
        tim = self.env_j.get_template("wp_backup.timer.j2").render(**self.ctx)
        self.assertIn("OnCalendar=*-*-* 03:15:00", tim); self.assertIn("Persistent=true", tim)
        self.assertEqual(self.v["wp_backup_calendar"], "*-*-* 03:15:00", "after the update window, before the Proxmox job at 04:00")

    def test_the_backup_projects_cannot_collide_with_the_sites(self):
        ids = [s["quota_id"] for s in self.v["wp_sites"]]
        self.assertGreater(self.v["wp_backup_quota_offset"], max(ids) - min(ids))
        self.assertLess(max(ids) + self.v["wp_backup_quota_offset"], self.v["wp_scratch_quota_id"])
        for i in (4000000, 4000001):
            self.assertGreater(i, self.v["wp_scratch_quota_id"], "the verifier and rehearsals use 4000000+")


class BackupCheck(Base):
    def setUp(self):
        super().setUp()
        self.script = self.install("wp_backup_check.sh.j2", "bin/wp-backup-check")

    def mark(self, hours_old, stamp="20261024T201500Z"):
        f = self.backups / "wptest" / ".last-success"
        f.write_text(stamp)
        t = time.time() - hours_old * 3600
        os.utime(f, (t, t))

    def said(self):
        return self.log("ntfy.log").splitlines()

    def test_fresh_is_quiet_stale_is_told_once_and_a_new_backup_closes_it(self):
        self.mark(2)
        for _ in range(2):
            self.assertEqual(self.run_script(self.script).returncode, 0)
        self.assertEqual(self.said(), [])
        self.mark(31)
        for _ in range(3):
            self.run_script(self.script)
        self.assertEqual(len(self.said()), 1, self.said())
        self.assertIn("site wptest has no backup newer than 30 hours (last good: 20261024T201500Z, 31 hours ago)", self.said()[0])
        self.mark(1)
        self.run_script(self.script); self.run_script(self.script)
        self.assertEqual(len(self.said()), 2)
        self.assertIn("has a fresh backup again", self.said()[1])

    def test_a_site_that_only_the_registry_knows_is_judged_like_any_other(self):
        d = self.v["wp_site_defaults"]
        self.rows("wptest hold 1001 %d 2001 %d notify" % (d["quota_mb"], d["backup_quota_mb"]), "newsite live 1002 %d 2002 %d notify" % (d["quota_mb"], d["backup_quota_mb"]))
        (self.site.parent / "newsite").mkdir(); (self.backups / "newsite").mkdir()
        self.mark(2)
        f = self.backups / "newsite" / ".last-success"
        f.write_text("20261024T201500Z")
        t = time.time() - 40 * 3600
        os.utime(f, (t, t))
        self.assertEqual(self.run_script(self.script).returncode, 0)
        self.assertEqual(len(self.said()), 1, self.said())
        self.assertIn("site newsite has no backup newer than 30 hours", self.said()[0])

    def test_a_site_the_registry_does_not_list_is_not_judged(self):
        self.rows()                                     # a closed site, or one that was never registered
        self.mark(100)
        self.assertEqual(self.run_script(self.script).returncode, 0)
        self.assertEqual(self.said(), [])

    def test_a_registry_that_cannot_be_read_fails_the_check_loudly_and_does_not_clear_or_raise_anything(self):
        self.mark(100)
        self.run_script(self.script)
        told = self.said()
        self.assertEqual(len(told), 1)
        self.put("rows_fail", "")
        r = self.run_script(self.script)
        self.assertEqual(r.returncode, 2, "the unit fails, so OnFailure= tells ntfy")
        self.assertIn("cannot read the site registry", r.stderr)
        self.assertEqual(self.said(), told, "no new message, and the stale flag of the site is still up")
        self.assertTrue((self.tmp / "state" / "backup-stale-wptest.flag").exists())

    def waiting_for_an_import(self, tables="0"):
        self.shim("docker", DOCKER_COUNT)
        self.put("tables", tables)
        t = time.time() - 40 * 3600
        os.utime(self.site.parent / "wptest", (t, t))

    def test_a_site_with_no_tables_and_no_snapshot_is_not_stale_however_old_it_is(self):
        self.waiting_for_an_import()
        for _ in range(2):
            self.run_script(self.script)
        self.assertEqual(self.said(), [], "an empty site waiting for an import has nothing to back up")

    def test_a_site_with_tables_and_no_snapshot_is_stale(self):
        self.waiting_for_an_import("57")
        self.run_script(self.script)
        self.assertEqual(len(self.said()), 1)

    def test_a_site_with_no_tables_but_a_snapshot_is_stale(self):
        self.waiting_for_an_import()
        (self.backups / "wptest" / "20261020T201500Z").mkdir()
        self.run_script(self.script)
        self.assertEqual(len(self.said()), 1, "it once had a backup, so an emptied database is a problem")

    def test_a_site_whose_last_good_backup_is_old_is_stale_even_with_no_tables(self):
        self.waiting_for_an_import()
        self.mark(40)
        self.run_script(self.script)
        self.assertEqual(len(self.said()), 1, "it was backed up once: an emptied database must not silence the alert")

    def test_a_database_that_cannot_be_counted_does_not_hide_a_stale_site(self):
        self.waiting_for_an_import()
        self.put("tables_fail", "1")
        self.run_script(self.script)
        self.assertEqual(len(self.said()), 1)

    def test_the_limit_is_thirty_hours_exactly(self):
        for age, told in ((29, 0), (30, 1)):
            self.setUp()
            self.mark(age); self.run_script(self.script)
            self.assertEqual(len(self.said()), told, age)

    def test_a_site_never_backed_up_is_stale_only_once_it_is_that_old_itself(self):
        self.run_script(self.script)
        self.assertEqual(self.said(), [], "a site deployed minutes ago has had no night yet")
        t = time.time() - 40 * 3600
        os.utime(self.site.parent / "wptest", (t, t))
        self.run_script(self.script)
        self.assertEqual(len(self.said()), 1)
        self.assertIn("last good: never", self.said()[0])

    def test_a_frozen_site_is_not_called_stale_and_is_judged_again_when_unfrozen(self):
        self.mark(40)
        (self.tmp / "freeze").write_text("")
        self.run_script(self.script); self.run_script(self.script)
        self.assertEqual(self.said(), [], "a paused backup is not a missing one")
        (self.tmp / "freeze").unlink(); (self.site / "freeze").write_text("")
        self.run_script(self.script)
        self.assertEqual(self.said(), [], "the site's own flag exempts it too")
        (self.site / "freeze").unlink()
        self.run_script(self.script)
        self.assertEqual(len(self.said()), 1, "once unfrozen it is judged on its age")

    def test_a_stopped_site_is_not_called_stale_and_is_judged_again_when_started(self):
        self.mark(40)
        (self.site / "stopped.flag").write_text("")
        self.run_script(self.script); self.run_script(self.script)
        self.assertEqual(self.said(), [], "a stopped site has its backup timer off on purpose")
        (self.site / "stopped.flag").unlink()
        self.run_script(self.script)
        self.assertEqual(len(self.said()), 1, "once started it is judged on its age")

    def test_the_environment_can_make_every_site_stale_at_once_for_the_rehearsal(self):
        self.mark(1)
        self.run_script(self.script, WP_BACKUP_STALE_HOURS="0")
        self.assertEqual(len(self.said()), 1)

    def test_the_hourly_unit_alerts_on_failure(self):
        tasks = yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())
        by = {t.get("name"): t for t in tasks}
        self.assertIn("OnFailure=wp-job-failed@%N.service", by["Stage the backup freshness check service"]["ansible.builtin.copy"]["content"])
        self.assertIn("OnCalendar={{ wp_backup_check_calendar }}", by["Stage the backup freshness check timer"]["ansible.builtin.copy"]["content"])
        self.assertEqual(self.v["wp_backup_check_calendar"], "hourly")


class Drill(Base):
    def setUp(self):
        super().setUp()
        self.shim("wp-site-restore-record", "")
        # the drill calls wp-site-restore in the tools directory: a recording stand-in there
        (self.tmp / "bin" / "wp-site-restore").write_text('#!/bin/bash\necho "$*" >> "%s"\n' % (self.tmp / "shim" / "restore.calls"))
        (self.tmp / "bin" / "wp-site-restore").chmod(0o755)
        self.script = self.install("wp_restore_drill.sh.j2", "bin/wp-restore-drill")
        self.row_names("wptest", "alpha", "beta", "gamma")
        for n in ("wptest", "alpha", "beta"):
            (self.backups / n).mkdir(exist_ok=True)
            (self.backups / n / ".last-success").write_text("20261024T201500Z")
        (self.backups / "gamma").mkdir(exist_ok=True)   # a site with no good backup yet

    def row_names(self, *names):
        d = self.v["wp_site_defaults"]
        self.rows(*["%s hold %d %d %d %d %s" % (n, 1001 + i, d["quota_mb"], 2001 + i, d["backup_quota_mb"], d["update_policy"]) for i, n in enumerate(names)])

    def picks(self, n=1, **env):
        for _ in range(n):
            self.assertEqual(self.run_script(self.script, **env).returncode, 0)
        return [l.split()[0] for l in self.log("restore.calls").splitlines()]

    def test_it_picks_a_random_site_that_has_a_good_backup_and_asks_for_a_notification(self):
        picked = self.picks(40)
        self.assertEqual(set(picked), {"wptest", "alpha", "beta"}, "random over the sites with a backup, never one without")
        for line in self.log("restore.calls").splitlines():
            self.assertTrue(line.endswith("latest --notify"), line)

    def test_a_named_site_is_used_only_if_it_has_a_backup(self):
        self.assertEqual(self.picks(1, WP_DRILL_SITE="beta"), ["beta"])
        r = self.run_script(self.script, WP_DRILL_SITE="gamma")
        self.assertEqual(r.returncode, 2); self.assertIn("not a site with a good backup", r.stderr)
        r = self.run_script(self.script, WP_DRILL_SITE="../x")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.log("restore.calls").count("\n"), 1, "nothing else was run")

    def test_with_no_site_at_all_there_is_nothing_to_drill_and_nothing_is_wrong(self):
        self.rows()                                     # the registry is readable and holds no site that is not closed
        r = self.run_script(self.script)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("there are no sites", r.stdout)
        self.assertEqual(self.log("restore.calls"), "", "no restore was started")

    def test_a_registry_that_cannot_be_read_fails_the_drill_and_is_not_no_sites(self):
        self.put("rows_fail", "")
        r = self.run_script(self.script)
        self.assertEqual(r.returncode, 2); self.assertIn("cannot read the site registry", r.stderr)
        self.assertNotIn("there are no sites", r.stdout)
        self.assertEqual(self.log("restore.calls"), "")

    def test_a_closed_site_with_an_old_backup_is_never_drilled(self):
        self.row_names("alpha")                         # `gone` is closed: the registry does not list it
        (self.backups / "alpha").mkdir(exist_ok=True); (self.backups / "alpha" / ".last-success").write_text("20261024T201500Z")
        (self.backups / "gone").mkdir(exist_ok=True); (self.backups / "gone" / ".last-success").write_text("20261024T201500Z")
        for _ in range(8):
            (self.tmp / "shim" / "restore.calls").unlink(missing_ok=True)
            self.assertEqual(self.run_script(self.script).returncode, 0)
            self.assertEqual(self.log("restore.calls").split()[0], "alpha")

    def test_with_no_backup_anywhere_it_fails_so_that_the_unit_alerts(self):
        for n in ("wptest", "alpha", "beta"):
            (self.backups / n / ".last-success").unlink()
        r = self.run_script(self.script)
        self.assertEqual(r.returncode, 2); self.assertIn("nothing to drill", r.stderr)
        self.assertEqual(self.log("restore.calls"), "")

    def test_it_is_a_monthly_timer_after_the_proxmox_job_and_a_failure_alerts(self):
        by = {t.get("name"): t for t in yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())}
        self.assertIn("OnFailure=wp-job-failed@%N.service", by["Stage the monthly restore drill service"]["ansible.builtin.copy"]["content"])
        self.assertIn("OnCalendar={{ wp_drill_calendar }}", by["Stage the monthly restore drill timer"]["ansible.builtin.copy"]["content"])
        self.assertIn("Persistent=true", by["Stage the monthly restore drill timer"]["ansible.builtin.copy"]["content"])
        self.assertEqual(self.v["wp_drill_calendar"], "*-*-01 05:30:00")
        self.assertGreater(self.v["wp_drill_timeout_s"], 600)


class Restore(Base):
    def setUp(self):
        super().setUp()
        self.shim("docker", DOCKER_RESTORE)
        self.install("wp_site_cli.sh.j2", "bin/wp-site-cli")
        self.script = self.install("wp_site_restore.sh.j2", "bin/wp-site-restore")
        self.snap("20261023T201500Z"); self.snap("20261024T201500Z")
        self.scratch = self.tmp / "data/scratch/restore-wptest"

    def snap(self, stamp):
        d = self.backups / "wptest" / stamp
        (d / "files/uploads").mkdir(parents=True)
        (d / "files/uploads/a.jpg").write_text("a")
        gz(DUMP_OK, d / "db.sql.gz")

    def calls(self):
        return self.log("docker.calls").splitlines()

    def test_a_good_backup_restores_boots_and_reports_next_to_the_live_site(self):
        r = self.run_script(self.script, "wptest", "--notify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("restore drill of wptest from 20261024T201500Z: OK (https://wptest.example.com; users 2 (live 2), posts 7 (live 7), active plugins 1 (live 1))", r.stdout)
        self.assertEqual(len(self.log("ntfy.log").splitlines()), 1)
        self.assertIn("dellwp restore drill of wptest", self.log("ntfy.log"))
        self.assertIn("CREATE TABLE `wp_options`", self.log("loaded.sql"), "the dump must be loaded into the scratch database")
        self.assertFalse(self.scratch.exists(), "the scratch copy is removed")

    def test_notices_a_plugin_prints_before_the_answers_do_not_break_the_comparison(self):
        r = self.run_script(self.script, "wptest", SHIM_NOISE="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK (https://wptest.example.com; users 2 (live 2), posts 7 (live 7), active plugins 1 (live 1))", r.stdout)

    def test_notices_do_not_hide_an_empty_restore(self):
        r = self.run_script(self.script, "wptest", SHIM_NOISE="1", SHIM_USERS="0")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("no users", r.stdout)

    def test_the_counts_are_read_without_running_the_friends_plugins_or_theme_on_both_sides(self):
        self.run_script(self.script, "wptest")
        reads = [c for c in self.calls() if "--entrypoint wp" in c and "wprestore_" in c and (" user list " in c or " post list " in c or " plugin list " in c or " option get " in c)]
        self.assertEqual(len(reads), 4)
        for c in reads:
            self.assertIn("--skip-plugins --skip-themes", c, c)
        live = [c for c in self.calls() if " list " in c and "wprestore_" not in c and "--entrypoint wp" in c]
        self.assertEqual(len(live), 3, "the live site is asked the same three questions")
        for c in live:
            self.assertIn("--skip-plugins --skip-themes", c, c)
        boot = [c for c in self.calls() if " core is-installed" in c]
        self.assertEqual(len(boot), 1); self.assertNotIn("--skip-plugins", boot[0], "the boot proof loads the plugins")

    def test_a_wp_cli_command_that_fails_is_named_with_what_it_printed(self):
        r = self.run_script(self.script, "wptest", SHIM_WPFAIL="post")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAILED: WP-CLI 'post list --post_type=any --post_status=any --format=count' exited 3; it printed: partial output|", r.stdout)
        self.assertIn("stderr: Error: Database connection error|", r.stdout)
        self.assertFalse(self.scratch.exists())

    def test_an_answer_that_is_not_there_is_named_with_what_was_printed_instead(self):
        r = self.run_script(self.script, "wptest", SHIM_NOANSWER="plugin")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAILED: WP-CLI 'plugin list --status=active --format=count' printed no usable answer; it printed: Deprecated: only a notice|", r.stdout)

    def test_a_live_site_that_cannot_answer_is_only_a_question_mark(self):
        (self.tmp / "bin" / "wp-site-cli").write_text("#!/bin/bash\nexit 1\n")
        r = self.run_script(self.script, "wptest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("users 2 (live ?), posts 7 (live ?), active plugins 1 (live ?)", r.stdout)

    def test_the_scratch_database_and_checks_are_as_locked_down_as_the_site(self):
        self.run_script(self.script, "wptest")
        text = "\n".join(self.calls())
        db = [c for c in self.calls() if c.startswith("run -d")][0]
        self.assertIn("--user 20999:20999", db); self.assertIn("--cap-drop ALL", db); self.assertIn("--network-alias db", db)
        self.assertIn("--env-file %s/.env" % self.site, db)
        mem = re.search(r"--memory (\d+m) --memory-swap (\d+m)", db)
        self.assertEqual(mem.group(1), mem.group(2))
        wp = [c for c in self.calls() if "--entrypoint wp" in c and "wprestore_" in c]
        self.assertTrue(wp)
        for c in wp:
            self.assertIn("--user 33:33", c); self.assertIn("--read-only", c); self.assertIn(":/var/www/html:ro", c); self.assertIn("--cap-drop ALL", c)
        self.assertIn("setquota -P %d 0 %d 0 0" % (self.v["wp_scratch_quota_id"], self.v["wp_scratch_quota_mb"] * 1024), self.log("quota.calls"))
        self.assertTrue(self.log("quota.calls").strip().splitlines()[-1].endswith("0 0 0 0 %s" % (self.tmp / "data")), "the quota is reset at the end")

    def test_it_never_touches_the_real_site(self):
        self.run_script(self.script, "wptest")
        for c in self.calls():
            words = c.split()
            if words[0] in ("rm", "stop", "restart", "kill", "start", "down", "network") and words[0] != "network":
                self.assertNotIn("wp_wptest_", c, c)
            self.assertNotIn("%s/db:/var/lib/mysql" % self.site, c, "the live database directory must never be mounted")
            self.assertNotIn("%s/html:/var/www/html:rw" % self.site, c)

    def test_a_site_that_does_not_boot_fails_loudly_and_is_cleaned_up(self):
        self.put("wp_not_installed", "")
        r = self.run_script(self.script, "wptest", "--notify")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED: WordPress does not boot on the restored database and files", r.stdout)
        self.assertIn("FAILED", self.log("ntfy.log"))
        self.assertFalse(self.scratch.exists())
        self.assertTrue(any(c.startswith("rm -f wprestore_wptest_db") for c in self.calls()), "the scratch database is removed")
        self.assertTrue(any(c.startswith("network rm wprestore_wptest") for c in self.calls()))

    def test_a_dump_that_does_not_load_and_an_empty_restore_both_fail(self):
        r = self.run_script(self.script, "wptest", SHIM_LOAD_RC="1")
        self.assertEqual(r.returncode, 1); self.assertIn("the dump did not load", r.stdout)
        self.assertFalse(self.scratch.exists())
        r = self.run_script(self.script, "wptest", SHIM_USERS="0")
        self.assertEqual(r.returncode, 1); self.assertIn("no users", r.stdout)
        self.assertFalse(self.scratch.exists())

    def test_a_database_that_never_starts_is_cleaned_up(self):
        self.put("db_never_healthy", "")
        env = dict(self.env)
        self.shim("sleep", "#!/bin/bash\nexit 0\n")
        r = self.run_script(self.script, "wptest")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.scratch.exists())
        self.assertTrue(any(c.startswith("rm -f wprestore_wptest_db") for c in self.calls()))

    def test_it_picks_the_newest_snapshot_or_the_one_it_is_told_and_refuses_the_rest(self):
        r = self.run_script(self.script, "wptest")
        self.assertIn("from 20261024T201500Z", r.stdout)
        r = self.run_script(self.script, "wptest", "20261023T201500Z")
        self.assertIn("from 20261023T201500Z", r.stdout)
        for args in (("wptest", "20250101T000000Z"), ("wptest", "yesterday"), ("wptest", "../x"), ("nosuch",), ("wptest", "20261024T201500Z", "extra")):
            (self.tmp / "shim" / "docker.calls").unlink(missing_ok=True)
            r = self.run_script(self.script, *args)
            self.assertEqual(r.returncode, 2, args)
            self.assertFalse(any(c.startswith("network create") for c in self.calls()), "nothing may start for %r" % (args,))

    def test_a_site_with_no_snapshot_says_so(self):
        shutil.rmtree(self.backups / "wptest"); (self.backups / "wptest").mkdir()
        r = self.run_script(self.script, "wptest")
        self.assertEqual(r.returncode, 2)
        self.assertIn("there is no snapshot of wptest", r.stderr)
        self.assertFalse(any(c.startswith("network create") for c in self.calls()))

    def test_an_incomplete_snapshot_and_a_leftover_scratch_directory_are_refused(self):
        shutil.rmtree(self.backups / "wptest/20261024T201500Z/files")
        r = self.run_script(self.script, "wptest", "20261024T201500Z")
        self.assertEqual(r.returncode, 2); self.assertIn("not a complete snapshot", r.stderr)
        self.scratch.mkdir()
        (self.scratch / "precious").write_text("a drill that is still running")
        r = self.run_script(self.script, "wptest", "20261023T201500Z")
        self.assertEqual(r.returncode, 2); self.assertIn("exists", r.stderr)
        self.assertTrue((self.scratch / "precious").exists(), "another drill's files must not be removed by this one")


if __name__ == "__main__":
    unittest.main(verbosity=1)
