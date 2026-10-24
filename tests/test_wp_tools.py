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
case " $* " in *" is-installed "*) [ "${SHIM_INSTALLED:-0}" = 1 ] && exit 0 || exit 1 ;; esac
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
        v = dict(self.v, wp_tools_bin=str(self.tmp / "bin"), wp_tools_lib=str(self.tmp / "lib"), wp_data_mount=str(self.tmp / "data"))
        self.ctx = v
        for d in ("bin", "lib", "shim", "log", "data/sites/wptest"):
            (self.tmp / d).mkdir(parents=True)
        (self.tmp / "data/sites/wptest/wp-config.php").write_text("<?php\n")
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
        for src, dest in (("wp_tools_common.sh.j2", "lib/common.sh"), ("wp_site_cli.sh.j2", "bin/wp-site-cli"), ("wp_site_install.sh.j2", "bin/wp-site-install"),
                          ("wp_site_login_check.sh.j2", "bin/wp-site-login-check")):
            (self.tmp / dest).write_text(env.get_template(src).render(**v))
            if dest.startswith("bin/"):
                (self.tmp / dest).chmod(0o755)  # production: mode 0750 root:root from the play
        for name, body in (("docker", DOCKER), ("curl", CURL), ("logger", "#!/bin/bash\nexit 0\n")):
            f = self.tmp / "shim" / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def run_tool(self, name, *args, stdin="", tty=None, installed=False):
        env = {"PATH": "%s:/usr/bin:/bin" % (self.tmp / "shim"), "SHIM_LOG": str(self.tmp / "log"), "HOME": str(self.tmp)}
        if installed:
            env["SHIM_INSTALLED"] = "1"
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

    def test_with_plugins_is_explicit_and_only_for_code_mode(self):
        self.assertEqual(self.run_tool("wp-site-cli", "wptest", "--with-plugins", "core", "version").returncode, 2)
        self.assertEqual(self.log("docker.argv"), "")
        self.assertEqual(self.run_tool("wp-site-cli", "wptest", "--code", "--with-plugins", "wc", "update").returncode, 0)
        a = self.argv()
        self.assertNotIn("--skip-plugins", a)
        self.assertIn("--allow-root", a)

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
