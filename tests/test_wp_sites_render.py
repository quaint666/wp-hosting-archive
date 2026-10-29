#!/usr/bin/env python3
"""Offline checks of the WordPress site templates (templates/wp/) and the helpers the other suites share (the real variables, the fixture site, the Jinja environment of the play).

Renders every template with the real variables (vars/wp.yml, host_vars/dellwp/vars.yml, the VM's declared size in host_vars/delloptiplex3040/vars.yml) and stub secrets. Plain Jinja cannot
see what Ansible does to a value (an empty string turned into None, the real `hash` filter, template trimming), so a pass here is not a pass of the play: it catches a broken template. The fit
rules are tests/test_wp_site_model.py's now. Run: python3 tests/test_wp_sites_render.py
"""
import ast
import copy
import hashlib
import random
import re
import sys
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(path):
    return yaml.safe_load((ROOT / path).read_text())


# The repo declares no site (a site is a record on the VM), so the suites that render a site's files need one: base_vars() adds it as `wp_sites`, a variable that exists in the tests only. Everything
# else is the real variables, including wp_closed_floor.
FIXTURE_SITE = {"name": "wptest", "port": 9101, "quota_id": 1001, "state": "live", "update_policy": "auto"}


def fixture_vars_text():
    """host_vars/dellwp/vars.yml as it is: the tests that need a copy of it in a throwaway repo ask for it here."""
    return (ROOT / "host_vars/dellwp/vars.yml").read_text()


def env():
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.StrictUndefined,
                           trim_blocks=True, keep_trailing_newline=True)
    e.filters["hash"] = lambda value, algo="sha1": hashlib.new(algo, str(value).encode()).hexdigest()
    # Ansible's difference and intersect return the result of a set operation: any order. Model that, so a template that depends on the order fails here.
    e.filters["difference"] = lambda a, b: random.sample([x for x in a if x not in b], len([x for x in a if x not in b]))
    e.filters["intersect"] = lambda a, b: random.sample([x for x in a if x in b], len([x for x in a if x in b]))
    return e


def inventory_hostvars():
    """{host: {"overlay_ip": ...}} for every inventory host that has an overlay address, so variables like wp_ctl_panel_from (hostvars['aopenpanel']['overlay_ip']) resolve in the tests."""
    out = {}

    def walk(node):
        for name, host in (node.get("hosts") or {}).items():
            if (host or {}).get("overlay_ip"):
                out[name] = {"overlay_ip": host["overlay_ip"]}
        for key, child in node.items():
            if isinstance(child, dict) and key not in ("hosts", "vars"):
                walk(child)
        for child in (node.get("children") or {}).values():
            walk(child or {})
    walk(load("inventory.yml"))
    return out


def base_vars():
    v = {"hostvars": inventory_hostvars()}
    v.update(load("vars/wp.yml"))
    v.update(load("host_vars/dellwp/vars.yml"))
    v["wp_sites"] = [dict(FIXTURE_SITE)]               # test-only: the one live site the template tests render
    v["wp_master_secret"] = "stub-master-secret"
    # Ansible templates variables that refer to other variables (wp_image_cli is built from two lines): do the same here, so a test sees the value the play would use.
    e = jinja2.Environment(undefined=jinja2.StrictUndefined)
    e.filters["regex_search"] = lambda value, pattern: (__import__("re").search(pattern, value) or [None])[0]
    for _ in range(3):
        for key, val in list(v.items()):
            if isinstance(val, str) and "{{" in val:
                v[key] = e.from_string(val).render(**v)
    return v


def dellwp_vm():
    """dellwp's entry in the Dell's declared VMs (host_vars/delloptiplex3040/vars.yml): its memory is what the play's RAM rule is checked against, so tests read it here and never hardcode a size."""
    return next(vm for vm in load("host_vars/delloptiplex3040/vars.yml")["proxmox_vms"] if vm["name"] == "dellwp")


