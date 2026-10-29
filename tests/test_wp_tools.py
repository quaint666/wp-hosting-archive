#!/usr/bin/env python3
"""Offline tests of the operator's tools on dellwp (templates/wp/wp_site_cli.sh.j2, wp_site_install.sh.j2, wp_site_login_check.sh.j2, wp_tools_common.sh.j2).

Renders the real scripts with the real variables into a temporary directory and runs them with stand-ins for `docker`, `curl` and `logger` on the PATH. The stand-ins
record every argument and everything on stdin, so the tests can say what actually reaches Docker and curl: that a password is never in an argument, that a bad site
name never reaches Docker, that --code changes the user and the flags. They prove the scripts' logic and no more: not that WP-CLI reads a piped password (the first real
install shows that), not that the image runs, not what Docker does with the flags. Run: python3 -I tests/test_wp_tools.py
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wp_sites_render import ROOT, base_vars  # noqa: E402

GOOD = "correct-horse-battery"
DOCKER = r'''#!/bin/bash
echo "--- docker" >> "$SHIM_LOG/docker.argv"
for a in "$@"; do printf '%s\n' "$a" >> "$SHIM_LOG/docker.argv"; done
case "$1" in network) exit 0 ;; esac
[ -n "${SHIM_RUN_RC:-}" ] && exit "$SHIM_RUN_RC"
case " $* " in *" is-installed "*) [ "${SHIM_INSTALLED:-0}" = 1 ] && exit 0 || exit 1 ;; esac
case " $* " in *" user get "*) [ "${SHIM_NO_USER:-0}" = 1 ] && exit 1; echo 1; exit 0 ;; esac
case " $* " in *" verify-checksums "*) if [ "${SHIM_VERIFY_FAIL:-0}" = 1 ]; then echo "Error: checksums do not match"; exit 1; fi; echo "Success: Verified 1 of 1 plugins."; exit 0 ;; esac
# like Docker: the container sees the caller's stdin only when -i is given
case " $* " in *" -i "*)
  in=$(cat); { echo "--- stdin"; printf '%s\n' "$in"; } >> "$SHIM_LOG/docker.stdin" 2>/dev/null
  # like WP-CLI's prompt mode (seen on dellwp 2026-10-24, twice in the output): the prompt line shows the typed value, and then it PRINTS the command it built, password included, shell-quoted
  case " $* " in *" user update "*"--prompt=user_pass"*)
    [ "${SHIM_UPDATE_FAIL:-0}" = 1 ] && { echo "Error: could not update"; exit 1; }
    echo "1/1 [--user_pass=<password>]: $in"; echo "wp user update adminuser --user_pass='${in//\'/\'\\\'\'}'"; echo "Success: Updated user 1." ;; esac
  case " $* " in *" core install "*"--prompt=admin_password"*)
    echo "2/3 [--admin_password=<password>]: $in"; echo "wp core install --skip-email --admin_password='${in//\'/\'\\\'\'}'"
    # and a message that has the value in it but not the option name (a validation warning, say)
    [ "${SHIM_ECHO_ELSEWHERE:-0}" = 1 ] && echo "Warning: the password $in is weak" ;; esac ;;
esac
exit 0
'''
CURL = r'''#!/bin/bash
echo "--- curl" >> "$SHIM_LOG/curl.argv"
for a in "$@"; do printf '%s\n' "$a" >> "$SHIM_LOG/curl.argv"; done
pw=$(cat)
if [ "$pw" = "GOODPW" ]; then printf 'HTTP/1.1 302 Found\r\nSet-Cookie: wordpress_logged_in_abc=x; path=/\r\n\r\n'
else printf 'HTTP/1.1 200 OK\r\n\r\n'; fi
'''.replace("GOODPW", GOOD)


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wptools-"))
        self.v = base_vars()
        v = dict(self.v, wp_tools_bin=str(self.tmp / "bin"), wp_tools_lib=str(self.tmp / "lib"), wp_data_mount=str(self.tmp / "data"), wp_freeze_file=str(self.tmp / "freeze"),
                 inventory_hostname="dellwp", wp_cli_cache_dir=str(self.tmp / "cache"))
        self.ctx = v
        for d in ("bin", "lib", "shim", "log", "cache", "data/sites/wptest"):
            (self.tmp / d).mkdir(parents=True)
        (self.tmp / "data/sites/wptest/wp-config.php").write_text("<?php\n")
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        for src, dest in (("wp_tools_common.sh.j2", "lib/common.sh"), ("wp_site_cli.sh.j2", "bin/wp-site-cli"), ("wp_site_install.sh.j2", "bin/wp-site-install"), ("wp_site_password.sh.j2", "bin/wp-site-password"),
                          ("wp_site_login_check.sh.j2", "bin/wp-site-login-check"), ("wp_site_ext.sh.j2", "bin/wp-site-ext"), ("wp_site_cron.sh.j2", "bin/wp-site-cron")):
            (self.tmp / dest).write_text(env.get_template(src).render(**v))
            if dest.startswith("bin/"):
                (self.tmp / dest).chmod(0o755)  # production: mode 0750 root:root from the play
        for name, body in (("docker", DOCKER), ("curl", CURL), ("logger", "#!/bin/bash\necho \"$*\" >> \"$SHIM_LOG/logger.log\"\n")):
            f = self.tmp / "shim" / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def run_tool(self, name, *args, stdin="", tty=None, installed=False, verify_fails=False, run_rc=None, env_extra=None):
        env = {"PATH": "%s:/usr/bin:/bin" % (self.tmp / "shim"), "SHIM_LOG": str(self.tmp / "log"), "HOME": str(self.tmp)}
        if installed:
            env["SHIM_INSTALLED"] = "1"
        if verify_fails:
            env["SHIM_VERIFY_FAIL"] = "1"
        if run_rc is not None:
            env["SHIM_RUN_RC"] = str(run_rc)
        env.update(env_extra or {})
        if tty is not None:
            (self.tmp / "tty").write_text(tty)
            env["WP_SITE_TTY"] = str(self.tmp / "tty")
        return subprocess.run(["bash", str(self.tmp / "bin" / name), *args], input=stdin, capture_output=True, text=True, env=env, timeout=30)

    def log(self, name):
        p = self.tmp / "log" / name
        return p.read_text() if p.exists() else ""

    def argv(self):
        return self.log("docker.argv").splitlines()

    def pair(self, flag, value):
        a = self.argv()
        return any(a[i] == flag and a[i + 1] == value for i in range(len(a) - 1))

    # --- the site name is the only thing that selects what the tool touches
    def test_bad_and_unknown_site_names_never_reach_docker(self):
        for bad in ("nosuch", "../etc", "wptest;id", "*", "Wptest", "wptest/..", "", "-rf", "wptest extra"):
            with self.subTest(bad):
                r = self.run_tool("wp-site-cli", bad, "core", "version")
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(self.log("docker.argv"), "")

    def test_the_name_pattern_holds_even_if_a_bad_name_is_in_the_list(self):
        # Second layer: the allowlist is rendered from wp_sites, whose own checks refuse such names, but the tool must not depend on that.
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        hostile = dict(self.ctx, wp_sites=[{"name": "wptest"}, {"name": "../etc"}, {"name": "a;id"}, {"name": "x y"}])
        (self.tmp / "lib/common.sh").write_text(env.get_template("wp_tools_common.sh.j2").render(**hostile))
        for bad in ("../etc", "a;id", "x y"):
            with self.subTest(bad):
                r = self.run_tool("wp-site-cli", bad, "core", "version")
                self.assertEqual(r.returncode, 2)
                self.assertIn("is not a site name", r.stderr)
                self.assertEqual(self.log("docker.argv"), "")

    def test_user_mode_is_the_web_apps_power(self):
        r = self.run_tool("wp-site-cli", "wptest", "core", "version")
        self.assertEqual(r.returncode, 0, r.stderr)
        a = self.argv()
        self.assertTrue(self.pair("--user", "33:33"))
        self.assertTrue(self.pair("--network", "wp-wptest_default"))
        self.assertTrue(self.pair("--entrypoint", "wp"))
        self.assertTrue(self.pair("--cap-drop", "ALL"))
        self.assertIn("--read-only", a)
        self.assertIn("no-new-privileges:true", a)
        mem = a[a.index("--memory") + 1]
        self.assertEqual(mem, a[a.index("--memory-swap") + 1], "--memory-swap must equal --memory")
        self.assertIn(self.ctx["wp_image_cli"], a)
        html = [x for x in a if x.endswith(":/var/www/html:ro")]
        self.assertEqual(len(html), 1, "the code must be mounted read-only")
        self.assertEqual(a[-2:], ["core", "version"])
        for flag in ("--allow-root", "--skip-plugins", "--skip-themes"):
            self.assertNotIn(flag, a)
        self.assertNotIn("/var/run/docker.sock", " ".join(a))

    def test_only_code_mode_keeps_its_downloads_between_runs(self):
        cache = str(self.tmp / "cache")
        self.run_tool("wp-site-cli", "wptest", "--code", "plugin", "install", "woocommerce")
        a = self.argv()
        self.assertIn("%s:/wp-cli-cache" % cache, a)
        self.assertTrue(self.pair("-e", "WP_CLI_CACHE_DIR=/wp-cli-cache"))
        self.assertEqual([x for x in a if x.startswith("WP_CLI_CACHE_DIR=")], ["WP_CLI_CACHE_DIR=/wp-cli-cache"], "one setting, not two that fight")
        self.assertFalse([x for x in a if x.startswith(cache) and x.endswith(":ro")], "the cache is writable: that is how it fills")
        (self.tmp / "log" / "docker.argv").unlink()
        self.run_tool("wp-site-cli", "wptest", "core", "version")
        a = self.argv()
        self.assertNotIn("%s:/wp-cli-cache" % cache, a, "the web app's own uid never sees the cache")
        self.assertEqual([x for x in a if x.startswith("WP_CLI_CACHE_DIR=")], ["WP_CLI_CACHE_DIR=/tmp/cache"])
        self.assertFalse([x for x in a if "wp-cli-cache" in x])

    def test_code_mode_without_the_cache_directory_stops_and_says_what_to_run_and_a_plain_read_does_not_need_it(self):
        (self.tmp / "cache").rmdir()
        r = self.run_tool("wp-site-cli", "wptest", "--code", "plugin", "list")
        self.assertEqual(r.returncode, 2); self.assertIn("is missing (run dellwp/wp_sites_dellwp.yml)", r.stderr)
        self.assertNotIn("run", self.argv(), "no container was started")
        self.assertEqual(self.run_tool("wp-site-cli", "wptest", "core", "version").returncode, 0)

    def test_the_cache_directory_is_made_root_only_and_aged_out(self):
        tasks = {t["name"]: t for t in yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())}
        d = tasks["Make the WP-CLI download cache directory"]["ansible.builtin.file"]
        self.assertEqual((d["path"], d["owner"], d["group"], d["mode"]), ("{{ wp_cli_cache_dir }}", "root", "root", "0700"))
        c = tasks["Age out the WP-CLI download cache"]["ansible.builtin.copy"]
        self.assertEqual(c["dest"], "/etc/tmpfiles.d/wp-cli-cache.conf")
        self.assertEqual(c["content"].strip(), "d {{ wp_cli_cache_dir }} 0700 root root {{ wp_cli_cache_days }}d")
        self.assertRegex(self.v["wp_cli_cache_dir"], r"^/var/cache/[a-z-]+$"); self.assertGreaterEqual(self.v["wp_cli_cache_days"], 7)

    def test_code_mode_is_root_without_capabilities_and_skips_plugins(self):
        r = self.run_tool("wp-site-cli", "wptest", "--code", "plugin", "update", "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        a = self.argv()
        self.assertTrue(self.pair("--user", "0:0"))
        self.assertTrue(self.pair("--cap-drop", "ALL"))
        self.assertEqual([x for x in a if x.endswith(":/var/www/html:ro")], [], "--code needs the code writable")
        self.assertIn("--allow-root", a)
        self.assertIn("--skip-plugins", a)
        self.assertIn("--skip-themes", a)
        self.assertEqual(a[a.index("plugin"):a.index("plugin") + 3], ["plugin", "update", "--all"])

    def test_with_plugins_is_gone_so_root_never_loads_a_plugin(self):
        self.assertEqual(self.run_tool("wp-site-cli", "wptest", "--code", "--with-plugins", "plugin", "list").returncode, 2)
        self.assertEqual(self.log("docker.argv"), "")
        self.assertNotIn("with-plugins", (self.tmp / "bin/wp-site-cli").read_text())

    def test_code_mode_allows_only_wordpress_org_slugs_and_a_few_flags(self):
        allowed = [("core", "update", "--minor"), ("core", "version"), ("plugin", "install", "woocommerce", "--version=9.3.1"), ("plugin", "update", "--all"),
                   ("plugin", "update", "woocommerce", "--dry-run"), ("plugin", "delete", "foo-bar"), ("theme", "install", "twentytwentysix"), ("plugin", "list", "--format=json"),
                   ("plugin", "verify-checksums", "--all"), ("core", "check-update", "--major", "--field=version"), ("core", "check-update", "--minor", "--field=version")]
        for cmd in allowed:
            with self.subTest(("allowed",) + cmd):
                (self.tmp / "log" / "docker.argv").unlink(missing_ok=True)
                self.assertEqual(self.run_tool("wp-site-cli", "wptest", "--code", *cmd).returncode, 0, cmd)
        denied = [("eval", "phpinfo();"), ("eval-file", "x.php"), ("shell",), ("db", "query", "DROP TABLE wp_users"), ("user", "create", "a", "a@b.org"),
                  ("plugin", "activate", "woocommerce"), ("plugin", "uninstall", "woocommerce"), ("theme", "activate", "x"), ("core", "download"), ("core", "update", "--force"),
                  ("plugin", "install", "https://evil.example/x.zip"), ("plugin", "install", "x.zip"), ("plugin", "install", "/tmp/x"), ("plugin", "install", "../x"),
                  ("plugin", "install", "woocommerce", "--activate"), ("plugin", "install", "woocommerce", "--force"), ("plugin", "install", "woocommerce", "--require=/x.php"),
                  ("plugin", "install", "Woo"), ("plugin", "install", "woo;id"), ("plugin", "install", "woo$(id)"), ("plugin", "install", "woocommerce", "--version=1;id"),
                  ("--require=/x.php", "plugin", "install", "a"), ("--path=/x", "plugin", "list"), ("plugin",), ("plugin", "")]
        for cmd in denied:
            with self.subTest(("denied",) + cmd):
                (self.tmp / "log" / "docker.argv").unlink(missing_ok=True)
                r = self.run_tool("wp-site-cli", "wptest", "--code", *cmd)
                self.assertEqual(r.returncode, 2, cmd)
                self.assertEqual(self.log("docker.argv"), "", "docker was called for %r" % (cmd,))

    def test_the_cli_tmpfs_is_big_enough_to_unpack_core_and_comes_from_one_variable(self):
        # 128 MB unpacked 1347 of 3341 files and still reported success (dellwp, 2026-10-24): the size is a variable and must not fall back below what was proven
        self.run_tool("wp-site-cli", "wptest", "--code", "core", "version")
        self.assertIn("--tmpfs\n/tmp:size=%sm,mode=1777" % self.v["wp_cli_tmp_mb"], self.log("docker.argv"))
        self.assertGreaterEqual(self.v["wp_cli_tmp_mb"], 256); self.assertGreaterEqual(self.v["wp_cli_mem_mb"], 768)

    def test_core_download_is_allowed_only_in_the_one_form_wp_site_recover_uses(self):
        ok = ("core", "download", "--version=7.1.3", "--force", "--skip-content")
        self.assertEqual(self.run_tool("wp-site-cli", "wptest", "--code", *ok).returncode, 0)
        self.assertEqual(self.wp_calls(), [(True, list(ok))])
        for cmd in (("core", "download"), ("core", "download", "--version=7.1.3"), ("core", "download", "--force", "--skip-content"), ("core", "download", "--version=7.1.3", "--force"),
                    ("core", "download", "--version=7.1.3", "--force", "--skip-content", "--locale=de_DE"), ("core", "download", "--version=7.1.3;id", "--force", "--skip-content"),
                    ("core", "download", "--version=latest", "--force", "--skip-content"), ("core", "download", "--version=https://evil.example/w.zip", "--force", "--skip-content"),
                    ("core", "download", "--skip-content", "--force", "--version=7.1.3"), ("core", "download", "--version=7.1.3", "--force", "--skip-content", "--path=/x"),
                    ("plugin", "install", "x", "--skip-content"), ("plugin", "update", "x", "--force"),
                    ("core", "update", "--major"), ("plugin", "update", "--all", "--major"), ("core", "update", "--version=7.2", "--major")):
            with self.subTest(cmd):
                (self.tmp / "log" / "docker.argv").unlink(missing_ok=True)
                self.assertEqual(self.run_tool("wp-site-cli", "wptest", "--code", *cmd).returncode, 2, cmd)
                self.assertEqual(self.log("docker.argv"), "", "docker was called for %r" % (cmd,))

    def test_user_mode_stays_unrestricted_because_it_is_only_the_web_apps_power(self):
        for cmd in (("eval", "echo 1;"), ("plugin", "activate", "woocommerce"), ("db", "check")):
            self.assertEqual(self.run_tool("wp-site-cli", "wptest", *cmd).returncode, 0, cmd)

    def test_code_mode_logs_the_whole_checked_command_and_user_mode_two_words(self):
        self.run_tool("wp-site-cli", "wptest", "--code", "plugin", "install", "woocommerce", "--version=9.3.1")
        self.run_tool("wp-site-cli", "wptest", "user", "update", "1", "--user_pass=secret-value-123")
        text = self.log("logger.log")
        self.assertIn("mode=code cmd=plugin install woocommerce --version=9.3.1", text)
        self.assertIn("mode=user cmd=user update", text)
        self.assertNotIn("secret-value-123", text)

    def test_stdin_is_not_swallowed_unless_asked_for(self):
        # the 2026-10-24 bug: `ssh host 'bash -s' <<EOF` lost every line after the first wp-site-cli call
        bash = "%s core version; echo after-the-call; cat" % (self.tmp / "bin/wp-site-cli wptest")
        env = {"PATH": "%s:/usr/bin:/bin" % (self.tmp / "shim"), "SHIM_LOG": str(self.tmp / "log"), "HOME": str(self.tmp)}
        r = subprocess.run(["bash", "-c", bash], input="the rest of the caller's script\n", capture_output=True, text=True, env=env)
        self.assertIn("after-the-call", r.stdout)
        self.assertIn("the rest of the caller's script", r.stdout, "the tool consumed its caller's stdin")
        self.assertNotIn("-i", self.argv())
        (self.tmp / "log" / "docker.argv").unlink()
        self.run_tool("wp-site-cli", "wptest", "--stdin", "core", "version", stdin="x\n")
        self.assertIn("-i", self.argv())
        self.assertNotIn("--stdin", self.argv(), "--stdin is the tool's flag, not WP-CLI's")

    def test_the_wp_config_is_always_read_only(self):
        for extra in ([], ["--code"]):
            (self.tmp / "log" / "docker.argv").unlink(missing_ok=True)
            self.run_tool("wp-site-cli", "wptest", *extra, "core", "version")
            self.assertEqual(len([x for x in self.argv() if x.endswith("wp-config.php:/var/www/html/wp-config.php:ro")]), 1, extra)

    def test_a_missing_network_is_a_clear_error(self):
        (self.tmp / "shim" / "docker").write_text("#!/bin/bash\n[ \"$1\" = network ] && exit 1\nexit 0\n")
        r = self.run_tool("wp-site-cli", "wptest", "core", "version")
        self.assertEqual(r.returncode, 2)
        self.assertIn("does not exist", r.stderr)

    # --- the password
    def answers(self, pw=GOOD, pw2=None):
        return "Test site\nadminuser\nadmin@example.org\n%s\n%s\n" % (pw, pw if pw2 is None else pw2)

    def test_install_never_puts_the_password_in_an_argument_or_on_screen(self):
        r = self.run_tool("wp-site-install", "wptest", tty=self.answers())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(GOOD, self.log("docker.argv"), "password in a docker argument")
        self.assertNotIn(GOOD, self.log("curl.argv"), "password in a curl argument")
        self.assertNotIn(GOOD, r.stdout + r.stderr, "password printed")
        self.assertIn(GOOD, self.log("docker.stdin"), "the password must reach WP-CLI on stdin")
        self.assertIn("%d characters" % len(GOOD), r.stderr)
        self.assertIn("right password ok, wrong password refused", r.stderr)
        a = self.argv()
        self.assertIn("--prompt=admin_password", a)
        self.assertIn("--skip-email", a)
        self.assertIn("--url=https://wptest.%s" % self.ctx["wp_domain"], a)
        self.assertIn("blog_public", a)

    def test_install_does_not_print_the_password_even_when_wp_cli_echoes_the_command(self):
        # WP-CLI's prompt mode prints the command it built with the password in it; the tool must not let that reach the terminal, scrollback or a pasted log
        for pw in (GOOD, "it's a long 'quoted' pass$word"):
            with self.subTest(pw):
                (self.tmp / "log" / "docker.stdin").unlink(missing_ok=True)
                r = self.run_tool("wp-site-install", "wptest", tty="T\nadminuser\nadmin@example.org\n%s\n%s\n" % (pw, pw))
                shown = r.stdout + r.stderr
                self.assertEqual(r.returncode, 0 if pw == GOOD else r.returncode)
                self.assertNotIn(pw, shown)
                for piece in (pw.split()[-1], "quoted", "pass$word", "correct-horse"):
                    self.assertNotIn(piece, shown, "a piece of the password is on screen: " + piece)
                self.assertIn("--admin_password=REDACTED", shown, "the echo reaches the screen with the value removed")
                self.assertIn(pw, self.log("docker.stdin"), "the password still reaches WP-CLI on stdin")

    def test_install_redacts_the_value_wherever_it_appears_not_only_after_the_option_name(self):
        r = self.run_tool("wp-site-install", "wptest", tty=self.answers(), env_extra={"SHIM_ECHO_ELSEWHERE": "1"})
        self.assertNotIn(GOOD, r.stdout + r.stderr); self.assertIn("Warning: the password REDACTED is weak", r.stderr)

    def test_a_failing_core_install_stops_the_tool_before_the_login_check(self):
        r = self.run_tool("wp-site-install", "wptest", tty=self.answers(), run_rc=3)
        self.assertNotEqual(r.returncode, 0, "the pipe through the redaction must not hide the failure")
        self.assertNotIn("blog_public", self.log("docker.argv")); self.assertEqual(self.log("curl.argv"), "", "no login check after a failed install")

    def test_install_refuses_a_mismatch_a_short_password_and_an_installed_site(self):
        r = self.run_tool("wp-site-install", "wptest", tty=self.answers(pw2="different-entry-xyz"))
        self.assertEqual(r.returncode, 2)
        r = self.run_tool("wp-site-install", "wptest", tty=self.answers(pw="short"))
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("install", [x for x in self.argv() if x == "install"], "nothing may be installed")
        r = self.run_tool("wp-site-install", "wptest", tty=self.answers(), installed=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("already has WordPress", r.stderr)
        self.assertNotIn("install", [x for x in self.argv() if x == "install"])

    def test_install_rejects_odd_user_names_and_emails(self):
        for answers in ("T\nbad user!\na@b.org\n%s\n%s\n" % (GOOD, GOOD), "T\nadminuser\nnot-an-email\n%s\n%s\n" % (GOOD, GOOD)):
            with self.subTest(answers.splitlines()[1:3]):
                self.assertEqual(self.run_tool("wp-site-install", "wptest", tty=answers).returncode, 2)

    def test_install_takes_title_user_and_email_as_options_and_still_asks_for_the_password(self):
        r = self.run_tool("wp-site-install", "wptest", "--title=My Friend's Site", "--user=adminuser", "--email=admin@example.org", tty="%s\n%s\n" % (GOOD, GOOD))
        self.assertEqual(r.returncode, 0, r.stderr)
        a = self.argv()
        self.assertIn("--title=My Friend's Site", a, "a title with spaces stays one argument")
        self.assertIn("--admin_user=adminuser", a); self.assertIn("--admin_email=admin@example.org", a)
        self.assertNotIn("Site title:", r.stderr, "no prompt for what was given")
        self.assertIn("Password", r.stderr); self.assertNotIn(GOOD, self.log("docker.argv"))

    def test_install_asks_only_for_what_was_not_given(self):
        r = self.run_tool("wp-site-install", "wptest", "--user=adminuser", tty="A title\nadmin@example.org\n%s\n%s\n" % (GOOD, GOOD))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Site title:", r.stderr); self.assertIn("Administrator email:", r.stderr); self.assertNotIn("Administrator user name:", r.stderr)
        self.assertIn("--admin_user=adminuser", self.argv())

    def test_install_options_are_checked_like_the_answers_and_there_is_no_password_option(self):
        # every run gets valid values for whatever the case under test does not replace, and only the password is scripted: a failure can only come from the option under test, and a
        # tool that ignored or accepted a bad option would install
        base = {"--title": "--title=T", "--user": "--user=adminuser", "--email": "--email=admin@example.org"}
        pw = "%s\n%s\n" % (GOOD, GOOD)
        good = self.run_tool("wp-site-install", "wptest", *base.values(), tty=pw)
        self.assertEqual(good.returncode, 0, "the valid options alone are a good run: " + good.stderr)
        cases = [("--user", "--user=bad user!"), ("--email", "--email=not-an-email"), ("--title", "--title=" + "x" * 101), ("--title", "--title=line\nbreak"), ("--title", "--title=a\x07b"),
                 ("--title", "--title=\x7f")]
        extras = ["--password=" + GOOD, "--pw=" + GOOD, "--user", "--title", "extra", "--unknown=1"]
        runs = [[v for k, v in base.items() if k != key] + [bad] for key, bad in cases] + [list(base.values()) + [e] for e in extras]
        for opts in runs:
            with self.subTest(opts[-1]):
                (self.tmp / "log" / "docker.argv").unlink(missing_ok=True)
                r = self.run_tool("wp-site-install", "wptest", *opts, tty=pw)
                self.assertEqual(r.returncode, 2, (opts, r.stderr))
                self.assertNotIn("install", [x for x in self.argv() if x == "install"], "nothing may be installed")

    # --- changing a password
    def words(self):
        return self.log("docker.argv").splitlines()

    def at(self, *seq):
        w = self.words()
        for i in range(len(w) - len(seq) + 1):
            if w[i:i + len(seq)] == list(seq):
                return i
        return -1

    def pw_answers(self, pw=GOOD, pw2=None):
        return "%s\n%s\n" % (pw, pw if pw2 is None else pw2)

    def test_password_asks_at_a_hidden_prompt_and_never_shows_or_passes_the_value(self):
        for pw in (GOOD, "it's a long 'quoted' pass$word"):
            with self.subTest(pw):
                (self.tmp / "log" / "docker.stdin").unlink(missing_ok=True); (self.tmp / "log" / "docker.argv").unlink(missing_ok=True)
                r = self.run_tool("wp-site-password", "wptest", "adminuser", tty=self.pw_answers(pw))
                shown = r.stdout + r.stderr
                self.assertNotIn(pw, shown); self.assertNotIn(pw, self.log("docker.argv"), "password in a docker argument"); self.assertNotIn(pw, self.log("curl.argv"))
                for piece in (pw.split()[-1], "quoted", "pass$word", "correct-horse"):
                    self.assertNotIn(piece, shown, "a piece of the password is on screen: " + piece)
                self.assertIn("--user_pass=REDACTED", shown, "WP-CLI's echo reaches the screen with the value removed")
                self.assertIn(pw, self.log("docker.stdin"), "the password reaches WP-CLI on stdin")
                self.assertIn("%d characters" % len(pw), r.stderr)
                self.assertIn("--prompt=user_pass", self.words())
                if pw == GOOD:
                    self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("right password ok, wrong password refused", r.stderr)

    def test_password_checks_the_user_changes_ends_the_other_sessions_then_proves_the_result_in_that_order(self):
        r = self.run_tool("wp-site-password", "wptest", "adminuser", tty=self.pw_answers())
        self.assertEqual(r.returncode, 0, r.stderr)
        get, upd, ses = self.at("user", "get", "adminuser"), self.at("user", "update", "adminuser"), self.at("user", "session", "destroy", "adminuser", "--all")
        self.assertTrue(0 <= get < upd < ses, (get, upd, ses))
        self.assertGreater(self.log("curl.argv").count("--- curl"), 1, "the login check runs after the sessions are ended")
        self.assertIn("other sessions ended", r.stderr)

    def test_password_stops_at_each_failure_before_doing_anything_further(self):
        r = self.run_tool("wp-site-password", "wptest", "ghost", tty=self.pw_answers(), env_extra={"SHIM_NO_USER": "1"})
        self.assertEqual(r.returncode, 2); self.assertIn("has no user 'ghost'", r.stderr); self.assertEqual(self.at("user", "update"), -1); self.assertNotIn("Password", r.stderr)
        for label, ans in (("mismatch", self.pw_answers(pw2="different-entry-xyz")), ("short", self.pw_answers(pw="short"))):
            with self.subTest(label):
                (self.tmp / "log" / "docker.argv").unlink(missing_ok=True)
                self.assertEqual(self.run_tool("wp-site-password", "wptest", "adminuser", tty=ans).returncode, 2)
                self.assertEqual(self.at("user", "update"), -1, "nothing may be changed")
        (self.tmp / "log" / "docker.argv").unlink(missing_ok=True); (self.tmp / "log" / "curl.argv").unlink(missing_ok=True)
        r = self.run_tool("wp-site-password", "wptest", "adminuser", tty=self.pw_answers(), env_extra={"SHIM_UPDATE_FAIL": "1"})
        self.assertNotEqual(r.returncode, 0, "a failing update must not be hidden by the pipe through the redaction")
        self.assertEqual(self.at("user", "session", "destroy"), -1); self.assertEqual(self.log("curl.argv"), "", "no login check after a failed update")

    def test_password_refuses_odd_arguments_and_names_and_has_no_password_option(self):
        pw = self.pw_answers()
        for args in (("wptest",), ("wptest", "adminuser", "extra"), ("wptest", "adminuser", "--password=" + GOOD), ("nosuch", "adminuser"), ("wptest", "bad user!"), ("wptest", "a;id"),
                     ("wptest", "../x"), ("wptest", ""), ("wptest", "x" * 61), ()):
            with self.subTest(args):
                (self.tmp / "log" / "docker.argv").unlink(missing_ok=True)
                self.assertEqual(self.run_tool("wp-site-password", *args, tty=pw).returncode, 2, args)
                self.assertEqual(self.at("user", "update"), -1)

    def test_password_wrong_password_after_the_change_is_reported(self):
        (self.tmp / "shim" / "curl").write_text("#!/bin/bash\ncat >/dev/null\nprintf 'HTTP/1.1 200 OK\\r\\n\\r\\n'\n")
        r = self.run_tool("wp-site-password", "wptest", "adminuser", tty=self.pw_answers())
        self.assertEqual(r.returncode, 1); self.assertIn("right password FAILED", r.stderr)

    def test_a_closed_site_is_not_in_the_tools_allowlist_and_has_no_update_policy(self):
        import jinja2
        sites = [{"name": "alive"}, {"name": "gone", "state": "closed"}, {"name": "held", "state": "hold"}, {"name": "tomb2", "state": "closed"}]
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        lib = env.get_template("wp_tools_common.sh.j2").render(**dict(self.ctx, wp_sites=sites))
        self.assertIn("readonly WP_SITES='alive held'", lib, "the tools act on sites that exist; a tombstone is 'not in wp_sites'")
        self.assertEqual(env.get_template("wp_tools_common.sh.j2").render(**dict(self.ctx, wp_sites=[{"name": "only"}])).count("readonly WP_SITES='only'"), 1)
        tools = yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())
        expr = [t for t in tools if t.get("name") == "Stage the tools"][0]["vars"]["wp_update_policies"]
        got = jinja2.Environment().from_string(expr).render(wp_sites=sites, wp_site_defaults=self.v["wp_site_defaults"]).strip()
        self.assertEqual(got, "alive:%s held:%s" % (self.v["wp_site_defaults"]["update_policy"], self.v["wp_site_defaults"]["update_policy"]))

    def test_install_and_password_refuse_a_stopped_site_before_asking_for_anything(self):
        (self.tmp / "data/sites/wptest/stopped.flag").write_text("")
        for tool, args in (("wp-site-install", ("wptest",)), ("wp-site-password", ("wptest", "adminuser"))):
            (self.tmp / "log" / "docker.argv").unlink(missing_ok=True)
            r = self.run_tool(tool, *args, tty=self.answers() if tool == "wp-site-install" else self.pw_answers())
            self.assertEqual(r.returncode, 2, tool); self.assertIn("is stopped", r.stderr); self.assertNotIn("not shown", r.stderr, "no prompt for anything")
            self.assertEqual(self.at("user", "update"), -1); self.assertNotIn("install", [x for x in self.words() if x == "install"])

    def test_install_fails_loudly_when_the_login_does_not_work(self):
        (self.tmp / "shim" / "curl").write_text("#!/bin/bash\ncat >/dev/null\nprintf 'HTTP/1.1 200 OK\\r\\n\\r\\n'\n")
        r = self.run_tool("wp-site-install", "wptest", tty=self.answers())
        self.assertEqual(r.returncode, 1)
        self.assertIn("right password FAILED", r.stderr)

    def test_login_check_reads_stdin_and_sends_the_tunnels_headers(self):
        ok = self.run_tool("wp-site-login-check", "wptest", "adminuser", stdin=GOOD)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        bad = self.run_tool("wp-site-login-check", "wptest", "adminuser", stdin=GOOD + "x")
        self.assertEqual(bad.returncode, 1)
        argv = self.log("curl.argv")
        self.assertNotIn(GOOD, argv)
        self.assertIn("Host: wptest.%s" % self.ctx["wp_domain"], argv)
        self.assertIn("X-Forwarded-Proto: https", argv)
        self.assertIn("http://127.0.0.1:%s/wp-login.php" % self.ctx["wp_nginx_listen_port"], argv)
        self.assertEqual(self.run_tool("wp-site-login-check", "nosuch", "adminuser", stdin=GOOD).returncode, 2)
        self.assertEqual(self.run_tool("wp-site-login-check", "wptest", "bad user;", stdin=GOOD).returncode, 2)

    # --- wp-site-ext: the one way to add or change plugins and themes
    def wp_calls(self):
        """Every docker run, as (is_root, the wp arguments after the image)."""
        out = []
        for block in self.log("docker.argv").split("--- docker\n")[1:]:
            a = block.splitlines()
            if a and a[0] == "run":
                i = a.index(self.ctx["wp_image_cli"])
                user = a[a.index("--user") + 1]
                out.append((user == "0:0", [x for x in a[i + 1:] if x not in ("--allow-root", "--skip-plugins", "--skip-themes")]))
        return out

    def test_ext_install_is_root_then_verified_then_activated_as_the_web_app(self):
        r = self.run_tool("wp-site-ext", "wptest", "plugin", "install", "woocommerce", "--version=9.3.1", "--activate")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.wp_calls()
        self.assertEqual(calls[0], (True, ["plugin", "install", "woocommerce", "--version=9.3.1"]))
        self.assertEqual(calls[1], (False, ["plugin", "verify-checksums", "woocommerce"]))
        self.assertEqual(calls[2], (False, ["plugin", "activate", "woocommerce"]), "activation runs the plugin's code: never as root")
        self.assertEqual(len(calls), 3)
        self.assertNotIn("--activate", self.argv(), "--activate would run the plugin as root inside the install call")
        self.assertIn("plugin install woocommerce version=9.3.1 result=ok", self.log("logger.log"))

    def test_ext_removes_a_plugin_whose_checksums_do_not_match_and_does_not_activate_it(self):
        r = self.run_tool("wp-site-ext", "wptest", "plugin", "install", "shady-plugin", "--activate", verify_fails=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("was removed", r.stderr)
        calls = self.wp_calls()
        self.assertEqual(calls[-1], (True, ["plugin", "delete", "shady-plugin"]))
        self.assertNotIn("activate", [c[1][1] for c in calls if len(c[1]) > 1])
        self.assertIn("result=FAILED", self.log("logger.log"))

    def test_ext_explains_a_failed_checksum_check_before_it_removes_the_plugin(self):
        r = self.run_tool("wp-site-ext", "wptest", "plugin", "install", "shady-plugin", verify_fails=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--- diagnostics for 'shady-plugin' (its checksum check failed):", r.stderr)
        for needle in ("plugin list:", "debug lines of the same check:", "the plugin's directory:", "--- end of diagnostics"):
            self.assertIn(needle, r.stderr)
        calls = self.wp_calls()
        self.assertIn((False, ["plugin", "list", "--fields=name,status,version", "--format=csv"]), calls)
        self.assertIn((False, ["plugin", "verify-checksums", "shady-plugin", "--debug"]), calls)
        order = [c[1][:2] for c in calls]
        self.assertLess(order.index(["plugin", "list"]), len(order) - 1 - order[::-1].index(["plugin", "delete"]), "the diagnostics come before the plugin is removed")
        self.assertTrue(all(not root for root, args in calls if args[:2] in (["plugin", "list"], ["plugin", "verify-checksums"])), "the diagnostics run as the web app, not as root")

    def test_ext_prints_no_diagnostics_when_the_check_passes(self):
        r = self.run_tool("wp-site-ext", "wptest", "plugin", "install", "woocommerce")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("diagnostics", r.stderr + r.stdout)
        self.assertEqual(len(self.wp_calls()), 2)

    def test_ext_installs_a_theme_by_slug_and_says_it_cannot_verify_it(self):
        r = self.run_tool("wp-site-ext", "wptest", "theme", "install", "twentytwentysix")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.wp_calls(), [(True, ["theme", "install", "twentytwentysix"])])
        self.assertIn("no checksum check", r.stderr)
        self.assertEqual(self.run_tool("wp-site-ext", "wptest", "theme", "install", "twentytwentysix", "--activate").returncode, 2)

    def test_ext_refuses_anything_that_is_not_a_slug_before_it_touches_docker(self):
        for args in (("plugin", "install", "https://evil.example/x.zip"), ("plugin", "install", "x.zip"), ("plugin", "install", "../x"), ("plugin", "install", "Woo"),
                     ("plugin", "install", "a;id"), ("plugin", "install", "woocommerce", "jetpack"), ("plugin", "install"), ("plugin", "install", "w", "--force"),
                     ("plugin", "install", "w", "--version=1;id"), ("plugin", "update", "w", "--all"), ("plugin", "update"), ("plugin", "delete", "--all"),
                     ("plugin", "update", "w", "--activate"), ("plugin", "install", "w", "--all"), ("plugin", "delete", "w", "--version=1.0"), ("widget", "install", "w"),
                     ("plugin", "eval", "w")):
            with self.subTest(args):
                r = self.run_tool("wp-site-ext", "wptest", *args)
                self.assertIn(r.returncode, (2,), args)
                self.assertEqual(self.log("docker.argv"), "", "docker was called for %r" % (args,))
        self.assertEqual(self.run_tool("wp-site-ext", "nosuch", "plugin", "install", "woocommerce").returncode, 2)

    def test_ext_checks_slugs_on_its_own_even_if_wp_site_cli_would_accept_anything(self):
        # each layer must hold alone: swap wp-site-cli for a stub that accepts everything and records what it is given
        stub = self.tmp / "bin" / "wp-site-cli"
        stub.write_text('#!/bin/bash\necho "$*" >> "%s"\n' % (self.tmp / "log" / "stub.calls"))
        for args in (("plugin", "install", "https://evil.example/x.zip"), ("plugin", "install", "x.zip"), ("plugin", "install", "../x"), ("plugin", "install", "Woo"),
                     ("theme", "install", "a/b"), ("plugin", "install", "w", "--version=1;id"), ("plugin", "update", "w;id")):
            with self.subTest(args):
                self.assertEqual(self.run_tool("wp-site-ext", "wptest", *args).returncode, 2, args)
        self.assertEqual(self.log("stub.calls"), "", "wp-site-ext passed a bad argument down to wp-site-cli")

    def test_ext_update_verifies_named_plugins_and_delete_deactivates_first(self):
        self.assertEqual(self.run_tool("wp-site-ext", "wptest", "plugin", "update", "woocommerce").returncode, 0)
        self.assertEqual(self.wp_calls(), [(True, ["plugin", "update", "woocommerce"]), (False, ["plugin", "verify-checksums", "woocommerce"])])
        (self.tmp / "log" / "docker.argv").unlink()
        self.assertEqual(self.run_tool("wp-site-ext", "wptest", "plugin", "update", "--all").returncode, 0)
        self.assertEqual(self.wp_calls(), [(True, ["plugin", "update", "--all"])])
        (self.tmp / "log" / "docker.argv").unlink()
        self.assertEqual(self.run_tool("wp-site-ext", "wptest", "plugin", "delete", "woocommerce").returncode, 0)
        self.assertEqual(self.wp_calls(), [(False, ["plugin", "deactivate", "woocommerce"]), (True, ["plugin", "delete", "woocommerce"])])

    # --- the cron timer: the script, the units, and which sites get a running timer
    def test_cron_runs_the_due_events_as_the_web_app(self):
        r = self.run_tool("wp-site-cron", "wptest")
        self.assertEqual(r.returncode, 0, r.stderr)
        # one call, not root: cron runs plugin code (WooCommerce's scheduler), which must never be root's
        self.assertEqual(self.wp_calls(), [(False, ["cron", "event", "run", "--due-now"])])
        self.assertNotIn("--allow-root", self.argv())

    def test_cron_skips_a_frozen_site_and_counts_it_as_success(self):
        (self.tmp / "freeze").write_text("")
        r = self.run_tool("wp-site-cron", "wptest")
        self.assertEqual((r.returncode, self.log("docker.argv")), (0, ""))
        self.assertIn("frozen", r.stdout)
        (self.tmp / "freeze").unlink()
        (self.tmp / "data/sites/wptest/freeze").write_text("")
        r = self.run_tool("wp-site-cron", "wptest")
        self.assertEqual((r.returncode, self.log("docker.argv")), (0, ""))
        (self.tmp / "data/sites/wptest/freeze").unlink()
        self.assertEqual(self.run_tool("wp-site-cron", "wptest").returncode, 0)
        self.assertNotEqual(self.log("docker.argv"), "", "with no freeze flag cron must run")

    def test_cron_skips_a_site_in_maintenance_and_counts_it_as_success(self):
        (self.tmp / "data/sites/wptest/maintenance.flag").write_text("")
        r = self.run_tool("wp-site-cron", "wptest")
        self.assertEqual((r.returncode, self.log("docker.argv")), (0, ""))
        self.assertIn("maintenance", r.stdout)
        (self.tmp / "data/sites/wptest/maintenance.flag").unlink()
        self.assertEqual(self.run_tool("wp-site-cron", "wptest").returncode, 0)
        self.assertNotEqual(self.log("docker.argv"), "", "with the flag gone cron must run again")

    def test_cron_fails_when_the_job_fails_so_the_unit_can_alert(self):
        self.assertEqual(self.run_tool("wp-site-cron", "wptest", run_rc=3).returncode, 3)
        (self.tmp / "shim" / "docker").write_text("#!/bin/bash\n[ \"$1\" = network ] && exit 1\nexit 0\n")
        self.assertEqual(self.run_tool("wp-site-cron", "wptest").returncode, 2, "a missing site network must fail loudly, not skip")

    def test_cron_refuses_bad_names(self):
        for bad in ("nosuch", "../etc", "a;id", "", "wptest extra"):
            self.assertEqual(self.run_tool("wp-site-cron", *( [bad] if bad else [] )).returncode, 2, bad)
        self.assertEqual(self.log("docker.argv"), "")

    def test_the_units_run_the_script_per_site_and_alert_on_failure(self):
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        svc = env.get_template("wp_cron.service.j2").render(**self.ctx)
        self.assertIn("ExecStart=%s/wp-site-cron %%i" % self.ctx["wp_tools_bin"], svc)
        self.assertIn("OnFailure=wp-job-failed@%N.service", svc)
        self.assertIn("Type=oneshot", svc)
        self.assertIn("TimeoutStartSec=%s" % self.ctx["wp_cron_timeout_s"], svc)
        tim = env.get_template("wp_cron.timer.j2").render(**self.ctx)
        self.assertIn("OnCalendar=%s" % self.ctx["wp_cron_calendar"], tim)
        self.assertIn("RandomizedDelaySec=%s" % self.ctx["wp_cron_jitter_s"], tim)
        self.assertIn("WantedBy=timers.target", tim)
        fail = env.get_template("wp_job_failed.service.j2").render(**self.ctx)
        self.assertIn("/usr/local/bin/ntfy-notify %s " % self.ctx["wp_ntfy_topic"], fail)
        self.assertIn("failed. Look with: journalctl -u %i", fail)
        for name in ("wp_cron.service.j2", "wp_cron.timer.j2", "wp_job_failed.service.j2"):
            code = "\n".join(l for l in env.get_template(name).render(**self.ctx).splitlines() if not l.lstrip().startswith("#"))
            self.assertNotIn("%I", code, name + ": %I unescapes the instance and turns the - of wp-cron into /")
        for unit in (svc, tim, fail):
            self.assertNotIn(self.ctx["wp_master_secret"], unit)

    # --- the stack's lifecycle: systemd owns it, Ansible only says start, restart or reload
    def unit(self, **over):
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        ctx = dict(compose_stack_description="WordPress site wptest", compose_stack_dir="/srv/wp/sites/wptest", compose_stack_start_timeout_s=self.ctx["wp_stack_start_timeout_s"],
                   compose_stack_stop_timeout_s=self.ctx["wp_stack_stop_timeout_s"], compose_stack_reload_service="php")
        ctx.update(over)
        return env.get_template("compose_stack.service.j2").render(**ctx)

    def test_the_shared_unit_gives_one_command_start_stop_restart_and_a_php_reload(self):
        u = self.unit()
        code = [l for l in u.splitlines() if l and not l.startswith("#")]
        self.assertIn("Type=oneshot", code)
        self.assertIn("RemainAfterExit=yes", code)
        self.assertIn("WorkingDirectory=/srv/wp/sites/wptest", code)
        self.assertIn("ExecStart=/usr/bin/docker compose up -d --wait --remove-orphans", code)
        self.assertIn("ExecStop=/usr/bin/docker compose down", code)
        self.assertIn("ExecReload=/usr/bin/docker compose up -d --wait --force-recreate --no-deps php", code)
        self.assertNotIn("docker restart", u.replace("# ", ""), "the standing rule: systemctl restart, never docker restart")
        self.assertNotIn("ExecReload", "\n".join(self.unit(compose_stack_reload_service="").splitlines()), "no reload service, no ExecReload")
        self.assertIn("TimeoutStartSec=%s" % self.ctx["wp_stack_start_timeout_s"], code)

    def test_the_stop_timeout_outlasts_the_databases_grace_period(self):
        self.assertGreater(self.ctx["wp_stack_stop_timeout_s"], self.ctx["wp_db_stop_grace_s"] + 10, "systemd would kill `compose down` while MariaDB is still flushing")
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        site = dict(self.ctx, wp_s=dict(self.ctx["wp_site_defaults"], name="wptest", port=9101))
        compose = yaml.safe_load(env.get_template("site_compose.yml.j2").render(**site))
        self.assertEqual(compose["services"]["db"]["stop_grace_period"], "%ds" % self.ctx["wp_db_stop_grace_s"])

    def test_ansible_does_not_run_compose_itself_so_the_unit_is_the_only_owner(self):
        text = (ROOT / "tasks/wp_site_one_common.yml").read_text()
        self.assertNotIn("docker_compose_v2", text)
        tasks = yaml.safe_load(text)
        names = [t.get("name", "") for t in tasks]
        deploy = names.index("Deploy the site's systemd unit")
        start = names.index("Start the site, or restart it when its compose file or database settings changed; stop it when it is stopped")
        reload_ = names.index("Recreate only the php container when only its mounted files changed")
        self.assertLess(deploy, start)
        self.assertLess(start, reload_)
        unit = tasks[deploy]["ansible.builtin.template"]
        self.assertTrue(unit["src"].endswith("/compose_stack.service.j2"))
        self.assertEqual(tasks[deploy]["vars"]["compose_stack_reload_service"], "php")
        self.assertTrue((ROOT / "templates/compose_stack.service.j2").is_file())

    def test_what_changed_decides_restart_reload_or_nothing(self):
        tasks = yaml.safe_load((ROOT / "tasks/wp_site_one_common.yml").read_text())
        facts = [t for t in tasks if t.get("name", "").startswith("Work out whether the stack needs")][0]["ansible.builtin.set_fact"]
        e = jinja2.Environment()
        e.filters["bool"] = lambda v: str(v).strip().lower() in ("true", "1", "yes")  # Ansible's filter; plain Jinja has none

        def decide(env=False, wpconfig=False, **files):
            ctx = dict(wp_site_env={"changed": env}, wp_site_wpconfig={"changed": wpconfig},
                       wp_site_files={"results": [{"item": {"dest": d}, "changed": files.get(d.split(".")[0].replace("-", "_"), False)} for d in ("php.ini", "fpm-pool.conf", "compose.yml")]})
            restart = e.from_string(facts["wp_site_restart"]).render(**ctx).strip() == "True"
            php = e.from_string(facts["wp_site_php_changed"]).render(**ctx).strip() == "True"
            return restart, php

        self.assertEqual(decide(), (False, False), "nothing changed: just start")
        self.assertEqual(decide(compose=True), (True, False), "a compose.yml change (image, limits) restarts the stack")
        self.assertEqual(decide(env=True), (True, False), "a .env change (database settings) restarts the stack")
        self.assertEqual(decide(php=True), (False, True), "php.ini alone: reload php only")
        self.assertEqual(decide(wpconfig=True), (False, True), "wp-config.php alone: reload php only")
        self.assertEqual(decide(fpm_pool=True), (False, True), "the pool alone: reload php only")
        self.assertEqual(decide(env=True, php=True), (True, True), "both: the restart covers it (the reload task is skipped)")
        start = [t for t in tasks if t.get("name", "").startswith("Start the site, or restart it")][0]["ansible.builtin.systemd"]
        for restart, want in ((True, "restarted"), (False, "started")):
            for state in ("hold", "live"):
                self.assertEqual(e.from_string(start["state"]).render(wp_site_restart=restart, wp_s={"state": state}).strip(), want, "the unit must act on the decision")
        for restart in (True, False):
            self.assertEqual(e.from_string(start["state"]).render(wp_site_restart=restart, wp_s={"state": "stopped"}).strip(), "stopped", "a stopped site's unit is stopped whatever changed")
        reload_task = [t for t in tasks if t.get("name", "").startswith("Recreate only the php container")][0]
        self.assertEqual(reload_task["ansible.builtin.systemd"]["state"], "reloaded")
        self.assertIn("not (wp_site_restart | bool)", reload_task["when"])
        self.assertIn("wp_s.state != 'stopped'", reload_task["when"], "no php to recreate in a stopped site")

    def test_a_stopped_site_loses_the_final_backups_maintenance_flag_only_after_its_containers_are_down(self):
        tasks = yaml.safe_load((ROOT / "tasks/wp_site_one_common.yml").read_text())
        names = [t.get("name", "") for t in tasks]
        stop = [i for i, n in enumerate(names) if n.startswith("Start the site, or restart it") and "stop it when it is stopped" in n][0]
        rm = names.index("Take the maintenance flag away from a stopped site")
        self.assertGreater(rm, stop, "the flag keeps the site quiet until the unit has stopped")
        t = tasks[rm]
        self.assertEqual((t["ansible.builtin.file"]["state"], t["when"]), ("absent", "wp_s.state == 'stopped'"))
        self.assertTrue(t["ansible.builtin.file"]["path"].endswith("/maintenance.flag"))

    def test_only_live_sites_get_a_running_timer_and_systemd_is_reloaded_first(self):
        tasks = yaml.safe_load((ROOT / "tasks/wp_site_one_common.yml").read_text())
        t = [x for x in tasks if "wp-cron@" in str(x.get("ansible.builtin.systemd", {}).get("name", ""))][0]["ansible.builtin.systemd"]
        e = jinja2.Environment()
        for state, want in (("live", ("True", "started")), ("hold", ("False", "stopped"))):
            got = (e.from_string(t["enabled"]).render(wp_s={"state": state}), e.from_string(t["state"]).render(wp_s={"state": state}))
            self.assertEqual(got, want, state)
        tools = yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())
        send = [x for x in tools if x.get("name") == "Send the WordPress units"][0]
        self.assertEqual(send["notify"], "Reload systemd")
        names = [x.get("name") for x in tools]
        self.assertGreater(names.index("Reload systemd now if a unit changed"), names.index("Send the WordPress units"), "the reload must follow the units it loads")
        self.assertLess(names.index("Reload systemd now if a unit changed"), names.index("Enable the backup freshness check timer"), "the timers are enabled only after systemd knows the units")
        wrapper = yaml.safe_load((ROOT / "dellwp/wp_sites_dellwp.yml").read_text())[0]
        self.assertIn("Reload systemd", [h["name"] for h in wrapper["handlers"]])

    # --- how they are installed
    def test_the_image_tag_is_built_from_the_two_pins(self):
        m = __import__("re").search(r"php[0-9]+[.][0-9]+", self.v["wp_image_php"]).group(0)
        self.assertEqual(self.v["wp_image_cli"], "wordpress:cli-%s-%s" % (self.v["wp_cli_version"], m))
        self.assertIn("wordpress:cli-%s-%s" % (self.v["wp_cli_version"], m), (self.tmp / "bin/wp-site-cli").read_text())

    def test_the_tasks_install_the_tools_root_only(self):
        tasks = yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())
        by = {t["name"]: t["ansible.posix.synchronize"] for t in tasks if "ansible.posix.synchronize" in t}
        self.assertTrue({"--chown=root:root", "--chmod=F0750"} <= set(by["Send the tools"]["rsync_opts"]))
        for n in ("Send the tools library and the Python modules", "Send the WordPress units"):
            self.assertTrue({"--chown=root:root", "--chmod=F0644"} <= set(by[n]["rsync_opts"]), n)
        # what the tasks DO (comments are not parsed): no task writes a sudoers file or sets a setuid mode
        plays = yaml.safe_load((ROOT / "dellwp/wp_sites_dellwp.yml").read_text())
        text = yaml.dump(tasks) + yaml.dump(plays)
        self.assertNotIn("sudoers", text)
        for mode in __import__("re").findall(r"mode: '?\"?([0-7]{3,4})", text):
            self.assertLess(int(mode, 8) & 0o7000, 1, "setuid/setgid/sticky bit in mode " + mode)


class Staging(unittest.TestCase):
    """tasks/wp_tools_common.yml renders on the controller and sends the files with rsync. These check the shape of those tasks, and run the real rsync with the real options on a
    throwaway tree (as the current user: root's --chown is the only difference) to prove what the options do to the destination."""
    def setUp(self):
        self.tasks = yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())
        self.by = {t.get("name"): t for t in self.tasks}
        self.d = tempfile.TemporaryDirectory()
        self.tmp = Path(self.d.name)

    def tearDown(self):
        self.d.cleanup()

    def sync(self, name):
        return self.by[name]["ansible.posix.synchronize"]

    SENDS = {"Send the tools library and the Python modules": ("lib", "{{ wp_tools_lib }}/", "F0644"), "Send the tools": ("bin", "{{ wp_tools_bin }}/", "F0750"),
             "Send the WordPress units": ("units", "/etc/systemd/system/", "F0644"), "Send the site templates": ("tpl", "{{ wp_tools_lib }}/templates/", "D0755,F0644")}

    # the only things written on the VM by a task of their own (everything else is rendered on the controller and sent by rsync): two directories and one tmpfiles rule
    REMOTE_OWN = {"Make the tools library directory": "{{ wp_tools_lib }}", "Make the WP-CLI download cache directory": "{{ wp_cli_cache_dir }}", "Age out the WP-CLI download cache": "/etc/tmpfiles.d/wp-cli-cache.conf"}

    # and the provisioning secrets: written straight to the VM, never staged (a staging directory on the controller would hold a copy of the master secret), root-only, no_log (test below)
    REMOTE_SECRETS = ("Make the directory of the provisioning secrets", "Write the master secret for wp-site-provision", "Write the mail provider key for wp-site-provision",
                      "Take the mail provider key away when the vault has none")

    def test_the_provisioning_secrets_are_root_only_never_logged_and_only_what_the_vault_holds(self):
        d, master, key, gone = (self.by[n] for n in self.REMOTE_SECRETS)
        self.assertEqual((d["ansible.builtin.file"]["path"], d["ansible.builtin.file"]["mode"], d["ansible.builtin.file"]["owner"], d["ansible.builtin.file"]["group"]), ("{{ wp_provision_secret_dir }}", "0700", "root", "root"))
        for t, dest, value in ((master, "master_secret", "{{ wp_master_secret }}"), (key, "resend_key", "{{ dellwp_wp_resend_key }}")):
            body = t["ansible.builtin.copy"]
            self.assertEqual((body["dest"], body["content"], body["mode"], body["owner"], body["group"]), ("{{ wp_provision_secret_dir }}/" + dest, value, "0400", "root", "root"), t["name"])
            self.assertIs(t.get("no_log"), True, t["name"]); self.assertIs(t.get("diff"), False, t["name"])
            self.assertNotIn("delegate_to", t, t["name"] + " is a task on the VM")
        self.assertNotIn("when", master, "the master secret is always written")
        self.assertEqual(key["when"], "dellwp_wp_resend_key is defined")
        self.assertEqual((gone["ansible.builtin.file"]["state"], gone["ansible.builtin.file"]["path"], gone["when"]), ("absent", "{{ wp_provision_secret_dir }}/resend_key", "dellwp_wp_resend_key is not defined"))

    def test_nothing_is_written_on_the_vm_except_by_rsync_two_directories_one_tmpfiles_rule_and_the_enabled_timers(self):
        for t in self.tasks:
            for mod in ("ansible.builtin.template", "ansible.builtin.copy"):
                if mod in t and t["name"] not in self.REMOTE_OWN and t["name"] not in self.REMOTE_SECRETS:
                    self.assertEqual((t.get("delegate_to"), t.get("become"), t.get("check_mode")), ("localhost", False, False), t["name"] + ": rendering happens on the controller, in check mode too, without sudo")
                    self.assertTrue(t[mod]["dest"].startswith("{{ wp_stage.path }}/"), t["name"] + " must write into the staging directory")
                    self.assertTrue(t["name"].startswith("Stage ") or t["name"] == "Write the lists of the staged files", t["name"])
            if "ansible.builtin.file" in t and t["name"] not in self.REMOTE_OWN and t["name"] not in self.REMOTE_SECRETS:
                self.assertEqual((t.get("delegate_to"), t.get("become")), ("localhost", False), t["name"])
        for name, path in self.REMOTE_OWN.items():
            t = self.by[name]
            body = next(v for k, v in t.items() if k in ("ansible.builtin.file", "ansible.builtin.copy"))
            self.assertEqual((body.get("path") or body.get("dest"), body["owner"], body["group"]), (path, "root", "root"), name)
            self.assertNotIn("delegate_to", t, name + " is a task on the VM")
        self.assertEqual(self.by["Make the tools library directory"]["ansible.builtin.file"]["path"], "{{ wp_tools_lib }}")

    def test_staging_never_counts_as_a_change_and_never_prints_a_diff(self):
        staged = [t for t in self.tasks if t.get("delegate_to") == "localhost"]
        self.assertGreaterEqual(len(staged), 12)
        for t in staged:
            self.assertIs(t.get("changed_when"), False, t["name"] + ": staging is rewritten every run; counting it would hide the real changed=0 of the sends")
            self.assertIs(t.get("diff"), False, t["name"] + ": a diff of every staged file buries the real differences")

    def test_the_syncs_are_exactly_these_four_and_each_sends_only_what_was_staged(self):
        syncs = [t["name"] for t in self.tasks if "ansible.posix.synchronize" in t]
        self.assertEqual(sorted(syncs), sorted(self.SENDS))
        for name, (sub, dest, mode) in self.SENDS.items():
            s = self.sync(name)
            self.assertEqual((s["src"], s["dest"]), ("{{ wp_stage.path }}/%s/" % sub, dest), name)
            opts = s["rsync_opts"]
            self.assertIn("--files-from={{ wp_stage.path }}/lists/%s.list" % sub, opts, "the destination directory itself must never be touched: /usr/local/sbin is root:staff")
            self.assertIn("--chown=root:root", opts); self.assertIn("--chmod=%s" % mode, opts)
            self.assertFalse([o for o in opts if "delete" in o], "nothing on the VM is ever deleted by these syncs")
            self.assertEqual((s["archive"], s["checksum"], s["perms"], s["owner"], s["group"]), (False, True, True, True, True), name)
            self.assertNotIn("delete", s); self.assertNotIn("recursive", s)
            for part in mode.split(","):
                self.assertLess(int(part[1:], 8) & 0o7022, 1, "no setuid, setgid, sticky or group/other write bit")
        self.assertEqual(self.sync("Send the WordPress units")["dest"], "/etc/systemd/system/")
        self.assertEqual(self.by["Send the WordPress units"]["notify"], "Reload systemd")

    def test_every_tool_module_and_unit_is_staged_where_its_send_looks(self):
        tools = [i["dest"] for i in self.by["Stage the tools"]["loop"]]
        self.assertEqual(len(tools), len(set(tools)))
        self.assertTrue({"wp-site-import", "wp-site-backup", "wp-site-delete", "wp-restore-drill"} <= set(tools))
        for t in self.by["Stage the tools"], self.by["Stage the WordPress job units"]:
            self.assertEqual(t["ansible.builtin.template"]["dest"].rsplit("/", 1)[0], "{{ wp_stage.path }}/" + ("bin" if t is self.by["Stage the tools"] else "units"))
        self.assertEqual(self.by["Stage the tools library"]["ansible.builtin.template"]["dest"], "{{ wp_stage.path }}/lib/common.sh")
        self.assertEqual(self.by["Stage the Python modules of wp-site-import"]["ansible.builtin.copy"]["dest"], "{{ wp_stage.path }}/lib/{{ item }}")
        inline = [t["ansible.builtin.copy"]["dest"] for t in self.tasks if t["name"].startswith("Stage the ") and ("service" in t["name"] or "timer" in t["name"]) and "ansible.builtin.copy" in t]
        self.assertEqual(len(inline), 6)
        for d in inline:
            self.assertRegex(d, r"^\{\{ wp_stage.path \}\}/units/wp-[a-z-]+\.(service|timer)$")

    def test_no_staging_or_sending_task_can_be_skipped_by_a_condition(self):
        for t in self.tasks:
            if t["name"].startswith(("Stage ", "Send ", "Find the staged", "Write the lists", "Make the staging", "Remove the staging")):
                self.assertNotIn("when", t, t["name"] + ": a skipped step would leave a stale or empty file set to be sent")
                self.assertNotIn("ignore_errors", t, t["name"]); self.assertNotIn("failed_when", t, t["name"])

    def test_the_order_stage_then_send_then_reload_then_enable_then_clean_up(self):
        names = [t["name"] for t in self.tasks]
        last_stage = max(i for i, n in enumerate(names) if n.startswith("Stage "))
        first_send = min(i for i, n in enumerate(names) if n.startswith("Send "))
        self.assertLess(names.index("Make the staging subdirectories"), min(i for i, n in enumerate(names) if n.startswith("Stage ")))
        self.assertLess(last_stage, names.index("Find the staged files"))
        self.assertLess(names.index("Write the lists of the staged files"), first_send)
        self.assertLess(max(i for i, n in enumerate(names) if n.startswith("Send ")), names.index("Reload systemd now if a unit changed"))
        for n in ("Enable the backup freshness check timer", "Enable the monthly restore drill timer", "Enable the archive purge timer"):
            self.assertGreater(names.index(n), names.index("Reload systemd now if a unit changed"))
        self.assertGreater(names.index("Remove the staging directory"), max(i for i, n in enumerate(names) if n.startswith("Send ")))

    def test_the_lists_are_the_staged_files_and_nothing_else(self):
        w = self.by["Write the lists of the staged files"]
        self.assertEqual(w["ansible.builtin.copy"]["dest"], "{{ wp_stage.path }}/lists/{{ item.item }}.list")
        f = self.by["Find the staged files"]
        self.assertEqual(f["loop"], ["bin", "lib", "units", "tpl"]); self.assertEqual(f["ansible.builtin.find"]["file_type"], "file")
        self.assertIs(f["ansible.builtin.find"]["recurse"], True, "the site templates sit in a subdirectory")
        env = jinja2.Environment(keep_trailing_newline=True)    # Ansible puts back the newline Jinja strips
        env.filters["relpath"] = os.path.relpath             # Ansible's filter
        render = lambda item, paths: env.from_string(w["ansible.builtin.copy"]["content"]).render(item={"item": item, "files": [{"path": x} for x in paths]}, wp_stage={"path": "/s"})  # noqa: E731
        self.assertEqual(render("lib", ["/s/lib/b", "/s/lib/a"]), "a\nb\n", "a flat directory lists plain file names, as it always did")
        self.assertEqual(render("tpl", ["/s/tpl/wp/site_env.j2", "/s/tpl/compose_stack.service.j2"]), "compose_stack.service.j2\nwp/site_env.j2\n", "a nested one lists the path inside it")

    # --- what the rsync options really do, with the real rsync
    def run_rsync(self, sub, user_chown):
        s = self.sync(next(n for n, v in self.SENDS.items() if v[0] == sub))
        stage = self.tmp / "stage"
        opts = [o.replace("{{ wp_stage.path }}", str(stage)).replace("--chown=root:root", "--chown=" + user_chown) for o in s["rsync_opts"]]
        r = subprocess.run(["rsync", "-c", "-p", "-o", "-g", "--itemize-changes"] + opts + [str(stage / sub) + "/", str(self.tmp / "dest") + "/"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [l for l in r.stdout.splitlines() if l.strip()]

    def tree(self):
        stage = self.tmp / "stage"
        (stage / "bin").mkdir(parents=True); (stage / "lists").mkdir()
        (stage / "bin/wp-a").write_text("one\n"); (stage / "bin/wp-b").write_text("two\n"); (stage / "bin/not-listed").write_text("x\n")
        (stage / "lists/bin.list").write_text("wp-a\nwp-b\n")
        dest = self.tmp / "dest"; dest.mkdir(); (dest / "other").write_text("not ours\n")
        os.chmod(dest, 0o2775)
        return dest

    def test_rsync_sets_mode_and_owner_of_the_listed_files_and_leaves_the_directory_and_strangers_alone(self):
        dest = self.tree()
        me = "%d:%d" % (os.getuid(), os.getgid())
        first = self.run_rsync("bin", me)
        self.assertEqual(len(first), 2, first)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["other", "wp-a", "wp-b"], "a file that was not listed is not sent")
        for n in ("wp-a", "wp-b"):
            self.assertEqual(stat.S_IMODE((dest / n).stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o2775, "the destination directory keeps its mode (setgid, group write)")
        self.assertEqual((dest / "other").read_text(), "not ours\n")

    def test_a_second_run_changes_nothing_and_a_changed_file_or_a_drifted_mode_is_sent_again(self):
        dest = self.tree()
        me = "%d:%d" % (os.getuid(), os.getgid())
        self.run_rsync("bin", me)
        self.assertEqual(self.run_rsync("bin", me), [], "idempotent: the second run reports nothing")
        os.utime(self.tmp / "stage/bin/wp-a", (1, 1))
        self.assertEqual(self.run_rsync("bin", me), [], "times do not matter, content does")
        (self.tmp / "stage/bin/wp-a").write_text("changed\n")
        again = self.run_rsync("bin", me)
        self.assertEqual(len(again), 1); self.assertIn("wp-a", again[0])
        os.chmod(dest / "wp-b", 0o777)
        again = self.run_rsync("bin", me)
        self.assertEqual(len(again), 1); self.assertIn("wp-b", again[0])
        self.assertEqual(stat.S_IMODE((dest / "wp-b").stat().st_mode), 0o750, "a drifted mode is put back")

    def test_the_site_templates_keep_their_subdirectory_and_get_directory_and_file_modes_and_a_second_run_changes_nothing(self):
        stage, dest = self.tmp / "stage", self.tmp / "dest"
        (stage / "tpl/wp").mkdir(parents=True); (stage / "lists").mkdir(); dest.mkdir()
        (stage / "tpl/wp/site_env.j2").write_text("env\n"); (stage / "tpl/compose_stack.service.j2").write_text("unit\n"); (stage / "tpl/stray.j2").write_text("x\n")
        (stage / "lists/tpl.list").write_text("compose_stack.service.j2\nwp/site_env.j2\n")
        me = "%d:%d" % (os.getuid(), os.getgid())
        first = self.run_rsync("tpl", me)
        self.assertTrue(first, "something was sent")
        self.assertEqual(sorted(str(p.relative_to(dest)) for p in dest.rglob("*")), ["compose_stack.service.j2", "wp", "wp/site_env.j2"], "the subdirectory is made, the unlisted file is not sent")
        self.assertEqual(stat.S_IMODE((dest / "wp").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((dest / "wp/site_env.j2").stat().st_mode), 0o644)
        self.assertEqual(self.run_rsync("tpl", me), [], "idempotent")
        (stage / "tpl/wp/site_env.j2").write_text("changed\n")
        self.assertEqual(len(self.run_rsync("tpl", me)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
