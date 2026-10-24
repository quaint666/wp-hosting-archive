#!/usr/bin/env python3
"""Offline checks of the WordPress site engine (tasks/wp_sites_common.yml and templates/wp/).

Renders every template with the real variables (vars/wp.yml, host_vars/dellwp/vars.yml, the VM's declared size in host_vars/delloptiplex3040/vars.yml) and
stub secrets, then evaluates the play's own assertion expressions against the real site list and against lists that must be refused. Plain Jinja cannot
see what Ansible does to a value (an empty string turned into None, the real `hash` filter, template trimming), so a pass here is not a pass of the play:
it catches a broken template, a wrong expression and a limit that no longer fits. Run: python3 tests/test_wp_sites_render.py
"""
import copy
import hashlib
import random
import sys
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(path):
    return yaml.safe_load((ROOT / path).read_text())


def env():
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.StrictUndefined,
                           trim_blocks=True, keep_trailing_newline=True)
    e.filters["hash"] = lambda value, algo="sha1": hashlib.new(algo, str(value).encode()).hexdigest()
    # Ansible's difference and intersect return the result of a set operation: any order. Model that, so a template that depends on the order fails here.
    e.filters["difference"] = lambda a, b: random.sample([x for x in a if x not in b], len([x for x in a if x not in b]))
    e.filters["intersect"] = lambda a, b: random.sample([x for x in a if x in b], len([x for x in a if x in b]))
    return e


def base_vars():
    v = {}
    v.update(load("vars/wp.yml"))
    v.update(load("host_vars/dellwp/vars.yml"))
    v["wp_master_secret"] = "stub-master-secret"
    # Ansible templates variables that refer to other variables (wp_image_cli is built from two lines): do the same here, so a test sees the value the play would use.
    e = jinja2.Environment(undefined=jinja2.StrictUndefined)
    e.filters["regex_search"] = lambda value, pattern: (__import__("re").search(pattern, value) or [None])[0]
    for _ in range(3):
        for key, val in list(v.items()):
            if isinstance(val, str) and "{{" in val:
                v[key] = e.from_string(val).render(**v)
    return v


def site_vars(v, site):
    s = {**v["wp_site_defaults"], **site}
    out = dict(v)
    out.update(wp_status_sites=" ".join("%s:%s:%s:%s:%s:%s" % (x["name"], x["state"], x["quota_id"], x["quota_mb"], x["quota_id"] + v["wp_backup_quota_offset"], x["backup_quota_mb"])
                                        for x in [dict(v["wp_site_defaults"], **y) for y in v["wp_sites"]]))
    out.update(wp_disk_sites=" ".join("%s:%s:%s" % (x["name"], x["quota_id"], x["quota_mb"]) for x in [dict(v["wp_site_defaults"], **y) for y in v["wp_sites"]]))
    out.update(wp_update_policies=" ".join("%s:%s" % (x["name"], x["update_policy"]) for x in [dict(v["wp_site_defaults"], **y) for y in v["wp_sites"]]))
    out.update(inventory_hostname="dellwp", wp_s=s, wp_host="%s.%s" % (s["name"], v["wp_domain"]), wp_site_dir="%s/sites/%s" % (v["wp_data_mount"], s["name"]))
    out["wp_secret_base"] = "%s|%s|%s" % (v["wp_master_secret"], s["name"], s["secret_rev"])
    out["wp_db_password"] = hashlib.sha256((out["wp_secret_base"] + "|db").encode()).hexdigest()
    return out


def assertions():
    plays = load("tasks/wp_sites_common.yml")
    for task in plays:
        that = task.get("ansible.builtin.assert", {}).get("that", [])
        if any("wp_sites_merged" in expr for expr in that):
            return that
    raise AssertionError("no assert on the site list found in tasks/wp_sites_common.yml")


def failing(that, v, sites):
    """The expressions of the play's assert that do NOT hold for this site list (scripts/wp_sites.py uses this to say which rule a new site would break)."""
    e = jinja2.Environment(undefined=jinja2.StrictUndefined)
    e.tests["match"] = lambda value, pattern: __import__("re").match(pattern, value) is not None
    e.filters["difference"] = lambda a, b: [x for x in a if x not in b]
    e.filters["intersect"] = lambda a, b: [x for x in a if x in b]
    e.filters["unique"] = lambda a: list(dict.fromkeys(a))
    ctx = dict(v)
    ctx["wp_sites"] = sites
    ctx["wp_sites_merged"] = [{**v["wp_site_defaults"], **s} for s in sites]
    ctx["wp_vm"] = next(vm for vm in load("host_vars/delloptiplex3040/vars.yml")["proxmox_vms"] if vm["name"] == "dellwp")
    bad = []
    for expr in that:
        try:
            ok = e.compile_expression(expr)(**ctx)
        except (KeyError, jinja2.UndefinedError, TypeError):
            ok = False
        if not ok:
            bad.append(expr)
    return bad


