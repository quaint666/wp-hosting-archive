#!/usr/bin/env python3
"""Runs the REAL nginx (`nginx -t`) over the configuration the repo renders for the WordPress sites: the base (tasks/wp_nginx_base_common.yml) and every kind of vhost (templates/wp/site_nginx.conf.j2: live and on
hold, with the mini-cart cache and the page cache each on and off, and a stopped site). It proves nginx ACCEPTS the files (every directive known, every variable defined, no duplicate location); it does not prove
what nginx does with a request: that is the checklist's loopback steps. Skipped when `nginx` is not installed. Nothing is started and nothing outside a temporary directory is touched: the paths the files name
(logs, the cache, the mu directory) are pointed into that directory first, and nginx runs as the current user with its own prefix.
Run: python3 -I tests/test_wp_nginx_conf.py"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wp_sites_render import ROOT, base_vars, env, site_vars  # noqa: E402

NGINX = shutil.which("nginx")


def base_text(v, tmp):
    """The base file the play writes, rendered with the repo's variables."""
    tasks = yaml.safe_load((ROOT / "tasks/wp_nginx_base_common.yml").read_text())
    block = tasks[0]["block"]
    content = next(t for t in block if t.get("name") == "Deploy the nginx base (settings and catch-all)")["ansible.builtin.copy"]["content"]
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(content).render(**v)


def vhost_text(v, site):
    return env().get_template("wp/site_nginx.conf.j2").render(**site_vars(v, site))


@unittest.skipIf(NGINX is None, "nginx is not installed here")
class NginxAcceptsIt(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.tmp = Path(self.d.name)
        self.v = base_vars()
        self.v["wp_page_cache_path"] = str(self.tmp / "cache" / "wp_pages")
        (self.tmp / "cache").mkdir()
        (self.tmp / "log").mkdir()
        (self.tmp / "conf.d").mkdir()
        (self.tmp / "fastcgi_params").write_text("fastcgi_param QUERY_STRING $query_string;\nfastcgi_param REQUEST_METHOD $request_method;\n")
        self.v["wp_mu_dir"] = str(self.tmp / "mu")
        (self.tmp / "mu").mkdir()

    def own_paths(self, text):
        return text.replace("/var/log/nginx/", str(self.tmp / "log") + "/")

    def write_site(self, name, **site):
        base = dict(name=name, port=9100 + len(list((self.tmp / "conf.d").glob("wp_site_*"))), quota_id=1001, state="live", update_policy="auto")
        base.update(site)
        (self.tmp / "conf.d" / ("wp_site_%s.conf" % name)).write_text(self.own_paths(vhost_text(self.v, base)))

    def check(self):
        (self.tmp / "conf.d" / "wp_base.conf").write_text(self.own_paths(base_text(self.v, self.tmp)))
        (self.tmp / "nginx.conf").write_text("pid %s/nginx.pid;\nerror_log %s/log/error.log;\nevents {}\nhttp {\n  access_log off;\n  client_body_temp_path %s/t1;\n  proxy_temp_path %s/t2;\n"
                                             "  fastcgi_temp_path %s/t3;\n  uwsgi_temp_path %s/t4;\n  scgi_temp_path %s/t5;\n  include %s/conf.d/*.conf;\n}\n"
                                             % ((self.tmp,) * 8))
        p = subprocess.run([NGINX, "-t", "-p", str(self.tmp), "-c", str(self.tmp / "nginx.conf")], capture_output=True, text=True)
        return p.returncode, p.stderr

    def test_the_base_alone(self):
        rc, err = self.check()
        self.assertEqual(rc, 0, err)

    def test_every_kind_of_vhost_together_with_the_base(self):
        for i, (state, cart, cache_s) in enumerate((("live", "normal", 300), ("live", "cache", 300), ("hold", "cache", 300), ("hold", "normal", 300), ("stopped", "normal", 300), ("live", "cache", 0), ("live", "normal", 0))):
            self.v["wp_page_cache_s"] = cache_s
            self.write_site("site%d" % i, state=state, cart_fragments=cart)
        # the page cache setting is global (one value for all vhosts rendered above would be last one); the cache-off kinds are checked on their own below
        rc, err = self.check()
        self.assertEqual(rc, 0, err)

    def test_the_cache_off_vhosts_alone(self):
        self.v["wp_page_cache_s"] = 0
        for i, cart in enumerate(("normal", "cache")):
            self.write_site("off%d" % i, cart_fragments=cart)
        rc, err = self.check()
        self.assertEqual(rc, 0, err)

    def test_the_switches_each_way(self):
        for i, (xmlrpc, enum_) in enumerate(((True, True), (False, False), (True, False), (False, True))):
            self.write_site("sw%d" % i, allow_xmlrpc=xmlrpc, allow_user_enum=enum_)
        rc, err = self.check()
        self.assertEqual(rc, 0, err)

    def test_the_check_really_fails_on_a_wrong_file(self):
        """A test that cannot fail proves nothing: break the rendered vhost three ways and nginx must refuse each."""
        for label, old, new in (("an unknown variable", "fastcgi_cache_bypass $wph_frag_skip;", "fastcgi_cache_bypass $wph_no_such_variable;"), ("an unknown directive", "fastcgi_cache_lock on;", "fastcgi_cache_lockk on;"),
                                ("a duplicate location", "location / {", "location / {\n    }\n    location / {")):
            with self.subTest(label):
                self.setUp()
                self.write_site("alpha", cart_fragments="cache")
                f = self.tmp / "conf.d" / "wp_site_alpha.conf"
                text = f.read_text()
                self.assertIn(old, text, label)
                f.write_text(text.replace(old, new, 1))
                rc, err = self.check()
                self.assertNotEqual(rc, 0, label)

    def test_the_mini_cart_location_is_in_the_file_nginx_accepted(self):
        self.write_site("alpha", cart_fragments="cache")
        text = (self.tmp / "conf.d" / "wp_site_alpha.conf").read_text()
        self.assertIn("location = /__wph_fragments", text)
        rc, err = self.check()
        self.assertEqual(rc, 0, err)


if __name__ == "__main__":
    unittest.main()
