#!/usr/bin/env python3
"""Offline tests of the nightly update job: templates/wp/wp_site_update.sh.j2 (wp-site-update) and its units.

The tools the job calls (wp-site-cli, wp-site-ext, wp-site-backup, wp-site-recover), systemctl, curl, docker and ntfy-notify are stand-ins that append one line to an order log,
with whether the maintenance flag was up at that moment. The tests prove the job's logic: what it does for each policy, the order of the steps, that the site is in maintenance from the
snapshot until it has been proven, that any failure rolls back through wp-site-recover (and that a failed rollback keeps the site closed), and that a failed set is not retried every night.
They do not prove that WP-CLI answers the way the stand-in does (check-update's output, the csv listing), that wordpress.org serves the updates, or that nginx honours the probe port:
scripts/wp_update_rehearsal_dellwp.sh does those on the real host. Run: python3 -I tests/test_wp_update.py"""
import re
import sys
import unittest
from pathlib import Path

import jinja2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wp_backup import Base  # noqa: E402
from test_wp_sites_render import ROOT  # noqa: E402

CLI = r'''#!/bin/bash
site=$1; shift
S="$SHIM"; F=$([ -e "$SITE/maintenance.flag" ] && echo up || echo down)
echo "cli flag=$F $*" >> "$S/order"
case " $* " in
  *" check-update "*) f=core_available; case " $* " in *" --major "*) f=core_major ;; esac
    if [ -f "$S/$f" ]; then cat "$S/$f"; else echo "Success: WordPress is at the latest version."; fi ;;
  *" plugin list "*) [ -f "$S/list_fail" ] && exit 1; echo "name,version,update_version"; cat "$S/plugins.csv" 2>/dev/null; exit 0 ;;
  *" theme list "*) echo "name,version,update_version"; cat "$S/themes.csv" 2>/dev/null; exit 0 ;;
  *" core update --minor "*) [ -f "$S/core_update_fail" ] && exit 1; exit 0 ;;
  *" core verify-checksums "*) [ -f "$S/verify_fail" ] && exit 1; [ -f "$S/verify_warn" ] && echo "Warning: File should not exist: x.php"; echo "Success: verifies"; exit 0 ;;
  *" core update-db "*) [ -f "$S/db_fail" ] && exit 1; exit 0 ;;
  *" core is-installed "*) [ -f "$S/not_installed" ] && exit 1; exit 0 ;;
esac
exit 0
'''
EXT = r'''#!/bin/bash
S="$SHIM"; F=$([ -e "$SITE/maintenance.flag" ] && echo up || echo down)
echo "ext flag=$F $*" >> "$S/order"
[ -f "$S/term_in_ext" ] && kill -TERM $PPID && sleep 1
case " $* " in *" plugin "*) [ -f "$S/plugin_fail" ] && exit 1 ;; *" theme "*) [ -f "$S/theme_fail" ] && exit 1 ;; esac
exit 0
'''
BACKUP = r'''#!/bin/bash
S="$SHIM"; F=$([ -e "$SITE/maintenance.flag" ] && echo up || echo down)
echo "backup flag=$F $*" >> "$S/order"
[ -f "$S/backup_fail" ] && { echo "wp-site-backup: dump failed" >&2; exit 1; }
echo "OK: snapshot 20261025T020000Z of wptest (123 KiB on disk, core 7.1.3)"
'''
RECOVER = r'''#!/bin/bash
S="$SHIM"; F=$([ -e "$SITE/maintenance.flag" ] && echo up || echo down)
echo "recover flag=$F $*" >> "$S/order"
[ -f "$S/recover_fail" ] && exit 1
exit 0
'''
SYSTEMCTL = r'''#!/bin/bash
S="$SHIM"; F=$([ -e "$SITE/maintenance.flag" ] && echo up || echo down)
echo "systemctl flag=$F $*" >> "$S/order"
[ -f "$S/reload_fail" ] && exit 1
exit 0
'''
CURL = r'''#!/bin/bash
S="$SHIM"; F=$([ -e "$SITE/maintenance.flag" ] && echo up || echo down)
url=${@: -1}; port=${url#http://127.0.0.1:}; port=${port%%/*}
case " $* " in
  *" -w "*) echo "curl flag=$F port=$port home" >> "$S/order"; [ "$port" = "8080" ] && { cat "$S/public_code" 2>/dev/null || echo 200; } || { cat "$S/home_code" 2>/dev/null || echo 200; } ;;
  *) echo "curl flag=$F port=$port login" >> "$S/order"; [ -f "$S/login_missing" ] || echo '<input id="user_login">' ;;
esac
'''
DOCKER = r'''#!/bin/bash
echo "docker $*" >> "$SHIM/order"
case "$1" in logs) cat "$SHIM/php_log" 2>/dev/null; exit 0 ;; esac
exit 0
'''
NTFY = '#!/bin/bash\necho "$*" >> "$SHIM/ntfy.log"\n'
SLEEP = '#!/bin/bash\necho sleep >> "$SHIM/sleeps"\n'


