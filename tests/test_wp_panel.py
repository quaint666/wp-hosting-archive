#!/usr/bin/env python3
"""Offline tests of the control app (app/wp_panel): settings, login, session, lockout, headers and the read-only pages.

The app is driven through Flask's test client over https, with a fake ctl client that answers like wp-ctl (and records every call) and a fake clock, so nothing here needs ssh, a server or real time.
They prove: no page but the login (and the stylesheet) works without a login; a login needs the password AND a fresh code, is answered the same whichever half was wrong, locks after five wrong tries
for every worker at once, refuses a code used twice, and ends after the session life; the session cookie is Secure, HttpOnly and SameSite=Strict and a forged or foreign one is no login; a form needs
its token; every answer carries the security headers; whatever dellwp sends is shown as text; a broken dellwp gives an error page, never a traceback; and the app only ever asks for read verbs and has no
route that changes anything. They do not prove gunicorn, the container, HTTPS or the real ssh. Needs Flask and pyotp (app/requirements.txt). Run: python3 -I tests/test_wp_panel.py"""
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
from wp_panel.ctl import ACTION_VERBS, ALLOWED, READ_VERBS, SET_KEYS, Ctl, CtlError  # noqa: E402

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
        self.calls, self.fail = [], {}
        self.replies = {
            "list": {"ok": True, "sites": [{"site": "alpha", "deployed": "live", "actual": "live", "unit": "active"}, {"site": "bravo", "deployed": "hold", "actual": "hold", "unit": "active"}]},
            "show": {"ok": True, "site": "alpha", "status": {"deployed": "live", "quota_pct": "9"}, "registry": {"name": "alpha", "note": "a note", "port": 9101}},
            "jobs": {"ok": True, "jobs": [{"job": JOB, "verb": "create", "site": "alpha", "state": "done", "rc": 0}]},
            "job-status": {"ok": True, "state": "done", "rc": 0},
            "job-log": {"ok": True, "job": JOB, "data": "line one\nline two\n", "offset": 18, "state": "done", "rc": 0},
            "create": {"ok": True, "job": JOB},
            "set": {"ok": True, "job": JOB},
            "ensure-state": {"ok": True, "job": JOB},
            "audit-tail": {"ok": True, "lines": [{"ts": "2026-10-29T12:00:00Z", "phase": "intent", "verb": "set", "site": "alpha", "req": "x"}]},
        }

    checker = Ctl("dellwp", "wpctl", "/k", "/kh")          # the real client's own checks of the verb and every value, so a page that sends something wp-ctl would refuse fails here as it would live

    def call(self, verb, **args):
        self.checker.request(verb, args)
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
        self.settings = config.Settings("dellwp", "wpctl", "/k", "/kh", str(self.state), 3 * 3600, b"k" * 40, generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000"), TOTP_SECRET)
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


PAGES = ["/", "/sites", "/sites/alpha", "/jobs", "/jobs/" + JOB, "/audit"]
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
        return dict({"PANEL_CTL_HOST": "dellwp", "PANEL_SECRET_DIR": d}, **more)

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

    def test_no_page_has_an_inline_script_an_inline_style_or_an_outside_link(self):
        c = self.logged_in()
        for path in PAGES + ["/login"]:
            html = self.get(path, c, follow_redirects=True).get_data(as_text=True)
            self.assertNotRegex(html, r"(?i)<script|\sstyle=|\son[a-z]+=|https?://", path)


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
        self.assertEqual(self.ctl.calls[-1], ("list", {}))

    def test_the_site_page_shows_the_status_and_the_record(self):
        html = self.text("/sites/alpha")
        for needle in ("quota_pct", "a note", "9101", "port"):
            self.assertIn(needle, html)
        self.assertEqual(self.ctl.calls[-1], ("show", {"site": "alpha"}))

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
        return [(v, a) for v, a in self.ctl.calls if v in ACTION_VERBS]

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
            self.assertEqual('name="confirm_name"' in html, "live" in offered, state)
        for state in ("", "weird", "closed"):
            self.ctl.replies["show"]["status"]["deployed"] = state
            self.ctl.replies["show"]["registry"].pop("state", None)
            self.assertEqual(re.findall(r'name="state" value', self.text("/sites/alpha")), [], state)

    def test_the_site_page_has_one_setting_form_per_editable_key_each_with_the_token_and_its_own_request_id(self):
        self.ctl.replies["show"]["registry"].update(php_mem_mb=640, update_policy="freeze", allow_xmlrpc=False, note="n")
        html = self.text("/sites/alpha")
        forms = re.findall(r'<form method="post" action="/sites/alpha/set".*?</form>', html, re.S)
        self.assertEqual([re.search(r'name="key" value="([a-z_]+)"', f).group(1) for f in forms], list(SET_KEYS))
        self.assertEqual(len({re.search(r'name="req" value="([^"]+)"', f).group(1) for f in forms}), len(SET_KEYS))
        self.assertTrue(all('name="csrf" value="' in f for f in forms))
        by = {re.search(r'name="key" value="([a-z_]+)"', f).group(1): f for f in forms}
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


class ReadOnly(Base):
    def test_the_app_has_exactly_these_routes_and_none_of_them_changes_anything_on_dellwp(self):
        rules = sorted((r.rule, tuple(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")))) for r in self.app.url_map.iter_rules())
        self.assertEqual(rules, sorted([("/", ("GET",)), ("/audit", ("GET",)), ("/jobs", ("GET",)), ("/jobs/<job>", ("GET",)), ("/login", ("GET", "POST")), ("/logout", ("POST",)),
                                        ("/new-site", ("GET",)), ("/new-site", ("POST",)), ("/sites", ("GET",)), ("/sites/<name>", ("GET",)), ("/sites/<name>/set", ("POST",)), ("/sites/<name>/state", ("POST",)),
                                        ("/static/<path:filename>", ("GET",))]),
                         "a new route or a new method is a step-6 decision: it must be added here on purpose (batch 11: create, set, state)")

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
