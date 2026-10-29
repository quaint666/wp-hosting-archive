#!/usr/bin/env python3
"""Offline tests of the delete and the archive purge: templates/wp/wp_site_delete.sh.j2 (wp-site-delete) and wp_archive_purge.sh.j2 (wp-archive-purge).

The filesystem is real (a temporary tree standing in for /srv/wp, the unit directory and the vhost directory), and so are gzip, tar, sha256sum, find and rm; systemctl, docker, nginx, setquota,
lsattr and ntfy-notify are stand-ins that log what they are given (and, for systemctl and docker, whether the archive already existed at that moment). They prove the order (the archive is
complete and verified before anything is removed), what is refused and that a refusal changes nothing, that only the named site's own paths go, that a failure part way leaves a state
the tool can resume, and that the purge removes exactly the expired archives and nothing else. They do not prove that systemd stops a unit, that nginx reloads, that ext4 drops a
project's limit, or that the Proxmox backups still hold the data: scripts/wp_delete_rehearsal_dellwp.sh does the first three on the real host. Run: python3 -I tests/test_wp_delete.py"""
import datetime
import gzip
import os
import re
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wp_backup import Base, DUMP_OK, gz  # noqa: E402
from test_wp_sites_render import ROOT, load  # noqa: E402

SYSTEMCTL = '#!/bin/bash\necho "systemctl $* archive=$(ls "$ARCHIVE_DIR" 2>/dev/null | grep -vc "^\\.")" >> "$SHIM/calls"\nexit 0\n'
DOCKER = '''#!/bin/bash
echo "docker $*" >> "$SHIM/calls"
[ "$1" = ps ] && [ -f "$SHIM/running" ] && cat "$SHIM/running"
exit 0
'''
NGINX = '#!/bin/bash\necho "nginx $* vhost=$([ -e "$VHOST_DIR/wp_site_wptest.conf" ] && echo present || echo absent)" >> "$SHIM/calls"\n[ "$1" = -t ] && [ -f "$SHIM/nginx_fail" ] && exit 1\nexit 0\n'
SETQUOTA = '#!/bin/bash\necho "setquota $*" >> "$SHIM/calls"\n'
LSATTR = '#!/bin/bash\ncase "$2" in */backups/*) echo "  2001 --------------e------- $2" ;; */sites/*) echo "  ${LSATTR_SITE_ID:-1001} --------------e------- $2" ;; esac\n'
GZIP = '#!/bin/bash\nif [ "$1" = -t ] && [[ "$2" == */.partial-* ]] && [ -f "$SHIM/gzip_archive_fail" ]; then exit 1; fi\nexec /usr/bin/gzip "$@"\n'
TAR = """#!/bin/bash
[ -f "$SHIM/tar_fail" ] && { echo "tar: write error" >&2; exit 1; }
/usr/bin/tar "$@"; rc=$?
# a tar that exits 0 and leaves a truncated file behind: only reading the archive back can tell
if [ -f "$SHIM/tar_corrupt" ] && [ "$3" = -czf ]; then truncate -s 20 "$4"; fi
exit $rc
"""
STAMP = "20261024T150001Z"
# wp-site-provision close: logs what stood at the moment it was called (is the archive complete, is anything removed yet) and can be made to fail
PROVISION = '''#!/bin/bash
echo "provision $* archive=$([ -f "$ARCHIVE_DIR/wptest-%s/MANIFEST" ] && echo complete || echo no) site=$([ -d "$SITE_DIR" ] && echo present || echo gone) vhost=$([ -e "$VHOST_DIR/wp_site_wptest.conf" ] && echo present || echo gone)" >> "$SHIM/calls"
[ -f "$SHIM/provision_fail" ] && { echo "the registry is unreadable" >&2; exit 1; }
# what the real close does: the tombstone first, then the record goes
[ "$1" = close ] && { mkdir -p "$TOMB_DIR"; echo '{}' > "$TOMB_DIR/$2.json"; rm -f "$SITE_DIR/site.json"; }
exit 0
''' % "20261024T150001Z"


