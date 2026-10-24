#!/usr/bin/env python3
"""Offline tests of the in-place recover: templates/wp/wp_site_recover.sh.j2 (wp-site-recover).

docker, systemctl and curl are stand-ins, but the database is a file the stand-in docker reads and writes (dump, drop, load), so the tests can say what the database holds before
and after, and the real filesystem holds wp-content, the snapshot, the undo material and the maintenance flag. They prove the order of the steps, the transaction (every failure
after the swap puts the old state back; a failed undo leaves the site in maintenance), the checks that run before anything is touched, and the core version handling. They do
not prove that MariaDB loads a real dump, that `systemctl reload` recreates php, that WP-CLI downloads and verifies core, or that nginx answers 503 under the flag:
scripts/wp_recover_rehearsal_dellwp.sh does those against the real thing. Run: python3 -I tests/test_wp_recover.py"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wp_backup import Base, gz  # noqa: E402
from test_wp_sites_render import ROOT  # noqa: E402

ORIGINAL_DB = "ORIGINAL-DB\n-- Dump completed on 2026-10-25\n"
SNAPSHOT_DB = "-- MariaDB dump\nCREATE TABLE `wp_options` (id int);\nSNAPSHOT-DB\n-- Dump completed on 2026-10-24\n"
DOCKER = r'''#!/bin/bash
echo "$*" >> "$SHIM/docker.calls"
args=" $* "
case "$1" in
  inspect) [ -f "$SHIM/db_unhealthy" ] && echo starting || echo healthy; exit 0 ;;
  stop) echo stop >> "$SHIM/order"; exit 0 ;;
  network) exit 0 ;;
  exec)
    case "$args" in
      *mariadb-dump*) echo dump >> "$SHIM/order"; [ -f "$SHIM/dump_fail" ] && exit 1; cat "$SHIM/db_current"; exit 0 ;;
      *" -i "*)
        in=$(cat)
        case "$in" in
          *information_schema*) printf 'wp_options\nwp_posts\n'; exit 0 ;;
          *"DROP TABLE"*) echo drop >> "$SHIM/order"; : > "$SHIM/db_current"; exit 0 ;;
          *) echo load >> "$SHIM/order"
             if [ -f "$SHIM/load_fail_once" ]; then rm -f "$SHIM/load_fail_once"; exit 1; fi
             [ -f "$SHIM/load_fail" ] && exit 1
             printf '%s\n' "$in" > "$SHIM/db_current"; exit 0 ;;
        esac ;;
    esac ;;
  run)
    case "$args" in
      *" --entrypoint cat "*) printf "<?php\n\$wp_version = '%s';\n" "${IMAGE_CORE:-7.1.3}"; exit 0 ;;
      *" --entrypoint sh "*)
        echo reseed >> "$SHIM/order"; [ -f "$SHIM/reseed_fail" ] && exit 1
        mkdir -p "$SITE/html/wp-includes"; printf "<?php\n\$wp_version = '%s';\n" "${IMAGE_CORE:-7.1.3}" > "$SITE/html/wp-includes/version.php"; exit 0 ;;
      *" core is-installed "*) [ -f "$SHIM/wp_not_installed" ] && exit 1; exit 0 ;;
      *" user list "*) echo "${SHIM_USERS:-2}"; exit 0 ;;
      *" core verify-checksums "*) echo verify >> "$SHIM/order"; [ -f "$SHIM/verify_fail" ] && exit 1; [ -f "$SHIM/verify_warn" ] && echo "Warning: File should not exist: x.php"; exit 0 ;;
      *" core download "*)
        echo "download $(echo "$args" | sed -n 's/.*--version=\([0-9.]*\).*/\1/p') root=$(case "$args" in *"--user 0:0"*) echo yes;; *) echo no;; esac)" >> "$SHIM/order"
        [ -f "$SHIM/core_fail" ] && exit 1
        v=$(echo "$args" | sed -n 's/.*--version=\([0-9.]*\).*/\1/p')
        printf "<?php\n\$wp_version = '%s';\n" "$v" > "$SITE/html/wp-includes/version.php"; exit 0 ;;
    esac ;;
esac
exit 0
'''
SYSTEMCTL = '#!/bin/bash\necho "$*" >> "$SHIM/systemctl.calls"\necho "$1" >> "$SHIM/order"\n'
CURL = r'''#!/bin/bash
url=${@: -1}; port=${url#http://127.0.0.1:}; port=${port%%/*}
echo "flag=$([ -e "$SITE/maintenance.flag" ] && echo up || echo down) port=$port" >> "$SHIM/curl.log"
case " $* " in
  *" -w "*) if [ "$port" = 8081 ]; then [ -f "$SHIM/http_fail" ] && echo 500 || echo 200; else [ -f "$SHIM/public_fail" ] && echo 502 || echo 200; fi ;;
  *wp-login.php*) echo '<input id="user_login" name="log">' ;;
esac
'''
CP = r'''#!/bin/bash
[ -f "$SHIM/cp_fail" ] && { echo "cp: error writing: Disk quota exceeded" >&2; exit 1; }
exec /usr/bin/cp "$@"
'''


class Recover(Base):
    def setUp(self):
        super().setUp()
        for name, body in (("docker", DOCKER), ("systemctl", SYSTEMCTL), ("curl", CURL), ("cp", CP), ("sleep", "#!/bin/bash\nexit 0\n")):
            self.shim(name, body)
        self.env["SITE"] = str(self.site)
        self.install("wp_site_cli.sh.j2", "bin/wp-site-cli")
        self.script = self.install("wp_site_recover.sh.j2", "bin/wp-site-recover")
        self.flag = self.site / "maintenance.flag"
        self.undo = self.site / ".recover-undo"
        self.wpc = self.site / "html/wp-content"
        (self.wpc / "uploads/live-only.jpg").write_text("live")
        (self.site / "html/wp-config-sample.php").write_text("<?php // official sample\n")
        self.put("db_current", ORIGINAL_DB)
        self.snap("20261024T201500Z")

    def snap(self, stamp, core="7.1.3", dump=SNAPSHOT_DB):
        d = self.backups / "wptest" / stamp
        (d / "files/uploads").mkdir(parents=True)
        (d / "files/uploads/snap.jpg").write_text("snapshot")
        (d / "files/plugins/p").mkdir(parents=True)
        (d / "files/plugins/p/p.php").write_text("<?php // snapshot version\n")
        (d / "MANIFEST").write_text("site=wptest\nstamp=%s\ncore=%s\n" % (stamp, core))
        gz(dump, d / "db.sql.gz")
        return d

    def order(self):
        return self.log("order").splitlines()

    def db(self):
        return self.log("db_current").strip()

    def core(self):
        return re.search(r"'([^']+)'", (self.site / "html/wp-includes/version.php").read_text()).group(1)

    def recover(self, *args, **env):
        return self.run_script(self.script, "wptest", *args, **env)

    def assert_untouched(self, r=None):
        self.assertEqual(self.db(), ORIGINAL_DB.strip(), "the database must be as it was")
        self.assertTrue((self.wpc / "uploads/live-only.jpg").exists(), "wp-content must be as it was")
        self.assertFalse(self.flag.exists()); self.assertFalse(self.undo.exists())

    # --- the good path
    def test_a_recover_replaces_the_database_and_wp_content_and_cleans_up(self):
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK: wptest is back to snapshot 20261024T201500Z (core 7.1.3)", r.stdout)
        self.assertIn("SNAPSHOT-DB", self.db()); self.assertNotIn("ORIGINAL-DB", self.db())
        self.assertTrue((self.wpc / "uploads/snap.jpg").exists())
        self.assertFalse((self.wpc / "uploads/live-only.jpg").exists(), "what the snapshot does not have must be gone, not merged")
        self.assertEqual((self.wpc / "plugins/p/p.php").read_text(), "<?php // snapshot version\n")
        self.assertFalse(self.flag.exists(), "the flag this run raised comes down"); self.assertFalse(self.undo.exists(), "the undo material goes after a success")

    def test_the_steps_run_in_the_order_that_keeps_the_site_safe(self):
        self.recover("--yes")
        # php stops first, the current database is dumped for the undo, the tables are dropped and the snapshot loaded, php is recreated by the unit's reload, and only then is core proven
        self.assertEqual(self.order(), ["stop", "dump", "drop", "load", "reload"])
        self.assertEqual(self.log("systemctl.calls").strip(), "reload wp-wptest.service")

    def test_the_newest_snapshot_is_used_or_the_one_named(self):
        self.snap("20261025T201500Z", dump=SNAPSHOT_DB.replace("SNAPSHOT-DB", "NEWER-DB"))
        self.recover("--yes"); self.assertIn("NEWER-DB", self.db())
        self.put("db_current", ORIGINAL_DB)
        self.recover("20261024T201500Z", "--yes"); self.assertIn("SNAPSHOT-DB", self.db())

    def test_a_stopped_site_is_refused_before_anything_is_touched(self):
        (self.site / "stopped.flag").write_text("")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 2); self.assertIn("is stopped", r.stderr)
        self.assert_untouched(); self.assertEqual(self.order(), [])

    # --- consent
    def test_at_a_terminal_the_site_name_must_be_typed_and_a_wrong_answer_changes_nothing(self):
        tty = self.tmp / "tty"
        tty.write_text("wrongname\n")
        r = self.recover(WP_SITE_TTY=str(tty))
        self.assertEqual(r.returncode, 2); self.assertIn("not confirmed", r.stderr)
        self.assertEqual(self.log("docker.calls").count("stop"), 0, "php must not be stopped"); self.assert_untouched()
        tty.write_text("wptest\n")
        r = self.recover(WP_SITE_TTY=str(tty))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn("SNAPSHOT-DB", self.db())

    def test_without_yes_and_without_a_terminal_nothing_happens(self):
        r = self.recover(WP_SITE_TTY=str(self.tmp / "no-such-tty"))
        self.assertNotEqual(r.returncode, 0); self.assert_untouched()

    # --- the checks before anything is touched
    def test_a_bad_snapshot_or_a_sick_database_is_refused_before_php_is_stopped(self):
        bad = {
            "truncated dump": lambda d: gz("CREATE TABLE `wp_options` (id int);\n", d / "db.sql.gz"),
            "empty database": lambda d: gz("-- x\n-- Dump completed on 2026-10-24\n", d / "db.sql.gz"),
            "no manifest": lambda d: (d / "MANIFEST").unlink(),
            "no core in the manifest": lambda d: (d / "MANIFEST").write_text("site=wptest\n"),
            "a core that is not a version": lambda d: (d / "MANIFEST").write_text("core=7.1.3;id\n"),
            "no files": lambda d: __import__("shutil").rmtree(d / "files"),
            "corrupt gzip": lambda d: (d / "db.sql.gz").write_bytes(b"not gzip"),
        }
        for name, damage in bad.items():
            with self.subTest(name):
                d = self.snap("20261026T000000Z"); damage(d)
                r = self.recover("--yes")
                self.assertEqual(r.returncode, 2, name + r.stderr)
                self.assertNotIn("stop", self.order()); self.assert_untouched()
                __import__("shutil").rmtree(d)
        self.put("db_unhealthy", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 2); self.assertIn("not healthy", r.stderr); self.assertNotIn("stop", self.order()); self.assert_untouched()

    def test_arguments_and_names_are_checked_and_a_leftover_undo_directory_blocks_a_second_run(self):
        for args in (("nosuch", "--yes"), ("../x", "--yes")):
            self.assertEqual(self.run_script(self.script, *args).returncode, 2, args)
        for extra in ("yesterday", "../x", "20261024T201500Z;id", "--force"):
            self.assertEqual(self.recover(extra, "--yes").returncode, 2, extra)
        self.assertEqual(self.recover("20250101T000000Z", "--yes").returncode, 2, "a snapshot that does not exist")
        self.assertEqual(self.order(), [])
        self.undo.mkdir()
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 2); self.assertIn("an earlier recover died", r.stderr); self.assertEqual(self.order(), [])
        self.assertTrue(self.undo.exists(), "the leftover is not touched")

    # --- the transaction
    def test_a_dump_that_cannot_be_loaded_puts_the_old_site_back(self):
        self.put("load_fail_once", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("rolled back", r.stderr); self.assertIn("the snapshot's dump did not load", r.stderr)
        self.assert_untouched()
        self.assertEqual(self.order()[-1], "reload", "php is recreated after the undo")
        self.assertEqual(self.order().count("load"), 2, "the snapshot's load failed, the undo's load worked")

    def test_a_copy_that_runs_out_of_quota_puts_the_old_site_back(self):
        self.put("cp_fail", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("could not copy the snapshot's wp-content", r.stderr); self.assertIn("rolled back", r.stderr)
        self.assert_untouched()

    def test_a_site_that_does_not_boot_or_has_no_users_after_the_swap_is_rolled_back(self):
        self.put("wp_not_installed", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("does not boot", r.stderr); self.assert_untouched()
        (self.tmp / "shim" / "wp_not_installed").unlink()
        r = self.recover("--yes", SHIM_USERS="0")
        self.assertEqual(r.returncode, 1); self.assertIn("no users", r.stderr); self.assert_untouched()

    def test_when_the_undo_fails_too_the_site_stays_in_maintenance_and_says_where_the_material_is(self):
        self.put("load_fail", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT ROLLED BACK", r.stderr); self.assertIn(str(self.undo), r.stderr)
        self.assertTrue(self.flag.exists(), "a site in an unknown state must not serve visitors")
        self.assertTrue((self.undo / "db.sql.gz").exists(), "the old database is still there to load by hand")
        self.assertTrue((self.wpc / "uploads/live-only.jpg").exists(), "wp-content was put back before the database undo failed")

    def test_a_failed_dump_for_the_undo_stops_before_anything_is_replaced(self):
        self.put("dump_fail", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("could not keep the current state", r.stderr)
        self.assert_untouched(); self.assertNotIn("drop", self.order()); self.assertEqual(self.order()[-1], "reload", "php, which was stopped, is started again")

    def test_no_undo_skips_the_dump_and_a_failure_then_cannot_be_undone(self):
        r = self.recover("--yes", "--no-undo")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertNotIn("dump", self.order()); self.assertIn("SNAPSHOT-DB", self.db())
        self.put("db_current", ORIGINAL_DB); self.put("load_fail", "")
        r = self.recover("--yes", "--no-undo")
        self.assertEqual(r.returncode, 1); self.assertIn("NOT ROLLED BACK", r.stderr); self.assertTrue(self.flag.exists())

    def test_the_site_is_proven_on_the_probe_port_while_the_flag_is_still_up_and_opened_only_then(self):
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.log("curl.log").splitlines()
        self.assertEqual(calls[:2], ["flag=up port=8081", "flag=up port=8081"], "home and login, asked while the site is still closed")
        self.assertEqual(calls[2:], ["flag=down port=8080"], "then the flag comes down and the visitor port is asked once")

    def test_an_http_failure_on_the_probe_port_keeps_the_site_closed_and_the_previous_state(self):
        # the snapshot is in place and WordPress booted: it is not rolled back, but it is not opened either, and what it replaced is kept for the operator
        self.put("http_fail", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("probe port", r.stderr); self.assertNotIn("rolled back", r.stderr)
        self.assertIn("SNAPSHOT-DB", self.db()); self.assertTrue(self.flag.exists(), "not proven, so not opened")
        self.assertTrue((self.undo / "db.sql.gz").exists() and (self.undo / "wp-content/uploads/live-only.jpg").exists(), "the previous state is kept")
        self.assertIn(str(self.undo), r.stderr)
        self.assertEqual(self.log("curl.log").count("port=8080"), 0, "the visitor port is not asked for a site that did not prove itself")

    def test_a_visitor_port_failure_after_opening_is_reported_and_keeps_the_previous_state(self):
        self.put("public_fail", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("visitor port", r.stderr); self.assertNotIn("rolled back", r.stderr)
        self.assertFalse(self.flag.exists(), "it proved itself on the probe port and was opened"); self.assertTrue(self.undo.exists())

    def test_someone_elses_flag_is_left_alone_and_no_visitor_request_is_made(self):
        self.flag.write_text("")   # the update job's flag
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.flag.exists())
        self.assertEqual(set(self.log("curl.log").split()) - {"flag=up", "flag=down"}, {"port=8081"}, "the check works under someone else's flag because it uses the probe port")
        self.assertFalse(self.undo.exists(), "a success clears the undo material")

    # --- the maintenance flag belongs to whoever raised it
    def test_a_flag_that_was_already_up_stays_up_on_success_and_on_failure(self):
        self.flag.write_text("")
        self.assertEqual(self.recover("--yes").returncode, 0); self.assertTrue(self.flag.exists(), "the update job owns this flag")
        self.put("db_current", ORIGINAL_DB); self.put("load_fail_once", "")
        self.assertEqual(self.recover("--yes").returncode, 1); self.assertTrue(self.flag.exists())

    def test_a_second_recover_at_the_same_time_is_refused(self):
        import fcntl
        with open(self.site / ".recover.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            r = self.recover("--yes")
        self.assertEqual(r.returncode, 2); self.assertIn("another recover", r.stderr); self.assert_untouched()

    # --- core
    def set_core(self, v):
        (self.site / "html/wp-includes/version.php").write_text("<?php\n$wp_version = '%s';\n" % v)

    def downloads(self):
        return [o for o in self.order() if o.startswith("download")]

    def test_the_images_own_core_is_reseeded_from_the_image_not_downloaded(self):
        self.set_core("7.1.4")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.core(), "7.1.3")
        self.assertIn("reseed", self.order()); self.assertEqual(self.downloads(), [])
        self.assertIn("verify", self.order(), "core is proven after it is put back")
        self.assertTrue((self.site / "html/wp-config-sample.php").exists(), "wp-config-sample.php is in wordpress.org's checksums: it must never be deleted")

    def test_a_reseed_replaces_wp_admin_and_wp_includes_whole_and_lays_the_files_down_as_root(self):
        # a newer core's extra files must not survive (verify-checksums would call them "should not exist"), and the image's files are owned by www-data, so tar must not keep that
        self.set_core("7.1.4")
        (self.site / "html/wp-admin").mkdir(); (self.site / "html/wp-admin/only-in-7.1.4.php").write_text("x")
        (self.site / "html/wp-includes/only-in-7.1.4.php").write_text("x")
        self.assertEqual(self.recover("--yes").returncode, 0)
        self.assertFalse((self.site / "html/wp-admin").exists()); self.assertFalse((self.site / "html/wp-includes/only-in-7.1.4.php").exists())
        call = [c for c in self.log("docker.calls").splitlines() if "--entrypoint sh" in c][0]
        self.assertIn("--no-same-owner", call); self.assertIn("--exclude=./wp-content", call); self.assertIn("--exclude=./wp-config-docker.php", call)
        self.assertIn("--cap-drop ALL", call); self.assertIn("--memory 256m --memory-swap 256m", call)
        self.assertIn("%s/html:/var/www/html" % self.site, call)

    def test_any_other_core_version_is_downloaded_by_the_narrow_root_command_and_proven(self):
        self.snap("20261025T201500Z", core="7.1.2")
        r = self.recover("20261025T201500Z", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.core(), "7.1.2")
        self.assertEqual(self.downloads(), ["download 7.1.2 root=yes"]); self.assertNotIn("reseed", self.order()); self.assertIn("verify", self.order())
        self.assertTrue(re.search(r"--force --skip-content", self.log("docker.calls")))
        self.assertTrue((self.site / "html/wp-config-sample.php").exists())

    def test_core_is_left_alone_when_the_version_already_matches(self):
        self.recover("--yes")
        self.assertEqual(self.downloads(), []); self.assertNotIn("reseed", self.order()); self.assertNotIn("verify", self.order())

    def test_a_failure_after_a_core_change_puts_core_back_too(self):
        self.set_core("7.1.4")   # not the image's: the undo has to download it
        self.put("wp_not_installed", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("rolled back", r.stderr); self.assertNotIn("EXCEPT CORE", r.stderr)
        self.assertEqual(self.core(), "7.1.4", "core is back at the version the site had")
        self.assertEqual([o for o in self.order() if o in ("reseed", "download 7.1.4 root=yes")], ["reseed", "download 7.1.4 root=yes"])
        self.assert_untouched()

    def test_a_core_download_that_fails_is_rolled_back_when_the_old_core_is_the_images(self):
        self.snap("20261025T201500Z", core="7.1.2")
        self.put("core_fail", "")
        r = self.recover("20261025T201500Z", "--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("could not put core back to 7.1.2", r.stderr); self.assertIn("rolled back", r.stderr)
        self.assertEqual(self.core(), "7.1.3"); self.assert_untouched()

    def test_when_core_cannot_be_put_back_the_data_is_restored_but_the_site_stays_in_maintenance(self):
        self.set_core("7.1.4"); self.snap("20261025T201500Z", core="7.1.2")
        self.put("core_fail", "")   # the download of 7.1.2 fails, and so does the undo's download of 7.1.4
        r = self.recover("20261025T201500Z", "--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("ROLLED BACK EXCEPT CORE", r.stderr); self.assertIn("verify-checksums", r.stderr)
        self.assertEqual(self.db(), ORIGINAL_DB.strip()); self.assertTrue((self.wpc / "uploads/live-only.jpg").exists())
        self.assertTrue(self.flag.exists(), "a core that does not verify must not serve visitors")
        self.assertFalse(self.undo.exists(), "the data is back, so the undo material is not left to block the next recover")

    def test_a_checksum_warning_counts_as_a_failure_even_when_the_exit_code_is_zero(self):
        self.set_core("7.1.4"); self.put("verify_warn", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("Warning: File should not exist", r.stderr); self.assertIn("EXCEPT CORE", r.stderr)

    def test_a_reseed_that_fails_is_a_core_failure(self):
        self.set_core("7.1.4"); self.put("reseed_fail", "")
        r = self.recover("--yes")
        self.assertEqual(r.returncode, 1); self.assertIn("could not put core back to 7.1.3", r.stderr)

    # --- what the files promise
    def test_the_tool_is_deployed_and_never_touches_wp_config_or_env(self):
        tasks = (ROOT / "tasks/wp_tools_common.yml").read_text()
        self.assertIn("{ src: wp_site_recover.sh.j2, dest: wp-site-recover }", tasks)
        text = (ROOT / "templates/wp/wp_site_recover.sh.j2").read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("wp-config.php", code); self.assertNotIn("wp-config-sample", code); self.assertNotIn(".env", code)
        self.assertNotIn("docker restart", text)

    def test_the_script_is_run_by_root_tools_only_and_logs_each_outcome(self):
        self.recover("--yes")
        self.assertEqual(self.log("logger.log"), "")   # no logger stand-in installed: the tool must work without one
        self.shim("logger", '#!/bin/bash\necho "$*" >> "$SHIM/logger.log"\n')
        self.put("db_current", ORIGINAL_DB)
        self.recover("--yes")
        self.assertIn("site=wptest snapshot=20261024T201500Z core=7.1.3 result=ok", self.log("logger.log"))
        self.put("db_current", ORIGINAL_DB); self.put("load_fail_once", "")
        self.recover("--yes")
        self.assertIn("result=FAILED undone=yes", self.log("logger.log"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
