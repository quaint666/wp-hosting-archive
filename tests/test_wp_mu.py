#!/usr/bin/env python3
"""Offline tests of the must-use plugins of the hosting service (templates/wp/mu/) and of everything that wires them in: the wp-config constants, the compose mount, the deploy tasks, the pinned Two
Factor plugin, the editable site fields and the wp-site-2fa-reset tool.

What these cannot do is RUN the PHP: PHP is not installed where the repo is edited and no container may be started there. They check its shape (the guard, balanced brackets, no dangerous functions,
every constant it reads is one the wp-config template defines, and the other way round). The behaviour is tested by tests/php/, which this file runs when `php` is installed and reports as skipped
when it is not; run it with the container command in tests/php/run_all.php before deploying a change to these files.
Run: python3 -I tests/test_wp_mu.py"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import jinja2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wp_backup import Base  # noqa: E402
from test_wp_sites_render import ROOT, base_vars, env, load, site_vars  # noqa: E402

MU = ROOT / "templates/wp/mu"
FILES = ("10-wp-hosting-login.php", "20-wp-hosting-two-factor.php.j2", "30-wp-hosting-mail.php", "40-wp-hosting-timing.php", "50-wp-hosting-plugin-requests.php")
BANNED = ("eval", "exec", "system", "shell_exec", "passthru", "proc_open", "popen", "assert", "create_function", "unserialize", "file_put_contents", "fopen", "curl_exec", "extract",
          "call_user_func", "preg_replace_callback", "base64_decode", "move_uploaded_file", "chmod", "unlink")


def rendered(name):
    v = base_vars()
    return env().get_template("wp/mu/" + name).render(**site_vars(v, v["wp_sites"][0]))


def code_only(text):
    """The PHP with comments and string contents blanked, so brackets and function calls in text do not count. Not a parser; enough for shape checks."""
    out, i, n = [], 0, len(text)
    while i < n:
        c, two = text[i], text[i:i + 2]
        if two == "/*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif two == "//" or c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in "'\"":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(c + c)
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


class Shape(unittest.TestCase):
    def test_every_file_is_a_php_file_with_the_guard_and_without_a_closing_tag(self):
        for f in FILES:
            t = rendered(f)
            self.assertTrue(t.startswith("<?php\n"), f)
            self.assertNotIn("?>", code_only(t), f + ": no closing tag (trailing output breaks headers)")
            self.assertRegex(t[:3000], r"if \( ! defined\( 'ABSPATH' \) \) \{\n\texit;\n\}", f + ": a request to the file itself does nothing")
            self.assertNotIn("{{", t); self.assertNotIn("{%", t); self.assertNotIn("{#", t.split("*/", 1)[1], f + ": no Jinja left over")

    def test_brackets_balance(self):
        for f in FILES:
            c = code_only(rendered(f))
            for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
                self.assertEqual(c.count(a), c.count(b), f + " " + a + b)

    def test_nothing_dangerous_is_called(self):
        for f in FILES:
            c = code_only(rendered(f))
            for fn in BANNED:
                self.assertIsNone(re.search(r"(?<![\w>:$])" + fn + r"\s*\(", c), "%s calls %s()" % (f, fn))
            self.assertNotRegex(c, r"\$\$", f + ": no variable variables")
            self.assertNotRegex(c, r"`", f + ": no backticks")

    def test_the_only_require_is_the_pinned_plugin_inside_its_own_directory(self):
        loader = code_only(rendered("20-wp-hosting-two-factor.php.j2"))
        requires = re.findall(r"\b(?:require|include)(?:_once)?\b[^;]*;", loader)
        self.assertEqual(len(requires), 1)
        self.assertIn("WP_HOSTING_TWO_FACTOR_DIR", requires[0]); self.assertIn("two-factor.php", (MU / "20-wp-hosting-two-factor.php.j2").read_text())
        for f in ("10-wp-hosting-login.php", "30-wp-hosting-mail.php"):
            self.assertNotRegex(code_only(rendered(f)), r"\b(?:require|include)(?:_once)?\b", f)
        self.assertIn("define( 'WP_HOSTING_TWO_FACTOR_DIR', __DIR__ . '/two-factor-%s' );" % base_vars()["wp_two_factor_version"], rendered("20-wp-hosting-two-factor.php.j2"))

    def test_every_function_is_prefixed_and_guarded_against_a_second_load(self):
        for f in FILES:
            t = rendered(f)
            names = re.findall(r"^\s*function (\w+)\s*\(", code_only(t), re.M)
            self.assertTrue(names, f)
            for n in names:
                self.assertTrue(n.startswith("wph_"), "%s: %s is not prefixed" % (f, n))
            self.assertRegex(t, r"if \( ! function_exists\( 'wph_\w+' \) \) \{", f)

    def test_the_constants_the_plugins_read_are_the_ones_wp_config_defines_and_none_is_dead(self):
        cfg = (ROOT / "templates/wp/site_wp-config.php.j2").read_text()
        defined = set(re.findall(r"define\( '(WP_HOSTING_[A-Z0-9_]+)'", cfg))
        read = set()
        for f in FILES:
            read |= set(re.findall(r"WP_HOSTING_[A-Z0-9_]+", rendered(f)))
        loader_own = {"WP_HOSTING_TWO_FACTOR_DIR"}
        self.assertEqual(read - defined - loader_own, set(), "read by a plugin but never defined by wp-config")
        self.assertEqual(defined - read, set(), "defined by wp-config and read by nothing")

    def test_a_site_setting_in_wp_config_is_never_read_from_the_database(self):
        for f in FILES:
            c = code_only(rendered(f))
            self.assertNotRegex(c, r"get_option\(\s*'(WP_HOSTING|wph_(mail_key|login|2fa))", f)
        mail = code_only(rendered("30-wp-hosting-mail.php"))
        self.assertIn("wph_mail_setting( 'WP_HOSTING_MAIL_KEY'", (MU / "30-wp-hosting-mail.php").read_text())
        self.assertNotIn("update_option( 'wph_mail_key'", mail)

    def test_the_plugin_request_screen_answers_before_wordpress_and_never_installs_anything(self):
        text = (MU / "50-wp-hosting-plugin-requests.php").read_text()
        c = code_only(text)
        self.assertIn("add_action( 'wp_ajax_install-plugin', 'wph_pr_ajax', 0 );", text, "WordPress registers its own handler at priority 1: this one must come first")
        self.assertIn("'upload-plugin' === $action", text)
        for forbidden in ("Plugin_Upgrader", "plugins_api", "WP_Filesystem", "download_url", "unzip_file", "activate_plugin", "wp_remote_get", "wp_remote_post"):
            self.assertNotIn(forbidden, c, "this file files requests; it installs, downloads and activates nothing")
        self.assertEqual(re.findall(r"update_option\(\s*'(\w+)'", text), ["wph_plugin_requests"], "the site writes only its own option; the host's answers are another one")

    def test_the_visitor_address_is_only_ever_remote_addr(self):
        c = code_only(rendered("10-wp-hosting-login.php"))
        self.assertIn("REMOTE_ADDR", (MU / "10-wp-hosting-login.php").read_text())
        self.assertNotRegex((MU / "10-wp-hosting-login.php").read_text().split("*/", 1)[1], r"HTTP_(X_FORWARDED|CF_|CLIENT_IP|X_REAL)")
        self.assertNotIn("getallheaders", c)


class WpConfigAndCompose(unittest.TestCase):
    def setUp(self):
        self.v = base_vars()
        self.ctx = site_vars(self.v, self.v["wp_sites"][0])

    def cfg(self, **site):
        ctx = dict(self.ctx, wp_s={**self.ctx["wp_s"], **site})
        ctx.setdefault("dellwp_wp_resend_key", "re_testkey123")
        return env().get_template("wp/site_wp-config.php.j2").render(**ctx)

    def test_the_mu_directory_is_named_outside_wp_content_and_the_constants_follow_the_variables(self):
        t = self.cfg()
        v = self.v
        self.assertIn("define( 'WPMU_PLUGIN_DIR', '%s' );" % v["wp_mu_mount"], t)
        for name, want in (("WP_HOSTING_LOGIN_LIMIT_IP", v["wp_login_limit_ip"]), ("WP_HOSTING_LOGIN_LIMIT_USER", v["wp_login_limit_user"]), ("WP_HOSTING_LOGIN_WINDOW_S", v["wp_login_window_s"]),
                           ("WP_HOSTING_MAIL_PER_HOUR", v["wp_mail_per_hour"]), ("WP_HOSTING_MAIL_PER_DAY", v["wp_mail_per_day"]), ("WP_HOSTING_PLUGIN_REQUESTS_MAX", v["wp_plugin_requests_max"])):
            self.assertIn("define( '%s', %d );" % (name, want), t)
        self.assertIn("define( 'WP_HOSTING_2FA_ROLES', '%s' );" % v["wp_2fa_roles"], t)
        self.assertLess(t.index("WPMU_PLUGIN_DIR"), t.index("wp-settings.php"), "defined before WordPress loads its plugins")

    def test_the_flags_default_to_closed_and_follow_the_site(self):
        t = self.cfg()
        for name in ("XMLRPC", "APP_PASSWORDS", "USER_ENUM"):
            self.assertIn("define( 'WP_HOSTING_ALLOW_%s', false );" % name, t)
        t = self.cfg(allow_xmlrpc=True, allow_app_passwords=True, allow_user_enum=True)
        for name in ("XMLRPC", "APP_PASSWORDS", "USER_ENUM"):
            self.assertIn("define( 'WP_HOSTING_ALLOW_%s', true );" % name, t)

    def test_the_site_defaults_close_everything_and_keep_mail_in_the_log(self):
        d = self.v["wp_site_defaults"]
        self.assertEqual((d["allow_xmlrpc"], d["allow_app_passwords"], d["allow_user_enum"], d["mail_mode"]), (False, False, False, "log"))

    def test_the_ai1wm_backups_path_is_set_only_when_the_export_switch_is_on(self):
        self.assertNotIn("AI1WM_", self.cfg())
        self.assertFalse(self.v["wp_site_defaults"]["ai1wm_export"])
        self.assertIn("define( 'AI1WM_DEFAULT_BACKUPS_PATH', WP_CONTENT_DIR . '/uploads/ai1wm-backups' );", self.cfg(ai1wm_export=True))

    def test_the_key_is_in_the_config_only_in_send_mode(self):
        self.assertNotIn("WP_HOSTING_MAIL_KEY", self.cfg(mail_mode="log"))
        self.assertNotIn("re_testkey123", self.cfg(mail_mode="log"))
        t = self.cfg(mail_mode="send")
        self.assertIn("define( 'WP_HOSTING_MAIL_KEY', 're_testkey123' );", t)
        self.assertIn("define( 'WP_HOSTING_MAIL_MODE', 'send' );", t)

    def test_the_sender_is_the_site_at_the_mail_subdomain(self):
        name = self.ctx["wp_s"]["name"]
        self.assertIn("define( 'WP_HOSTING_MAIL_FROM', '%s <%s@mail.%s>' );" % (name, name, self.v["wp_domain"]), self.cfg())
        self.assertEqual(self.v["wp_mail_sender_domain"], "mail." + self.v["wp_domain"])

    def test_the_compose_file_mounts_the_directory_read_only_and_nothing_else_changed(self):
        doc = yaml.safe_load(env().get_template("wp/site_compose.yml.j2").render(**self.ctx))
        vols = doc["services"]["php"]["volumes"]
        self.assertIn("%s:%s:ro" % (self.v["wp_mu_dir"], self.v["wp_mu_mount"]), vols)
        self.assertEqual(len([x for x in vols if "wp-mu" in x]), 1)
        self.assertNotIn("wp-mu", " ".join(doc["services"]["db"].get("volumes", [])))
        self.assertTrue(all(x.endswith(":ro") or "uploads" in x for x in vols), "the only writable mount stays uploads")

    def test_php_may_read_the_directory_wordpress_loads_the_plugins_from(self):
        # dellwp 2026-10-25: open_basedir was /var/www/html:/tmp, is_dir() on the mount said no, and WordPress loaded none of the must-use plugins without a word
        ini = env().get_template("wp/site_php.ini.j2").render(**self.ctx)
        allowed = re.search(r"^open_basedir = (.+)$", ini, re.M).group(1).split(":")
        mount = self.v["wp_mu_mount"]
        self.assertIn(mount, allowed)
        cfg_dir = re.search(r"define\( 'WPMU_PLUGIN_DIR', '([^']+)' \);", self.cfg()).group(1)
        compose = yaml.safe_load(env().get_template("wp/site_compose.yml.j2").render(**self.ctx))["services"]["php"]["volumes"]
        mounted = [v.split(":")[1] for v in compose if v.split(":")[0] == self.v["wp_mu_dir"]]
        self.assertEqual((cfg_dir, mounted), (mount, [mount]), "wp-config, the mount and open_basedir name the same directory")
        self.assertTrue(any(cfg_dir == a or cfg_dir.startswith(a.rstrip("/") + "/") for a in allowed))
        for broad in ("/", "/var", "/var/www", "/etc", "/usr"):
            self.assertNotIn(broad, allowed, "open_basedir stays narrow")
        self.assertEqual(sorted(allowed), sorted(["/var/www/html", mount, "/tmp"]))

    def test_the_mu_directory_is_not_inside_a_site_or_a_wp_content(self):
        self.assertTrue(self.v["wp_mu_dir"].startswith(self.v["wp_data_mount"]))
        self.assertNotIn("/sites/", self.v["wp_mu_dir"]); self.assertNotIn("wp-content", self.v["wp_mu_dir"])


class Tasks(unittest.TestCase):
    def setUp(self):
        self.tasks = load("tasks/wp_mu_common.yml")
        self.by = {t["name"]: t for t in self.tasks}
        self.v = base_vars()

    def test_the_plugin_is_fetched_with_a_checksum_and_the_checksum_is_the_one_in_the_vars(self):
        t = self.by["Fetch the Two Factor plugin (pinned by checksum)"]["ansible.builtin.get_url"]
        self.assertEqual(t["checksum"], "sha256:{{ wp_two_factor_sha256 }}")
        self.assertTrue(t["url"].startswith("https://downloads.wordpress.org/plugin/two-factor."))
        self.assertRegex(self.v["wp_two_factor_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(self.v["wp_two_factor_version"], r"^\d+\.\d+\.\d+$")
        self.assertIn(self.v["wp_two_factor_version"], t["url"].replace("{{ wp_two_factor_version }}", self.v["wp_two_factor_version"]))

    def test_the_unpack_makes_the_versioned_folder_last_and_is_idempotent(self):
        t = self.by["Unpack the Two Factor plugin"]["ansible.builtin.command"]
        self.assertEqual(t["creates"], "{{ wp_mu_dir }}/two-factor-{{ wp_two_factor_version }}/two-factor.php")
        script = t["argv"][2]
        self.assertTrue(script.startswith("set -eu"))
        self.assertLess(script.index("python3 -m zipfile -e"), script.index("mv "), "the folder appears only after the unpack finished")
        self.assertIn("umask 022", script)
        self.assertLess(script.index("mv "), script.index("rmdir"), "the temporary folder is removed after the plugin has been moved out of it")

    def test_other_versions_are_removed_and_the_pinned_one_never(self):
        t = self.by["Remove the versions that are not the pinned one"]
        self.assertIn("rejectattr('path', 'eq', wp_mu_dir ~ '/two-factor-' ~ wp_two_factor_version)", t["loop"])
        self.assertEqual(t["ansible.builtin.file"]["state"], "absent")
        self.assertEqual(self.by["Look for other versions of the Two Factor plugin"]["ansible.builtin.find"]["patterns"], "two-factor-*")

    def test_everything_is_root_owned_and_not_writable_by_others(self):
        for name in ("Make the must-use plugin directory", "Deploy the must-use plugins", "Deploy the Two Factor loader", "Fetch the Two Factor plugin (pinned by checksum)"):
            d = next(v for k, v in self.by[name].items() if k.startswith("ansible.builtin."))
            self.assertEqual((d["owner"], d["group"]), ("root", "root"), name)
            self.assertIn(d["mode"], ("0644", "0755"), name)

    def test_the_files_deployed_are_exactly_the_ones_in_the_source_directory(self):
        loop = self.by["Deploy the must-use plugins"]["loop"]
        sources = sorted(p.name for p in MU.iterdir() if p.is_file())
        self.assertEqual(sorted(loop + ["20-wp-hosting-two-factor.php.j2"]), sources)
        self.assertEqual(self.by["Deploy the Two Factor loader"]["ansible.builtin.template"]["dest"], "{{ wp_mu_dir }}/20-wp-hosting-two-factor.php")

    # An unknown mail mode is refused by the registry's rules (tests/test_wp_site_model.py) and a site in mode send without the provider key by the provisioning tool
    # (tests/test_wp_site_provision.py: test_mail_send_needs_the_provider_key_file).

    def test_the_tool_for_a_locked_out_user_is_installed(self):
        tools = {t["name"]: t for t in load("tasks/wp_tools_common.yml")}
        self.assertIn({"src": "wp_site_2fa_reset.sh.j2", "dest": "wp-site-2fa-reset"}, tools["Stage the tools"]["loop"])


class Reset(Base):
    """wp-site-2fa-reset against a stand-in for wp-site-cli that records what it was asked."""
    def setUp(self):
        super().setUp()
        self.tool = self.install("wp_site_2fa_reset.sh.j2", "bin/wp-site-2fa-reset", wp_tools_bin=str(self.tmp / "bin"))
        (self.tmp / "bin/wp-site-cli").write_text(
            '#!/bin/bash\necho "$*" >> "%s/cli.calls"\ncase "$*" in\n'
            '  *"user get ghost"*) exit 1 ;;\n  *"user get "*) echo 42 ;;\n'
            '  *"user meta list"*) echo "${SHIM_LEFT:-0}" ;;\n'
            '  *"user meta delete"*) [ -f "%s/no_meta" ] && exit 1; exit 0 ;;\n'
            'esac\n' % (self.tmp / "shim", self.tmp / "shim"))
        (self.tmp / "bin/wp-site-cli").chmod(0o755)
        self.tty = self.tmp / "tty"

    def run_tool(self, *args, answer="wptest\n", **env):
        self.tty.write_text(answer)
        return self.run_script(self.tool, *args, WP_SITE_TTY=str(self.tty), **env)

    def calls(self):
        return self.log("cli.calls").splitlines()

    def test_it_clears_the_users_settings_and_sessions_after_the_name_is_typed(self):
        r = self.run_tool("wptest", "carol")
        self.assertEqual(r.returncode, 0, r.stderr)
        deletes = [c for c in self.calls() if "user meta delete" in c]
        keys = [c.split()[c.split().index("delete") + 2] for c in deletes]
        for k in ("_two_factor_enabled_providers", "_two_factor_totp_key", "_two_factor_backup_codes", "_two_factor_provider"):
            self.assertIn(k, keys)
        self.assertTrue(all(c.split()[c.split().index("delete") + 1] == "42" for c in deletes), "the numeric id, never the typed name")
        self.assertTrue([c for c in self.calls() if "user session destroy carol --all" in c])
        self.assertIn("cleared", r.stderr)
        for c in self.calls():
            self.assertIn("--skip-plugins", c); self.assertIn("--skip-themes", c)

    def test_the_wrong_name_changes_nothing(self):
        for answer in ("\n", "no\n", "WPTEST\n", "wptest2\n"):
            self.tmp.joinpath("shim/cli.calls").unlink(missing_ok=True)
            r = self.run_tool("wptest", "carol", answer=answer)
            self.assertEqual(r.returncode, 2, answer); self.assertIn("did not match", r.stderr)
            self.assertFalse([c for c in self.calls() if "delete" in c or "session" in c], answer)

    def test_a_user_that_does_not_exist_is_refused_before_asking(self):
        r = self.run_tool("wptest", "ghost")
        self.assertEqual(r.returncode, 2); self.assertIn("has no user 'ghost'", r.stderr)
        self.assertFalse([c for c in self.calls() if "delete" in c])

    def test_bad_arguments_are_refused(self):
        for args in ((), ("wptest",), ("nosuch", "carol"), ("wptest", "a b"), ("wptest", "x;id"), ("wptest", "../x"), ("wptest", "")):
            self.assertEqual(self.run_tool(*args).returncode, 2, args)
        self.assertFalse([c for c in self.calls() if "delete" in c])

    def test_settings_that_are_still_there_after_the_delete_fail_the_tool(self):
        r = self.run_tool("wptest", "carol", SHIM_LEFT="2")
        self.assertEqual(r.returncode, 2); self.assertIn("2 two-factor setting(s) of 'carol' are still there", r.stderr)


@unittest.skipUnless(shutil.which("php"), "php is not installed here: run the container command in tests/php/run_all.php")
class PhpBehaviour(unittest.TestCase):
    def test_the_php_suite_passes(self):
        r = subprocess.run(["php", str(ROOT / "tests/php/run_all.php")], capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("scenarios passed", r.stdout)


if __name__ == "__main__":
    unittest.main()
