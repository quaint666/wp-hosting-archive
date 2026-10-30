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


class PageCacheRules(unittest.TestCase):
    """The base's bypass maps, evaluated here the way nginx evaluates them (a regex entry matches in order, no match gives the default; PCRE and Python's re agree on every pattern used)."""

    @classmethod
    def setUpClass(cls):
        cls.base = (ROOT / "tasks/wp_nginx_base_common.yml").read_text()
        cls.v = base_vars()

    def entries(self, source, target):
        import re
        block = re.search(r"map %s %s \{\n(.*?)\n\s*\}" % (re.escape(source), re.escape(target)), self.base, re.S).group(1)
        default, rules = None, []
        for line in block.splitlines():
            m = re.match(r'\s*default (\S+);', line)
            if m:
                default = m.group(1); continue
            m = re.match(r'\s*"~\*?(.*)" (\S+);', line)
            if m:
                rules.append((re.compile(m.group(1), re.I), m.group(2)))
        return default, rules

    def mapped(self, source, target, value):
        default, rules = self.entries(source, target)
        return next((out for rx, out in rules if rx.search(value)), default)

    def skip(self, uri, cookie="", auth="", probe="0"):
        c = self.mapped("$http_cookie", "$wp_cache_cookie", cookie)
        u = self.mapped("$request_uri", "$wp_cache_uri", uri)
        key = "%s%s%s%s" % (c, u, auth, probe)
        return "0" if key == "000" else "1"                                             # the third map: default 1, only "000" is 0

    def test_the_third_map_is_what_this_evaluator_assumes(self):
        self.assertIn('map "$wp_cache_cookie$wp_cache_uri$http_authorization$wp_probe" $wp_skip_cache {\n              default 1;\n              "000" 0;\n          }', self.base)

    def test_anonymous_pages_are_cached(self):
        for uri in ("/", "/shop/", "/product/blue-shirt/", "/2026/10/a-post/", "/?p=12", "/category/news/page/2/", "/favicon.ico", "/sitemap.xml"):
            self.assertEqual(self.skip(uri), "0", uri)

    def test_everything_personal_or_dynamic_skips_the_cache(self):
        for uri in ("/wp-admin/", "/wp-admin/admin-ajax.php", "/wp-login.php", "/wp-login.php?action=lostpassword", "/wp-json/wp/v2/posts", "/wp-cron.php", "/xmlrpc.php",
                    "/wp-comments-post.php", "/?s=shoes", "/blog/?s=shoes&x=1", "/?preview=true&p=3", "/?add-to-cart=7", "/?wc-ajax=get_refreshed_fragments", "/?wc-api=wc_gateway",
                    "/cart/", "/cart", "/checkout/", "/checkout/order-received/5/", "/my-account/orders/", "/basket/", "/wp-content/plugins/x/track.php", "/index.php?p=1", "/anything.php"):
            self.assertEqual(self.skip(uri), "1", uri)
        for cookie in ("wordpress_logged_in_abc=1", "a=b; wordpress_sec_abc=1", "wp-postpass_x=1", "comment_author_h=1", "woocommerce_items_in_cart=1", "x=1; woocommerce_cart_hash=2",
                       "wp_woocommerce_session_9=1"):
            self.assertEqual(self.skip("/", cookie=cookie), "1", cookie)
        self.assertEqual(self.skip("/", cookie="_ga=GA1.2.3; theme=dark"), "0", "an analytics or preference cookie does not stop the cache")
        self.assertEqual(self.skip("/", auth="Basic abc"), "1", "an Authorization header")
        self.assertEqual(self.skip("/", probe="1"), "1", "the probe port sees the site as it is now")
        self.assertEqual(self.skip("/shop/?orderby=price"), "0", "a query string alone does not skip")

    def test_the_zone_the_vhost_names_is_the_one_the_base_defines_and_the_name_carries_the_size(self):
        zone = self.v["wp_page_cache_zone"]
        self.assertIn(str(self.v["wp_page_cache_keys_mb"]) + "m", zone)
        self.assertIn("keys_zone={{ wp_page_cache_zone }}:{{ wp_page_cache_keys_mb }}m", self.base)
        self.assertIn("fastcgi_cache_path {{ wp_page_cache_path }} ", self.base)
        self.assertEqual(self.v["wp_page_cache_path"].rsplit("/", 1)[0], "/var/cache/nginx")
        # nginx creates only the last directory, and /var/cache/nginx is not there on dellwp (nginx -t failed on mkdir, 2026-10-30): the play makes the parent, before the file that names the path
        self.assertIn('path: "{{ wp_page_cache_path | dirname }}"', self.base)
        self.assertLess(self.base.index("Make the parent of the page cache directory"), self.base.index("dest: /etc/nginx/conf.d/wp_base.conf"))
        self.assertIn("wp_page_cache_zone", self.v["wp_provision_platform_vars"]); self.assertIn("wp_page_cache_s", self.v["wp_provision_platform_vars"])

    def test_the_checklist_and_measurement_scripts_use_the_repos_paths(self):
        check = (ROOT / "scripts/wp_checklist_dellwp.sh").read_text(); perf = (ROOT / "scripts/wp_perf_measure.sh").read_text()
        self.assertIn(self.v["wp_page_cache_path"], check)
        for script in (check, perf):
            self.assertIn("SITES=%s/sites" % self.v["wp_data_mount"], script)
            self.assertIn("%s=%s" % ("VHOSTS", self.v["wp_vhost_dir"]), script)
        self.assertIn("MU=%s" % self.v["wp_mu_dir"], check)
        self.assertIn("SECRETS=%s" % self.v["wp_provision_secret_dir"], check)

    def test_the_checklists_vhost_patterns_match_the_vhost_the_template_renders(self):
        import re
        check = (ROOT / "scripts/wp_checklist_dellwp.sh").read_text()
        conf = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.StrictUndefined).get_template("wp/site_nginx.conf.j2").render(
            **{**self.v, "wp_s": {**self.v["wp_site_defaults"], "name": "alpha", "port": 9101, "state": "live"}, "wp_host": "alpha.x.test", "wp_site_dir": "/srv/wp/sites/alpha"})
        for pattern in re.findall(r"""count "\$conf" '([^']+)'""", check):
            self.assertTrue(re.search(pattern, conf) or "deny all" in pattern, "the checklist greps for %r, which the vhost does not contain" % pattern)

    def test_the_timed_log_format_is_the_combined_one_with_three_fields_the_vhost_uses_and_the_script_parses(self):
        import re
        fmt = re.search(r"log_format wp_timed '([^']+)';", self.base).group(1)
        combined = '$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent"'
        self.assertTrue(fmt.startswith(combined + " "), "the first nine fields must stay the combined format's, so older readers keep working")
        self.assertEqual(fmt[len(combined):].split(), ["rt=$request_time", "urt=$upstream_response_time", "cache=$upstream_cache_status"])
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.StrictUndefined)
        def vhost(state):
            return env.get_template("wp/site_nginx.conf.j2").render(**{**self.v, "wp_s": {**self.v["wp_site_defaults"], "name": "alpha", "port": 9101, "state": state}, "wp_host": "alpha.x.test", "wp_site_dir": "/srv/wp/sites/alpha"})
        for state in ("hold", "live"):
            self.assertIn("access_log /var/log/nginx/wp_alpha.access.log wp_timed;", vhost(state), state)
        self.assertNotIn("wp_timed", vhost("stopped"), "a stopped site serves a fixed 503: nothing to time")
        # the line nginx would write, with the variables filled in, is read by the regex the measurement script carries (a redirect makes urt a list)
        script = (ROOT / "scripts/wp_perf_measure.sh").read_text()
        rx = re.compile(re.search(r"rx = re\.compile\(r'(.*)'\)", script).group(1))
        for urt, cache in (("0.384", "BYPASS"), ("0.002, 0.384", "MISS"), ("-", "-"), ("-", "HIT")):
            line = fmt                                        # longest names first: $request_time must go before $request
            for name, value in (("$request_time", "0.391"), ("$upstream_response_time", urt), ("$upstream_cache_status", cache), ("$remote_addr", "1.2.3.4"), ("$remote_user", "-"),
                                ("$time_local", "30/Oct/2026:13:00:00 +0000"), ("$request", "GET /shop/?x=1 HTTP/2.0"), ("$status", "200"), ("$body_bytes_sent", "5120"), ("$http_referer", "-"),
                                ("$http_user_agent", "Mozilla/5.0 (X11; quoted)")):
                line = line.replace(name, value)
            m = rx.search(line)
            self.assertTrue(m, line)
            self.assertEqual((m.group("method"), m.group("uri"), m.group("status"), m.group("rt"), m.group("urt"), m.group("cache")), ("GET", "/shop/?x=1", "200", "0.391", urt, cache))
        self.assertIsNone(rx.search('1.2.3.4 - - [30/Oct/2026:13:00:00 +0000] "GET / HTTP/2.0" 200 5120 "-" "ua"'), "an older line has no timing and is counted as untimed")

    def test_the_slow_log_summary_in_the_measurement_script_ranks_what_the_stacks_name(self):
        import re
        import subprocess
        import tempfile
        script = (ROOT / "scripts/wp_perf_measure.sh").read_text()
        code = re.search(r"python3 - \"\$slow\" <<'PYSLOW'\n(.*?)\nPYSLOW\n", script, re.S).group(1)
        entry = ("[30-Oct-2026 13:00:00]  [pool www] pid %d\nscript_filename = /var/www/html/index.php\n[0x00007f01] %s %s\n[0x00007f02] get_option() /var/www/html/wp-includes/option.php:123\n"
                 "[0x00007f03] %s /var/www/html/wp-content/plugins/%s/x.php:9\n[0x00007f04] require() /var/www/html/wp-settings.php:500\n")
        log = "".join(entry % (i, "mysqli_query()", "/var/www/html/wp-includes/class-wpdb.php:2345", "do_thing()", "woocommerce") for i in range(3)) \
            + entry % (9, "preg_match()", "/var/www/html/wp-content/themes/envo/functions.php:10", "setup()", "elementor") + "noise that is not an entry\n"
        with tempfile.NamedTemporaryFile("w", suffix=".log") as f:
            f.write(log)
            f.flush()
            out = subprocess.run([sys.executable, "-I", "-c", code, f.name], capture_output=True, text=True, timeout=30).stdout
        flat = re.sub(r" +", " ", out)
        for needle in ("slow-log entries in the last 7 days: 4", "4 /index.php", "3 plugin woocommerce", "1 theme envo", "3 mysqli_query() wp-includes/class-wpdb.php:2345"):
            self.assertIn(needle, flat)

    def test_the_base_stays_balanced_after_the_template_variables_are_taken_out(self):
        import re
        content = self.base.split("content: |\n", 1)[1].split("register: wp_nginx_base", 1)[0]
        content = re.sub(r"\{\{.*?\}\}", "X", content)
        self.assertEqual(content.count("{"), content.count("}"))


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
