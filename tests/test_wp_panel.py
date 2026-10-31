#!/usr/bin/env python3
"""Offline tests of the control app (app/wp_panel): settings, login, session, lockout, headers and the read-only pages.

The app is driven through Flask's test client over https, with a fake ctl client that answers like wp-ctl (and records every call) and a fake clock, so nothing here needs ssh, a server or real time.
They prove: no page but the login (and the stylesheet) works without a login; a login needs the password AND a fresh code, is answered the same whichever half was wrong, locks after five wrong tries
for every worker at once, refuses a code used twice, and ends after the session life; the session cookie is Secure, HttpOnly and SameSite=Strict and a forged or foreign one is no login; a form needs
its token; every answer carries the security headers; whatever dellwp sends is shown as text; a broken dellwp gives an error page, never a traceback; and the app only ever asks for read verbs and has no
route that changes anything. They do not prove gunicorn, the container, HTTPS or the real ssh. Needs Flask and pyotp (app/requirements.txt). Run: python3 -I tests/test_wp_panel.py"""
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))

import pyotp  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402
from werkzeug.http import parse_cookie  # noqa: E402

from wp_panel import auth, config  # noqa: E402
from wp_panel.app import HEADERS, create_app  # noqa: E402
from wp_panel.ctl import ACTION_VERBS, ALLOWED, READ_VERBS, SECRET_VERBS, SET_KEYS, STREAM_VERBS, UPLOAD_MAX, Ctl, CtlError, good_password  # noqa: E402