def eval_that(that, v, sites):
    """True if every expression in the play's assert holds for this site list."""
    return not failing(that, v, sites)


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

    def test_the_images_config_template_is_removed_from_the_docroot(self):
        tasks = yaml.safe_load((ROOT / "tasks/wp_site_one_common.yml").read_text())
        gone = [t for t in tasks if t.get("ansible.builtin.file", {}).get("path", "").endswith("/html/wp-config-docker.php")]
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0]["ansible.builtin.file"]["state"], "absent")

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
        for name in sorted(p.name for p in (ROOT / "templates/wp").iterdir()):
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

    def test_nginx_base_defines_what_the_vhost_uses(self):
        base = (ROOT / "tasks/wp_nginx_base_common.yml").read_text()
        for needle in ("$wp_https", "zone=wp_login_attempts:", "map $request_uri $wp_login_key"):
            self.assertIn(needle, base)
        # the zone the vhost uses must be one the base defines, and the old name (same name, other key) must not come back
        self.assertNotIn("zone=wp_login:", base)
        zone = __import__("re").search(r"limit_req zone=(\w+)", self.render("site_nginx.conf.j2")).group(1)
        self.assertIn("zone=%s:" % zone, base)


class Wiring(unittest.TestCase):
    def test_every_template_the_tasks_name_exists(self):
        text = (ROOT / "tasks/wp_site_one_common.yml").read_text()
        names = set(__import__("re").findall(r"wp_template_dir \}\}/(site_[\w.-]+)", text))
        names |= set(__import__("re").findall(r"src: (site_[\w.-]+)", text))
        self.assertGreaterEqual(len(names), 6)
        for name in names:
            self.assertTrue((ROOT / "templates/wp" / name).is_file(), name)
        self.assertNotIn("src: wp/", text)
        for shared in __import__("re").findall(r"shared_template_dir \}\}/([\w.-]+)", text):
            self.assertTrue((ROOT / "templates" / shared).is_file(), shared)
        self.assertIn("wp_template_dir", (ROOT / "dellwp/wp_sites_dellwp.yml").read_text())


class Assertions(unittest.TestCase):
    def setUp(self):
        self.v = base_vars()
        self.that = assertions()
        self.good = copy.deepcopy(self.v["wp_sites"])

    def ok(self, sites):
        return eval_that(self.that, self.v, sites)

    def test_the_real_list_fits(self):
        self.assertTrue(self.ok(self.good))

    def test_refused(self):
        site = self.good[0]
        cases = {
            "empty list": [],
            "no port": [{"name": "a1", "quota_id": 1005}],
            "no quota_id": [{"name": "a1", "port": 9105}],
            "reserved name": [{**site, "name": "bloomy"}],
            "bad name": [{**site, "name": "Bad_Name"}],
            "one character": [{**site, "name": "a"}],
            "duplicate name": [site, {**site, "port": 9102, "quota_id": 1002}],
            "duplicate port": [site, {"name": "other", "port": site["port"], "quota_id": 1002}],
            "duplicate project id": [site, {"name": "other", "port": 9102, "quota_id": site["quota_id"]}],
            "privileged port": [{**site, "port": 80}],
            "project id below 1000": [{**site, "quota_id": 5}],
            "unknown state": [{**site, "state": "paused"}],
            "unknown update policy": [{**site, "update_policy": "sometimes"}],
            "an empty update policy": [{**site, "update_policy": ""}],
            "RAM does not fit": [{**site, "php_mem_mb": 9000}],
            "quota does not fit": [{**site, "quota_mb": 100000}],
            "backup quota does not fit": [{**site, "backup_quota_mb": 100000}],
            "backups of two sites fit only together": [{**site, "quota_mb": 10000, "backup_quota_mb": 10000}, {"name": "other", "port": 9102, "quota_id": 1002, "quota_mb": 10000, "backup_quota_mb": 10000}],
            "a backup project id could equal another site's id": [site, {"name": "other", "port": 9102, "quota_id": site["quota_id"] + 1499}],
            "a backup project id would reach the scratch project": [{**site, "quota_id": 2999500}],
        }
        for label, sites in cases.items():
            with self.subTest(label):
                self.assertFalse(self.ok(sites))

    def test_five_default_sites_fit(self):
        sites = [{"name": "site%d" % i, "port": 9100 + i, "quota_id": 1000 + i} for i in range(1, 6)]
        self.assertTrue(self.ok(sites))

    def test_nine_default_sites_do_not(self):
        sites = [{"name": "site%d" % i, "port": 9100 + i, "quota_id": 1000 + i} for i in range(1, 10)]
        self.assertFalse(self.ok(sites))


if __name__ == "__main__":
    unittest.main(verbosity=1)