class Delete(Base):
    def setUp(self):
        super().setUp()
        self.data = self.tmp / "data"
        self.archive = self.data / "archive"; self.archive.mkdir()
        self.units = self.tmp / "units"; self.units.mkdir()
        self.vhosts = self.tmp / "vhosts"; self.vhosts.mkdir()
        self.env.update(ARCHIVE_DIR=str(self.archive), VHOST_DIR=str(self.vhosts), SITE_DIR=str(self.site), TOMB_DIR=str(self.data / "tombstones"))
        provision = self.tmp / "bin" / "wp-site-provision"
        provision.write_text(PROVISION); provision.chmod(0o755)
        for name, body in (("systemctl", SYSTEMCTL), ("docker", DOCKER), ("nginx", NGINX), ("setquota", SETQUOTA), ("lsattr", LSATTR), ("gzip", GZIP), ("tar", TAR),
                           ("logger", '#!/bin/bash\necho "$*" >> "$SHIM/logger.log"\n')):
            self.shim(name, body)
        self.script = self.install("wp_site_delete.sh.j2", "bin/wp-site-delete", wp_archive_dir=str(self.archive), wp_unit_dir=str(self.units), wp_vhost_dir=str(self.vhosts))
        self.tty = self.tmp / "tty"; self.env["WP_SITE_TTY"] = str(self.tty); self.tty.write_text("wptest\n")
        self.bk = self.backups / "wptest"
        (self.site / "stopped.flag").write_text("")
        (self.site / "db").mkdir(); (self.site / "db" / "ibdata1").write_text("data")
        (self.site / "html/wp-content/uploads/a.jpg").write_text("a" * 100)
        self.snapshot(STAMP, tag="final")
        self.vhost = self.vhosts / "wp_site_wptest.conf"; self.vhost.write_text("server { }\n")
        self.unit = self.units / "wp-wptest.service"; self.unit.write_text("[Unit]\n")
        (self.tmp / "state").mkdir(exist_ok=True); (self.tmp / "state" / "backup-stale-wptest.flag").write_text("")
        # a second site that must never be touched
        (self.data / "sites/other").mkdir(); (self.data / "sites/other/keep.txt").write_text("other")
        (self.backups / "other").mkdir(); (self.backups / "other/keep.txt").write_text("other")
        (self.vhosts / "wp_site_other.conf").write_text("server { }\n"); (self.units / "wp-other.service").write_text("[Unit]\n")

    def snapshot(self, stamp, tag=None, dump=DUMP_OK, files=True, manifest=True, empty=False):
        d = self.bk / stamp
        d.mkdir(parents=True)
        gz(dump, d / "db.sql.gz")
        if files:
            (d / "files/uploads").mkdir(parents=True); (d / "files/uploads/a.jpg").write_text("a" * 100); (d / "files/plugins").mkdir(); (d / "files/plugins/p.php").write_text("<?php\n")
        if manifest:
            (d / "MANIFEST").write_text("site=wptest\nstamp=%s\ncore=7.1.3\n%s%s" % (stamp, "tag=%s\n" % tag if tag else "", "empty=yes\n" if empty else ""))
        return d

    def delete(self, *args, **env):
        return self.run_script(self.script, *(args or ("wptest",)), **env)

    def calls(self):
        """What was asked of systemd, docker, nginx and the quota tools, except the read-only look for running containers that the tool makes before it asks anything."""
        return [l for l in self.log("calls").splitlines() if not l.startswith("docker ps ")]

    def untouched(self):
        for p in (self.site, self.bk, self.vhost, self.unit, self.site / "stopped.flag", self.site / "db/ibdata1"):
            self.assertTrue(p.exists(), "%s was removed" % p)
        self.assertEqual(self.calls(), [], "nothing was asked of systemd, docker or nginx")
        self.assertEqual([p.name for p in self.archive.iterdir()], [], "and no archive was left")

    def final(self):
        return self.archive / ("wptest-" + STAMP)

    # --- a site that never had data
    EMPTY_DUMP = "-- MariaDB dump\n-- Dump completed on 2026-10-24 20:15:00\n"

    def only_snapshot(self, **kw):
        import shutil
        shutil.rmtree(self.bk / STAMP)
        return self.snapshot(STAMP, tag="final", **kw)

    def test_a_site_that_never_had_data_is_archived_and_deleted_from_its_empty_final_snapshot(self):
        self.only_snapshot(dump=self.EMPTY_DUMP, empty=True)
        r = self.delete()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        f = self.final()
        m = dict(l.split("=", 1) for l in (f / "MANIFEST").read_text().splitlines())
        self.assertEqual(m.get("empty"), "yes", "the archive says it holds nothing")
        self.assertEqual(gzip.open(f / "db.sql.gz", "rt").read(), self.EMPTY_DUMP)
        self.assertFalse(self.site.exists())

    def test_an_empty_dump_that_is_not_marked_empty_is_still_refused(self):
        self.only_snapshot(dump=self.EMPTY_DUMP, empty=False)
        r = self.delete()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no wp_options table", r.stderr)
        self.untouched()

    def test_a_snapshot_marked_empty_whose_dump_has_tables_is_refused(self):
        self.only_snapshot(dump=DUMP_OK, empty=True)
        r = self.delete()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("marked empty but its dump has tables", r.stderr)
        self.untouched()

    def test_a_normal_archive_has_no_empty_mark(self):
        self.assertEqual(self.delete().returncode, 0)
        self.assertNotIn("empty", (self.final() / "MANIFEST").read_text())

    # --- a good delete
    def test_a_good_delete_archives_first_checks_the_archive_and_then_removes_exactly_this_site(self):
        r = self.delete()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        f = self.final()
        self.assertTrue(f.is_dir())
        self.assertEqual(sorted(p.name for p in f.iterdir()), ["MANIFEST", "db.sql.gz", "wp-content.tar.gz"], "no .partial directory is left")
        self.assertEqual(gzip.open(f / "db.sql.gz", "rt").read(), DUMP_OK)
        with tarfile.open(f / "wp-content.tar.gz") as t:
            self.assertEqual(sorted(t.getnames()), ["files", "files/plugins", "files/plugins/p.php", "files/uploads", "files/uploads/a.jpg"])
        m = dict(l.split("=", 1) for l in (f / "MANIFEST").read_text().splitlines())
        want = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=self.v["wp_archive_keep_days"])).strftime("%Y-%m-%d")
        self.assertEqual((m["site"], m["snapshot"], m["core"], m["expires"]), ("wptest", STAMP, "7.1.3", want))
        import hashlib
        self.assertEqual(m["db_sha256"], hashlib.sha256((f / "db.sql.gz").read_bytes()).hexdigest()); self.assertEqual(m["files_sha256"], hashlib.sha256((f / "wp-content.tar.gz").read_bytes()).hexdigest())
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o700"); self.assertEqual(oct((f / "db.sql.gz").stat().st_mode & 0o777), "0o600")
        # gone: the site, its backups, its vhost, its unit, its stale-backup flag; the limits of its two projects
        for p in (self.site, self.bk, self.vhost, self.unit, self.tmp / "state" / "backup-stale-wptest.flag"):
            self.assertFalse(p.exists(), "%s should be gone" % p)
        self.assertIn("setquota -P 1001 0 0 0 0 %s" % self.data, self.log("quota.calls") + "\n".join(self.calls()))
        q = "\n".join(self.calls()); self.assertIn("setquota -P 2001 0 0 0 0", q)
        # untouched: the other site in every place
        for p in (self.data / "sites/other/keep.txt", self.backups / "other/keep.txt", self.vhosts / "wp_site_other.conf", self.units / "wp-other.service"):
            self.assertTrue(p.exists(), "%s belongs to another site" % p)
        self.assertIn("ntfy", "ntfy" if self.log("ntfy.log") else ""); self.assertEqual(len(self.log("ntfy.log").splitlines()), 1); self.assertIn(str(f), self.log("ntfy.log"))
        self.assertIn("site=wptest", self.log("logger.log"))

    def registered(self):
        (self.site / "site.json").write_text("{}")      # Base makes one already; kept for the tests that say it

    def unregistered(self):
        (self.site / "site.json").unlink()

    def test_a_site_with_a_registry_record_is_closed_there_after_the_archive_is_complete_and_before_anything_is_removed(self):
        self.registered()
        r = self.delete()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        closes = [c for c in calls if c.startswith("provision ")]
        self.assertEqual(closes, ["provision close wptest archive=complete site=present vhost=present"], "once, with the archive verified and the site still whole")
        self.assertLess(calls.index(closes[0]), min(i for i, c in enumerate(calls) if c.startswith(("systemctl", "docker rm", "nginx", "setquota"))), "before the first removal")
        self.assertFalse(self.site.exists())

    def test_a_site_with_neither_a_record_nor_a_tombstone_is_refused_and_nothing_changes(self):
        self.unregistered()
        r = self.delete()
        self.assertEqual(r.returncode, 2)
        self.assertIn("no registry record and no tombstone", r.stderr)
        self.untouched()

    def test_a_record_that_is_a_link_is_not_a_record(self):
        self.unregistered()
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text("{}")
        (self.site / "site.json").symlink_to(elsewhere)
        r = self.delete()
        self.assertEqual(r.returncode, 2)
        self.assertIn("no registry record and no tombstone", r.stderr)
        self.assertTrue(elsewhere.exists(), "the link was not followed")
        self.untouched()

    def test_a_tombstone_that_is_a_link_is_not_a_tombstone(self):
        self.unregistered()
        tomb = self.data / "tombstones"
        tomb.mkdir()
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text("{}")
        (tomb / "wptest.json").symlink_to(elsewhere)
        r = self.delete()
        self.assertEqual(r.returncode, 2)
        self.untouched()

    def test_a_delete_interrupted_after_the_registry_was_closed_is_finished_by_running_it_again(self):
        """The registry is closed (tombstone written, record removed) and the files are still there: the state a crash between the two steps of the removal leaves."""
        self.unregistered()
        (self.data / "tombstones").mkdir()
        (self.data / "tombstones" / "wptest.json").write_text("{}")
        r = self.delete()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.site.exists())
        self.assertEqual([c for c in self.calls() if c.startswith("provision ")], [], "nothing left to close")

    def test_a_registry_that_cannot_close_the_site_stops_the_delete_before_anything_is_removed_and_a_rerun_finishes_it(self):
        self.registered()
        self.put("provision_fail", "")
        r = self.delete()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the registry could not close wptest", r.stderr)
        self.assertIn("the registry is unreadable", r.stderr)
        self.assertTrue(self.final().is_dir() and (self.final() / "MANIFEST").exists(), "the archive is complete")
        for p in (self.site, self.bk, self.vhost, self.unit, self.site / "stopped.flag"):
            self.assertTrue(p.exists(), "%s was removed although the registry did not close the site" % p)
        self.assertEqual([c for c in self.calls() if c.startswith(("systemctl", "docker rm", "nginx", "setquota"))], [], "nothing was asked of the host")
        (self.tmp / "shim" / "provision_fail").unlink()
        self.tty.write_text("wptest\n")
        r = self.delete()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.site.exists())

    def test_the_resumption_of_a_delete_does_not_close_the_site_again(self):
        self.registered()
        self.delete()
        self.vhost.write_text("server { }\n"); self.unit.write_text("[Unit]\n")
        self.tty.write_text("")
        r = self.delete()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("finishing the clean-up", r.stdout)
        self.assertEqual(len([c for c in self.calls() if c.startswith("provision ")]), 1)

    def test_the_archive_is_complete_before_the_first_thing_is_removed_and_nginx_is_checked_before_it_reloads(self):
        self.delete()
        calls = self.calls()
        first = [c for c in calls if c.startswith("systemctl")][0]
        self.assertTrue(first.endswith("archive=1"), "the first call to systemd came after the archive existed: " + first)
        order = [c.split()[0] + " " + c.split()[1] for c in calls if c.split()[0] in ("systemctl", "docker", "nginx")]
        i = lambda s: next(n for n, c in enumerate(calls) if c.startswith(s))
        self.assertLess(i("systemctl disable --now wp-wptest.service"), i("nginx -t"))
        self.assertLess(i("nginx -t"), i("systemctl reload nginx")); self.assertLess(i("systemctl reload nginx"), i("systemctl daemon-reload"))
        self.assertIn("vhost=absent", [c for c in calls if c.startswith("nginx -t")][0], "nginx is tested WITHOUT the vhost")
        for t in ("cron", "backup", "update"):
            self.assertIn("systemctl disable --now wp-%s@wptest.timer archive=1" % t, calls)
        self.assertTrue(any(c.startswith("docker rm -f wp_wptest_php wp_wptest_db") for c in calls)); self.assertIn("docker network rm wp-wptest_default", calls)
        self.assertTrue(order)

    # --- what is refused, and that a refusal changes nothing
    def refused(self, why, *args, **env):
        r = self.delete(*args, **env)
        self.assertNotEqual(r.returncode, 0, why + "\n" + r.stdout)
        self.untouched()
        return r

    def test_a_site_that_is_not_stopped_is_refused(self):
        (self.site / "stopped.flag").unlink()
        r = self.delete(); self.assertEqual(r.returncode, 2); self.assertIn("is not stopped", r.stderr)
        self.assertTrue(self.site.exists() and self.bk.exists() and self.vhost.exists()); self.assertEqual(self.calls(), [])

    def test_a_frozen_site_and_a_running_container_are_refused(self):
        (self.tmp / "freeze").write_text("")
        self.assertIn("is frozen", self.refused("global freeze").stderr)
        (self.tmp / "freeze").unlink(); (self.site / "freeze").write_text("")
        self.assertIn("is frozen", self.refused("site freeze").stderr)
        (self.site / "freeze").unlink(); self.put("running", "abc123\n")
        self.assertIn("a container of wptest is running", self.refused("running container").stderr.replace("\n", " ") if False else self.delete().stderr)
        self.assertTrue(self.site.exists() and self.bk.exists())

    def test_the_newest_snapshot_must_be_a_final_one(self):
        self.snapshot("20261025T031500Z")   # a nightly newer than the final one
        r = self.delete(); self.assertEqual(r.returncode, 2); self.assertIn("is not a final one", r.stderr); self.assertIn("wp_sites.py stop wptest", r.stderr)
        self.assertTrue(self.site.exists() and self.bk.exists() and self.final().exists() is False)
        import shutil
        shutil.rmtree(self.bk / "20261025T031500Z"); self.snapshot("20261025T041500Z", tag="pre-update")
        r = self.delete(); self.assertEqual(r.returncode, 2); self.assertIn("is not a final one", r.stderr)

    def test_a_site_with_no_snapshot_or_a_broken_final_one_is_refused(self):
        import shutil
        cases = {
            "truncated dump": dict(dump="CREATE TABLE `wp_options` (id int);\n"),
            "no wp_options": dict(dump="-- x\n-- Dump completed on 2026-10-24\n"),
            "no files": dict(files=False),
            "no manifest": dict(manifest=False),
        }
        shutil.rmtree(self.bk / STAMP)
        r = self.delete(); self.assertEqual(r.returncode, 2); self.assertIn("no snapshot", r.stderr)
        for label, kw in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.bk, ignore_errors=True); self.bk.mkdir()
                self.snapshot(STAMP, tag="final", **kw)
                r = self.delete(); self.assertNotEqual(r.returncode, 0, label); self.assertTrue(self.site.exists() and self.bk.exists()); self.assertEqual(self.calls(), [])
        shutil.rmtree(self.bk); self.bk.mkdir(); d = self.snapshot(STAMP, tag="final"); (d / "db.sql.gz").write_bytes(b"not gzip")
        r = self.delete(); self.assertNotEqual(r.returncode, 0); self.assertIn("gzip", r.stderr); self.assertEqual(self.calls(), [])
        # a dump whose data and completion marker read fine but whose CRC trailer is wrong: only gzip -t notices, and only gzip -t is checking the whole file
        shutil.rmtree(self.bk); self.bk.mkdir(); d = self.snapshot(STAMP, tag="final")
        raw = bytearray((d / "db.sql.gz").read_bytes()); raw[-8] ^= 0xFF; (d / "db.sql.gz").write_bytes(bytes(raw))
        r = self.delete(); self.assertEqual(r.returncode, 2); self.assertIn("the final snapshot's dump fails gzip's integrity check", r.stderr, "caught on the SOURCE, before any copying; the archived copy is checked again later")
        self.assertTrue(self.site.exists() and self.bk.exists()); self.assertEqual(self.calls(), []); self.assertEqual([p.name for p in self.archive.iterdir()], [], "no archive was even started")

    def test_links_in_place_of_the_directories_are_never_followed(self):
        import shutil
        real = self.tmp / "elsewhere"; real.mkdir(); (real / "precious").write_text("do not delete"); (real / "site.json").write_text("{}")   # a record seen THROUGH the link: the directory check must still refuse
        shutil.rmtree(self.site); self.site.symlink_to(real)
        r = self.delete(); self.assertEqual(r.returncode, 2); self.assertIn("not a real directory", r.stderr); self.assertTrue((real / "precious").exists())
        self.site.unlink(); self.site.mkdir(); (self.site / "stopped.flag").write_text(""); (self.site / "site.json").write_text("{}")
        shutil.rmtree(self.bk); self.bk.symlink_to(real)
        r = self.delete(); self.assertEqual(r.returncode, 2); self.assertTrue((real / "precious").exists()); self.assertEqual(self.calls(), [])

    def test_the_name_must_be_typed_and_a_wrong_answer_changes_nothing(self):
        for answer in ("other\n", "\n", "WPTEST\n", "wptest extra\n", "yes\n"):
            with self.subTest(answer):
                self.tty.write_text(answer)
                r = self.delete(); self.assertEqual(r.returncode, 2, answer); self.assertIn("not confirmed", r.stderr); self.untouched()
        self.env["WP_SITE_TTY"] = str(self.tmp / "no-such-tty")
        r = self.delete(); self.assertNotEqual(r.returncode, 0); self.untouched()

    def test_unknown_sites_bad_names_and_extra_arguments_are_refused(self):
        for args in (("nosuch",), ("../x",), ("wptest", "extra"), ("a;id",), ()):
            r = self.run_script(self.script, *args); self.assertEqual(r.returncode, 2, args)
        self.assertTrue(self.site.exists()); self.assertEqual(self.calls(), [])

    # --- failures part way
    def test_an_archive_that_cannot_be_built_or_does_not_verify_removes_nothing(self):
        self.put("tar_fail", "")
        r = self.delete(); self.assertNotEqual(r.returncode, 0); self.untouched()
        (self.tmp / "shim" / "tar_fail").unlink(); self.put("tar_corrupt", "")
        r = self.delete(); self.assertNotEqual(r.returncode, 0); self.assertIn("does not read back", r.stderr); self.untouched()
        (self.tmp / "shim" / "tar_corrupt").unlink(); self.put("gzip_archive_fail", "")
        r = self.delete(); self.assertNotEqual(r.returncode, 0); self.assertIn("fails gzip", r.stderr); self.untouched()
        (self.tmp / "shim" / "gzip_archive_fail").unlink()
        self.assertEqual(self.delete().returncode, 0, "and with the cause gone it works")

    def test_nginx_failing_without_the_vhost_puts_the_vhost_back_and_removes_no_data(self):
        original = self.vhost.read_text(); self.put("nginx_fail", "")
        r = self.delete()
        self.assertNotEqual(r.returncode, 0); self.assertIn("put back", r.stderr)
        self.assertEqual(self.vhost.read_text(), original); self.assertFalse((self.vhosts / "wp_site_wptest.conf.deleting").exists())
        self.assertTrue(self.site.exists() and self.bk.exists() and self.unit.exists(), "no data and no unit was removed")
        self.assertTrue(self.final().exists(), "the archive is kept, verified, for the next run")
        (self.tmp / "shim" / "nginx_fail").unlink(); (self.tmp / "shim" / "calls").unlink()
        r = self.delete()
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("checking it", r.stdout); self.assertFalse(self.site.exists())
        self.assertEqual(len(list(self.archive.iterdir())), 1)

    def test_a_leftover_archive_that_was_changed_since_it_was_made_is_not_trusted(self):
        self.put("nginx_fail", ""); self.assertNotEqual(self.delete().returncode, 0)   # leaves a verified archive behind and the site in place
        gz("-- a different dump\nCREATE TABLE `wp_options` (id int);\n-- Dump completed on 2026-10-24\n", self.final() / "db.sql.gz")   # valid gzip, other content
        (self.tmp / "shim" / "nginx_fail").unlink(); (self.tmp / "shim" / "calls").unlink()
        r = self.delete()
        self.assertEqual(r.returncode, 2); self.assertIn("does not match its checksum", r.stderr)
        self.assertTrue(self.site.exists() and self.bk.exists() and self.vhost.exists(), "nothing was removed on the strength of a changed archive"); self.assertEqual(self.calls(), [])
        (self.tmp / "tty").write_text("wptest\n")
        import shutil
        shutil.rmtree(self.final())   # the operator removes the bad archive; a fresh one is made
        self.assertEqual(self.delete().returncode, 0); self.assertFalse(self.site.exists())

    def test_an_interrupted_delete_resumes_and_finishes_without_asking_again(self):
        import shutil
        self.delete()   # a complete one first, to have a real archive
        self.vhost.write_text("server { }\n"); self.unit.write_text("[Unit]\n")   # as if the run stopped after the data was gone but before the vhost and unit
        self.tty.write_text("")
        r = self.delete()
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("finishing the clean-up", r.stdout)
        self.assertFalse(self.vhost.exists() or self.unit.exists())
        # ... or after the site directory but before its backups
        self.bk.mkdir(); (self.bk / "leftover").write_text("x"); self.vhost.write_text("server { }\n")
        r = self.delete()
        self.assertEqual(r.returncode, 0, r.stderr); self.assertFalse(self.bk.exists() or self.vhost.exists()); self.assertTrue(self.final().exists())
        shutil.rmtree(self.archive / ("wptest-" + STAMP))
        r = self.delete(); self.assertEqual(r.returncode, 2); self.assertIn("nothing to delete", r.stderr)
        self.bk.mkdir(); (self.bk / "keep").write_text("x")
        r = self.delete(); self.assertEqual(r.returncode, 2); self.assertTrue((self.bk / "keep").exists(), "backups with no site and no archive are not touched: a person must look")

    def test_a_project_id_that_cannot_be_read_leaves_no_limit_change_and_does_not_stop_the_delete(self):
        r = self.delete(LSATTR_SITE_ID="0")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertNotIn("setquota -P 0", "\n".join(self.calls()))

    def test_the_tool_removes_nothing_outside_the_sites_own_paths(self):
        before = {p: p.read_text() for p in [self.data / "sites/other/keep.txt", self.backups / "other/keep.txt", self.vhosts / "wp_site_other.conf", self.units / "wp-other.service"]}
        self.delete()
        self.assertEqual({p: p.read_text() for p in before}, before)
        self.assertEqual(sorted(p.name for p in (self.data / "sites").iterdir()), ["other"]); self.assertEqual(sorted(p.name for p in self.backups.iterdir()), ["other"])
        src = (ROOT / "templates/wp/wp_site_delete.sh.j2").read_text()
        rms = [l.split("   #")[0].strip() for l in src.splitlines() if "rm -r" in l.split("   #")[0] and not l.lstrip().startswith("#")]
        for l in rms:
            self.assertTrue(re.search(r'rm -rf? -- "\$\{(tmp|d):\?\}"', l) or "deleting" in l or "$vhost" in l or "$UNIT_DIR" in l or "$STATE_DIR" in l, "an rm that is not guarded: " + l)

    def test_the_file_never_calls_delete_data_outside_and_is_deployed_root_only(self):
        tasks = load("tasks/wp_tools_common.yml")
        deploy = [t for t in tasks if t.get("name") == "Stage the tools"][0]
        self.assertIn({"src": "wp_site_delete.sh.j2", "dest": "wp-site-delete"}, deploy["loop"]); self.assertIn({"src": "wp_archive_purge.sh.j2", "dest": "wp-archive-purge"}, deploy["loop"])
        send = [t for t in tasks if t.get("name") == "Send the tools"][0]["ansible.posix.synchronize"]
        self.assertIn("--chown=root:root", send["rsync_opts"]); self.assertIn("--chmod=F0750", send["rsync_opts"]); self.assertEqual(send["dest"], "{{ wp_tools_bin }}/")
        code = "\n".join(l for l in (ROOT / "templates/wp/wp_site_delete.sh.j2").read_text().splitlines() if not l.lstrip().startswith("#"))
        for bad in ("git ", "ansible", "rm -rf /", "docker restart"):
            self.assertNotIn(bad, code)