def site_vars(v, site):
    s = {**v["wp_site_defaults"], **site}
    out = dict(v)
    out.update(inventory_hostname="dellwp", wp_s=s, wp_host="%s.%s" % (s["name"], v["wp_domain"]), wp_site_dir="%s/sites/%s" % (v["wp_data_mount"], s["name"]))
    # what the wrapper that reads hostvars and lookup('vars', ...) (templates/wp/wp_site_provision.py.j2) needs to render at all; its content is tested in tests/test_wp_site_provision_wrapper.py
    out.update(hostvars={"delloptiplex3040": {"proxmox_vms": [dellwp_vm()]}}, lookup=lambda kind, name: out[name])
    out["wp_secret_base"] = "%s|%s|%s" % (v["wp_master_secret"], s["name"], s["secret_rev"])
    out["wp_db_password"] = hashlib.sha256((out["wp_secret_base"] + "|db").encode()).hexdigest()
    return out


class Render(unittest.TestCase):
    def setUp(self):
        self.v = base_vars()
        self.ctx = site_vars(self.v, self.v["wp_sites"][0])

    def render(self, name):
        return env().get_template("wp/" + name).render(**self.ctx)

    def test_compose_is_valid_yaml_with_the_limits(self):
        doc = yaml.safe_load(self.render("site_compose.yml.j2"))
        s = self.ctx["wp_s"]
        self.assertEqual(doc["name"], "wp-" + s["name"])
        for svc in ("db", "php"):
            c = doc["services"][svc]
            self.assertEqual(c["mem_limit"], c["memswap_limit"], "--memory-swap must equal --memory")
            self.assertEqual(c["cap_drop"], ["ALL"])
            self.assertIn("no-new-privileges:true", c["security_opt"])
        self.assertEqual(doc["services"]["php"]["ports"], ["127.0.0.1:%d:9000" % s["port"]])
        self.assertNotIn("ports", doc["services"]["db"])
        self.assertTrue(doc["services"]["php"]["read_only"])
        self.assertIn(self.v["wp_image_php"], self.render("site_compose.yml.j2"))

    def test_db_uid_has_no_account_on_the_vm(self):
        self.assertGreaterEqual(self.v["wp_db_uid"], 10000, "low uids belong to packages on the VM (999 is alloy)")
        self.assertNotEqual(self.v["wp_db_uid"], self.v["wp_php_uid"])

    def test_images_are_pinned_and_anchored(self):
        text = (ROOT / "vars/wp.yml").read_text().splitlines()
        for i, line in enumerate(text):
            if line.startswith("wp_image_") and "{{" not in line:
                self.assertTrue(text[i - 1].startswith("# renovate: depName="), line)
                self.assertNotIn(":latest", line)
            if line.startswith("wp_image_cli:"):
                # built from two pinned lines, so it carries no anchor of its own
                self.assertIn("wp_cli_version", line)
                self.assertIn("wp_image_php", line)
            if line.startswith("wp_cli_version:"):
                self.assertTrue(text[i - 1].startswith("# renovate: datasource=github-releases depName=wp-cli/wp-cli"), line)

    def test_wp_config_has_the_hardening_and_no_unrendered_text(self):
        php = self.render("site_wp-config.php.j2")
        self.assertTrue(php.startswith("<?php"))
        self.assertNotIn("{{", php)
        self.assertNotIn("{%", php)
        for needle in ("DISALLOW_FILE_MODS', true", "DISABLE_WP_CRON', true", "AUTOMATIC_UPDATER_DISABLED', true", "FORCE_SSL_ADMIN', true"):
            self.assertIn(needle, php)
        salts = [l for l in php.splitlines() if "_KEY'" in l or "_SALT'" in l]
        self.assertEqual(len(salts), 8)
        self.assertEqual(len({l.split("'")[3] for l in salts}), 8, "the eight salts must differ")

    def test_secrets_change_with_the_revision_and_the_site(self):
        a = self.render("site_wp-config.php.j2")
        other = site_vars(self.v, {**self.v["wp_sites"][0], "secret_rev": 2})
        b = env().get_template("wp/site_wp-config.php.j2").render(**other)
        self.assertNotEqual(a, b)
        third = site_vars(self.v, {**self.v["wp_sites"][0], "name": "other"})
        self.assertNotEqual(a.split("DB_PASSWORD")[1][:80], env().get_template("wp/site_wp-config.php.j2").render(**third).split("DB_PASSWORD")[1][:80])

    def test_no_secret_in_the_files_that_are_world_readable(self):
        for name in ("site_compose.yml.j2", "site_php.ini.j2", "site_fpm_pool.conf.j2", "site_nginx.conf.j2"):
            self.assertNotIn(self.ctx["wp_db_password"], self.render(name), name)

    def disabled(self):
        line = [l for l in self.render("site_php.ini.j2").splitlines() if l.startswith("disable_functions")][0]
        return line.split("=", 1)[1].strip().split(",")

    def test_every_template_renders_the_same_each_time(self):
        # Ansible reports changed when a rendered file differs: two renders of one input must be byte-identical.
        for name in sorted(str(p.relative_to(ROOT / "templates/wp")) for p in (ROOT / "templates/wp").rglob("*") if p.is_file()):      # includes mu/: plain PHP must pass through Jinja untouched
            first = self.render(name)
            for _ in range(30):
                self.assertEqual(first, self.render(name), name)

    def test_php_ini_disables_functions_minus_exceptions(self):
        self.assertIn("exec", self.disabled())
        self.ctx["wp_s"] = {**self.ctx["wp_s"], "php_allow_functions": ["exec"]}
        self.assertNotIn("exec", self.disabled())
        self.assertIn("pcntl_exec", self.disabled())
        self.assertIn("session.upload_progress.enabled = 0", self.render("site_php.ini.j2"))

    def test_nginx_vhost_rules(self):
        self.ctx["wp_s"] = {**self.ctx["wp_s"], "state": "hold", "allow_xmlrpc": False}  # the test sets the state it tests; the repo's site may be live
        conf = self.render("site_nginx.conf.j2")
        self.assertNotIn("default_server", conf)
        self.assertIn("allow 127.0.0.1;\n    deny all;", conf, "a site on hold must be local-only")
        self.assertIn("limit_req zone=wp_login_attempts", conf)
        self.assertIn("fastcgi_param HTTPS $wp_https;", conf)
        self.assertEqual(conf.count("{"), conf.count("}"))
        self.ctx["wp_s"] = {**self.ctx["wp_s"], "state": "live"}
        live = self.render("site_nginx.conf.j2")
        self.assertNotIn("deny all;\n\n    # Our own", live)
        self.assertNotIn("allow 127.0.0.1", live)
        self.assertIn("xmlrpc.php", live)
        self.ctx["wp_s"] = {**self.ctx["wp_s"], "allow_xmlrpc": True}
        self.assertNotIn("location = /xmlrpc.php", self.render("site_nginx.conf.j2"))

    def test_the_probe_port_is_the_same_vhost_and_only_the_maintenance_rule_ignores_it(self):
        # The update job probes the site while the maintenance flag is up. One server block on two loopback ports, so the probe meets the same rules a visitor does.
        conf = self.render("site_nginx.conf.j2")
        probe, main = str(self.v["wp_nginx_probe_port"]), str(self.v["wp_nginx_listen_port"])
        self.assertNotEqual(probe, main)
        self.assertEqual(conf.count("server {"), 1, "a second server block would be rules that can drift from the first")
        self.assertIn("listen 127.0.0.1:%s;" % main, conf); self.assertIn("listen 127.0.0.1:%s;" % probe, conf)
        self.assertNotIn("0.0.0.0", conf); self.assertNotIn("listen %s" % probe, conf, "loopback only")
        flag_at = conf.index("maintenance.flag"); probe_at = conf.index("if ($wp_probe)"); return_at = conf.index("return 503;")
        self.assertLess(flag_at, probe_at); self.assertLess(probe_at, return_at, "the probe exemption must come after the flag is read and before the 503")
        self.assertEqual(conf.count("return 503;"), 1)
        base = (ROOT / "tasks/wp_nginx_base_common.yml").read_text()
        self.assertIn("map $server_port $wp_probe", base); self.assertIn("{{ wp_nginx_probe_port }} 1;", base)
        self.assertIn("listen 127.0.0.1:{{ wp_nginx_probe_port }} default_server;", base, "a host no site claims must get the closed connection on the probe port too")
        # the tunnel is pointed at the visitor port only (8081 also appears in main.tf: the Bloomy prod origin on dellapps, another VM)
        tf = (ROOT / "terraform/cloudflare/main.tf").read_text()
        origin = __import__("re").search(r'variable "wp_origin" \{[^}]*default\s*=\s*"([^"]+)"', tf).group(1)
        self.assertEqual(origin, "http://127.0.0.1:%s" % main, "terraform's wp_origin must be the visitor port, never the probe port")

    def test_a_stopped_site_answers_a_paused_page_and_nothing_else(self):
        self.ctx["wp_s"] = {**self.ctx["wp_s"], "state": "stopped"}
        conf = self.render("site_nginx.conf.j2")
        self.assertIn('return 503 "This site is paused.\\n";', conf); self.assertEqual(conf.count("server {"), 1); self.assertEqual(conf.count("{"), conf.count("}"))
        for needle in ("fastcgi", "root ", "try_files", "limit_req", "maintenance.flag", "location"):
            self.assertNotIn(needle, conf, "a stopped site serves and passes nothing: " + needle)
        for port in (self.v["wp_nginx_listen_port"], self.v["wp_nginx_probe_port"]):
            self.assertIn("listen 127.0.0.1:%s;" % port, conf)
        self.assertIn("server_name %s;" % self.ctx["wp_host"], conf)
        for state in ("hold", "live"):
            self.ctx["wp_s"] = {**self.ctx["wp_s"], "state": state}
            self.assertNotIn("paused", self.render("site_nginx.conf.j2"), state)

    def test_the_status_tool_on_the_host_recognises_the_paused_vhost(self):
        sh = (ROOT / "templates/wp/wp_sites_status.sh.j2").read_text()
        self.assertIn('return 503 "This site is paused', sh); self.assertIn("actual=stopped", sh)

    def test_nginx_base_defines_what_the_vhost_uses(self):
        base = (ROOT / "tasks/wp_nginx_base_common.yml").read_text()
        for needle in ("$wp_https", "zone=wp_login_attempts:", "map $request_uri $wp_login_key"):
            self.assertIn(needle, base)
        # the zone the vhost uses must be one the base defines, and the old name (same name, other key) must not come back
        self.assertNotIn("zone=wp_login:", base)
        zone = __import__("re").search(r"limit_req zone=(\w+)", self.render("site_nginx.conf.j2")).group(1)
        self.assertIn("zone=%s:" % zone, base)


class Wiring(unittest.TestCase):
    def test_every_template_the_renderer_names_exists_and_the_play_reads_the_same_directory(self):
        renderer = (ROOT / "scripts/wp_site_renderer.py").read_text()
        import re
        names = re.findall(r'"((?:wp/)?[\w.-]+\.j2)"', renderer)
        self.assertGreaterEqual(len(set(names)), 7)
        for name in set(names):
            self.assertTrue((ROOT / "templates" / name).is_file(), name)
        self.assertIn("wp_template_dir", (ROOT / "dellwp/wp_sites_dellwp.yml").read_text())


if __name__ == "__main__":
    unittest.main()
