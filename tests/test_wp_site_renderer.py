#!/usr/bin/env python3
"""Offline tests of scripts/wp_site_renderer.py: the site files rendered without Ansible.

The proof that matters is a diff: every shape of site (each state, the switches, mail send, a hyphenated name, custom limits and allowed functions) is rendered through the renderer and through the
play's offline helper (tests/test_wp_sites_render.py) and the text must be identical, file by file, with the unit rendered from the shared template the way tasks/wp_site_one_common.yml passes it.
The rest: the platform values the templates need are all in the repo's variables, the secrets derive as the play derives them and reach only the two files that are not world-readable, the
files come with the owner, group and mode the play gives them, the master secret file is refused when it is a link or open to others, and a missing value fails by name.
What this does not prove: that the helper's Jinja environment is what Ansible uses (the next batch proves that on the host: the renderer's output for a site the play deployed, diffed against the files
on disk), or anything about the disk. Run: python3 -I tests/test_wp_site_renderer.py"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import wp_site_renderer as r  # noqa: E402
import test_wp_sites_render as play  # noqa: E402

TEMPLATES = HERE.parent / "templates"
MASTER, RESEND = "stub-master-secret", "re_stub_resend_key"

SHAPES = {
    "defaults": {"name": "alpha", "port": 9101, "quota_id": 1001, "state": "hold", "update_policy": "notify"},
    "live": {"name": "alpha", "port": 9101, "quota_id": 1001, "state": "live", "update_policy": "auto"},
    "stopped": {"name": "alpha", "port": 9101, "quota_id": 1001, "state": "stopped", "update_policy": "freeze"},
    "hyphen name": {"name": "my-shop-2", "port": 9150, "quota_id": 1050, "state": "hold", "update_policy": "notify"},
    "switches": {"name": "alpha", "port": 9101, "quota_id": 1001, "state": "live", "update_policy": "notify", "allow_xmlrpc": True, "allow_app_passwords": True, "allow_user_enum": True},
    "mail send": {"name": "alpha", "port": 9101, "quota_id": 1001, "state": "hold", "update_policy": "notify", "mail_mode": "send"},
    "limits": {"name": "alpha", "port": 9101, "quota_id": 1001, "state": "hold", "update_policy": "notify", "php_mem_mb": 1024, "db_mem_mb": 512, "upload_mb": 256, "quota_mb": 9000,
               "cpus": 2.0, "pids_limit": 400, "php_memory_limit": "512M", "fpm_max_children": 8, "opcache_mb": 256, "db_buffer_pool_mb": 128},
    "functions": {"name": "alpha", "port": 9101, "quota_id": 1001, "state": "hold", "update_policy": "notify", "php_allow_functions": ["exec", "popen"]},
    "secret rev": {"name": "alpha", "port": 9101, "quota_id": 1001, "state": "hold", "update_policy": "notify", "secret_rev": 7},
    "info only": {"name": "alpha", "port": 9101, "quota_id": 1001, "state": "hold", "update_policy": "notify", "label": "A shop", "note": "for a friend"},
}


def platform():
    """The values the play has in scope: vars/wp.yml and host_vars/dellwp/vars.yml, with the references between them resolved (what the wrapper of the next batch will carry)."""
    return {k: v for k, v in play.base_vars().items() if k.startswith(("wp_", "dellwp_"))}


class Parity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = play.base_vars()
        cls.v["dellwp_wp_resend_key"] = RESEND
        cls.v["wp_master_secret"] = MASTER
        cls.renderer = r.Renderer(TEMPLATES, platform(), MASTER, RESEND)
        cls.old = play.env()

    def old_files(self, site):
        ctx = play.site_vars(self.v, site)
        unit = dict(ctx, compose_stack_description="WordPress site %s" % site["name"], compose_stack_dir=ctx["wp_site_dir"], compose_stack_start_timeout_s=self.v["wp_stack_start_timeout_s"],
                    compose_stack_stop_timeout_s=self.v["wp_stack_stop_timeout_s"], compose_stack_reload_service="php")
        d = ctx["wp_site_dir"]
        names = {"site_env.j2": d + "/.env", "site_wp-config.php.j2": d + "/wp-config.php", "site_php.ini.j2": d + "/php.ini", "site_fpm_pool.conf.j2": d + "/fpm-pool.conf",
                 "site_compose.yml.j2": d + "/compose.yml", "site_nginx.conf.j2": "%s/wp_site_%s.conf" % (self.v["wp_vhost_dir"], site["name"])}
        out = {path: self.old.get_template("wp/" + t).render(**ctx) for t, path in names.items()}
        out["%s/wp-%s.service" % (self.v["wp_unit_dir"], site["name"])] = self.old.get_template("compose_stack.service.j2").render(**unit)
        return out

    def test_every_shape_renders_to_the_same_text_as_the_play_helper(self):
        for label, site in SHAPES.items():
            merged = {**self.v["wp_site_defaults"], **site}
            new = {f["path"]: f["content"] for f in self.renderer.files(merged)}
            old = self.old_files(site)
            self.assertEqual(sorted(new), sorted(old), label)
            for path in old:
                self.assertEqual(new[path], old[path], "%s: %s" % (label, path))

    def test_the_shapes_differ_from_each_other_where_they_should(self):
        # Parity of two identical outputs would pass on a renderer that ignored the site: check the shapes change the files they are meant to.
        def files(label):
            return {f["path"].rsplit("/", 1)[1]: f["content"] for f in self.renderer.files({**self.v["wp_site_defaults"], **SHAPES[label]})}
        base = files("defaults")
        self.assertNotEqual(files("live")["wp_site_alpha.conf"], base["wp_site_alpha.conf"], "state shows in the vhost")
        self.assertNotEqual(files("switches")["wp_site_alpha.conf"], base["wp_site_alpha.conf"])
        self.assertNotEqual(files("switches")["wp-config.php"], base["wp-config.php"])
        self.assertNotEqual(files("mail send")["wp-config.php"], base["wp-config.php"])
        self.assertIn(RESEND, files("mail send")["wp-config.php"])
        self.assertNotIn(RESEND, base["wp-config.php"])
        self.assertNotEqual(files("limits")["compose.yml"], base["compose.yml"])
        self.assertNotEqual(files("functions")["php.ini"], base["php.ini"])
        self.assertNotEqual(files("secret rev")[".env"], base[".env"])
        self.assertEqual(files("info only"), base, "label and note are registry-only: no file changes")

    def test_the_vhost_serves_the_must_use_directory_and_gates_the_author_rule_on_the_setting(self):
        def vhost(label):
            return next(f["content"] for f in self.renderer.files({**self.v["wp_site_defaults"], **SHAPES[label]}) if f["path"].endswith("wp_site_alpha.conf"))
        for label in ("defaults", "live", "switches"):
            self.assertIn("location ^~ /wp-mu/ {\n        alias %s/;" % self.v["wp_mu_dir"], vhost(label), label)
            self.assertRegex(vhost(label), r"location \^~ /wp-mu/ \{[^}]*\n        location ~ \\\.php\$ \{\n            return 403;", label)       # plugin PHP is never served as text
        self.assertIn('author=[0-9]', vhost("live"))
        self.assertNotIn('author=[0-9]', vhost("switches"), "allow_user_enum true must open ?author=N")
        self.assertNotIn("/wp-mu/", vhost("stopped"), "a stopped site serves nothing")

    def test_static_files_compression_and_the_page_cache_in_the_vhost(self):
        def vhost(label, **extra):
            site = {**self.v["wp_site_defaults"], **SHAPES[label]}
            r_ = r.Renderer(TEMPLATES, {**platform(), **extra}, MASTER, RESEND)
            return next(f["content"] for f in r_.files(site) if f["path"].endswith("wp_site_alpha.conf"))
        live = vhost("live")
        # static files never reach PHP: a missing one is a 404, a found one has a long lifetime, and .ico (WordPress's own /favicon.ico) is not in the list
        self.assertRegex(live, r"location ~\* \\\.\(\?:css\|js[^)]*\)\$ \{\n        try_files \$uri =404;\n        expires 30d;")
        self.assertNotIn("ico|", live.split("location ~* \\.(?:css")[1].split("{")[0])
        # the static location comes after the deny rules (an upload named x.php.js must not slip past them) and before the catch-all
        self.assertLess(live.index("wp-content/(uploads|upgrade)"), live.index("location ~* \\.(?:css"))
        self.assertLess(live.index("location ~* \\.(?:css"), live.index("location / {"))
        for needle in ("gzip on;", "gzip_vary on;", "gzip_proxied any;", "application/javascript", "image/svg+xml"):
            self.assertIn(needle, live)
        self.assertNotIn("text/html", live.split("gzip_types")[1].split(";")[0], "text/html is always compressed; listing it only warns")
        # the cache: in the PHP location only, named by the platform's zone, skipping on the base's map, and the headers the location's own add_header would drop are repeated
        before, _, php = live.rpartition("location ~ \\.php$ {")        # the last one: /wp-mu/ has a nested one that refuses PHP
        zone = self.v["wp_page_cache_zone"]
        for needle in ("fastcgi_cache %s;" % zone, "fastcgi_cache_bypass $wp_skip_cache;", "fastcgi_no_cache $wp_skip_cache;", "fastcgi_cache_lock on;", "X-Cache-Status $upstream_cache_status",
                       "X-Content-Type-Options nosniff", "fastcgi_cache_valid 200 301 302 %ds;" % int(self.v["wp_page_cache_s"])):
            self.assertIn(needle, php)
        self.assertNotIn("fastcgi_cache", before, "only the PHP location caches")
        # off means off, and the PHP location then has no add_header of its own (the server's headers are inherited)
        off = vhost("live", wp_page_cache_s=0)
        self.assertNotIn("fastcgi_cache", off)
        self.assertNotIn("add_header", off.rpartition("location ~ \\.php$ {")[2])
        self.assertNotIn("fastcgi_cache", vhost("stopped"), "a stopped site serves a 503 and nothing else")

    def test_the_php_and_fpm_variables_default_to_what_ran_before_and_each_mode_renders_what_php_fpm_accepts(self):
        def render(extra=None, **site):
            rr = r.Renderer(TEMPLATES, {**platform(), **(extra or {})}, MASTER, RESEND)
            files = {f["path"].rsplit("/", 1)[1]: f["content"] for f in rr.files({**self.v["wp_site_defaults"], **SHAPES["live"], **site})}
            return files["fpm-pool.conf"], files["php.ini"]
        pool, ini = render()
        for line in ("pm = ondemand", "pm.max_children = 4", "pm.process_idle_timeout = 20s", "pm.max_requests = 500", "request_terminate_timeout = 120s", "request_slowlog_timeout = 10s"):
            self.assertIn(line + "\n", pool, "the defaults are the values that ran before they were variables: " + line)
        for line in ("opcache.max_accelerated_files = 4000", "opcache.interned_strings_buffer = 16", "opcache.validate_timestamps = 1", "opcache.revalidate_freq = 2", "realpath_cache_ttl = 120", "zend.assertions = 1"):
            self.assertIn(line + "\n", ini, line)      # interned strings: 16 since 2026-10-31 (the image's 8 MB buffer was measured full), every other line is what ran before
        self.assertNotIn("pm.start_servers", pool)
        pool, _ = render({"wp_fpm_pm": "dynamic"})
        for line in ("pm = dynamic", "pm.start_servers = 1", "pm.min_spare_servers = 1", "pm.max_spare_servers = 2"):
            self.assertIn(line + "\n", pool)
        self.assertNotIn("process_idle_timeout", pool, "an ondemand setting has no place in a dynamic pool")
        pool, _ = render({"wp_fpm_pm": "dynamic", "wp_fpm_start_servers": 9, "wp_fpm_min_spare_servers": 9, "wp_fpm_max_spare_servers": 9}, fpm_max_children=2)
        for key in ("start_servers", "min_spare_servers", "max_spare_servers"):
            self.assertIn("pm.%s = 2\n" % key, pool, "php-fpm refuses to start when a spare count is above pm.max_children: it is cut down")
        pool, _ = render({"wp_fpm_pm": "static"})
        self.assertIn("pm = static\n", pool)
        for gone in ("process_idle_timeout", "spare_servers", "start_servers"):
            self.assertNotIn(gone, pool)
        with self.assertRaises(r.RenderError) as cm:
            render({"wp_fpm_pm": "fast"})
        self.assertIn("must be ondemand, dynamic or static", str(cm.exception))
        pool, ini = render({"wp_fpm_slowlog_s": 1, "wp_php_assertions": -1, "wp_php_opcache_files": 20000, "wp_php_opcache_strings_mb": 16, "wp_php_opcache_validate": 0})
        for line in ("request_slowlog_timeout = 1s",):
            self.assertIn(line + "\n", pool)
        for line in ("zend.assertions = -1", "opcache.max_accelerated_files = 20000", "opcache.interned_strings_buffer = 16", "opcache.validate_timestamps = 0"):
            self.assertIn(line + "\n", ini)

    def test_the_timing_level_and_open_basedir_follow_their_variables(self):
        def files(extra=None):
            rr = r.Renderer(TEMPLATES, {**platform(), **(extra or {})}, MASTER, RESEND)
            out = {f["path"].rsplit("/", 1)[1]: f["content"] for f in rr.files({**self.v["wp_site_defaults"], **SHAPES["live"]})}
            return out["wp-config.php"], out["php.ini"]
        cfg, ini = files()
        self.assertIn("define( 'WP_HOSTING_TIMING', 0 );", cfg, "off by default: the diagnostic is opt-in")
        self.assertRegex(ini, r"(?m)^open_basedir = /var/www/html:/var/www/wp-mu:/tmp$")
        cfg, ini = files({"wp_php_timing": 2, "wp_php_open_basedir": 0})
        self.assertIn("define( 'WP_HOSTING_TIMING', 2 );", cfg)
        self.assertNotIn("open_basedir", ini, "off means the line is not there, not an empty value")
        self.assertIn("opcache.memory_consumption", ini, "the lines after it are still there")

    def test_ai1wm_export_adds_the_storage_mount_and_the_two_nginx_blocks_only_when_on(self):
        rr = r.Renderer(TEMPLATES, platform(), MASTER, RESEND)

        def files(**site):
            out = rr.files({**self.v["wp_site_defaults"], **SHAPES["live"], **site})
            return {f["path"].rsplit("/", 1)[1]: f["content"] for f in out}
        off, on = files(), files(ai1wm_export=True)
        self.assertNotIn("all-in-one-wp-migration", off["compose.yml"] + off["wp_site_alpha.conf"])
        self.assertIn("./html/wp-content/plugins/all-in-one-wp-migration/storage:/var/www/html/wp-content/plugins/all-in-one-wp-migration/storage", on["compose.yml"])
        self.assertNotIn(":ro", [ln for ln in on["compose.yml"].splitlines() if "all-in-one-wp-migration/storage" in ln and not ln.lstrip().startswith("#")][0])
        for needle in ("location ~* ^/wp-content/plugins/all-in-one-wp-migration/storage/ {", "location ~* ^/wp-content/uploads/ai1wm-backups/ {"):
            self.assertIn(needle, on["wp_site_alpha.conf"])
            self.assertIn("return 403;", on["wp_site_alpha.conf"].split(needle)[1].split("}")[0])

    def test_cart_fragments_cache_is_its_own_location_and_only_when_asked(self):
        def vhost(state="live", extra=None, **site):
            rr = r.Renderer(TEMPLATES, {**platform(), **(extra or {})}, MASTER, RESEND)
            files = rr.files({**self.v["wp_site_defaults"], **SHAPES["live"], "state": state, **site})
            return next(f["content"] for f in files if f["path"].endswith("wp_site_alpha.conf"))
        self.assertEqual(self.v["wp_site_defaults"]["cart_fragments"], "normal", "the default changes nothing for a site")
        for conf in (vhost(), vhost(cart_fragments="normal"), vhost(state="stopped", cart_fragments="cache"), vhost(extra={"wp_page_cache_s": 0}, cart_fragments="cache")):
            self.assertNotIn("__wph_fragments", conf)
        on = vhost(cart_fragments="cache")
        loc = on.split("location = /__wph_fragments {")[1].split("\n    }\n")[0]
        for needle in ("internal;", "fastcgi_cache %s;" % self.v["wp_page_cache_zone"], "fastcgi_cache_methods POST;", 'fastcgi_cache_key "$wp_https$host$request_uri";', "fastcgi_ignore_headers Cache-Control Expires;",
                       "fastcgi_cache_bypass $wph_frag_skip;", "fastcgi_no_cache $wph_frag_skip;", "fastcgi_cache_valid 200 %ds;" % int(self.v["wp_page_cache_s"]), "fastcgi_pass 127.0.0.1:9101;",
                       "fastcgi_param SCRIPT_FILENAME /var/www/html/index.php;", "fastcgi_param SCRIPT_NAME /index.php;", "X-Cache-Status $upstream_cache_status"):
            self.assertIn(needle, loc)
        self.assertNotIn("Set-Cookie", loc, "a Set-Cookie is not in the list of headers ignored: nginx then never stores such an answer")
        # the header-ignoring is confined to that one location (the ordinary PHP location keeps obeying no-store and no-cache)
        self.assertEqual(on.count("fastcgi_ignore_headers"), 1)
        self.assertNotIn("fastcgi_ignore_headers", on.rpartition("location ~ \\.php$ {")[2])
        # the redirect to it comes after the maintenance 503 and the author rule, so those still win, and sends only what the base's map says is the mini-cart request
        self.assertLess(on.index("return 503;"), on.index("if ($wph_fragments) {"))
        self.assertLess(on.index("if ($wph_fragments) {"), on.index("location = /__wph_fragments {"))
        self.assertIn("rewrite ^ /__wph_fragments last;", on)

    def test_rendering_twice_gives_the_same_bytes(self):
        site = {**self.v["wp_site_defaults"], **SHAPES["functions"]}
        first = self.renderer.files(site)
        for _ in range(20):
            self.assertEqual(self.renderer.files(site), first)


class Platform(unittest.TestCase):
    def test_every_value_the_templates_read_is_in_the_repo_variables(self):
        required, optional = r.needed_platform_vars(TEMPLATES)
        have = platform()
        self.assertEqual([k for k in required if k not in have], [])
        self.assertEqual(optional, ["dellwp_wp_resend_key"])
        self.assertIn("wp_domain", required)
        self.assertNotIn("wp_s", required)
        self.assertNotIn("dellwp_wp_master_secret", required, "the master secret reaches the renderer from its file, not through a template variable")

    def test_a_missing_value_is_named(self):
        have = platform()
        del have["wp_php_uid"]
        with self.assertRaises(r.RenderError) as cm:
            r.Renderer(TEMPLATES, have, MASTER)
        self.assertIn("wp_php_uid", str(cm.exception))

    def test_no_master_secret_no_renderer(self):
        with self.assertRaises(r.RenderError):
            r.Renderer(TEMPLATES, platform(), "")

    def test_mail_send_without_the_provider_key_fails_by_name(self):
        v = play.base_vars()
        site = {**v["wp_site_defaults"], **SHAPES["mail send"]}
        with self.assertRaises(r.RenderError) as cm:
            r.Renderer(TEMPLATES, platform(), MASTER).files(site)
        self.assertIn("dellwp_wp_resend_key", str(cm.exception))
        self.assertIn("wp_provider_key.sh resend", str(cm.exception), "the refusal says what to run")
        r.Renderer(TEMPLATES, platform(), MASTER).files({**v["wp_site_defaults"], **SHAPES["defaults"]})      # hold and log: no key needed

    def test_a_site_missing_a_field_a_template_reads_fails_by_name(self):
        v = play.base_vars()
        site = {**v["wp_site_defaults"], **SHAPES["defaults"]}
        del site["fpm_max_children"]
        with self.assertRaises(r.RenderError) as cm:
            r.Renderer(TEMPLATES, platform(), MASTER).files(site)
        self.assertIn("fpm_max_children", str(cm.exception))


class Files(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = play.base_vars()
        cls.renderer = r.Renderer(TEMPLATES, platform(), MASTER, RESEND)
        cls.site = {**cls.v["wp_site_defaults"], **SHAPES["defaults"]}
        cls.files = cls.renderer.files(cls.site)
        cls.by = {f["path"].rsplit("/", 1)[1]: f for f in cls.files}

    def test_owner_group_and_mode_are_the_plays(self):
        want = {".env": ("root", "root", 0o600), "wp-config.php": ("root", self.v["wp_php_uid"], 0o640), "php.ini": ("root", "root", 0o644), "fpm-pool.conf": ("root", "root", 0o644),
                "compose.yml": ("root", "root", 0o644), "wp-alpha.service": ("root", "root", 0o644), "wp_site_alpha.conf": ("root", "root", 0o644)}
        self.assertEqual({k: (f["owner"], f["group"], f["mode"]) for k, f in self.by.items()}, want)

    def test_paths(self):
        d = "%s/sites/alpha" % self.v["wp_data_mount"]
        self.assertEqual(self.by[".env"]["path"], d + "/.env")
        self.assertEqual(self.by["wp-alpha.service"]["path"], self.v["wp_unit_dir"] + "/wp-alpha.service")
        self.assertEqual(self.by["wp_site_alpha.conf"]["path"], self.v["wp_vhost_dir"] + "/wp_site_alpha.conf")

    def test_the_database_password_reaches_only_the_two_files_that_are_not_world_readable(self):
        base = r.secret_base(MASTER, "alpha", self.site["secret_rev"])
        pw = r.db_password(base)
        self.assertEqual(len(pw), 64)
        for f in self.files:
            self.assertEqual(pw in f["content"], f["path"].endswith(("/.env", "/wp-config.php")), f["path"])
            if f["secret"]:
                self.assertFalse(f["mode"] & 0o004, "a secret file is not world-readable: " + f["path"])
        self.assertTrue(all(f["secret"] == (not f["mode"] & 0o004) for f in self.files))

    def test_the_derivation_is_the_plays(self):
        self.assertEqual(r.secret_base("m", "alpha", 3), "m|alpha|3")
        self.assertEqual(r.db_password("m|alpha|3"), __import__("hashlib").sha256(b"m|alpha|3|db").hexdigest())
        other = {**self.site, "name": "bravo"}
        self.assertNotEqual(self.renderer.context(self.site)["wp_db_password"], self.renderer.context(other)["wp_db_password"])
        self.assertNotEqual(self.renderer.context(self.site)["wp_db_password"], self.renderer.context({**self.site, "secret_rev": 2})["wp_db_password"])
        self.assertNotEqual(r.Renderer(TEMPLATES, platform(), "another").context(self.site)["wp_db_password"], self.renderer.context(self.site)["wp_db_password"])

    def test_no_unrendered_template_text_is_left(self):
        for f in self.files:
            self.assertNotIn("{{", f["content"], f["path"])
            self.assertNotIn("{%", f["content"], f["path"])


class SecretFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, "master_secret")

    def write(self, text, mode=0o400):
        self.path.unlink(missing_ok=True)
        self.path.write_text(text)
        os.chmod(self.path, mode)

    def test_the_text_without_its_one_trailing_newline(self):
        self.write("s3cret\n")
        self.assertEqual(r.read_secret_file(self.path), "s3cret")
        self.write("s3cret")
        self.assertEqual(r.read_secret_file(self.path), "s3cret")
        self.write("s3cret\n\n")
        self.assertEqual(r.read_secret_file(self.path), "s3cret\n")

    def test_refused_when_open_to_group_or_others(self):
        for mode in (0o440, 0o404, 0o644, 0o640, 0o666, 0o407):
            self.write("x", mode)
            with self.assertRaises(r.SecretFileError, msg=oct(mode)):
                r.read_secret_file(self.path)
        for mode in (0o400, 0o600):
            self.write("x", mode)
            self.assertEqual(r.read_secret_file(self.path), "x")

    def test_refused_when_another_user_owns_it(self):
        self.write("x")
        real = os.geteuid
        os.geteuid = lambda: real() + 1
        try:
            with self.assertRaises(r.SecretFileError) as cm:
                r.read_secret_file(self.path)
        finally:
            os.geteuid = real
        self.assertIn("uid", str(cm.exception))

    def test_refused_when_a_link_missing_or_not_a_file(self):
        with self.assertRaises(r.SecretFileError):
            r.read_secret_file(self.path)
        self.write("x")
        link = Path(self.tmp.name, "link")
        os.symlink(self.path, link)
        with self.assertRaises(r.SecretFileError):
            r.read_secret_file(link)
        with self.assertRaises(r.SecretFileError):
            r.read_secret_file(self.tmp.name)

    def test_the_error_does_not_contain_the_secret(self):
        self.write("topsecretvalue", 0o644)
        with self.assertRaises(r.SecretFileError) as cm:
            r.read_secret_file(self.path)
        self.assertNotIn("topsecretvalue", str(cm.exception))
        self.assertTrue(stat.S_ISREG(os.stat(self.path).st_mode))


if __name__ == "__main__":
    unittest.main()