class Purge(Base):
    def setUp(self):
        super().setUp()
        self.archive = self.tmp / "data/archive"; self.archive.mkdir()
        self.script = self.install("wp_archive_purge.sh.j2", "bin/wp-archive-purge", wp_archive_dir=str(self.archive))
        (self.tmp / "state").mkdir(exist_ok=True)

    def make(self, name, expires="2026-10-31", manifest=True):
        d = self.archive / name
        d.mkdir(); (d / "db.sql.gz").write_text("x")
        if manifest:
            (d / "MANIFEST").write_text("site=x\nexpires=%s\n" % expires if expires else "site=x\n")
        return d

    def purge(self, today="2026-11-01", **env):
        return self.run_script(self.script, WP_ARCHIVE_TODAY=today, **env)

    def test_it_removes_exactly_the_archives_whose_day_has_passed(self):
        gone = self.make("old-20261001T000000Z", "2026-10-31"); same = self.make("same-20261001T000000Z", "2026-11-01"); later = self.make("later-20261001T000000Z", "2026-11-02")
        r = self.purge()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(gone.exists()); self.assertTrue(same.exists(), "the last day is still a day"); self.assertTrue(later.exists())
        self.assertEqual(len(self.log("ntfy.log").splitlines()), 1); self.assertIn("old-20261001T000000Z", self.log("ntfy.log")); self.assertNotIn("same-", self.log("ntfy.log"))
        self.assertEqual(self.purge().returncode, 0); self.assertEqual(len(self.log("ntfy.log").splitlines()), 1, "nothing to remove, nothing said")

    def test_it_leaves_alone_and_tells_once_whatever_is_not_an_archive(self):
        outside = self.tmp / "outside"; outside.mkdir(); (outside / "precious").write_text("keep")
        (outside / "MANIFEST").write_text("site=x\nexpires=2000-01-01\n")   # expired, and well formed: the only reason to leave it is that it is reached through a link
        (self.archive / "link-20261001T000000Z").symlink_to(outside)
        (self.archive / "notes.txt").write_text("x")
        self.make("Bad_Name-20261001T000000Z"); self.make("nostamp"); self.make("nomanifest-20261001T000000Z", manifest=False)
        self.make("badexpiry-20261001T000000Z", "tomorrow"); self.make("noexpiry-20261001T000000Z", ""); self.make(".partial-x-20261001T000000Z", "2020-01-01")
        r = self.purge()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((outside / "precious").exists(), "a link is never followed")
        for n in ("notes.txt", "Bad_Name-20261001T000000Z", "nostamp", "nomanifest-20261001T000000Z", "badexpiry-20261001T000000Z", "noexpiry-20261001T000000Z", ".partial-x-20261001T000000Z", "link-20261001T000000Z"):
            self.assertTrue(os.path.lexists(self.archive / n), n + " must be left alone")
        told = self.log("ntfy.log"); self.assertEqual(len(told.splitlines()), 1); self.assertIn("not archives", told); self.assertNotIn(".partial-x", told, "a delete in progress is not odd")
        self.purge(); self.assertEqual(len(self.log("ntfy.log").splitlines()), 1, "told once, not every day")

    def test_it_never_goes_outside_the_archive_directory_and_checks_what_it_is_given(self):
        other = self.tmp / "data/sites"; other.mkdir(exist_ok=True); (other / "x").write_text("site")
        self.make("old-20261001T000000Z", "2000-01-01")
        self.purge()
        self.assertTrue((other / "x").exists())
        self.assertEqual(self.purge(today="tomorrow").returncode, 2)
        import shutil
        shutil.rmtree(self.archive); (self.tmp / "real").mkdir(); self.archive.symlink_to(self.tmp / "real")
        self.assertEqual(self.purge().returncode, 2, "the archive directory itself must not be a link")

    def test_the_unit_and_the_timer_alert_on_failure_and_run_daily(self):
        tasks = load("tasks/wp_tools_common.yml"); by = {t["name"]: t for t in tasks}
        svc = by["Stage the archive purge service"]["ansible.builtin.copy"]["content"]; tim = by["Stage the archive purge timer"]["ansible.builtin.copy"]["content"]
        self.assertIn("OnFailure=wp-job-failed@%N.service", svc); self.assertIn("ExecStart={{ wp_tools_bin }}/wp-archive-purge", svc)
        self.assertIn("OnCalendar={{ wp_archive_purge_calendar }}", tim); self.assertIn("Persistent=true", tim)
        self.assertEqual(by["Enable the archive purge timer"]["ansible.builtin.systemd"]["name"], "wp-archive-purge.timer")
        self.assertRegex(self.v["wp_archive_purge_calendar"], r"^\*-\*-\* \d\d:\d\d:\d\d$")

    def test_the_verifier_reads_the_paths_from_the_repo_and_hard_codes_none(self):
        v = (ROOT / "scripts/wp_site_verify_dellwp.sh").read_text()
        self.assertNotIn("/etc/nginx/conf.d", v); self.assertNotIn("/etc/systemd/system", v)
        for needle in ('h["wp_unit_dir"]', 'h["wp_vhost_dir"]', 'h["wp_data_mount"] + "/archive"', "$UNITDIR/wp-$SITE.service", "$VHOSTDIR/wp_site_$SITE.conf"):
            self.assertIn(needle, v)
        self.assertEqual(self.v["wp_archive_dir"], self.v["wp_data_mount"] + "/archive", "the verifier derives the archive path the way the variable is built")

    def test_the_two_tools_agree_on_the_archive_layout(self):
        # wp-site-delete writes <site>-<stamp> with expires=YYYY-MM-DD, and the purge reads exactly that
        delete = (ROOT / "templates/wp/wp_site_delete.sh.j2").read_text(); purge = (ROOT / "templates/wp/wp_archive_purge.sh.j2").read_text()
        self.assertIn('echo "expires=$expires"', delete); self.assertIn("expires=$expires", delete.replace('"', ""))
        self.assertIn("s/^expires=//p", purge); self.assertIn("[0-9]{8}T[0-9]{6}Z", purge); self.assertIn('final=$ARCHIVE/$site-$newest', delete); self.assertIn("%Y-%m-%d", delete)


if __name__ == "__main__":
    unittest.main(verbosity=1)
