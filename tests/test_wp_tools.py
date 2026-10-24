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
case " $* " in *" verify-checksums "*) if [ "${SHIM_VERIFY_FAIL:-0}" = 1 ]; then echo "Error: checksums do not match"; exit 1; fi; echo "Success: Verified 1 of 1 plugins."; exit 0 ;; esac
# like Docker: the container sees the caller's stdin only when -i is given
case " $* " in *" -i "*) { echo "--- stdin"; cat; } >> "$SHIM_LOG/docker.stdin" 2>/dev/null ;; esac
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
                 inventory_hostname="dellwp")
        self.ctx = v
        for d in ("bin", "lib", "shim", "log", "data/sites/wptest"):
            (self.tmp / d).mkdir(parents=True)
        (self.tmp / "data/sites/wptest/wp-config.php").write_text("<?php\n")
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        for src, dest in (("wp_tools_common.sh.j2", "lib/common.sh"), ("wp_site_cli.sh.j2", "bin/wp-site-cli"), ("wp_site_install.sh.j2", "bin/wp-site-install"),
                          ("wp_site_login_check.sh.j2", "bin/wp-site-login-check"), ("wp_site_ext.sh.j2", "bin/wp-site-ext"), ("wp_site_cron.sh.j2", "bin/wp-site-cron")):
            (self.tmp / dest).write_text(env.get_template(src).render(**v))
            if dest.startswith("bin/"):
                (self.tmp / dest).chmod(0o755)  # production: mode 0750 root:root from the play
        for name, body in (("docker", DOCKER), ("curl", CURL), ("logger", "#!/bin/bash\necho \"$*\" >> \"$SHIM_LOG/logger.log\"\n")):
            f = self.tmp / "shim" / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def run_tool(self, name, *args, stdin="", tty=None, installed=False, verify_fails=False, run_rc=None):
        env = {"PATH": "%s:/usr/bin:/bin" % (self.tmp / "shim"), "SHIM_LOG": str(self.tmp / "log"), "HOME": str(self.tmp)}
        if installed:
            env["SHIM_INSTALLED"] = "1"
        if verify_fails:
            env["SHIM_VERIFY_FAIL"] = "1"
        if run_rc is not None:
            env["SHIM_RUN_RC"] = str(run_rc)
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
                   ("plugin", "verify-checksums", "--all")]
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

    def test_core_download_is_allowed_only_in_the_one_form_wp_site_recover_uses(self):
        ok = ("core", "download", "--version=7.1.3", "--force", "--skip-content")
        self.assertEqual(self.run_tool("wp-site-cli", "wptest", "--code", *ok).returncode, 0)
        self.assertEqual(self.wp_calls(), [(True, list(ok))])
        for cmd in (("core", "download"), ("core", "download", "--version=7.1.3"), ("core", "download", "--force", "--skip-content"), ("core", "download", "--version=7.1.3", "--force"),
                    ("core", "download", "--version=7.1.3", "--force", "--skip-content", "--locale=de_DE"), ("core", "download", "--version=7.1.3;id", "--force", "--skip-content"),
                    ("core", "download", "--version=latest", "--force", "--skip-content"), ("core", "download", "--version=https://evil.example/w.zip", "--force", "--skip-content"),
                    ("core", "download", "--skip-content", "--force", "--version=7.1.3"), ("core", "download", "--version=7.1.3", "--force", "--skip-content", "--path=/x"),
                    ("plugin", "install", "x", "--skip-content"), ("plugin", "update", "x", "--force")):
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
        start = names.index("Start the site, or restart it when its compose file or database settings changed")
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
            self.assertEqual(e.from_string(start["state"]).render(wp_site_restart=restart).strip(), want, "the unit must act on the decision")
        reload_task = [t for t in tasks if t.get("name", "").startswith("Recreate only the php container")][0]
        self.assertEqual(reload_task["ansible.builtin.systemd"]["state"], "reloaded")
        self.assertIn("not (wp_site_restart | bool)", reload_task["when"])

    def test_only_live_sites_get_a_running_timer_and_systemd_is_reloaded_first(self):
        tasks = yaml.safe_load((ROOT / "tasks/wp_site_one_common.yml").read_text())
        t = [x for x in tasks if "wp-cron@" in str(x.get("ansible.builtin.systemd", {}).get("name", ""))][0]["ansible.builtin.systemd"]
        e = jinja2.Environment()
        for state, want in (("live", ("True", "started")), ("hold", ("False", "stopped"))):
            got = (e.from_string(t["enabled"]).render(wp_s={"state": state}), e.from_string(t["state"]).render(wp_s={"state": state}))
            self.assertEqual(got, want, state)
        tools = yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())
        units = [x for x in tools if x.get("name") == "Deploy the WordPress job units"][0]
        self.assertEqual(units["notify"], "Reload systemd")
        names = [x.get("name") for x in tools]
        self.assertGreater(names.index("Reload systemd now if a unit changed"), names.index("Deploy the WordPress job units"), "the reload must follow the units it loads")
        self.assertLess(names.index("Reload systemd now if a unit changed"), names.index("Deploy the backup freshness check service"), "the tools tasks end by deploying their own timer; the sites come after all of them")
        wrapper = yaml.safe_load((ROOT / "dellwp/wp_sites_dellwp.yml").read_text())[0]
        self.assertIn("Reload systemd", [h["name"] for h in wrapper["handlers"]])

    # --- how they are installed
    def test_the_image_tag_is_built_from_the_two_pins(self):
        m = __import__("re").search(r"php[0-9]+[.][0-9]+", self.v["wp_image_php"]).group(0)
        self.assertEqual(self.v["wp_image_cli"], "wordpress:cli-%s-%s" % (self.v["wp_cli_version"], m))
        self.assertIn("wordpress:cli-%s-%s" % (self.v["wp_cli_version"], m), (self.tmp / "bin/wp-site-cli").read_text())

    def test_the_tasks_install_the_tools_root_only(self):
        tasks = yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())
        deploy = [t for t in tasks if t.get("name") == "Deploy the tools"][0]["ansible.builtin.template"]
        self.assertEqual((deploy["owner"], deploy["group"], deploy["mode"]), ("root", "root", "0750"))
        lib = [t for t in tasks if t.get("name") == "Deploy the tools library"][0]["ansible.builtin.template"]
        self.assertEqual((lib["owner"], lib["mode"]), ("root", "0644"))
        # what the tasks DO (comments are not parsed): no task writes a sudoers file or sets a setuid mode
        plays = yaml.safe_load((ROOT / "dellwp/wp_sites_dellwp.yml").read_text())
        text = yaml.dump(tasks) + yaml.dump(plays)
        self.assertNotIn("sudoers", text)
        for mode in __import__("re").findall(r"mode: '?\"?([0-7]{3,4})", text):
            self.assertLess(int(mode, 8) & 0o7000, 1, "setuid/setgid/sticky bit in mode " + mode)


if __name__ == "__main__":
    unittest.main(verbosity=1)