PASSWORD = "correct horse battery staple 42"
TOTP_SECRET = pyotp.random_base32()
JOB = "20261029T122103Z-2cf66a88"
BASE = "https://panel.test"
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class FakeCtl:
    """Answers like wp-ctl; every call is recorded. `fail` maps a verb to a CtlError to raise."""

    def __init__(self):
        self.calls, self.fail, self.secrets, self.streams = [], {}, [], []
        self.replies = {
            "list": {"ok": True, "sites": [{"site": "alpha", "deployed": "live", "actual": "live", "unit": "active"}, {"site": "bravo", "deployed": "hold", "actual": "hold", "unit": "active"}]},
            "show": {"ok": True, "site": "alpha", "status": {"deployed": "live", "quota_pct": "9"}, "registry": {"name": "alpha", "note": "a note", "port": 9101}},
            "jobs": {"ok": True, "jobs": [{"job": JOB, "verb": "create", "site": "alpha", "state": "done", "rc": 0}]},
            "job-status": {"ok": True, "verb": "set", "site": "alpha", "state": "done", "rc": 0},
            "job-log": {"ok": True, "job": JOB, "data": "line one\nline two\n", "offset": 18, "state": "done", "rc": 0},
            "import-report": {"ok": True, "rc": 0, "ran": True, "out": "Import report for alpha\n  plugins: 3 reinstalled\n"},
            "import-list": {"ok": True, "files": [{"name": "friend.wpress", "bytes": 5242880, "mtime": T0 - 7200, "site": None}, {"name": "used.wpress", "bytes": 1048576, "mtime": T0 - 86400 * 2, "site": "bravo"}],
                            "imports": [{"site": "bravo", "file": "used.wpress", "stage": "reported", "problems": ["known vulnerabilities: FOUND: 7 finding(s): core wordpress 7.1.3"]}]},
            "users": {"ok": True, "site": "alpha", "users": [{"user": "admin", "roles": "administrator"}, {"user": "ed", "roles": "editor"}]},
            "plugins": {"ok": True, "site": "alpha", "plugins": [
                {"name": "woocommerce", "title": "WooCommerce", "status": "active", "version": "9.3.3", "update": False},
                {"name": "old-thing", "title": "Old <b>Thing</b>", "status": "inactive", "version": "1.0", "update": True},
                {"name": "hosting-mail", "title": "hosting-mail", "status": "must-use", "version": "1", "update": False}]},
            "plugin-requests": {"ok": True, "requests": [
                {"site": "alpha", "id": "aabbccdd0011", "slug": "akismet", "user": "owner", "first_seen": T0 - 600, "state": "pending", "note": "", "name": "Akismet <i>Anti-spam</i>", "version": "5.3", "installs": 5000000,
                 "updated": "2026-10-01", "found": True, "vuln": "clean", "vuln_line": "none known"},
                {"site": "alpha", "id": "aabbccdd0022", "slug": "shady", "user": "owner", "first_seen": T0 - 300, "state": "pending", "note": "", "name": "Shady", "version": "1.0", "found": True, "vuln": "found",
                 "vuln_line": "plugin shady 1.0: Shady - RCE (CVE-2026-9)"},
                {"site": "bravo", "id": "aabbccdd0033", "slug": "ghost", "user": "x", "first_seen": T0 - 100, "state": "pending", "note": "", "found": False, "vuln": "unchecked", "vuln_line": "not on wordpress.org"},
                {"site": "alpha", "id": "aabbccdd0044", "slug": "jetpack", "user": "owner", "first_seen": T0 - 86400, "state": "installed", "note": ""},
                {"site": "alpha", "id": "aabbccdd0055", "kind": "theme", "slug": "astra", "user": "owner", "first_seen": T0 - 90000, "state": "installed", "note": ""}]},
            "plugin-requests-collect": {"ok": True, "rc": 0, "ran": True, "out": "alpha: 1 request read, 0 new\n3 requests waiting\n"},
            "plugin-request-decide": {"ok": True, "rc": 0, "ran": True, "out": "installed akismet on alpha, inactive\n"},
            "plugin-set": {"ok": True, "rc": 0, "ran": True, "out": "Success: done.\npage cache: 12 files cleared\n"},
            "vuln-check": {"ok": True, "rc": 1, "ran": True, "out": "core wordpress 7.1.3: WordPress Core 4.7.0 - 7.1.3 (7 findings)\n"},
            "update-check": {"ok": True, "job": JOB}, "update-apply": {"ok": True, "job": JOB}, "import-new": {"ok": True, "job": JOB}, "import-rescan": {"ok": True, "job": JOB},
            "import-run": {"ok": True, "job": JOB},
            "import-approve": {"ok": True, "job": JOB},
            "import-reset": {"ok": True, "job": JOB},
            "backups": {"ok": True, "site": "alpha", "backups": [
                {"stamp": "20261031T020000Z", "tag": "pre-update", "core": "7.1.4", "kbytes": 204800, "empty": False},
                {"stamp": "20261030T020000Z", "tag": "", "core": "7.1.3", "kbytes": None, "empty": True}]},
            "restore": {"ok": True, "job": JOB},
            "import-discard": {"ok": True, "rc": 0, "ran": True, "out": "removed a1.wpress from the intake directory\n"},
            "create": {"ok": True, "job": JOB},
            "delete": {"ok": True, "job": JOB},
            "password": {"ok": True, "rc": 0, "ran": True, "out": "Success: Password changed.\n"},
            "install": {"ok": True, "rc": 0, "ran": True, "out": "WordPress installed.\n"},
            "set": {"ok": True, "job": JOB},
            "ensure-state": {"ok": True, "job": JOB},
            "audit-tail": {"ok": True, "lines": [{"ts": "2026-10-29T12:00:00Z", "phase": "intent", "verb": "set", "site": "alpha", "req": "x"}]},
        }

    checker = Ctl("dellwp", "wpctl", "/k", "/kh")          # the real client's own checks of the verb and every value, so a page that sends something wp-ctl would refuse fails here as it would live

    def stream(self, verb, chunks, **args):
        self.checker.request(verb, args)
        if verb not in STREAM_VERBS:
            raise CtlError("not_allowed", "no")
        h, n, first = hashlib.sha256(), 0, None
        try:
            for chunk in chunks:
                first = first or chunk
                h.update(chunk)
                n += len(chunk)
        finally:
            self.streams.append((verb, dict(args), n, h.hexdigest()))
        if verb in self.fail:
            raise self.fail[verb]
        return {"file": args["file"], "bytes": n, "sha256": h.hexdigest()}

    def call(self, verb, secret=None, **args):
        self.checker.request(verb, args)
        if verb in SECRET_VERBS:
            if not good_password(secret):
                raise CtlError("not_allowed", "the password must be 14 to 200 characters on one line, without control characters")
            self.secrets.append(secret)                      # kept apart from the calls, so a test can look for it everywhere else
        elif secret is not None:
            raise CtlError("not_allowed", "takes no password")
        self.calls.append((verb, dict(args)))
        if verb in self.fail:
            raise self.fail[verb]
        if verb not in ALLOWED:
            raise CtlError("not_allowed", "no")
        return json.loads(json.dumps(self.replies[verb]))


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.state = Path(self.d.name) / "state"
        self.clock = Clock()
        self.ctl = FakeCtl()
        self.settings = config.Settings("dellwp", "wpctl", "/k", "/kh", str(self.state), 3 * 3600, b"k" * 40, generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000"), TOTP_SECRET, "example.test")
        self.app = self.make_app()
        self.c = self.app.test_client()

    def make_app(self):
        return create_app(self.settings, self.ctl, self.clock)

    def code(self, offset=0):
        return pyotp.TOTP(TOTP_SECRET).at(int(self.clock.now // 30 + offset) * 30)

    def csrf(self, client=None, path="/login"):
        r = (client or self.c).get(path, base_url=BASE)
        return re.search(r'name="csrf" value="([^"]+)"', r.get_data(as_text=True)).group(1)

    def login(self, client=None, password=PASSWORD, code=None, token=None, **kw):
        client = client or self.c
        token = token or self.csrf(client)
        return client.post("/login", data={"password": password, "code": self.code() if code is None else code, "csrf": token}, base_url=BASE, **kw)

    def get(self, path, client=None, **kw):
        return (client or self.c).get(path, base_url=BASE, **kw)

    def logged_in(self):
        r = self.login()
        self.assertEqual(r.status_code, 303, r.get_data(as_text=True)[:300])
        return self.c


PAGES = ["/", "/sites", "/sites/alpha", "/jobs", "/jobs/" + JOB, "/audit", "/import", "/sites/alpha/import", "/sites/alpha/security", "/sites/alpha/plugins", "/sites/alpha/backups", "/requests", "/sites/alpha/password"]
# written out here, not read from the app: a test that compares the app with its own constant would pass when a header was deleted
EXPECTED_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class Settings(unittest.TestCase):
    def files(self, **over):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        good = {"session_key": b"k" * 40, "password_hash": generate_password_hash("x", method="pbkdf2:sha256:1000").encode(), "totp_secret": b"JBSWY3DPEHPK3PXP"}
        good.update(over)
        for name, data in good.items():
            if data is not None:
                (Path(d.name) / name).write_bytes(data)
        return d.name

    def env(self, d, **more):
        return dict({"PANEL_CTL_HOST": "dellwp", "PANEL_SITE_DOMAIN": "example.test", "PANEL_SECRET_DIR": d}, **more)

    def test_good_files_load_and_the_defaults_are_the_documented_ones(self):
        s = config.load(self.env(self.files()))
        self.assertEqual((s.ctl_host, s.ctl_user, s.session_s, s.state_dir), ("dellwp", "wpctl", 3 * 3600, "/run/panel"))

    def test_a_missing_or_malformed_secret_stops_the_start_and_names_the_file_not_the_value(self):
        for over, needle in (({"session_key": None}, "session_key"), ({"session_key": b"short"}, "session_key"), ({"password_hash": None}, "password_hash"),
                             ({"password_hash": b"hunter2 is my password"}, "password_hash"), ({"password_hash": b"plaintext"}, "password_hash"), ({"totp_secret": b"abc"}, "totp_secret"), ({"totp_secret": b"1" * 20}, "totp_secret")):
            with self.subTest(over=over), self.assertRaises(config.SettingsError) as cm:
                config.load(self.env(self.files(**over)))
            self.assertIn(needle, str(cm.exception))
            self.assertNotIn("hunter2", str(cm.exception))

    def test_the_host_and_the_session_hours_are_checked(self):
        d = self.files()
        for env in ({"PANEL_SECRET_DIR": d}, self.env(d, PANEL_SESSION_HOURS="0"), self.env(d, PANEL_SESSION_HOURS="99"), self.env(d, PANEL_SESSION_HOURS="x"), self.env(d, PANEL_SESSION_HOURS="")):
            with self.subTest(env=env), self.assertRaises(config.SettingsError):
                config.load(env)
        self.assertEqual(config.load(self.env(d, PANEL_SESSION_HOURS="2")).session_s, 7200)


class Access(Base):
    def test_every_page_sends_a_visitor_without_a_login_to_the_login_and_asks_dellwp_nothing(self):
        for path in PAGES:
            r = self.get(path)
            self.assertEqual((r.status_code, r.headers["Location"].endswith("/login")), (302, True), path)
        r = self.c.post("/logout", base_url=BASE)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.ctl.calls, [])

    def test_the_login_page_and_the_stylesheet_are_open_and_say_nothing_about_the_sites(self):
        r = self.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("alpha", r.get_data(as_text=True))
        self.assertEqual(self.get("/static/panel.css").status_code, 200)
        self.assertEqual(self.ctl.calls, [])

    def test_unknown_paths_and_wrong_methods_need_a_login_first_too(self):
        self.assertEqual(self.get("/admin").status_code, 302)
        self.assertEqual(self.c.post("/sites", base_url=BASE).status_code, 302)

    def test_a_good_login_opens_every_page(self):
        c = self.logged_in()
        for path in PAGES:
            r = self.get(path, c, follow_redirects=True)
            self.assertEqual(r.status_code, 200, path)

    def test_the_cookie_is_secure_httponly_and_samesite_strict_and_holds_no_password(self):
        r = self.login()
        cookie = [h for h in r.headers.getlist("Set-Cookie") if h.startswith("panel_session=")][0]
        for flag in ("Secure", "HttpOnly", "SameSite=Strict"):
            self.assertIn(flag, cookie)
        self.assertNotIn(PASSWORD, cookie)

    def test_a_cookie_that_was_not_signed_by_this_app_is_no_login(self):
        from flask.sessions import SecureCookieSessionInterface
        from itsdangerous import URLSafeTimedSerializer
        forged = URLSafeTimedSerializer(b"x" * 40, salt="cookie-session", serializer=SecureCookieSessionInterface.serializer, signer_kwargs={"key_derivation": "hmac", "digest_method": SecureCookieSessionInterface.digest_method}).dumps({"uid": "operator", "iat": int(self.clock.now)})
        self.c.set_cookie("panel_session", forged, domain="panel.test")
        self.assertEqual(self.get("/sites").status_code, 302)
        self.c.set_cookie("panel_session", "garbage.value.here", domain="panel.test")
        self.assertEqual(self.get("/sites").status_code, 302)
        self.assertEqual(self.ctl.calls, [])

    def test_a_signed_session_that_does_not_say_the_operator_logged_in_is_no_login(self):
        for data in ({}, {"uid": "someone"}, {"uid": "operator"}, {"uid": "operator", "iat": "now"}, {"uid": "operator", "iat": True}, {"uid": "operator", "iat": self.clock.now + 10_000}):
            with self.subTest(data=data), self.c.session_transaction(base_url=BASE) as s:
                s.clear()
                s.update(data)
            self.assertEqual(self.get("/sites").status_code, 302, data)

    def test_a_login_time_that_is_a_switch_is_no_login_even_when_the_clock_is_small(self):
        self.clock.now = 2.0
        with self.c.session_transaction(base_url=BASE) as s:
            s.update(uid="operator", iat=True)
        self.assertEqual(self.get("/sites").status_code, 302)

    def test_a_login_starts_a_session_with_nothing_left_from_before(self):
        self.csrf()
        with self.c.session_transaction(base_url=BASE) as s:
            s["leftover"] = "from before the login"
        self.login(token=self.csrf())
        with self.c.session_transaction(base_url=BASE) as s:
            self.assertNotIn("leftover", s)
            self.assertEqual(set(s) - {"_permanent"}, {"uid", "iat", "csrf"})

    def test_the_session_ends_after_its_life_by_the_apps_own_clock(self):
        c = self.logged_in()
        self.clock.now += 3 * 3600 - 1
        self.assertEqual(self.get("/sites", c).status_code, 200)
        self.clock.now += 2
        self.assertEqual(self.get("/sites", c).status_code, 302)
        self.assertEqual(self.get("/sites", c).status_code, 302, "and it stays ended")

    def test_logging_out_ends_the_session_and_needs_the_token(self):
        c = self.logged_in()
        self.assertEqual(self.get("/logout", c).status_code, 405, "a link or an image tag cannot log the operator out")
        self.assertEqual(c.post("/logout", data={"csrf": "wrong"}, base_url=BASE).status_code, 400)
        self.assertEqual(self.get("/sites", c).status_code, 200, "a refused logout leaves the session alone")
        token = re.search(r'name="csrf" value="([^"]+)"', self.get("/sites", c).get_data(as_text=True)).group(1)
        r = c.post("/logout", data={"csrf": token}, base_url=BASE)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.get("/sites", c).status_code, 302)

    def test_a_new_login_gives_a_new_token_and_nothing_survives_from_before(self):
        before = self.csrf()
        self.login(token=before)
        with self.c.session_transaction(base_url=BASE) as s:
            self.assertNotEqual(s["csrf"], before)

    def test_every_answer_carries_the_security_headers_even_a_redirect_a_404_and_the_stylesheet(self):
        c = self.logged_in()
        for r in (self.get("/login"), self.get("/sites"), self.get("/static/panel.css"), self.get("/nope", c), self.get("/sites/A", c)):
            for name, value in EXPECTED_HEADERS.items():
                self.assertEqual(r.headers.get(name), value, (r.status_code, name))

    def test_the_policy_allows_no_script_no_frame_and_no_outside_host(self):
        self.assertEqual(HEADERS, EXPECTED_HEADERS)
        csp = HEADERS["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("script-src", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotRegex(csp, r"https?:|\*|unsafe")

    def test_the_stylesheet_loads_nothing_and_keeps_dark_mode_focus_and_small_screens(self):
        css = self.get("/static/panel.css").get_data(as_text=True)
        for bad in ("url(", "@import", "http://", "https://", "@font-face", "expression("):
            self.assertNotIn(bad, css, "the policy allows this one file and nothing else: " + bad)
        for needed in ("prefers-color-scheme: dark", "prefers-reduced-motion", ":focus-visible", "max-width: 40rem"):
            self.assertIn(needed, css)

    def test_the_bar_marks_the_page_you_are_on(self):
        c = self.logged_in()
        for path, label in (("/sites", "Sites"), ("/sites/alpha", "Sites"), ("/new-site", "Sites"), ("/import", "Import"), ("/sites/alpha/import", "Import"), ("/requests", "Requests"), ("/jobs", "Jobs"), ("/jobs/" + JOB, "Jobs"), ("/audit", "Audit")):
            html = self.get(path, c).get_data(as_text=True)
            marked = re.findall(r'<a href="[^"]+" aria-current="page">([^<]+)</a>', html)
            self.assertEqual(marked, [label], path)

    def test_the_phone_menu_is_one_button_that_needs_no_script(self):
        c = self.logged_in()
        html = self.get("/sites", c).get_data(as_text=True)
        self.assertIn('<input type="checkbox" id="navtoggle"', html)
        self.assertEqual(len(re.findall(r'<label for="navtoggle"', html)), 2, "the button and the click-away layer")
        self.assertLess(html.index('id="navtoggle"'), html.index("<nav>"), "the sibling selectors need the checkbox before the nav")
        self.assertNotIn("<script", html)

    def test_no_page_has_an_inline_script_an_inline_style_or_an_outside_link(self):
        c = self.logged_in()
        for path in PAGES + ["/login"]:
            html = self.get(path, c, follow_redirects=True).get_data(as_text=True)
            html = re.sub(r"https://[a-z0-9-]+\.example\.test/[A-Za-z0-9/._-]*", "", html)       # the addresses of the sites themselves (the sites' zone is the app's setting); every other outside link still fails
            pattern = r"(?i)\sstyle=|\son[a-z]+=|https?://" if path == "/import" else r"(?i)<script|\sstyle=|\son[a-z]+=|https?://"       # the one page with a script: tested on its own (Imports)
            self.assertNotRegex(html, pattern, path)


class Login(Base):
    def test_the_right_password_and_a_fresh_code_log_in(self):
        self.assertEqual(self.login().status_code, 303)

    def test_every_kind_of_wrong_login_is_answered_the_same(self):
        answers = set()
        for kw in (dict(password="wrong"), dict(code="000000"), dict(password="wrong", code="000000"), dict(password=""), dict(code=""), dict(code="12345"), dict(code="abcdef"), dict(code="1234567"),
                   dict(password="x" * 300), dict(password=PASSWORD + " ")):
            r = self.login(**kw)
            self.assertEqual(r.status_code, 401, kw)
            answers.add(re.sub(r'name="csrf" value="[^"]+"', "", r.get_data(as_text=True)))
            self.setUp()           # a fresh lockout for each, so the count never reaches the lock
        self.assertEqual(len(answers), 1, "one page for every kind of wrong login")
        self.assertIn("not accepted", answers.pop())

    def test_a_missing_field_or_a_missing_token_is_refused(self):
        token = self.csrf()
        self.assertEqual(self.c.post("/login", data={"code": self.code(), "csrf": token}, base_url=BASE).status_code, 401)
        self.assertEqual(self.c.post("/login", data={"password": PASSWORD, "csrf": token}, base_url=BASE).status_code, 401)
        self.assertEqual(self.c.post("/login", data={"password": PASSWORD, "code": self.code()}, base_url=BASE).status_code, 400)
        self.assertEqual(self.c.post("/login", data={"password": PASSWORD, "code": self.code(), "csrf": "wrong"}, base_url=BASE).status_code, 400)

    def test_the_code_of_the_step_before_and_after_is_accepted_two_steps_away_is_not(self):
        for offset, want in ((-1, 303), (1, 303), (0, 303)):
            self.setUp()
            self.assertEqual(self.login(code=self.code(offset)).status_code, want, offset)
        for offset in (-2, 2):
            self.setUp()
            self.assertEqual(self.login(code=self.code(offset)).status_code, 401, offset)

    def test_a_code_that_was_used_cannot_be_used_again_even_in_its_own_30_seconds(self):
        code = self.code()
        self.assertEqual(self.login(code=code).status_code, 303)
        other = self.app.test_client()
        self.assertEqual(self.login(client=other, code=code).status_code, 401)
        self.clock.now += 30
        self.assertEqual(self.login(client=other, code=self.code()).status_code, 303, "the next code works")
        self.clock.now -= 30
        self.assertEqual(self.login(client=self.app.test_client(), code=code).status_code, 401, "an older code than the last one used is refused too")

    def test_five_wrong_logins_lock_the_login_even_for_the_right_one_and_it_opens_again_later(self):
        for i in range(5):
            self.assertEqual(self.login(password="wrong").status_code, 401, i)
        r = self.login()
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertGreater(int(r.headers["Retry-After"]), 0)
        self.assertIn("Too many wrong logins", r.get_data(as_text=True))
        self.clock.now += 899
        self.assertEqual(self.login().status_code, 429)
        self.clock.now += 2
        self.assertEqual(self.login().status_code, 303)

    def test_wrong_logins_spread_over_more_than_the_window_do_not_add_up(self):
        for i in range(4):
            self.login(password="wrong")
        self.clock.now += 901
        self.login(password="wrong")
        self.assertEqual(self.login().status_code, 303)

    def test_a_good_login_clears_the_count(self):
        for i in range(4):
            self.login(password="wrong")
        self.assertEqual(self.login().status_code, 303)
        self.clock.now += 30
        for i in range(4):
            self.assertEqual(self.login(client=self.app.test_client(), password="wrong").status_code, 401)
        self.assertEqual(self.login(client=self.app.test_client(), code=self.code()).status_code, 303)

    def test_every_worker_process_sees_the_same_count_and_the_same_used_code(self):
        workers = [self.make_app().test_client() for _ in range(3)]
        for i in range(5):
            self.assertEqual(self.login(client=workers[i % 3], password="wrong").status_code, 401)
        self.assertEqual(self.login(client=workers[0]).status_code, 429, "five failures spread over three workers lock all of them")
        self.assertEqual(self.login(client=workers[2]).status_code, 429)

    def test_the_lock_is_a_private_file_in_the_state_directory(self):
        self.login()
        names = {p.name for p in self.state.iterdir()}
        self.assertEqual(names, {"login.lock", "login.json"})
        self.assertEqual(oct((self.state / "login.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.state.stat().st_mode & 0o777), "0o700")
        self.assertNotIn(PASSWORD, (self.state / "login.json").read_text())

    def test_a_damaged_state_file_does_not_stop_a_login_and_does_not_open_a_lock_that_is_in_force(self):
        self.login()
        (self.state / "login.json").write_text("{not json")
        self.assertEqual(self.login(client=self.app.test_client(), code=self.code(1)).status_code, 303)

    def test_the_password_is_checked_even_when_the_code_is_wrong_so_neither_half_is_skipped(self):
        calls = []
        orig = auth.check_password_hash
        auth.check_password_hash = lambda *a: calls.append(a) or orig(*a)
        self.addCleanup(setattr, auth, "check_password_hash", orig)
        self.login(code="000000")
        self.assertEqual(len(calls), 1)

    def test_an_oversized_login_body_is_refused_before_it_is_read(self):
        r = self.c.post("/login", data={"password": "x" * 100_000, "code": "1", "csrf": "x"}, base_url=BASE)
        self.assertEqual(r.status_code, 413)


class Pages(Base):
    def setUp(self):
        super().setUp()
        self.logged_in()

    def text(self, path, status=200):
        r = self.get(path)
        self.assertEqual(r.status_code, status, (path, r.get_data(as_text=True)[:200]))
        return r.get_data(as_text=True)

    def test_the_sites_page_lists_every_site_with_its_state(self):
        html = self.text("/sites")
        for needle in ("alpha", "bravo", "live", "hold", "/sites/alpha"):
            self.assertIn(needle, html)
        self.assertEqual(self.ctl.calls[-2:], [("list", {}), ("import-list", {})])

    def test_the_site_page_shows_the_status_and_the_record(self):
        html = self.text("/sites/alpha")
        for needle in ("quota_pct", "a note", "9101", "port"):
            self.assertIn(needle, html)
        self.assertEqual(self.ctl.calls[-1], ("show", {"site": "alpha"}))

    def test_the_site_page_groups_the_details_and_leaves_nothing_out(self):
        self.ctl.replies["show"]["status"] = {"site": "alpha", "deployed": "live", "actual": "live", "backup_age_h": "15", "quota_pct": "9", "brand_new_key": "kept"}
        self.ctl.replies["show"]["registry"] = {"name": "alpha", "state": "live", "port": 9101, "extra_record_key": "kept too"}
        html = self.text("/sites/alpha")
        for needle in ("<h3>State</h3>", "<h3>Backup</h3>", "<h3>Registry record</h3>", "<h3>Other reported</h3>", "<h3>Other in the record</h3>",
                       "<dt>brand_new_key</dt><dd>kept</dd>", "<dt>extra_record_key</dt><dd>kept too</dd>", "<dd>15 h ago</dd>", "<dd>9 % used</dd>"):
            self.assertIn(needle, html)
        for hidden in ("<dt>site</dt>", "<dt>name</dt>"):
            self.assertNotIn(hidden, html, "the page head already says the name")

    def test_the_tables_are_laid_out_for_a_phone(self):
        sites = self.text("/sites")
        self.assertIn('class="table-wrap sticky-first"', sites, "the site name stays in view while the rest scrolls")
        jobs = self.text("/jobs")
        head = jobs.split("<thead>")[1].split("</thead>")[0]
        order = [h for h in ("What", "Site", "State", "Exit", "Job") if "<th>%s</th>" % h in head]
        self.assertEqual(order, ["What", "Site", "State", "Exit", "Job"])
        self.assertEqual([m for m in ("<th>What</th>", "<th>State</th>", "<th>Job</th>") if m in head], ["<th>What</th>", "<th>State</th>", "<th>Job</th>"])
        self.assertLess(head.index("<th>State</th>"), head.index("<th>Job</th>"), "the long job id comes last, behind the scroll")
        imp = self.text("/import")
        self.assertIn("table-wrap flat stack", imp)
        for label in ('data-label="File"', 'data-label="Size"', 'data-label="Used by"'):
            self.assertIn(label, imp)
        css = self.get("/static/panel.css").get_data(as_text=True)
        for rule in (".table-wrap.stack td[data-label]::before", ".navtoggle:checked ~ nav { display: flex; }", ".navtoggle:checked ~ .menu-away", ".table-wrap.sticky-first > table th:first-child"):
            self.assertIn(rule, css)

    def test_a_record_dellwp_could_not_read_is_said_so(self):
        self.ctl.replies["show"] = {"ok": True, "site": "alpha", "status": None, "registry": None, "registry_error": "the registry record of alpha cannot be read; nothing was guessed"}
        html = self.text("/sites/alpha")
        self.assertIn("cannot be read", html)
        self.assertIn("Nothing reported", html)

    def test_whatever_dellwp_sends_is_shown_as_text_never_as_markup(self):
        evil = '<script>alert(1)</script><img src=x onerror=alert(2)>"\'&'
        self.ctl.replies["list"]["sites"][0]["site"] = evil
        self.ctl.replies["list"]["sites"][0]["deployed"] = evil
        self.ctl.replies["show"]["registry"]["note"] = evil
        self.ctl.replies["show"]["status"]["x"] = evil
        self.ctl.replies["jobs"]["jobs"][0]["verb"] = evil
        self.ctl.replies["job-log"]["data"] = evil
        self.ctl.replies["audit-tail"]["lines"][0]["phase"] = evil
        self.ctl.replies["audit-tail"]["lines"][0]["extra"] = evil
        for path in ("/sites", "/sites/alpha", "/jobs", "/jobs/" + JOB, "/audit"):
            html = self.text(path)
            self.assertNotIn("<script", html, path)
            self.assertNotIn("<img", html, path)
            self.assertIn("&lt;script&gt;", html, path)

    def test_a_very_long_value_is_cut_not_dumped(self):
        self.ctl.replies["show"]["registry"]["note"] = "n" * 100_000
        self.assertLess(len(self.text("/sites/alpha")), 20_000)
        self.ctl.replies["job-log"]["data"] = "y" * 65_536
        self.assertIn("y" * 1000, self.text("/jobs/" + JOB), "the log itself is shown whole: it is the point of the page")

    def test_the_jobs_pages_show_the_state_the_log_and_a_way_to_read_on(self):
        html = self.text("/jobs")
        self.assertIn(JOB, html)
        job = self.text("/jobs/" + JOB)
        self.assertIn("line one", job)
        self.assertIn("<pre", job)
        self.assertIn("offset=18", job)
        self.assertIn(("job-log", {"job": JOB, "offset": 0}), self.ctl.calls)
        self.text("/jobs/%s?offset=18" % JOB)
        self.assertIn(("job-log", {"job": JOB, "offset": 18}), self.ctl.calls)

    def test_the_audit_page_shows_the_lines_and_takes_a_count_within_the_dispatchers_range(self):
        html = self.text("/audit")
        for needle in ("intent", "set", "alpha", "2026-10-29T12:00:00Z"):
            self.assertIn(needle, html)
        self.assertEqual(self.ctl.calls[-1], ("audit-tail", {"lines": 50}))
        self.text("/audit?lines=200")
        self.assertEqual(self.ctl.calls[-1], ("audit-tail", {"lines": 200}))

    def test_a_parameter_dellwp_would_refuse_is_a_404_before_anything_is_sent(self):
        before = len(self.ctl.calls)
        for path in ("/audit?lines=500", "/audit?lines=0", "/audit?lines=x", "/jobs/%s?offset=-1" % JOB, "/jobs/%s?offset=x" % JOB, "/jobs/nonsense", "/sites/A", "/sites/..%2fetc", "/sites/a--b"):
            self.text(path, 404)
        self.assertEqual(self.ctl.calls[before:], [], "the ctl client refused each one itself, so nothing was asked")

    def test_dellwp_saying_no_gives_404_for_an_unknown_site_or_job_and_400_for_the_rest(self):
        self.ctl.fail["show"] = CtlError("refused", "wp-ctl refused: unknown_site", "unknown_site")
        self.assertIn("unknown_site", self.text("/sites/alpha", 404))
        self.ctl.fail["show"] = CtlError("refused", "wp-ctl refused: something_else", "something_else")
        self.text("/sites/alpha", 400)

    def test_dellwp_not_answering_is_a_503_and_a_strange_answer_a_502_with_the_reason(self):
        self.ctl.fail["list"] = CtlError("unreachable", "dellwp did not answer within 30 seconds")
        self.assertIn("30 seconds", self.text("/sites", 503))
        self.ctl.fail["list"] = CtlError("bad_reply", "dellwp did not answer with one JSON line")
        self.assertIn("one JSON line", self.text("/sites", 502))
        self.ctl.fail["list"] = CtlError("failed", "wp-ctl failed: x")
        self.text("/sites", 502)

    def test_a_bug_in_the_app_is_a_plain_500_page_without_a_traceback_or_the_exception_text(self):
        class Boom(Exception):
            pass
        self.ctl.fail["list"] = Boom("secret detail /srv/x/y")
        self.app.logger.disabled = True
        html = self.text("/sites", 500)
        for bad in ("Traceback", "Boom", "secret detail", "/srv/x/y", "File \""):
            self.assertNotIn(bad, html)
        self.assertIn("Nothing was changed", html)

    def test_an_error_page_for_a_visitor_who_is_not_logged_in_is_never_reached(self):
        other = self.app.test_client()
        self.assertEqual(self.get("/sites/alpha", other).status_code, 302)


class Actions(Base):
    """The forms of batch 11: create a site, change one setting, move a site between hold, live and stopped. Each is a POST that needs the login and the token, asks for ONE verb and sends the job page."""

    def post(self, path, data, token=True, **kw):
        data = dict(data)
        if token is True:
            token = self.csrf(path="/sites/alpha")
        if token:
            data["csrf"] = token
        return self.c.post(path, data=data, base_url=BASE, **kw)

    def acted(self):
        return [(v, a) for v, a in self.ctl.calls if v in ACTION_VERBS or v in SECRET_VERBS] + [(v, a) for v, a, _, _ in self.ctl.streams]

    def setUp(self):
        super().setUp()
        self.logged_in()

    def test_every_form_needs_the_login_first_and_asks_nothing_without_it(self):
        other = self.app.test_client()
        for path, data in (("/new-site", {"name": "delta", "req": "p1"}), ("/sites/alpha/set", {"key": "note", "value": "x", "req": "p1"}), ("/sites/alpha/state", {"state": "hold", "req": "p1"})):
            r = other.post(path, data=data, base_url=BASE)
            self.assertEqual((r.status_code, r.headers["Location"].endswith("/login")), (302, True), path)
        self.assertEqual(self.acted(), [])

    def test_every_form_needs_the_token_and_a_wrong_or_empty_one_asks_nothing(self):
        for path, data in (("/new-site", {"name": "delta", "req": "p1"}), ("/sites/alpha/set", {"key": "note", "value": "x", "req": "p1"}), ("/sites/alpha/state", {"state": "hold", "req": "p1"})):
            for token in (False, "", "wrong", "x" * 40):
                with self.subTest(path=path, token=token):
                    self.assertEqual(self.post(path, data, token=token).status_code, 400)
        self.assertEqual(self.acted(), [])

    def test_the_token_of_another_session_is_no_token(self):
        other = self.app.test_client()
        self.clock.now += 60                       # the code of the first login was used: a code works once
        self.assertEqual(self.login(other).status_code, 303)
        foreign = self.csrf(other, "/sites/alpha")
        self.assertEqual(self.post("/new-site", {"name": "delta", "req": "p1"}, token=foreign).status_code, 400)
        self.assertEqual(self.acted(), [])

    def test_create_asks_for_create_with_the_name_the_label_and_the_request_id_and_sends_the_job_page(self):
        r = self.post("/new-site", {"name": "delta", "label": "Delta shop", "req": "pabc123"})
        self.assertEqual((r.status_code, r.headers["Location"]), (303, "/jobs/" + JOB))
        self.assertEqual(self.acted(), [("create", {"name": "delta", "label": "Delta shop", "req": "pabc123"})])

    def test_create_without_a_label_sends_none(self):
        self.post("/new-site", {"name": "delta", "label": "", "req": "pabc123"})
        self.assertEqual(self.acted(), [("create", {"name": "delta", "req": "pabc123"})])

    def test_a_name_that_is_not_a_site_name_a_label_with_control_characters_or_no_request_id_asks_nothing(self):
        bad = [{"name": n, "req": "p1"} for n in ("", "Alpha", "a", "a--b", "-ab", "ab-", "a_b", "x" * 25, "../x", 'a","verb":"delete', "ab\n", "日本", " delta")]
        bad += [{"name": "delta", "label": "a\nb", "req": "p1"}, {"name": "delta", "label": " lead", "req": "p1"}, {"name": "delta", "label": "x" * 101, "req": "p1"},
                {"name": "delta"}, {"name": "delta", "req": ""}, {"name": "delta", "req": "has space"}, {"name": "delta", "req": "x" * 65}]
        for data in bad:
            with self.subTest(data=data):
                r = self.post("/new-site", data)
                self.assertEqual(r.status_code, 400, data)
                self.assertIn("The form was not accepted", r.get_data(as_text=True))
        self.assertEqual(self.acted(), [])

    def test_a_refusal_by_wp_ctl_is_shown_as_text_and_nothing_is_retried(self):
        self.ctl.fail["create"] = CtlError("refused", "wp-ctl refused: name_reserved (<b>hub</b> is reserved)", "name_reserved")
        r = self.post("/new-site", {"name": "hub", "req": "p1"})
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("name_reserved", html)
        self.assertNotIn("<b>hub</b>", html)
        self.assertEqual(len(self.acted()), 1)

    def test_set_asks_for_set_with_the_key_the_value_and_the_request_id(self):
        r = self.post("/sites/alpha/set", {"key": "php_mem_mb", "value": "768", "req": "pk1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (303, "/jobs/" + JOB))
        self.assertEqual(self.acted(), [("set", {"site": "alpha", "key": "php_mem_mb", "value": "768", "req": "pk1"})])

    def test_set_may_clear_a_note_and_nothing_else_is_asked_for_a_key_that_is_not_editable(self):
        self.post("/sites/alpha/set", {"key": "note", "value": "", "req": "pk1"})
        self.assertEqual(self.acted(), [("set", {"site": "alpha", "key": "note", "value": "", "req": "pk1"})])
        self.ctl.calls.clear()
        for key in ("name", "port", "quota_id", "state", "cpus", "secret_rev", "", "Note", "note ", "update_policy,x"):
            with self.subTest(key=key):
                self.assertEqual(self.post("/sites/alpha/set", {"key": key, "value": "1", "req": "pk1"}).status_code, 400)
        self.assertEqual(self.acted(), [])

    def test_set_refuses_a_value_that_is_too_long_or_has_control_characters_and_a_site_that_is_not_a_name(self):
        for site, data in (("alpha", {"key": "note", "value": "x" * 501}), ("alpha", {"key": "note", "value": "a\x00b"}), ("alpha", {"key": "note", "value": "a\nb"}),
                           ("ALPHA", {"key": "note", "value": "x"}), ("a--b", {"key": "note", "value": "x"}), ("alpha", {"key": "note", "value": "x", "req": ""})):
            with self.subTest(site=site, data=data):
                r = self.post("/sites/%s/set" % site, dict({"req": "pk1"}, **data))
                self.assertIn(r.status_code, (400, 404), (site, data))
        self.assertEqual(self.acted(), [])

    def test_state_hold_and_stopped_ask_for_ensure_state_without_a_name_even_if_one_was_sent(self):
        for state in ("hold", "stopped"):
            self.ctl.calls.clear()
            r = self.post("/sites/alpha/state", {"state": state, "confirm_name": "alpha", "req": "ps1"})
            self.assertEqual((r.status_code, r.headers["Location"]), (303, "/jobs/" + JOB))
            self.assertEqual(self.acted(), [("ensure-state", {"site": "alpha", "state": state, "req": "ps1"})])

    def test_going_live_needs_the_sites_name_typed_exactly_and_asks_nothing_without_it(self):
        for typed in (None, "", "bravo", "Alpha", "alpha ", " alpha", "alph", "alphaa"):
            data = {"state": "live", "req": "ps1"}
            if typed is not None:
                data["confirm_name"] = typed
            with self.subTest(typed=typed):
                self.assertEqual(self.post("/sites/alpha/state", data).status_code, 400)
        self.assertEqual(self.acted(), [])
        r = self.post("/sites/alpha/state", {"state": "live", "confirm_name": "alpha", "req": "ps1"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.acted(), [("ensure-state", {"site": "alpha", "state": "live", "confirm_name": "alpha", "req": "ps1"})])

    def test_a_state_that_is_not_one_of_the_three_asks_nothing(self):
        for state in ("", "closed", "LIVE", "live,hold", "delete", "stopped "):
            with self.subTest(state=state):
                self.assertEqual(self.post("/sites/alpha/state", {"state": state, "confirm_name": "alpha", "req": "ps1"}).status_code, 400)
        self.assertEqual(self.acted(), [])

    def test_one_submit_is_one_verb_and_the_same_form_sent_twice_carries_the_same_request_id(self):
        for _ in range(2):
            self.post("/sites/alpha/set", {"key": "note", "value": "x", "req": "pk1"})
        self.assertEqual([a["req"] for _, a in self.acted()], ["pk1", "pk1"], "wp-ctl answers the second with the first's job")
        self.assertEqual({v for v, _ in self.acted()}, {"set"})

    def test_an_answer_without_a_job_is_a_502_and_not_a_redirect(self):
        for reply in ({"ok": True}, {"ok": True, "job": "../etc"}, {"ok": True, "job": 5}, {"ok": True, "job": JOB + "\n"}):
            with self.subTest(reply=reply):
                self.ctl.replies["set"] = reply
                r = self.post("/sites/alpha/set", {"key": "note", "value": "x", "req": "pk1"})
                self.assertEqual(r.status_code, 502)
                self.assertNotIn("Location", r.headers)

    def test_an_unreachable_dellwp_on_a_form_is_a_503_page_not_a_traceback(self):
        self.ctl.fail["set"] = CtlError("unreachable", "dellwp did not answer within 30 seconds")
        r = self.post("/sites/alpha/set", {"key": "note", "value": "x", "req": "pk1"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("30 seconds", r.get_data(as_text=True))

    def test_the_new_site_page_has_a_form_with_the_token_and_a_fresh_request_id_each_time(self):
        a, b = self.text("/new-site"), self.text("/new-site")
        forms = []
        for html in (a, b):
            form = re.search(r'<form method="post" action="/new-site".*?</form>', html, re.S)
            self.assertIsNotNone(form)
            self.assertRegex(form.group(0), r'name="csrf" value="[^"]{20,}"', "the token is inside the form itself, not only in the logout form of the page")
            forms.append(form.group(0))
        reqs = [re.search(r'name="req" value="([^"]+)"', f).group(1) for f in forms]
        self.assertNotEqual(reqs[0], reqs[1])
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", r) for r in reqs))

    def test_the_site_page_offers_only_the_moves_wp_ctl_allows_and_asks_the_name_for_live(self):
        for state, offered in (("hold", {"live", "stopped"}), ("live", {"hold", "stopped"}), ("stopped", {"hold"})):
            self.ctl.replies["show"]["status"]["deployed"] = state
            html = self.text("/sites/alpha")
            self.assertEqual(set(re.findall(r'name="state" value="([a-z]+)"', html)), offered, state)
            state_forms = re.findall(r'<form method="post" action="/sites/alpha/state".*?</form>', html, re.S)
            self.assertEqual(sorted(re.search(r'name="state" value="([a-z]+)"', f).group(1) for f in state_forms if 'name="confirm_name"' in f), sorted(offered & {"live", "stopped"}), state)
        for state in ("", "weird", "closed"):
            self.ctl.replies["show"]["status"]["deployed"] = state
            self.ctl.replies["show"]["registry"].pop("state", None)
            self.assertEqual(re.findall(r'name="state" value', self.text("/sites/alpha")), [], state)

    def test_stopping_a_site_needs_its_name_typed_and_a_wrong_name_asks_nothing(self):
        before = len(self.ctl.calls)
        r = self.post("/sites/alpha/state", {"state": "stopped", "req": "r1", "confirm_name": "alph"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("The site was not stopped", r.get_data(as_text=True))
        r = self.post("/sites/alpha/state", {"state": "stopped", "req": "r1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.acted(), [], "nothing was asked of wp-ctl")
        r = self.post("/sites/alpha/state", {"state": "stopped", "req": "r1", "confirm_name": "alpha"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.acted(), [("ensure-state", {"site": "alpha", "state": "stopped", "req": "r1"})], "wp-ctl is not sent the name: it does not ask for it for this move")

    def test_the_site_page_shows_the_full_addresses_and_says_when_they_will_not_open(self):
        html = self.text("/sites/alpha")
        self.assertIn("<code class=\"pick\">https://alpha.example.test/</code>", html)
        self.assertIn("<code class=\"pick\">https://alpha.example.test/wp-admin/</code>", html)
        self.assertIn('href="https://alpha.example.test/wp-admin/" target="_blank" rel="noopener noreferrer"', html)
        self.assertNotIn("closed to visitors, so these may not open", html)
        for state, said in (("hold", "closed to visitors, so these may not open"), ("stopped", "visitors get a 503")):
            self.ctl.replies["show"]["status"]["deployed"] = state
            self.assertIn(said, self.text("/sites/alpha"), state)

    def test_the_sites_table_has_each_sites_address_its_last_backup_and_its_disk_use(self):
        self.ctl.replies["list"]["sites"][0].update(backup_age_h="5", quota_pct="12", backup_quota_pct="30", core="7.1.3", frozen="no")
        self.ctl.replies["list"]["sites"][1].update(backup_age_h="-", quota_pct="?", backup_quota_pct="?", core="7.1.3", frozen="yes", actual="live")
        html = self.text("/sites")
        self.assertIn('href="https://alpha.example.test/" target="_blank" rel="noopener noreferrer" class="url">https://alpha.example.test/</a>', html)
        self.assertIn("5 h ago", html)
        self.assertIn("12% site", html)
        self.assertIn("none yet", html)
        self.assertIn("nginx does: live", html, "a site whose vhost differs from its record is said so in the row")
        self.assertIn("frozen", html)

    def test_a_finished_job_says_so_and_names_the_site_to_go_back_to(self):
        html = self.text("/jobs/" + JOB)
        self.assertIn("Finished: set on alpha.", html)
        self.assertIn('href="/sites/alpha">Back to alpha</a>', html)
        self.ctl.replies["job-status"] = {"ok": True, "verb": "set", "site": "alpha", "state": "done", "rc": 3}
        html = self.text("/jobs/" + JOB)
        self.assertIn("Failed: set on alpha.", html)
        self.assertIn("result-bad", html)
        self.ctl.replies["job-status"] = {"ok": True, "state": "done", "rc": 0}
        self.assertIn('href="/sites">Back to the sites</a>', self.text("/jobs/" + JOB), "a job that names no site goes back to the list")
        self.ctl.replies["job-status"] = {"ok": True, "verb": "<b>x</b>", "site": "alpha", "state": "done", "rc": 0}
        self.assertNotIn("<b>x</b>", self.text("/jobs/" + JOB))

    def test_the_password_form_lists_the_sites_users_and_falls_back_to_typing_when_it_cannot(self):
        html = self.text("/sites/alpha/password")
        self.assertRegex(html, r'<select name="user" required>')
        self.assertIn('<option value="admin">admin (administrator)</option>', html)
        self.assertIn(("users", {"site": "alpha"}), self.ctl.calls)
        self.assertIn('<option value="ed" selected>', self.text("/sites/alpha/password?user=ed"))
        self.ctl.fail["users"] = CtlError("failed", "alpha is stopped", None)
        html = self.text("/sites/alpha/password")
        self.assertNotIn("<select", html)
        self.assertRegex(html, r'<input name="user" required maxlength="60"')
        self.assertIn("alpha is stopped", html)
        self.ctl.fail.pop("users")
        self.ctl.replies["users"]["users"] = [{"user": '"><b>x</b>', "roles": "<i>"}, "junk", {"roles": "x"}]
        self.assertNotIn("<b>x</b>", self.text("/sites/alpha/password"))

    def test_the_site_page_has_one_setting_form_per_editable_key_each_with_the_token_and_its_own_request_id(self):
        self.ctl.replies["show"]["registry"].update(php_mem_mb=640, update_policy="freeze", allow_xmlrpc=False, note="n")
        html = self.text("/sites/alpha")
        forms = re.findall(r'<form method="post" action="/sites/alpha/set".*?</form>', html, re.S)
        self.assertEqual([re.search(r'name="key" value="([a-z0-9_]+)"', f).group(1) for f in forms], list(SET_KEYS))
        self.assertEqual(len({re.search(r'name="req" value="([^"]+)"', f).group(1) for f in forms}), len(SET_KEYS))
        self.assertTrue(all('name="csrf" value="' in f for f in forms))
        by = {re.search(r'name="key" value="([a-z0-9_]+)"', f).group(1): f for f in forms}
        self.assertIn('value="640"', by["php_mem_mb"])
        self.assertRegex(by["update_policy"], r'<option value="freeze" selected>')
        self.assertRegex(by["allow_xmlrpc"], r'<option value="false" selected>')
        self.assertRegex(by["allow_app_passwords"], r'<option value="" disabled selected>')
        self.assertIn('min="384" max="4096"', by["php_mem_mb"])

    def test_what_dellwp_sends_is_text_inside_the_forms_too(self):
        self.ctl.replies["show"]["registry"]["note"] = '"><script>alert(1)</script>'
        html = self.text("/sites/alpha")
        self.assertNotIn("<script", html.lower())
        self.assertIn("&lt;script&gt;", html)

    def test_a_running_job_reloads_its_page_by_itself_and_a_finished_one_does_not_and_no_page_has_a_script(self):
        self.ctl.replies["job-status"] = {"ok": True, "state": "running"}
        running = self.text("/jobs/" + JOB)
        self.assertIn('<meta http-equiv="refresh" content="3">', running)
        for state in ("done", "lost"):
            self.ctl.replies["job-status"] = {"ok": True, "state": state, "rc": 0}
            self.assertNotIn('http-equiv="refresh"', self.text("/jobs/" + JOB), state)
        for path in ("/new-site", "/sites/alpha", "/sites", "/jobs/" + JOB):
            self.assertNotIn("<script", self.text(path).lower(), path)

    def test_the_sites_page_links_to_the_new_site_form(self):
        self.assertIn('href="/new-site"', self.text("/sites"))

    def test_a_site_named_new_is_still_a_site_page(self):
        self.ctl.replies["show"]["site"] = "new"
        self.text("/sites/new")
        self.assertEqual(self.ctl.calls[-1], ("show", {"site": "new"}))

    def text(self, path, status=200):
        r = self.get(path)
        self.assertEqual(r.status_code, status, (path, r.get_data(as_text=True)[:200]))
        return r.get_data(as_text=True)


class PluginsPage(Base):
    def setUp(self):
        super().setUp()
        self.logged_in()

    def post(self, data, token=True):
        data = dict(data)
        if token is True:
            token = self.csrf(path="/sites/alpha")
        if token:
            data["csrf"] = token
        return self.c.post("/sites/alpha/plugins", data=data, base_url=BASE)

    def acted(self):
        return [(v, a) for v, a in self.ctl.calls if v == "plugin-set"]

    def test_the_page_lists_each_plugin_with_the_buttons_its_state_allows(self):
        html = self.get("/sites/alpha/plugins").get_data(as_text=True)
        rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)[1:]
        self.assertEqual(len(rows), 3)
        woo, old, mu = rows
        self.assertIn('value="deactivate"', woo)
        self.assertNotIn('value="delete"', woo, "an active plugin is deactivated before it can be deleted")
        self.assertIn('value="activate"', old)
        self.assertIn('value="delete"', old)
        self.assertIn("update", old)
        self.assertNotIn("<form", mu)
        self.assertIn("hosting", mu)
        self.assertEqual(self.get("/sites/alpha").get_data(as_text=True).count('href="/sites/alpha/plugins"'), 1, "the Site page links to it")

    def test_text_from_a_plugin_header_is_shown_as_text(self):
        html = self.get("/sites/alpha/plugins").get_data(as_text=True)
        self.assertNotIn("<b>Thing</b>", html)
        self.assertIn("&lt;b&gt;Thing&lt;/b&gt;", html)
        self.assertNotIn("<script", html.lower())

    def test_a_list_that_cannot_be_had_is_said_on_the_page_not_an_error_page(self):
        self.ctl.fail["plugins"] = CtlError("refused", "wp-ctl refused: tool_output (the plugins of alpha could not be listed)", "tool_output")
        r = self.get("/sites/alpha/plugins")
        self.assertEqual(r.status_code, 200)
        self.assertIn("could not be listed", r.get_data(as_text=True))

    def test_an_unknown_site_is_a_404_page(self):
        self.ctl.fail["show"] = CtlError("refused", "wp-ctl refused: unknown_site", "unknown_site")
        self.assertEqual(self.get("/sites/ghost/plugins").status_code, 404)

    def test_activate_and_deactivate_ask_for_one_verb_and_show_what_the_tool_said(self):
        for what in ("activate", "deactivate"):
            r = self.post({"plugin": "old-thing", "action": what})
            self.assertEqual(r.status_code, 200, what)
            self.assertIn("12 files cleared", r.get_data(as_text=True))
        self.assertEqual(self.acted(), [("plugin-set", {"site": "alpha", "plugin": "old-thing", "action": "activate"}), ("plugin-set", {"site": "alpha", "plugin": "old-thing", "action": "deactivate"})])

    def test_delete_is_two_steps_and_the_first_deletes_nothing(self):
        r = self.post({"plugin": "old-thing", "action": "delete"})
        html = r.get_data(as_text=True)
        self.assertEqual((r.status_code, self.acted()), (200, []))
        self.assertIn('name="confirm_plugin"', html)
        self.assertIn('value="old-thing"', html)
        r = self.post({"plugin": "old-thing", "action": "delete", "confirm_plugin": "old-thing"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.acted(), [("plugin-set", {"site": "alpha", "plugin": "old-thing", "action": "delete", "confirm_plugin": "old-thing"})])

    def test_a_delete_with_another_name_typed_is_refused_here_and_asks_nothing(self):
        r = self.post({"plugin": "old-thing", "action": "delete", "confirm_plugin": "old"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.acted(), [])

    def test_no_token_no_request_and_odd_forms_are_refused(self):
        for token in (False, "", "wrong"):
            self.assertEqual(self.post({"plugin": "old-thing", "action": "activate"}, token=token).status_code, 400)
        for data in ({"plugin": "../x", "action": "activate"}, {"plugin": "old-thing", "action": "install"}, {"plugin": "", "action": "delete"}, {"action": "activate"}):
            self.assertEqual(self.post(data).status_code, 400, data)
        self.assertEqual(self.acted(), [])

    def test_a_refusal_from_wp_ctl_is_shown(self):
        self.ctl.fail["plugin-set"] = CtlError("refused", "wp-ctl refused: wrong_state (woocommerce is active; activate needs it inactive)", "wrong_state")
        r = self.post({"plugin": "woocommerce", "action": "activate"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("wrong_state", r.get_data(as_text=True))


class RequestsPage(Base):
    def setUp(self):
        super().setUp()
        self.logged_in()

    def post(self, path, data, token=True):
        data = dict(data)
        if token is True:
            token = self.csrf(path="/sites/alpha")
        if token:
            data["csrf"] = token
        return self.c.post(path, data=data, base_url=BASE)

    def acted(self):
        return [(v, a) for v, a in self.ctl.calls if v in ("plugin-request-decide", "plugin-requests-collect")]

    def test_waiting_requests_come_first_with_the_buttons_their_checks_allow(self):
        html = self.get("/requests").get_data(as_text=True)
        self.assertIn("Waiting (3)", html)
        rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)[1:]
        clean, bad, ghost = rows[0], rows[1], rows[2]
        self.assertIn("none known", clean); self.assertIn('value="approve"', clean); self.assertNotIn("Approve…", clean)
        self.assertIn("known vulnerability", bad); self.assertIn("Approve…", bad); self.assertIn("RCE", bad)
        self.assertIn("not on wordpress.org", ghost); self.assertNotIn('value="approve"', ghost, "nothing to approve: wordpress.org does not have it"); self.assertIn('value="deny"', ghost)
        self.assertIn("Decided or expired", html); self.assertIn("jetpack", html)

    def test_a_theme_request_says_it_is_a_theme_and_a_request_without_a_kind_a_plugin(self):
        html = self.get("/requests").get_data(as_text=True)
        done = html.split("Decided or expired")[1]
        theme = next(r for r in re.findall(r"<tr>(.*?)</tr>", done, re.S) if "astra" in r)
        plugin = next(r for r in re.findall(r"<tr>(.*?)</tr>", done, re.S) if "jetpack" in r)
        self.assertIn(">theme</span>", theme); self.assertIn(">plugin</span>", plugin)

    def test_text_from_a_request_is_shown_as_text(self):
        html = self.get("/requests").get_data(as_text=True)
        self.assertNotIn("<i>Anti-spam</i>", html); self.assertIn("&lt;i&gt;Anti-spam&lt;/i&gt;", html)
        self.assertNotIn("<script", html.lower())

    def test_a_clean_request_is_approved_at_once_with_one_verb(self):
        r = self.post("/requests/decide", {"site": "alpha", "id": "aabbccdd0011", "decision": "approve"})
        self.assertEqual(r.status_code, 200); self.assertIn("installed akismet on alpha, inactive", r.get_data(as_text=True))
        self.assertEqual(self.acted(), [("plugin-request-decide", {"site": "alpha", "id": "aabbccdd0011", "decision": "approve"})])

    def test_deny_needs_no_confirmation(self):
        self.assertEqual(self.post("/requests/decide", {"site": "bravo", "id": "aabbccdd0033", "decision": "deny"}).status_code, 200)
        self.assertEqual(self.acted(), [("plugin-request-decide", {"site": "bravo", "id": "aabbccdd0033", "decision": "deny"})])

    def test_a_risky_approval_is_two_steps_and_the_first_installs_nothing(self):
        r = self.post("/requests/decide", {"site": "alpha", "id": "aabbccdd0022", "decision": "approve"})
        html = r.get_data(as_text=True)
        self.assertEqual((r.status_code, self.acted()), (200, []))
        self.assertIn("known vulnerability", html); self.assertIn('name="confirm_slug"', html); self.assertIn("CVE-2026-9", html)
        r = self.post("/requests/decide", {"site": "alpha", "id": "aabbccdd0022", "decision": "approve", "confirm_slug": "shady"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.acted(), [("plugin-request-decide", {"site": "alpha", "id": "aabbccdd0022", "decision": "approve", "confirm_slug": "shady"})])

    def test_a_typed_name_cannot_go_with_a_deny_and_odd_forms_are_refused(self):
        self.assertEqual(self.post("/requests/decide", {"site": "alpha", "id": "aabbccdd0011", "decision": "deny", "confirm_slug": "akismet"}).status_code, 400)
        for data in ({"site": "alpha", "id": "zz", "decision": "approve"}, {"site": "alpha", "id": "aabbccdd0011", "decision": "install"}, {"site": "alpha", "id": "aabbccdd0011"}, {}):
            self.assertEqual(self.post("/requests/decide", data).status_code, 400, data)
        self.assertEqual(self.acted(), [])

    def test_a_request_dellwp_does_not_remember_is_a_404_and_asks_nothing(self):
        r = self.post("/requests/decide", {"site": "alpha", "id": "ffffffffffff", "decision": "approve"})
        self.assertEqual(r.status_code, 404); self.assertEqual(self.acted(), [])

    def test_refresh_collects_and_shows_what_the_tool_said(self):
        r = self.post("/requests/refresh", {})
        self.assertEqual(r.status_code, 200); self.assertIn("3 requests waiting", r.get_data(as_text=True))
        self.assertEqual(self.acted(), [("plugin-requests-collect", {})])

    def test_no_token_no_request(self):
        for path, data in (("/requests/refresh", {}), ("/requests/decide", {"site": "alpha", "id": "aabbccdd0011", "decision": "approve"})):
            for token in (False, "", "wrong"):
                self.assertEqual(self.post(path, data, token=token).status_code, 400, (path, token))
        self.assertEqual(self.acted(), [])

    def test_a_refusal_from_the_tool_is_shown(self):
        self.ctl.fail["plugin-request-decide"] = CtlError("failed", "wp-ctl failed: no error code (akismet has a known vulnerability)", None)
        r = self.post("/requests/decide", {"site": "alpha", "id": "aabbccdd0011", "decision": "approve"})
        self.assertIn("known vulnerability", r.get_data(as_text=True))

    def test_the_list_that_cannot_be_had_is_an_error_page(self):
        self.ctl.fail["plugin-requests"] = CtlError("unreachable", "dellwp could not be reached")
        self.assertEqual(self.get("/requests").status_code, 503)


class Secrets(Base):
    """Batch 12: delete (two entries of the name, a stopped site only), a WordPress user's password and installing WordPress. The password is the one thing that must be found nowhere but in the one call."""
    PW = "correct horse battery 7"

    def setUp(self):
        super().setUp()
        self.logged_in()
        self.ctl.replies["show"]["status"]["deployed"] = "stopped"

    def post(self, path, data, token=True):
        data = dict(data)
        if token is True:
            token = self.csrf(path="/sites/alpha")
        if token:
            data["csrf"] = token
        return self.c.post(path, data=data, base_url=BASE)

    def acted(self):
        return [(v, a) for v, a in self.ctl.calls if v in ACTION_VERBS or v in SECRET_VERBS] + [(v, a) for v, a, _, _ in self.ctl.streams]

    def test_delete_is_two_steps_and_the_first_only_shows_what_would_go(self):
        r = self.post("/sites/alpha/delete", {"confirm_name": "alpha"})
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.acted(), [], "typing the name once deletes nothing")
        self.assertIn("for good", html)
        self.assertIn('name="confirm_again"', html)
        self.assertRegex(html, r'name="confirm_name" value="alpha"')
        self.assertRegex(html, r'name="req" value="p[0-9a-f]{32}"')

    def test_the_second_entry_starts_the_delete_and_sends_the_job_page(self):
        r = self.post("/sites/alpha/delete", {"confirm_name": "alpha", "confirm_again": "alpha", "req": "pd1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (303, "/jobs/" + JOB))
        self.assertEqual(self.acted(), [("delete", {"site": "alpha", "confirm_name": "alpha", "confirm_again": "alpha", "req": "pd1"})])

    def test_a_name_that_is_not_the_sites_deletes_nothing_at_either_step(self):
        for typed in ("", "bravo", "Alpha", "alpha ", " alpha", "alph"):
            with self.subTest(typed=typed):
                self.assertEqual(self.post("/sites/alpha/delete", {"confirm_name": typed}).status_code, 400)
                self.assertEqual(self.post("/sites/alpha/delete", {"confirm_name": typed, "confirm_again": typed, "req": "pd1"}).status_code, 400)
                if typed:                                     # an empty second entry is the first step again: it shows the page and deletes nothing
                    self.assertEqual(self.post("/sites/alpha/delete", {"confirm_name": "alpha", "confirm_again": typed, "req": "pd1"}).status_code, 400)
        self.assertEqual(self.post("/sites/alpha/delete", {"confirm_name": "alpha", "confirm_again": "", "req": "pd1"}).status_code, 200)
        self.assertEqual(self.acted(), [])

    def test_a_site_that_is_not_stopped_is_not_offered_a_delete_and_the_first_step_says_stop_it_first(self):
        for state in ("hold", "live"):
            self.ctl.replies["show"]["status"]["deployed"] = state
            self.assertNotIn('action="/sites/alpha/delete"', self.get("/sites/alpha").get_data(as_text=True), state)
            r = self.post("/sites/alpha/delete", {"confirm_name": "alpha"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("Stop the site first", r.get_data(as_text=True))
        self.ctl.replies["show"]["status"]["deployed"] = "stopped"
        self.assertIn('action="/sites/alpha/delete"', self.get("/sites/alpha").get_data(as_text=True))
        self.assertEqual(self.acted(), [])

    def test_the_password_goes_to_the_client_once_and_is_in_no_page_no_call_and_no_redirect(self):
        r = self.post("/sites/alpha/password", {"user": "admin", "password": self.PW, "again": self.PW})
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Password changed", html)
        self.assertEqual(self.ctl.secrets, [self.PW])
        self.assertEqual(self.acted(), [("password", {"site": "alpha", "user": "admin"})])
        self.assertNotIn(self.PW, html + str(self.ctl.calls) + str(r.headers))

    def test_two_passwords_that_differ_or_a_short_one_or_one_with_a_newline_ask_nothing_and_are_not_echoed(self):
        for pw, again in ((self.PW, self.PW + "x"), ("short", "short"), (self.PW + "\n" + "x", self.PW + "\n" + "x"), ("x" * 201, "x" * 201), ("", "")):
            with self.subTest(pw=pw[:6]):
                r = self.post("/sites/alpha/password", {"user": "admin", "password": pw, "again": again})
                self.assertEqual(r.status_code, 400)
                if len(pw) > 3:
                    self.assertNotIn(pw, r.get_data(as_text=True))
        self.assertEqual(self.acted(), [])
        self.assertEqual(self.ctl.secrets, [])

    def test_a_user_name_that_is_not_one_asks_nothing(self):
        for user in ("", "a b", "a;b", "x" * 61, "日本", "a\nb"):
            with self.subTest(user=user):
                self.assertEqual(self.post("/sites/alpha/password", {"user": user, "password": self.PW, "again": self.PW}).status_code, 400)
        self.assertEqual(self.acted(), [])

    def test_a_failed_tool_shows_its_words_and_never_the_password(self):
        self.ctl.fail["password"] = CtlError("failed", "wp-ctl failed: no error code (Error: the user does not exist)", None)
        r = self.post("/sites/alpha/password", {"user": "nobody", "password": self.PW, "again": self.PW})
        self.assertEqual(r.status_code, 502)
        self.assertIn("does not exist", r.get_data(as_text=True))
        self.assertNotIn(self.PW, r.get_data(as_text=True))

    def test_install_sends_the_title_user_and_email_as_arguments_and_the_password_apart(self):
        r = self.post("/sites/alpha/install", {"title": "Alpha shop", "user": "owner", "email": "o@example.com", "password": self.PW, "again": self.PW})
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("WordPress installed", html)
        self.assertEqual(self.acted(), [("install", {"site": "alpha", "title": "Alpha shop", "user": "owner", "email": "o@example.com"})])
        self.assertEqual(self.ctl.secrets, [self.PW])
        self.assertNotIn(self.PW, html)

    def test_install_refuses_what_wp_ctl_would_and_asks_nothing(self):
        good = {"title": "Alpha", "user": "owner", "email": "o@example.com", "password": self.PW, "again": self.PW}
        for over in ({"title": ""}, {"title": "x" * 101}, {"user": "ab"}, {"user": "has space"}, {"user": "x" * 31}, {"email": "nope"}, {"email": "a b@c.d"}, {"email": ""}, {"again": "different password 99"}, {"password": "short", "again": "short"}):
            with self.subTest(over=over):
                self.assertEqual(self.post("/sites/alpha/install", dict(good, **over)).status_code, 400)
        self.assertEqual(self.acted(), [])

    def test_the_password_pages_and_the_forms_have_the_token_and_ask_nothing_without_it(self):
        for path, data in (("/sites/alpha/password", {"user": "admin", "password": self.PW, "again": self.PW}), ("/sites/alpha/install", {"title": "A", "user": "owner", "email": "o@example.com", "password": self.PW, "again": self.PW}),
                           ("/sites/alpha/delete", {"confirm_name": "alpha", "confirm_again": "alpha", "req": "pd1"})):
            for token in (False, "", "wrong"):
                with self.subTest(path=path, token=token):
                    self.assertEqual(self.post(path, data, token=token).status_code, 400)
        other = self.app.test_client()
        self.assertEqual(other.get("/sites/alpha/password", base_url=BASE).status_code, 302)
        self.assertEqual(self.acted(), [])
        self.assertEqual(self.ctl.secrets, [])

    def test_the_password_inputs_are_password_fields_that_a_browser_does_not_fill_or_remember(self):
        for path in ("/sites/alpha/password", "/sites/alpha/install"):
            html = self.get(path).get_data(as_text=True)
            self.assertEqual(len(re.findall(r'<input name="(?:password|again)" type="password"[^>]*autocomplete="new-password"', html)), 2, path)
            self.assertNotIn("value=\"%s" % self.PW, html)
            self.assertNotIn("<script", html.lower())

    def test_the_forms_for_an_unknown_site_are_a_404_page(self):
        self.ctl.fail["show"] = CtlError("refused", "wp-ctl refused: unknown_site", "unknown_site")
        self.assertEqual(self.get("/sites/ghost/password").status_code, 404)
        self.assertEqual(self.get("/sites/ghost/install").status_code, 404)

    def test_a_tool_that_says_something_is_shown_as_text(self):
        self.ctl.replies["install"] = {"ok": True, "rc": 0, "ran": True, "out": "<script>alert(1)</script> done"}
        r = self.post("/sites/alpha/install", {"title": "A", "user": "owner", "email": "o@example.com", "password": self.PW, "again": self.PW})
        self.assertNotIn("<script", r.get_data(as_text=True).lower())
        self.assertIn("&lt;script&gt;", r.get_data(as_text=True))

    def test_the_site_page_links_to_the_two_forms(self):
        html = self.get("/sites/alpha").get_data(as_text=True)
        self.assertIn('href="/sites/alpha/install"', html)
        self.assertIn('href="/sites/alpha/password"', html)

    def test_the_app_never_logs_the_password(self):
        import logging
        records = []
        handler = logging.Handler()
        handler.emit = lambda rec: records.append(rec.getMessage())
        self.app.logger.addHandler(handler)
        self.ctl.fail["password"] = CtlError("failed", "wp-ctl failed: x", None)
        self.post("/sites/alpha/password", {"user": "admin", "password": self.PW, "again": self.PW})
        self.assertFalse([m for m in records if self.PW in m])


class Imports(Base):
    text = Actions.text
    """Batch 13: upload a .wpress as the raw body of a PUT, plan or run it, read the report, approve or reset, remove a file."""
    DATA = bytes(range(256)) * 1000

    def setUp(self):
        super().setUp()
        self.logged_in()
        self.token = self.csrf(path="/import")
        self.slept = []
        import wp_panel.views as views
        self._sleep = views.time.sleep
        views.time.sleep = self.slept.append
        self.addCleanup(setattr, views.time, "sleep", self._sleep)

    def put(self, name, data=None, token=True, **kw):
        headers = {"X-CSRF": self.token} if token is True else ({"X-CSRF": token} if token else {})
        return self.c.put("/import/files/" + name, data=self.DATA if data is None else data, headers=headers, base_url=BASE, **kw)

    def post(self, path, data, token=True):
        data = dict(data)
        if token is True:
            token = self.token
        if token:
            data["csrf"] = token
        return self.c.post(path, data=data, base_url=BASE)

    def discards(self):
        return [a for v, a in self.ctl.calls if v == "import-discard"]

    def test_the_import_page_is_the_only_one_that_may_run_a_script_and_only_this_apps_own_file(self):
        r = self.get("/import")
        csp = r.headers["Content-Security-Policy"]
        self.assertEqual(csp, "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.assertNotRegex(csp, r"https?:|\*|unsafe|data:|blob:")
        html = r.get_data(as_text=True)
        self.assertEqual(re.findall(r"<script[^>]*>", html), ['<script src="/static/upload.js" defer>'])
        self.assertEqual(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S), [""], "no inline code")
        for path in PAGES + ["/new-site", "/sites/alpha/password", "/sites/alpha/install", "/login"]:
            if path != "/import":
                self.assertEqual(self.get(path, follow_redirects=True).headers["Content-Security-Policy"], EXPECTED_HEADERS["Content-Security-Policy"], path)
        for name, value in EXPECTED_HEADERS.items():
            if name != "Content-Security-Policy":
                self.assertEqual(r.headers.get(name), value, name)

    def test_the_script_file_is_served_and_builds_no_html(self):
        r = self.get("/static/upload.js")
        self.assertEqual(r.status_code, 200)
        js = r.get_data(as_text=True)
        for bad in ("innerHTML", "outerHTML", "document.write", "eval(", "insertAdjacentHTML", "new Function", "setTimeout(\"", "localStorage", "sendBeacon", "http://", "https://"):
            self.assertNotIn(bad, js, bad)
        self.assertIn("textContent", js)
        self.assertIn('xhr.setRequestHeader("X-CSRF", form.dataset.csrf);', js, "the token the page was drawn with goes in the header")
        self.assertIn("xhr.send(file)", js, "the file itself is the body: no FormData, no multipart")
        self.assertNotIn("FormData", js)
        self.assertIn('"?file=" + encodeURIComponent(reply.file) + "&uploaded=1#start"', js, "after an upload the page opens again with the file chosen and listed")

    def test_the_hub_lists_the_sites_and_hands_the_script_the_token_and_the_limit(self):
        html = self.text("/import")
        self.assertIn('<option value="alpha">', html)
        self.assertIn('<option value="bravo">', html)
        self.assertRegex(html, r'data-csrf="%s"' % re.escape(self.token))
        self.assertIn('data-max-bytes="%d"' % UPLOAD_MAX, html)

    def test_an_upload_passes_every_byte_to_the_client_once_and_answers_with_the_count_and_hash(self):
        r = self.put("site-one.wpress")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json(), {"ok": True, "file": "site-one.wpress", "bytes": len(self.DATA), "sha256": hashlib.sha256(self.DATA).hexdigest()})
        self.assertEqual(self.ctl.streams, [("import-receive", {"file": "site-one.wpress"}, len(self.DATA), hashlib.sha256(self.DATA).hexdigest())])
        self.assertEqual(self.discards(), [])

    def test_an_upload_without_the_right_token_or_the_login_streams_nothing(self):
        for token in (False, "", "wrong", "x" * 40):
            with self.subTest(token=token):
                self.assertEqual(self.put("a1.wpress", token=token).status_code, 400)
        other = self.app.test_client()
        r = other.put("/import/files/a1.wpress", data=self.DATA, headers={"X-CSRF": self.token}, base_url=BASE)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.ctl.streams, [])
        form = self.c.put("/import/files/a1.wpress", data=self.DATA, base_url=BASE, query_string={"csrf": self.token})
        self.assertEqual(form.status_code, 400, "the token in the address is no token: only the header")

    def test_the_length_must_be_announced_and_must_fit_the_intake(self):
        self.assertIn(self.put("a1.wpress", data=b"").status_code, (400, 411))
        r = self.c.put("/import/files/a1.wpress", data=b"", headers={"X-CSRF": self.token}, base_url=BASE, environ_overrides={"CONTENT_LENGTH": "0"})
        self.assertEqual(r.status_code, 400)
        r = self.c.put("/import/files/a1.wpress", data=b"x" * 10, headers={"X-CSRF": self.token}, base_url=BASE, environ_overrides={"CONTENT_LENGTH": str(UPLOAD_MAX + 1)})
        self.assertEqual(r.status_code, 413)
        self.assertIn("larger than the 3072 MB", r.get_json()["error"], "the page's own answer, not a framework error page")
        r = self.c.put("/import/files/a1.wpress", input_stream=io.BytesIO(b"x" * 10), headers={"X-CSRF": self.token}, base_url=BASE, environ_overrides={"CONTENT_LENGTH": None})
        self.assertEqual(r.status_code, 411, "a chunked body announces no length: the page does not take it")
        self.assertEqual(self.ctl.streams, [])

    def test_a_name_that_is_not_a_plain_wpress_name_is_refused(self):
        for name in ("a.zip", "a", ".hidden.wpress", "a b.wpress", "x" * 101 + ".wpress", "A%0A.wpress", "a;b.wpress"):
            with self.subTest(name=name):
                r = self.put(name)
                self.assertIn(r.status_code, (400, 404), name)
        self.assertEqual([x for x in self.ctl.streams if x[2]], [] if False else [x for x in self.ctl.streams if x[2]])
        self.assertEqual(self.discards(), [])

    def test_a_refusal_of_dellwp_is_shown_and_removes_nothing(self):
        self.ctl.fail["import-receive"] = CtlError("refused", "wp-ctl refused: failed (a1.wpress is already in the intake directory)", None)
        r = self.put("a1.wpress")
        self.assertEqual(r.status_code, 400)
        self.assertIn("already in the intake", r.get_json()["error"])
        self.assertEqual(self.discards(), [], "a file that was there before is not ours to remove")

    def test_an_answer_that_does_not_match_or_an_unreachable_dellwp_removes_what_may_have_been_kept(self):
        for kind, status in (("bad_reply", 502), ("unreachable", 503)):
            self.ctl.calls.clear()
            self.ctl.fail["import-receive"] = CtlError(kind, "it went wrong")
            r = self.put("a1.wpress")
            self.assertEqual(r.status_code, status, kind)
            self.assertEqual(self.discards(), [{"file": "a1.wpress", "if_present": True}], kind)

    def test_a_body_that_ends_early_is_cut_off_and_what_reached_dellwp_is_removed_again(self):
        r = self.c.put("/import/files/a1.wpress", data=b"x" * 1000, headers={"X-CSRF": self.token}, base_url=BASE, environ_overrides={"CONTENT_LENGTH": "5000"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cut off", r.get_json()["error"])
        self.assertEqual(self.discards(), [{"file": "a1.wpress", "if_present": True}])
        self.assertTrue(self.slept, "it waits a moment first: dellwp finishes the short file when its input ends")

    def test_the_upload_is_read_in_chunks_of_at_most_64_kib(self):
        sizes = []
        real = self.ctl.stream

        def spy(verb, chunks, **args):
            return real(verb, (sizes.append(len(c)) or c for c in chunks), **args)
        self.ctl.stream = spy
        self.put("a1.wpress", data=b"y" * (300 * 1024))
        self.assertTrue(sizes and max(sizes) <= 64 * 1024 and sum(sizes) == 300 * 1024, sizes)

    def test_run_plan_run_and_replace_ask_for_the_right_flags_and_a_replace_needs_the_name(self):
        base = {"site": "alpha", "file": "a1.wpress", "req": "pr1"}
        self.assertEqual(self.post("/import/run", dict(base, mode="plan")).headers["Location"], "/jobs/" + JOB)
        self.post("/import/run", dict(base, mode="run"))
        self.post("/import/run", dict(base, mode="replace", confirm_name="alpha"))
        self.assertEqual([a for v, a in self.ctl.calls if v == "import-run"], [
            {"site": "alpha", "file": "a1.wpress", "req": "pr1", "plan": True, "replace": False},
            {"site": "alpha", "file": "a1.wpress", "req": "pr1", "plan": False, "replace": False},
            {"site": "alpha", "file": "a1.wpress", "req": "pr1", "plan": False, "replace": True, "confirm_name": "alpha"}])

    def test_run_refuses_a_replace_without_the_name_a_name_with_another_mode_and_a_mode_that_is_not_one(self):
        base = {"site": "alpha", "file": "a1.wpress", "req": "pr1"}
        for data in (dict(base, mode="replace"), dict(base, mode="replace", confirm_name="bravo"), dict(base, mode="replace", confirm_name="Alpha"), dict(base, mode=""), dict(base, mode="delete"),
                     dict(base, mode="run", file="../a.wpress"), dict(base, mode="run", site="A"), dict(base, mode="run", file="a1.zip"), dict(base, mode="run", req="")):
            with self.subTest(data=data):
                self.assertEqual(self.post("/import/run", data).status_code, 400)
        self.assertEqual([v for v, _ in self.ctl.calls if v == "import-run"], [])

    def test_a_replace_sends_the_name_only_for_a_replace(self):
        self.post("/import/run", {"site": "alpha", "file": "a1.wpress", "req": "pr1", "mode": "run", "confirm_name": "alpha"})
        self.assertNotIn("confirm_name", [a for v, a in self.ctl.calls if v == "import-run"][0])

    def test_remove_a_file_asks_for_the_discard_and_says_what_the_tool_said(self):
        r = self.post("/import/discard", {"file": "a1.wpress"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("removed a1.wpress", r.get_data(as_text=True))
        self.assertEqual(self.discards(), [{"file": "a1.wpress", "if_present": False}])
        for name in ("", "../a.wpress", "a.zip"):
            self.assertEqual(self.post("/import/discard", {"file": name}).status_code, 400)
        self.assertEqual(len(self.discards()), 1)

    def test_every_import_form_needs_the_token_and_asks_nothing_without_it(self):
        forms = (("/import/run", {"site": "alpha", "file": "a1.wpress", "req": "p1", "mode": "plan"}), ("/import/discard", {"file": "a1.wpress"}),
                 ("/sites/alpha/import/approve", {"req": "p1"}), ("/sites/alpha/import/reset", {"confirm_name": "alpha", "req": "p1"}))
        for path, data in forms:
            for token in (False, "", "wrong"):
                with self.subTest(path=path, token=token):
                    self.assertEqual(self.post(path, data, token=token).status_code, 400)
        self.assertEqual([c for c in self.ctl.calls if c[0].startswith("import-") and c[0] not in ("import-report", "import-list")], [])

    def test_the_site_page_shows_the_report_as_text_and_has_the_two_forms_with_a_token_each(self):
        self.ctl.replies["import-report"]["out"] = "<script>alert(1)</script> plugin x\n"
        html = self.text("/sites/alpha/import")
        self.assertNotIn("<script", html.lower())
        self.assertIn("&lt;script&gt;", html)
        for action in ("/sites/alpha/import/approve", "/sites/alpha/import/reset"):
            form = re.search(r'<form method="post" action="%s".*?</form>' % action, html, re.S)
            self.assertIsNotNone(form, action)
            self.assertRegex(form.group(0), r'name="csrf" value="[^"]{20,}"')
            self.assertRegex(form.group(0), r'name="req" value="p[0-9a-f]{32}(approve|reset)"')

    def test_no_report_yet_is_a_sentence_not_an_error_page(self):
        self.ctl.fail["import-report"] = CtlError("failed", "wp-ctl failed: no error code (no report for alpha)", None)
        html = self.text("/sites/alpha/import")
        self.assertIn("no report for alpha", html)
        self.assertIn("Approve", html)
        self.ctl.fail["import-report"] = CtlError("unreachable", "dellwp did not answer within 30 seconds")
        self.text("/sites/alpha/import", 503)

    def test_an_unknown_site_is_a_404_on_its_import_page(self):
        self.ctl.fail["show"] = CtlError("refused", "wp-ctl refused: unknown_site", "unknown_site")
        self.text("/sites/ghost/import", 404)

    def test_approve_sends_the_rejects_and_the_acceptance_only_when_it_was_given(self):
        self.post("/sites/alpha/import/approve", {"req": "pa1"})
        self.post("/sites/alpha/import/approve", {"req": "pa1", "reject": "akismet,hello-dolly", "accept_failures": "yes", "confirm_name": "alpha"})
        self.post("/sites/alpha/import/approve", {"req": "pa1", "accept_failures": "on", "confirm_name": "alpha"})
        calls = [a for v, a in self.ctl.calls if v == "import-approve"]
        self.assertEqual(calls[0], {"site": "alpha", "req": "pa1", "accept_failures": False})
        self.assertEqual(calls[1], {"site": "alpha", "req": "pa1", "accept_failures": True, "confirm_name": "alpha", "reject": "akismet,hello-dolly"})
        self.assertEqual(calls[2], {"site": "alpha", "req": "pa1", "accept_failures": False}, "only the checkbox's own value counts, and a name typed without it is not sent")
        for reject in ("a b", "a,,b", "../x", "a;b"):
            self.assertEqual(self.post("/sites/alpha/import/approve", {"req": "pa1", "reject": reject}).status_code, 400)

    def test_reset_needs_the_site_name_typed_exactly(self):
        for typed in ("", "bravo", "Alpha", "alpha "):
            self.assertEqual(self.post("/sites/alpha/import/reset", {"confirm_name": typed, "req": "pr1"}).status_code, 400)
        self.assertEqual([c for c in self.ctl.calls if c[0] == "import-reset"], [])
        r = self.post("/sites/alpha/import/reset", {"confirm_name": "alpha", "req": "pr1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (303, "/jobs/" + JOB))
        self.assertEqual([a for v, a in self.ctl.calls if v == "import-reset"], [{"site": "alpha", "confirm_name": "alpha", "req": "pr1"}])


class RestorePage(Base):
    """The backups list and the restore: a page that lists starts nothing, a restore needs the site's name typed and the session's token, and only a stamp from the list reaches wp-ctl."""
    STAMP = "20261031T020000Z"

    def setUp(self):
        super().setUp()
        self.logged_in()

    def post(self, path, data, token=True):
        data = dict(data)
        if token is True:
            token = self.csrf(path="/sites/alpha")
        if token:
            data["csrf"] = token
        return self.c.post(path, data=data, base_url=BASE)

    def test_the_backups_page_lists_the_snapshots_newest_first_with_a_button_each(self):
        html = self.get("/sites/alpha/backups").get_data(as_text=True)
        self.assertLess(html.index("2026-10-31 02:00"), html.index("2026-10-30 02:00"))
        self.assertIn("before an update", html); self.assertIn("nightly", html); self.assertIn("empty site", html); self.assertIn("200.0 MB", html)
        self.assertEqual(html.count("Restore…"), 2)
        self.assertIn("/sites/alpha/restore?stamp=" + self.STAMP, html)
        self.assertEqual([c for c in self.ctl.calls if c[0] == "restore"], [], "a page that lists starts nothing")

    def test_a_stopped_site_has_no_restore_button_and_the_page_says_why(self):
        self.ctl.replies["show"]["status"]["deployed"] = "stopped"
        html = self.get("/sites/alpha/backups").get_data(as_text=True)
        self.assertNotIn("Restore…", html); self.assertIn("Move it to hold first", html)

    def test_a_list_that_cannot_be_had_is_said_on_the_page(self):
        self.ctl.fail["backups"] = CtlError("failed", "the backup directory could not be read")
        r = self.get("/sites/alpha/backups")
        self.assertEqual(r.status_code, 200); self.assertIn("could not be read", r.get_data(as_text=True))

    def test_the_confirm_page_names_the_snapshot_and_a_live_site_gets_the_maintenance_warning(self):
        html = self.get("/sites/alpha/restore?stamp=" + self.STAMP).get_data(as_text=True)
        self.assertIn("2026-10-31 02:00 UTC", html); self.assertIn("before an update", html); self.assertIn("maintenance page", html)
        self.assertIn('name="stamp" value="%s"' % self.STAMP, html); self.assertIn('name="confirm_name"', html)
        self.assertEqual([c for c in self.ctl.calls if c[0] == "restore"], [])

    def test_a_stamp_that_is_not_one_or_not_in_the_list_is_refused_before_a_page_is_drawn(self):
        for stamp in ("", "latest", "../x", self.STAMP + "x", "20261029T020000Z"):
            self.assertIn(self.get("/sites/alpha/restore?stamp=" + stamp).status_code, (400, 404), stamp)

    def test_the_restore_needs_the_site_name_typed_exactly_and_nothing_else_reaches_the_tool(self):
        for typed in ("", "bravo", "Alpha", "alpha "):
            self.assertEqual(self.post("/sites/alpha/restore", {"confirm_name": typed, "stamp": self.STAMP, "req": "pr1"}).status_code, 400, typed)
        self.assertEqual(self.post("/sites/alpha/restore", {"confirm_name": "alpha", "stamp": "latest", "req": "pr1"}).status_code, 400)
        self.assertEqual([c for c in self.ctl.calls if c[0] == "restore"], [])
        r = self.post("/sites/alpha/restore", {"confirm_name": "alpha", "stamp": self.STAMP, "req": "pr1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (303, "/jobs/" + JOB))
        self.assertEqual([a for v, a in self.ctl.calls if v == "restore"], [{"site": "alpha", "stamp": self.STAMP, "confirm_name": "alpha", "req": "pr1"}])

    def test_the_restore_form_needs_the_session_token(self):
        self.assertIn(self.post("/sites/alpha/restore", {"confirm_name": "alpha", "stamp": self.STAMP, "req": "pr1"}, token=False).status_code, (400, 403))
        self.assertEqual([c for c in self.ctl.calls if c[0] == "restore"], [])


STAND_IN_SSH = """#!{python}
import hashlib, json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
request = json.loads(sys.stdin.buffer.readline())
open(os.path.join(here, "request.json"), "w").write(json.dumps(request))
if request["verb"] == "import-discard":
    open(os.path.join(here, "discarded"), "a").write(request["file"] + "\\n")
    print(json.dumps({{"ok": True, "rc": 0, "ran": True, "out": "removed\\n"}}))
    sys.exit(0)
h, n = hashlib.sha256(), 0
while True:
    b = sys.stdin.buffer.read(1 << 16)
    if not b:
        break
    h.update(b)
    n += len(b)
open(os.path.join(here, "received"), "w").write("%d %s" % (n, h.hexdigest()))
print(json.dumps({{"ok": True, "rc": 0, "ran": True, "out": "received %s %d %s\\n" % (h.hexdigest(), n, request["file"])}}))
"""


class UploadEndToEnd(Base):
    """The upload view with the REAL client and a stand-in ssh that does what wp-ctl's receive does: the whole chain from the PUT to the count and hash on 'dellwp'."""

    def make_app(self):
        import os
        import stat
        self.bin = Path(self.d.name) / "bin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text(STAND_IN_SSH.format(python=sys.executable))
        ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
        self.real = Ctl("dellwp", "wpctl", "/k", "/kh", ssh=str(ssh), path=os.environ["PATH"])
        return create_app(self.settings, self.real, self.clock)

    def put(self, name, data, length=None, token=True):
        headers = {"X-CSRF": self.token}
        r = self.c.put("/import/files/" + name, data=data, headers=headers, base_url=BASE, environ_overrides={"CONTENT_LENGTH": str(len(data) if length is None else length)})
        return r

    def setUp(self):
        super().setUp()
        self.logged_in()
        # the token of the logged-in session: any page carries it
        html = self.get("/new-site").get_data(as_text=True)
        self.token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    def test_a_file_goes_from_the_put_through_the_real_client_and_ssh_with_every_byte(self):
        import os
        data = os.urandom(3 * 1024 * 1024 + 17)
        r = self.put("big-one.wpress", data)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json(), {"ok": True, "file": "big-one.wpress", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual((self.bin / "received").read_text(), "%d %s" % (len(data), hashlib.sha256(data).hexdigest()))
        self.assertEqual(json.loads((self.bin / "request.json").read_text()), {"verb": "import-receive", "file": "big-one.wpress"})
        self.assertFalse((self.bin / "discarded").exists())

    def test_a_body_that_ends_early_stops_the_chain_and_asks_for_the_file_to_be_removed(self):
        import wp_panel.views as views
        real_sleep = views.time.sleep
        views.time.sleep = lambda s: None
        self.addCleanup(setattr, views.time, "sleep", real_sleep)
        r = self.put("cut.wpress", b"z" * 100000, length=300000)
        self.assertEqual(r.status_code, 400)
        self.assertIn("cut off", r.get_json()["error"])
        self.assertEqual((self.bin / "discarded").read_text(), "cut.wpress\n")


class FollowUps(Base):
    """After the click-through: pending imports are shown where you look, files are listed, a new site can be made from a file, the report says what to do, updates and the vulnerability check have a page."""

    def setUp(self):
        super().setUp()
        self.logged_in()
        self.token = self.csrf(path="/import")

    def post(self, path, data, token=True):
        data = dict(data)
        if token is True:
            token = self.token
        if token:
            data["csrf"] = token
        return self.c.post(path, data=data, base_url=BASE)

    def text(self, path, status=200):
        r = self.get(path)
        self.assertEqual(r.status_code, status, (path, r.get_data(as_text=True)[:200]))
        return r.get_data(as_text=True)

    def acted(self):
        return [(v, a) for v, a in self.ctl.calls if v in ACTION_VERBS]

    # ---- the sites page mentions what waits
    def test_the_sites_page_says_which_imports_wait_and_whether_they_are_clean(self):
        html = self.text("/sites")
        self.assertIn('href="/sites/bravo/import"', html)
        self.assertIn("1 import waiting for your decision", html)
        self.assertIn("not clean", html)
        self.ctl.replies["import-list"]["imports"][0]["problems"] = []
        self.assertRegex(self.text("/sites"), r'bravo</a> <span class="badge ok">clean</span>')

    def test_an_unfinished_import_is_mentioned_too_and_none_means_no_banner(self):
        self.ctl.replies["import-list"]["imports"] = [{"site": "bravo", "file": None, "stage": "unfinished", "problems": []}]
        self.assertIn("Unfinished import", self.text("/sites"))
        self.ctl.replies["import-list"]["imports"] = []
        html = self.text("/sites")
        self.assertNotIn("waiting for your decision", html)
        self.assertNotIn("Unfinished import", html)

    def test_the_sites_page_opens_even_when_dellwp_cannot_say_what_waits(self):
        self.ctl.fail["import-list"] = CtlError("unreachable", "dellwp did not answer within 30 seconds")
        html = self.text("/sites")
        self.assertIn('href="/sites/alpha"', html)
        self.assertNotIn("waiting for your decision", html)

    # ---- the import page
    def test_the_hub_lists_the_files_with_size_age_and_use_and_only_an_unused_file_can_be_removed(self):
        html = self.text("/import")
        self.assertIn("friend.wpress", html)
        self.assertIn("5.0 MB", html)
        self.assertIn("2 h ago", html)
        self.assertIn("2 d ago", html)
        self.assertIn("import in bravo", html)
        self.assertIn('href="/import?file=friend.wpress#start"', html)
        forms = re.findall(r'<form method="post" action="/import/discard".*?</form>', html, re.S)
        self.assertEqual(len(forms), 1, "only the unused file has a remove button")
        self.assertIn('name="file" value="friend.wpress"', forms[0])
        self.assertRegex(forms[0], r'name="csrf" value="[^"]{20,}"')
        self.assertNotIn('?file=used.wpress', html)

    def test_the_hub_shows_the_imports_waiting_with_the_first_problem_and_a_way_to_decide(self):
        html = self.text("/import")
        self.assertIn("Waiting for you", html)
        self.assertIn("Read the report and decide", html)
        self.assertIn("known vulnerabilities: FOUND: 7 finding(s)", html)
        self.ctl.replies["import-list"]["imports"] = []
        self.assertNotIn("Waiting for you", self.text("/import"))

    def test_the_hub_opens_when_dellwp_cannot_list_and_says_so(self):
        self.ctl.fail["import-list"] = CtlError("unreachable", "dellwp did not answer within 30 seconds")
        html = self.text("/import")
        self.assertIn("could not say what the intake holds", html)
        self.assertIn("30 seconds", html)
        self.assertIn('name="mode"', html)

    def test_a_file_name_in_the_address_is_put_in_the_form_only_when_it_is_a_plain_wpress_name(self):
        self.assertIn('value="friend.wpress"', self.text("/import?file=friend.wpress"))
        for bad in ("../x.wpress", "a.zip", '"><script>alert(1)</script>.wpress', "x" * 120 + ".wpress", "a b.wpress", ".h.wpress"):
            html = self.text("/import?file=" + bad.replace("<", "%3C").replace(">", "%3E").replace('"', "%22").replace(" ", "%20"))
            self.assertNotIn("<script>alert", html, bad)
            self.assertNotIn('value="%s"' % bad.replace('"', "&#34;"), html, bad)
            self.assertRegex(html, r'id="run-file"[^>]*value=""', bad)

    def test_what_the_tool_lists_is_text_not_markup(self):
        self.ctl.replies["import-list"]["files"][0]["name"] = '"><script>alert(1)</script>'
        self.ctl.replies["import-list"]["imports"][0]["problems"] = ["<img src=x onerror=alert(1)>"]
        html = self.text("/import")
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)

    def test_a_file_with_odd_numbers_is_shown_with_dashes_not_a_traceback(self):
        self.ctl.replies["import-list"]["files"] = [{"name": "a.wpress", "bytes": None, "mtime": "yesterday", "site": None}, {"name": "b.wpress", "bytes": True, "mtime": True, "site": None},
                                                    {"name": "c.wpress", "bytes": -5, "mtime": None, "site": None}, {"name": "d.wpress", "bytes": 0, "mtime": T0 + 99999, "site": None}]
        html = self.text("/import")
        for name in "abcd":
            self.assertIn("%s.wpress" % name, html)
        self.assertIn("just now", html)

    # ---- start an import: a new site, or an existing one
    def test_a_new_site_from_a_file_asks_for_one_job_with_the_name_the_label_and_the_file(self):
        r = self.post("/import/run", {"mode": "new", "name": "newshop", "label": "New shop", "file": "friend.wpress", "req": "pn1", "site": "alpha", "confirm_name": "alpha"})
        self.assertEqual((r.status_code, r.headers["Location"]), (303, "/jobs/" + JOB))
        self.assertEqual(self.acted(), [("import-new", {"name": "newshop", "file": "friend.wpress", "req": "pn1", "label": "New shop"})], "the existing site's fields are not sent")
        self.ctl.calls.clear()
        self.post("/import/run", {"mode": "new", "name": "newshop", "label": "", "file": "friend.wpress", "req": "pn1"})
        self.assertEqual(self.acted(), [("import-new", {"name": "newshop", "file": "friend.wpress", "req": "pn1"})])

    def test_a_new_site_needs_a_good_name_a_good_file_and_a_request_id_or_nothing_is_asked(self):
        base = {"mode": "new", "name": "newshop", "file": "friend.wpress", "req": "pn1"}
        for over in ({"name": ""}, {"name": "Bad"}, {"name": "a"}, {"name": "a--b"}, {"name": "x" * 25}, {"file": ""}, {"file": "../a.wpress"}, {"file": "a.zip"}, {"req": ""}, {"label": "a\nb"}, {"label": " x"}):
            with self.subTest(over=over):
                self.assertEqual(self.post("/import/run", dict(base, **over)).status_code, 400, over)
        self.assertEqual(self.acted(), [])

    def test_a_refusal_by_dellwp_of_a_new_site_is_shown_as_text(self):
        self.ctl.fail["import-new"] = CtlError("refused", "wp-ctl refused: name_reserved (<b>hub</b> is reserved)", "name_reserved")
        r = self.post("/import/run", {"mode": "new", "name": "hub", "file": "friend.wpress", "req": "pn1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("name_reserved", r.get_data(as_text=True))
        self.assertNotIn("<b>hub</b>", r.get_data(as_text=True))

    def test_the_form_offers_the_four_things_in_plain_words_new_site_first(self):
        html = self.text("/import")
        modes = re.findall(r'<input type="radio" name="mode" value="([a-z]+)"( checked)?>', html)
        self.assertEqual(modes, [("new", " checked"), ("run", ""), ("plan", ""), ("replace", "")])
        self.assertIn("Make a new site from it", html)
        self.assertRegex(html, r'<input name="name" maxlength="24"')

    # ---- the report page says what to do
    def test_the_report_page_of_a_site_that_is_not_clean_says_why_and_what_to_do(self):
        html = self.text("/sites/bravo/import")
        self.assertIn("The scan did not come back clean", html)
        self.assertIn("known vulnerabilities: FOUND: 7 finding(s)", html)
        self.assertIn('href="/sites/bravo/security"', html)
        self.assertIn("accept them below and type the site", html)
        self.assertRegex(html, r'<span class="badge bad">not clean</span>')

    def test_the_report_page_of_a_clean_import_says_so_and_one_that_did_not_finish_says_reset(self):
        self.ctl.replies["import-list"]["imports"][0]["problems"] = []
        html = self.text("/sites/bravo/import")
        self.assertIn("Clean: no failed step", html)
        self.assertNotIn("did not come back clean", html)
        self.ctl.replies["import-list"]["imports"][0]["stage"] = "unfinished"
        self.assertIn("did not finish", self.text("/sites/bravo/import"))
        self.assertNotIn("Clean: no failed step", self.text("/sites/alpha/import"))

    def test_accepting_the_failures_needs_the_sites_name_typed_and_nothing_is_asked_without_it(self):
        for typed in ("", "alph", "Alpha", "alpha ", "bravo"):
            r = self.post("/sites/alpha/import/approve", {"req": "pa1", "accept_failures": "yes", "confirm_name": typed})
            self.assertEqual(r.status_code, 400, typed)
        self.assertEqual(self.acted(), [])
        r = self.post("/sites/alpha/import/approve", {"req": "pa1", "accept_failures": "yes", "confirm_name": "alpha"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.acted()[-1], ("import-approve", {"site": "alpha", "req": "pa1", "accept_failures": True, "confirm_name": "alpha"}))

    # ---- updates and security
    def test_the_security_page_shows_what_the_check_found_as_text_with_a_badge(self):
        html = self.text("/sites/alpha/security")
        self.assertIn("vulnerabilities found", html)
        self.assertIn("WordPress Core 4.7.0 - 7.1.3 (7 findings)", html)
        for rc, word in ((0, "none known"), (3, "not checked")):
            self.ctl.replies["vuln-check"]["rc"] = rc
            self.assertIn(word, self.text("/sites/alpha/security"))
        self.ctl.replies["vuln-check"]["out"] = "<script>alert(1)</script>"
        self.assertNotIn("<script>alert", self.text("/sites/alpha/security"))

    def test_a_check_that_fails_is_shown_on_the_page_with_the_buttons_still_there(self):
        self.ctl.fail["vuln-check"] = CtlError("failed", "wp-ctl failed: no error code (the feed refused)", None)
        html = self.text("/sites/alpha/security")
        self.assertIn("the feed refused", html)
        self.assertIn('action="/sites/alpha/updates/check"', html)
        self.assertIn('action="/sites/alpha/updates/apply"', html)
        self.ctl.fail["vuln-check"] = CtlError("unreachable", "dellwp did not answer within 330 seconds")
        self.text("/sites/alpha/security", 503)

    def test_an_unknown_site_is_a_404_on_its_security_page(self):
        self.ctl.fail["show"] = CtlError("refused", "wp-ctl refused: unknown_site", "unknown_site")
        self.text("/sites/ghost/security", 404)

    def test_a_waiting_import_is_pointed_to_from_the_security_page_and_can_be_scanned_again_from_its_own(self):
        html = self.text("/sites/bravo/security")
        self.assertIn("An import of this site is waiting for a decision and its scan was not clean", html)
        self.assertIn('href="/sites/bravo/import"', html)
        self.assertNotIn("waiting for a decision", self.text("/sites/alpha/security"), "a site with no waiting import gets no notice")
        page = self.text("/sites/bravo/import")
        self.assertIn('action="/sites/bravo/import/rescan"', page)
        self.assertIn("Scan again", page)
        self.ctl.replies["import-list"]["imports"][0]["stage"] = "unfinished"
        self.assertNotIn("/import/rescan", self.text("/sites/bravo/import"), "only a reported import can be scanned again")

    def test_scan_again_starts_one_job_and_needs_the_token(self):
        r = self.post("/sites/bravo/import/rescan", {"req": "ps1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (303, "/jobs/" + JOB))
        self.assertEqual(self.acted(), [("import-rescan", {"site": "bravo", "req": "ps1"})])
        self.ctl.calls.clear()
        for token in (False, "", "wrong"):
            self.assertEqual(self.post("/sites/bravo/import/rescan", {"req": "ps1"}, token=token).status_code, 400)
        self.assertEqual(self.acted(), [])

    def test_check_for_updates_starts_one_job_and_needs_the_token(self):
        r = self.post("/sites/alpha/updates/check", {"req": "pu1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (303, "/jobs/" + JOB))
        self.assertEqual(self.acted(), [("update-check", {"site": "alpha", "req": "pu1"})])
        self.ctl.calls.clear()
        for token in (False, "", "wrong"):
            self.assertEqual(self.post("/sites/alpha/updates/check", {"req": "pu1"}, token=token).status_code, 400)
        self.assertEqual(self.acted(), [])

    def test_applying_updates_needs_the_box_ticked_and_the_token(self):
        self.assertEqual(self.post("/sites/alpha/updates/apply", {"req": "pu1"}).status_code, 400)
        self.assertEqual(self.post("/sites/alpha/updates/apply", {"req": "pu1", "understood": "on"}).status_code, 400)
        self.assertEqual(self.post("/sites/alpha/updates/apply", {"req": "pu1", "understood": "yes"}, token=False).status_code, 400)
        self.assertEqual(self.acted(), [])
        r = self.post("/sites/alpha/updates/apply", {"req": "pu1", "understood": "yes"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.acted(), [("update-apply", {"site": "alpha", "req": "pu1"})])

    def test_the_security_page_forms_carry_what_the_server_wants(self):
        html = self.text("/sites/alpha/security")
        apply_form = re.search(r'<form method="post" action="/sites/alpha/updates/apply".*?</form>', html, re.S).group(0)
        check_form = re.search(r'<form method="post" action="/sites/alpha/updates/check".*?</form>', html, re.S).group(0)
        self.assertIn('<input type="checkbox" name="understood" value="yes">', apply_form)
        for form in (apply_form, check_form):
            self.assertRegex(form, r'name="csrf" value="[^"]{20,}"')
            self.assertRegex(form, r'name="req" value="p[0-9a-f]{32}(apply|check)"')

    def test_a_site_name_that_is_not_one_is_refused_before_anything_is_asked(self):
        for path in ("/sites/A--b/updates/check", "/sites/x_y/updates/apply"):
            self.assertIn(self.post(path, {"req": "pu1", "understood": "yes"}).status_code, (400, 404))
        self.assertEqual(self.acted(), [])

    def test_the_site_page_links_to_updates_and_security(self):
        self.assertIn('href="/sites/alpha/security"', self.text("/sites/alpha"))

    def test_the_audit_table_keeps_its_narrow_columns_whole(self):
        html = self.text("/audit")
        self.assertIn('<table class="audit">', html)
        self.assertRegex(html, r'<td class="rest">')
        css = self.get("/static/panel.css").get_data(as_text=True)
        self.assertIn("table.audit td:not(.rest)", css)
        self.assertIn("white-space: nowrap", css.split("table.audit td:not(.rest)")[1].split("}")[0])


class ReadOnly(Base):
    def test_the_app_has_exactly_these_routes_and_none_of_them_changes_anything_on_dellwp(self):
        rules = sorted((r.rule, tuple(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")))) for r in self.app.url_map.iter_rules())
        self.assertEqual(rules, sorted([("/", ("GET",)), ("/audit", ("GET",)), ("/import", ("GET",)), ("/import/discard", ("POST",)), ("/import/files/<file>", ("PUT",)), ("/import/run", ("POST",)),
                                        ("/jobs", ("GET",)), ("/jobs/<job>", ("GET",)), ("/login", ("GET", "POST")), ("/logout", ("POST",)),
                                        ("/new-site", ("GET",)), ("/new-site", ("POST",)), ("/sites", ("GET",)), ("/sites/<name>", ("GET",)), ("/sites/<name>/backups", ("GET",)), ("/sites/<name>/delete", ("POST",)), ("/sites/<name>/restore", ("GET",)), ("/sites/<name>/restore", ("POST",)),
                                        ("/sites/<name>/import", ("GET",)), ("/sites/<name>/import/approve", ("POST",)), ("/sites/<name>/import/rescan", ("POST",)), ("/sites/<name>/import/reset", ("POST",)), ("/sites/<name>/install", ("GET",)), ("/sites/<name>/install", ("POST",)), ("/sites/<name>/password", ("GET",)), ("/sites/<name>/password", ("POST",)),
                                        ("/requests", ("GET",)), ("/requests/decide", ("POST",)), ("/requests/refresh", ("POST",)), ("/sites/<name>/plugins", ("GET",)), ("/sites/<name>/plugins", ("POST",)), ("/sites/<name>/security", ("GET",)), ("/sites/<name>/set", ("POST",)), ("/sites/<name>/state", ("POST",)),
                                        ("/sites/<name>/updates/apply", ("POST",)), ("/sites/<name>/updates/check", ("POST",)),
                                        ("/static/<path:filename>", ("GET",))]),
                         "a new route or a new method is a step-6 decision: it must be added here on purpose (batch 11: create, set, state; batch 12: delete, password, install; batch 13: the import)")

    def test_a_full_crawl_asks_for_read_verbs_only(self):
        self.logged_in()
        for path in PAGES + ["/audit?lines=10", "/jobs/%s?offset=5" % JOB]:
            self.get(path)
        verbs = {v for v, _ in self.ctl.calls}
        self.assertTrue(verbs and verbs <= set(READ_VERBS), verbs)
        self.assertEqual(verbs, set(READ_VERBS))

    def test_the_views_import_no_way_to_run_anything(self):
        for name in ("views.py", "app.py", "auth.py", "config.py"):
            text = (ROOT / "app/wp_panel" / name).read_text()
            code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
            for bad in ("subprocess", "os.system", "os.popen", "eval(", "exec(", "shell=True", "__import__", "pickle", "render_template_string", "|safe", "Markup("):
                self.assertNotIn(bad, code, "%s: %s" % (name, bad))
        for t in (ROOT / "app/wp_panel/templates").glob("*.html"):
            self.assertNotIn("|safe", t.read_text().replace(" ", ""), t.name)
            self.assertNotIn("autoescape", t.read_text(), t.name)

    def test_debug_is_off_and_the_body_limit_is_set(self):
        self.assertFalse(self.app.debug)
        self.assertEqual(self.app.config["MAX_CONTENT_LENGTH"], 16 * 1024)
        self.assertTrue(self.app.config["SESSION_COOKIE_SECURE"])


if __name__ == "__main__":
    unittest.main()