class Update(Base):
    def setUp(self):
        super().setUp()
        self.env["SITE"] = str(self.site)
        for name, body in (("docker", DOCKER), ("curl", CURL), ("systemctl", SYSTEMCTL), ("ntfy-notify", NTFY), ("sleep", SLEEP)):
            self.shim(name, body)
        for name, body in (("wp-site-cli", CLI), ("wp-site-ext", EXT), ("wp-site-backup", BACKUP), ("wp-site-recover", RECOVER)):
            f = self.tmp / "bin" / name
            f.write_text(body); f.chmod(0o755)
        self.policy("wptest:auto")
        self.flag = self.site / "maintenance.flag"

    def policy(self, text):
        self.script = self.install("wp_site_update.sh.j2", "bin/wp-site-update", wp_update_policies=text)

    def have(self, core=None, plugins=(), themes=()):
        if core:
            self.put("core_available", core + "\n")
        for name, rows in (("plugins.csv", plugins), ("themes.csv", themes)):
            if rows:
                self.put(name, "".join(r + "\n" for r in rows))

    def run_update(self, *args, **env):
        return self.run_script(self.script, "wptest", *args, **env)

    def order(self):
        return self.log("order").splitlines()

    def steps(self, prefix):
        return [l for l in self.order() if l.startswith(prefix)]

    def idx(self, needle):
        for i, l in enumerate(self.order()):
            if needle in l:
                return i
        self.fail("%r is not in the order log:\n%s" % (needle, "\n".join(self.order())))

    def told(self):
        return self.log("ntfy.log").splitlines()

    def last(self):
        p = self.site / ".update-last"
        return dict(l.split("=", 1) for l in p.read_text().splitlines()) if p.exists() else {}

    # --- nothing to do, and the policies
    def test_nothing_available_changes_nothing_and_says_nothing(self):
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("nothing to update", r.stdout)
        self.assertFalse(self.flag.exists()); self.assertEqual(self.told(), [])
        self.assertEqual(self.steps("backup") + self.steps("recover") + self.steps("ext") + self.steps("systemctl"), [])
        self.assertEqual(self.last()["result"], "none")

    def test_notify_tells_once_per_set_and_changes_nothing(self):
        self.policy("wptest:notify"); self.have(plugins=["akismet,5.3,5.4"])
        r = self.run_update(); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.told()), 1); self.assertIn("akismet:5.3>5.4", self.told()[0]); self.assertIn("wp-site-update wptest --apply", self.told()[0])
        self.assertIn("dellwp", self.told()[0])
        self.run_update(); self.assertEqual(len(self.told()), 1, "the same set is not told again")
        self.have(plugins=["akismet,5.3,5.4", "hello-dolly,1.0,1.1"])
        self.run_update(); self.assertEqual(len(self.told()), 2, "a changed set is told")
        self.assertEqual(self.steps("backup") + self.steps("ext") + self.steps("recover"), []); self.assertFalse(self.flag.exists())

    def test_check_lists_and_changes_nothing_under_any_policy(self):
        for pol in ("auto", "notify", "freeze"):
            self.policy("wptest:" + pol); self.have(core="7.1.4", plugins=["akismet,5.3,5.4"], themes=["twentytwentysix,1.0,1.1"])
            r = self.run_update("--check")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("core 7.1.4", r.stdout); self.assertIn("plugins akismet:5.3>5.4", r.stdout); self.assertIn("themes twentytwentysix:1.0>1.1", r.stdout)
        self.assertEqual(self.told(), []); self.assertEqual(self.steps("backup") + self.steps("ext"), []); self.assertFalse(self.flag.exists())

    def test_a_freeze_flag_or_the_freeze_policy_skips_and_apply_overrides_only_the_policy(self):
        self.have(plugins=["akismet,5.3,5.4"])
        for freeze in (self.tmp / "freeze", self.site / "freeze"):
            freeze.write_text("")
            for args in ((), ("--apply",)):
                r = self.run_update(*args)
                self.assertEqual(r.returncode, 0); self.assertIn("frozen", r.stdout)
                self.assertEqual(self.steps("backup") + self.steps("cli"), [], "a freeze flag wins over --apply")
            freeze.unlink()
        self.policy("wptest:freeze")
        r = self.run_update(); self.assertIn("policy freeze", r.stdout); self.assertEqual(self.steps("cli"), [])
        self.assertEqual(self.run_update("--apply").returncode, 0); self.assertTrue(self.steps("backup"), "--apply is the operator choosing to update")

    def test_apply_updates_a_notify_site(self):
        self.policy("wptest:notify"); self.have(plugins=["akismet,5.3,5.4"])
        self.assertEqual(self.run_update("--apply").returncode, 0)
        self.assertTrue(self.steps("backup")); self.assertIn("ext flag=up wptest plugin update akismet", self.order()[self.idx("ext flag=up wptest plugin update akismet")])

    # --- applying
    def test_a_good_update_is_snapshot_then_update_then_proof_then_reopen_in_that_order(self):
        self.have(core="7.1.4", plugins=["akismet,5.3,5.4", "woo-thing,1.0,1.1"], themes=["twentytwentysix,1.0,1.1"])
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        o = self.order()
        self.assertEqual(self.steps("backup"), ["backup flag=up wptest --tag=pre-update"], "the snapshot is tagged and taken with the flag already up")
        marks = ["backup flag=up", "cli flag=up --code core update --minor", "cli flag=up --code core verify-checksums", "ext flag=up wptest plugin update akismet woo-thing",
                 "ext flag=up wptest theme update twentytwentysix", "cli flag=up core update-db", "systemctl flag=up reload wp-wptest.service", "curl flag=up port=8081 home", "curl flag=up port=8081 login",
                 "cli flag=up core is-installed", "curl flag=down port=8080 home"]
        at = [self.idx(m) for m in marks]
        self.assertEqual(at, sorted(at), "the steps are out of order:\n" + "\n".join(o))
        self.assertFalse(self.flag.exists()); self.assertEqual(self.steps("recover"), [])
        self.assertEqual(len(self.told()), 1); self.assertIn("was updated and checked", self.told()[0]); self.assertIn("20261025T020000Z", self.told()[0])
        self.assertEqual(self.last()["result"], "updated")

    def test_core_is_not_touched_when_only_plugins_are_available_and_the_database_upgrade_runs_only_with_core(self):
        self.have(plugins=["akismet,5.3,5.4"])
        self.assertEqual(self.run_update().returncode, 0)
        self.assertEqual(self.steps("cli flag=up --code core update"), []); self.assertEqual(self.steps("cli flag=up core update-db"), [])
        self.assertTrue(self.steps("ext"))

    def test_a_name_that_is_not_a_slug_is_never_passed_on_and_is_told_once(self):
        self.have(plugins=["Some_Plugin,1.0,1.1"])
        r = self.run_update(); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.steps("backup") + self.steps("ext"), [], "nothing to apply: no snapshot, no maintenance")
        self.assertEqual(len(self.told()), 1); self.assertIn("by hand", self.told()[0]); self.assertIn("plugin:Some_Plugin", self.told()[0])
        self.run_update(); self.assertEqual(len(self.told()), 1)
        self.have(plugins=["akismet,5.3,5.4", "Some_Plugin,1.0,1.1"])
        self.run_update()
        self.assertIn("ext flag=up wptest plugin update akismet", "\n".join(self.order())); self.assertNotIn("Some_Plugin", "\n".join(self.steps("ext")))

    # --- majors: held under auto, applied by --apply, reported for core
    def applied(self):
        return [l.split("plugin update ")[1] for l in self.steps("ext") if "plugin update" in l]

    def test_auto_applies_minor_updates_and_holds_a_major_one_and_tells_once(self):
        self.have(plugins=["akismet,5.3,5.4", "woo-thing,10.2,11.0"])
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.applied(), ["akismet"], "only the minor update goes through")
        held = [t for t in self.told() if "holds for you" in t]
        self.assertEqual(len(held), 1); self.assertIn("plugin:woo-thing:10.2>11.0[MAJOR]", held[0]); self.assertIn("wp-site-update wptest --apply", held[0])
        self.assertNotIn("akismet", held[0])
        self.run_update()
        self.assertEqual(len([t for t in self.told() if "holds for you" in t]), 1, "the same held set is not told again")

    def test_only_majors_under_auto_changes_nothing_and_does_not_even_take_the_site_offline(self):
        self.have(plugins=["woo-thing,10.2,11.0"], themes=["twentytwentysix,1.4,2.0"])
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.steps("backup") + self.steps("ext") + self.steps("recover") + self.steps("systemctl"), []); self.assertFalse(self.flag.exists())
        self.assertEqual(len(self.told()), 1); self.assertIn("theme:twentytwentysix:1.4>2.0[MAJOR]", self.told()[0])
        self.assertEqual(self.last()["result"], "held-only")
        self.run_update(); self.assertEqual(len(self.told()), 1)
        self.have(plugins=["woo-thing,10.2,12.0"], themes=["twentytwentysix,1.4,2.0"])
        self.run_update(); self.assertEqual(len(self.told()), 2, "a changed held set is told")

    def test_apply_applies_majors_too_and_says_nothing_is_held(self):
        self.have(plugins=["akismet,5.3,5.4", "woo-thing,10.2,11.0"], themes=["twentytwentysix,1.4,2.0"])
        self.assertEqual(self.run_update("--apply").returncode, 0)
        self.assertEqual(self.applied(), ["akismet woo-thing"]); self.assertTrue([l for l in self.steps("ext") if "theme update twentytwentysix" in l])
        self.assertEqual([t for t in self.told() if "holds for you" in t], [])

    def test_notify_and_check_list_everything_with_the_majors_marked_and_hold_nothing(self):
        self.have(plugins=["akismet,5.3,5.4", "woo-thing,10.2,11.0"])
        r = self.run_update("--check")
        self.assertIn("akismet:5.3>5.4", r.stdout); self.assertIn("woo-thing:10.2>11.0[MAJOR]", r.stdout); self.assertNotIn("akismet:5.3>5.4[MAJOR]", r.stdout)
        self.assertEqual(self.told(), [])
        self.policy("wptest:notify")
        self.run_update()
        self.assertEqual(len(self.told()), 1); self.assertIn("woo-thing:10.2>11.0[MAJOR]", self.told()[0]); self.assertIn("akismet:5.3>5.4", self.told()[0])
        self.assertEqual(self.steps("backup") + self.steps("ext"), [])

    def test_what_counts_as_a_major_version(self):
        cases = [("1.6.3", "1.7.0", False), ("1.0", "1.0.1", False), ("10.2", "10.3", False), ("09.1", "9.2", False), ("1.0-beta", "1.0", False), ("0.5", "1.7.0", True),
                 ("5.9.9", "6.0", True), ("2024.1", "2025.1", True), ("9.9", "10.0", True), ("v2.0", "v2.1", True), ("1.0", "dev-trunk", True), ("", "1.0", True)]
        for old, new, major in cases:
            with self.subTest(old=old, new=new):
                for n in ("order", "ntfy.log"):
                    (self.tmp / "shim" / n).unlink(missing_ok=True)
                self.have(plugins=["akismet,%s,%s" % (old, new)])
                self.run_update()
                if major:
                    self.assertEqual(self.applied(), [], "a major is held"); self.assertEqual(len([t for t in self.told() if "holds for you" in t]), 1)
                else:
                    self.assertEqual(self.applied(), ["akismet"], "a minor goes through")

    def test_a_major_core_release_is_told_once_per_release_and_never_applied(self):
        self.put("core_major", "7.2\n")
        r = self.run_update(); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.told()), 1); self.assertIn("WordPress 7.2 is available for wptest", self.told()[0]); self.assertIn("never applies", self.told()[0])
        self.assertIn("wp-site-backup wptest", self.told()[0]); self.assertIn("wp-site-cli wptest --code core update", self.told()[0])
        self.assertEqual([l for l in self.steps("cli") if "core update" in l], [], "no core update of any kind")
        self.assertEqual(self.steps("backup"), []); self.assertFalse(self.flag.exists())
        self.run_update(); self.assertEqual(len(self.told()), 1, "once per release")
        self.put("core_major", "7.3\n"); self.run_update(); self.assertEqual(len(self.told()), 2)
        self.assertIn("7.3", self.told()[1])

    def test_the_major_core_notice_comes_with_a_normal_minor_update_and_not_from_check_apply_or_freeze(self):
        self.have(core="7.1.4", plugins=["akismet,5.3,5.4"]); self.put("core_major", "7.2\n")
        self.assertEqual(self.run_update().returncode, 0)
        self.assertEqual(len([t for t in self.told() if "WordPress 7.2" in t]), 1)
        self.assertTrue(self.steps("cli flag=up --code core update --minor"), "the minor core update still runs")
        for n in ("ntfy.log",):
            (self.tmp / "shim" / n).unlink(missing_ok=True)
        (self.site / ".update-core-major").unlink()
        r = self.run_update("--check"); self.assertIn("core 7.2 is a major release", r.stdout); self.assertEqual(self.told(), [])
        self.run_update("--apply"); self.assertEqual([t for t in self.told() if "WordPress 7.2" in t], [], "--apply is the operator's own run")
        self.policy("wptest:freeze"); self.run_update(); self.assertEqual([t for t in self.told() if "WordPress 7.2" in t], [])

    def test_the_failed_set_is_remembered_by_what_was_applied_not_by_what_was_held(self):
        self.have(plugins=["akismet,5.3,5.4", "woo-thing,10.2,11.0"]); self.put("plugin_fail", "")
        self.assertEqual(self.run_update().returncode, 1)
        (self.tmp / "shim" / "order").unlink()
        self.have(plugins=["akismet,5.3,5.4", "woo-thing,10.2,12.0"])   # only the held major changed
        r = self.run_update(); self.assertEqual(r.returncode, 0); self.assertIn("not retrying", r.stdout); self.assertEqual(self.steps("backup"), [])

    # --- failures roll back
    def assert_rolled_back(self, r, why):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.steps("recover"), ["recover flag=up wptest 20261025T020000Z --yes"], "recover is called with the pre-update snapshot, the flag still up")
        self.assertFalse(self.flag.exists(), "the flag comes down after a good rollback")
        self.assertEqual(len(self.told()), 1); self.assertIn("ROLLED BACK", self.told()[0]); self.assertIn(why, self.told()[0])
        self.assertEqual(self.last()["result"], "rolled-back"); self.assertTrue((self.site / ".update-failed").exists())

    def test_every_failing_step_rolls_back_and_names_itself(self):
        cases = [("core_update_fail", "core update to 7.1.4 failed"), ("verify_fail", "core does not verify"), ("verify_warn", "core does not verify"),
                 ("plugin_fail", "plugin update failed"), ("theme_fail", "theme update failed"), ("db_fail", "database upgrade failed"), ("reload_fail", "php did not come back"),
                 ("not_installed", "not healthy"), ("home_code", "not healthy"), ("login_missing", "not healthy")]
        for flagfile, why in cases:
            with self.subTest(flagfile):
                for n in ("order", "ntfy.log", "sleeps", "core_update_fail", "verify_fail", "verify_warn", "plugin_fail", "theme_fail", "db_fail", "reload_fail", "not_installed", "home_code", "login_missing"):
                    (self.tmp / "shim" / n).unlink(missing_ok=True)
                for n in (".update-failed", ".update-last", ".update-notified"):
                    (self.site / n).unlink(missing_ok=True)
                self.have(core="7.1.4", plugins=["akismet,5.3,5.4"], themes=["t,1.0,1.1"])
                self.put(flagfile, "500\n" if flagfile == "home_code" else "")
                self.assert_rolled_back(self.run_update(), why)

    def test_the_probe_tries_three_times_with_waits_and_a_php_fatal_error_fails_it(self):
        self.have(plugins=["akismet,5.3,5.4"]); self.put("php_log", "[25-Oct-2026 02:01:00] PHP Fatal error:  Uncaught Error in /var/www/html/wp-content/plugins/akismet/x.php\n")
        r = self.run_update()
        self.assert_rolled_back(r, "not healthy")
        self.assertEqual(len([l for l in self.order() if l.startswith("curl") and "port=8081 home" in l]), 3)
        self.assertEqual(self.log("sleeps").count("sleep"), 2, "10 s between the tries, none after the last")
        self.assertIn("logs --since", "\n".join(self.steps("docker")))

    def test_the_rehearsal_switch_fails_the_probe(self):
        self.have(plugins=["akismet,5.3,5.4"])
        self.assert_rolled_back(self.run_update(WP_UPDATE_FAIL_PROBE="1"), "not healthy")

    def test_a_failed_rollback_keeps_the_site_in_maintenance_and_says_where_the_snapshot_is(self):
        self.have(plugins=["akismet,5.3,5.4"]); self.put("plugin_fail", ""); self.put("recover_fail", "")
        r = self.run_update()
        self.assertEqual(r.returncode, 1)
        self.assertTrue(self.flag.exists(), "a site that could not be rolled back must not serve visitors")
        self.assertEqual(len(self.told()), 1); self.assertIn("ROLLBACK FAILED", self.told()[0]); self.assertIn("20261025T020000Z", self.told()[0]); self.assertIn("wp-site-recover wptest 20261025T020000Z", self.told()[0])
        self.assertEqual(self.last()["result"], "rollback-failed")

    def test_a_failed_snapshot_stops_before_anything_changes(self):
        self.have(core="7.1.4", plugins=["akismet,5.3,5.4"]); self.put("backup_fail", "")
        r = self.run_update()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.steps("ext") + self.steps("recover") + [l for l in self.steps("cli") if "update" in l and "check-update" not in l and "list" not in l], [], "no update, and nothing to roll back")
        self.assertFalse(self.flag.exists()); self.assertIn("stopped before it changed anything", self.told()[0]); self.assertEqual(self.last()["result"], "aborted")

    def test_a_signal_mid_update_rolls_back(self):
        self.have(plugins=["akismet,5.3,5.4"]); self.put("term_in_ext", "")
        r = self.run_update()
        self.assertEqual(r.returncode, 143, r.stdout + r.stderr)
        self.assertEqual(self.steps("recover"), ["recover flag=up wptest 20261025T020000Z --yes"]); self.assertFalse(self.flag.exists()); self.assertIn("ROLLED BACK", self.told()[0])

    def test_a_public_path_failure_after_reopening_is_reported_and_not_rolled_back(self):
        self.have(plugins=["akismet,5.3,5.4"]); self.put("public_code", "502\n")
        r = self.run_update()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.steps("recover"), [], "visitors may have written since: only the operator decides"); self.assertFalse(self.flag.exists())
        self.assertIn("applied and the site reopened", self.told()[0]); self.assertIn("20261025T020000Z", self.told()[0]); self.assertEqual(self.last()["result"], "reopened-unhealthy")

    # --- a failed set is not retried every night
    def test_a_rolled_back_set_is_not_retried_until_it_changes_or_the_operator_says_so(self):
        self.have(plugins=["akismet,5.3,5.4"]); self.put("plugin_fail", "")
        self.assertEqual(self.run_update().returncode, 1)
        (self.tmp / "shim" / "order").unlink(); (self.tmp / "shim" / "ntfy.log").unlink()
        r = self.run_update()
        self.assertEqual(r.returncode, 0); self.assertIn("not retrying", r.stdout); self.assertEqual(self.steps("backup"), []); self.assertEqual(self.told(), [])
        self.have(plugins=["akismet,5.3,5.5"])
        self.assertEqual(self.run_update().returncode, 1); self.assertTrue(self.steps("backup"), "a different set is tried")
        (self.tmp / "shim" / "order").unlink()
        self.assertEqual(self.run_update("--apply").returncode, 1); self.assertTrue(self.steps("backup"), "--apply retries")
        (self.tmp / "shim" / "plugin_fail").unlink()
        self.assertEqual(self.run_update("--apply").returncode, 0)
        self.assertFalse((self.site / ".update-failed").exists(), "a success clears the failure marker")

    # --- guards
    def test_a_stopped_site_is_refused_in_every_mode_before_anything_is_asked_of_it(self):
        (self.site / "stopped.flag").write_text("")
        self.have(plugins=["akismet,5.3,5.4"])
        for args in ((), ("--check",), ("--apply",)):
            r = self.run_update(*args)
            self.assertEqual(r.returncode, 2, args); self.assertIn("is stopped", r.stderr)
        self.assertEqual(self.order(), []); self.assertFalse(self.flag.exists()); self.assertEqual(self.told(), [])

    def test_only_one_update_runs_at_a_time_and_bad_arguments_are_refused(self):
        import fcntl
        self.have(plugins=["akismet,5.3,5.4"])
        with open(self.site / ".update.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            r = self.run_update()
        self.assertEqual(r.returncode, 2); self.assertIn("another update", r.stderr); self.assertEqual(self.steps("backup"), [])
        for args in (("--now",), ("--check", "--apply", "x"), ("extra",)):
            self.assertEqual(self.run_update(*args).returncode, 2, args)
        for site in ("nosuch", "../etc", "a;id"):
            self.assertEqual(self.run_script(self.script, site).returncode, 2, site)
        self.policy("wptest:sometimes")
        self.assertEqual(self.run_update().returncode, 2)
        self.policy("other:auto")
        self.assertEqual(self.run_update().returncode, 2, "a site with no policy in the list")
        self.assertEqual(self.steps("backup"), [])

    # --- what the files promise
    def test_the_units_alert_on_failure_never_run_late_and_have_a_hard_limit(self):
        svc = (ROOT / "templates/wp/wp_update.service.j2").read_text(); tim = (ROOT / "templates/wp/wp_update.timer.j2").read_text()
        self.assertIn("OnFailure=wp-job-failed@%N.service", svc); self.assertIn("TimeoutStartSec={{ wp_update_timeout_s }}", svc); self.assertIn("ExecStart={{ wp_tools_bin }}/wp-site-update %i", svc)
        self.assertIn("Persistent=false", tim, "a missed night is skipped, not run at a random hour"); self.assertIn("OnCalendar={{ wp_update_calendar }}", tim)
        self.assertLess(self.v["wp_update_timeout_s"] + 2 * 3600 + 300, 3 * 3600 + 15 * 60, "start 02:00 plus the jitter plus the limit must end before the 03:15 backup")
        self.assertLessEqual(2 * 3600 + self.v["wp_update_jitter_s"] + self.v["wp_update_timeout_s"], 3 * 3600 + 15 * 60 - 5 * 60)

    def test_the_tool_the_units_and_the_timer_are_deployed_and_the_policies_come_from_the_sites(self):
        tasks = yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())
        by = {t["name"]: t for t in tasks}
        tool = by["Deploy the tools"]
        self.assertIn({"src": "wp_site_update.sh.j2", "dest": "wp-site-update"}, tool["loop"])
        units = {i["dest"] for i in by["Deploy the WordPress job units"]["loop"]}
        self.assertTrue({"wp-update@.service", "wp-update@.timer"} <= units)
        sites = [{"name": "a", "update_policy": "auto"}, {"name": "b"}, {"name": "c", "update_policy": "freeze"}]
        got = jinja2.Environment().from_string(tool["vars"]["wp_update_policies"]).render(wp_sites=sites, wp_site_defaults=self.v["wp_site_defaults"]).strip()
        self.assertEqual(got, "a:auto b:%s c:freeze" % self.v["wp_site_defaults"]["update_policy"])
        self.assertEqual(self.v["wp_site_defaults"]["update_policy"], "notify", "the default is the safe one")
        one = (ROOT / "tasks/wp_site_one_common.yml").read_text()
        self.assertIn('name: "wp-update@{{ wp_s.name }}.timer"', one)
        common = yaml.safe_load((ROOT / "tasks/wp_sites_common.yml").read_text())
        asserts = [a for t in common if "ansible.builtin.assert" in t for a in t["ansible.builtin.assert"]["that"]]
        self.assertTrue([a for a in asserts if "update_policy" in a and "'auto', 'notify', 'freeze'" in a], "the play refuses an unknown policy")

    def test_the_script_never_touches_wp_config_or_secrets_and_never_restarts_with_docker(self):
        text = (ROOT / "templates/wp/wp_site_update.sh.j2").read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for bad in ("docker restart", "docker stop", ".env", "MARIADB", "cat \"$dir/wp-config"):
            self.assertNotIn(bad, code)
        self.assertEqual(len(re.findall(r"\$WP_BIN/wp-site-recover", code)), 1, "one rollback path")


if __name__ == "__main__":
    unittest.main(verbosity=1)
