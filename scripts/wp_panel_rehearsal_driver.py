#!/usr/bin/env python3
"""The HTTPS half of scripts/wp_panel_rehearsal_aopenpanel.sh: what a browser on the ThinkPad would meet at the control app's name, checked with the certificate verified. Standard library only.

    python3 -I scripts/wp_panel_rehearsal_driver.py --host NAME --ip ADDRESS [--port 443] [--cacert FILE] [--login | --login-only]

Without --login it asks nothing and logs nothing in: the TLS handshake and the certificate for the name (valid, not about to expire, at least TLS 1.2); a handshake for ANY OTHER name is refused; /login answers
200 with the security headers the app and nginx must send, and no version in the server banner; /sites without a login is sent to /login; the stylesheet is served; a form posted without its token is refused.
With --login (or --login-only, which skips the checks above and only logs in) it reads the password on the first line of standard input and the authenticator code on the second (the caller reads them hidden and pipes them in: they are never an argument, an environment
variable or a file) and walks a real session: one wrong login is refused the same way as any other; the right one opens Sites, Jobs and Audit (which asks dellwp through ssh, so this is the end-to-end proof) and the
first job's page when there is one; logging out ends the session. It prints one line per check ("ok: ..." or "FAIL: ...") and never a secret, a cookie or a page's content; the exit status is the number of failures."""
import argparse
import http.client
import re
import socket
import ssl
import sys
import time

FAIL = 0


def ok(text):
    print("ok: %s" % text, flush=True)


def bad(text):
    global FAIL
    FAIL += 1
    print("FAIL: %s" % text, flush=True)


def check(cond, what, detail=""):
    (ok if cond else bad)(what if cond else "%s%s" % (what, (" (%s)" % detail) if detail else ""))
    return bool(cond)


class Session:
    def __init__(self, host, ip, port, cacert):
        self.host, self.ip, self.port = host, ip, port
        self.context = ssl.create_default_context(cafile=cacert) if cacert else ssl.create_default_context()
        self.cookie = None

    def request(self, method, path, form=None, headers=None):
        # connect to the address but verify the certificate against the app's NAME (what a browser does after DNS): the socket is wrapped by hand, then handed to http.client
        raw = socket.create_connection((self.ip, self.port), timeout=30)
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=30, context=self.context)
        conn.sock = self.context.wrap_socket(raw, server_hostname=self.host)
        try:
            hdrs = {"Host": self.host, "Connection": "close"}
            if self.cookie:
                hdrs["Cookie"] = self.cookie
            body = None
            if form is not None:
                from urllib.parse import urlencode
                body = urlencode(form).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            hdrs.update(headers or {})
            conn.request(method, path, body=body, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
            for k, v in r.getheaders():
                if k.lower() == "set-cookie" and v.startswith("panel_session="):
                    self.cookie = v.split(";", 1)[0]
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data.decode("utf-8", "replace")
        finally:
            conn.close()

    def protocol(self):
        raw = socket.create_connection((self.ip, self.port), timeout=30)
        with self.context.wrap_socket(raw, server_hostname=self.host) as s:
            return s.version(), s.getpeercert()


def csrf_of(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def tls_checks(s):
    try:
        version, cert = s.protocol()
    except (ssl.SSLError, OSError) as e:
        bad("the TLS handshake for %s at %s:%s failed: %s" % (s.host, s.ip, s.port, getattr(e, "reason", None) or e.__class__.__name__))
        return False
    ok("TLS handshake for %s with the certificate verified (%s)" % (s.host, version))
    check(version in ("TLSv1.2", "TLSv1.3"), "the protocol is TLS 1.2 or newer", version)
    days = (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400
    check(days >= 14, "the certificate has %d days left (14 or more)" % days, "renew it: the HP's hook should have pushed one")
    return True


def other_name_refused(s):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection((s.ip, s.port), timeout=15)
        with ctx.wrap_socket(raw, server_hostname="not-the-panel.example.invalid"):
            bad("a handshake for another name was accepted: the default server should refuse it (ssl_reject_handshake)")
    except ssl.SSLError as e:
        ok("a handshake for another name is refused (%s)" % (getattr(e, "reason", None) or "ssl error"))
    except OSError as e:
        ok("a handshake for another name is refused (%s)" % e.__class__.__name__)


def page_checks(s):
    st, h, body = s.request("GET", "/login")
    if not check(st == 200, "GET /login answers 200", st):
        return
    csp = h.get("content-security-policy", "")
    check("default-src 'none'" in csp and "frame-ancestors 'none'" in csp, "the content security policy denies everything but the app's own style", csp[:60])
    check(h.get("x-frame-options") == "DENY" and h.get("x-content-type-options") == "nosniff", "no framing, no sniffing")
    check(h.get("cache-control") == "no-store", "the page is not cached")
    check(h.get("referrer-policy") == "no-referrer", "no referrer is sent")
    check("max-age=" in h.get("strict-transport-security", ""), "Strict-Transport-Security is set (by nginx)")
    check(not re.search(r"nginx/\d|gunicorn", h.get("server", "")), "the server banner names no version", h.get("server", ""))
    check("<script" not in body.lower(), "the login page has no script")
    st, h, _ = s.request("GET", "/sites")
    check(st == 302 and h.get("location", "").endswith("/login"), "/sites without a login is sent to /login", "%s %s" % (st, h.get("location")))
    st, h, _ = s.request("GET", "/static/panel.css")
    check(st == 200 and "css" in h.get("content-type", ""), "the stylesheet is served", st)
    s.cookie = None
    st, _, _ = s.request("POST", "/login", form={"password": "x", "code": "000000"})
    check(st == 400, "a form posted without its token is refused", st)
    s.cookie = None


def login_checks(s, password, code):
    s.cookie = None
    st, _, html = s.request("GET", "/login")
    token = csrf_of(html)
    check(bool(token), "the login form carries a token")
    st, _, wrong = s.request("POST", "/login", form={"password": password + "x", "code": "000000", "csrf": token})
    check(st == 401 and "not accepted" in wrong, "a wrong login is refused with the one plain message", st)
    st, h, _ = s.request("POST", "/login", form={"password": password, "code": code, "csrf": csrf_of(wrong) or token})
    if not check(st == 303 and h.get("location", "").endswith("/sites"), "the right password and code log in", "status %s" % st):
        if st == 429:
            print("   the login is locked for a while after too many wrong tries; wait and run again", flush=True)
        if st == 401:
            print("   password or code not accepted (is the code fresh? each code works once)", flush=True)
        return
    st, _, html = s.request("GET", "/sites")
    check(st == 200 and "<h1>Sites</h1>" in html, "Sites opens and lists what dellwp reports", st)
    rows = len(re.findall(r'<a href="/sites/[^"]+">', html))
    print("   (%d site(s) listed)" % rows, flush=True)
    if rows:
        name = re.search(r'<a href="/sites/([^"]+)">', html).group(1)
        st, _, page = s.request("GET", "/sites/" + name)
        check(st == 200 and "registry record" in page.lower(), "a site's page opens", st)
    st, _, html = s.request("GET", "/jobs")
    check(st == 200 and "<h1>Jobs</h1>" in html, "Jobs opens", st)
    m = re.search(r'<a href="(/jobs/[^"]+)">', html)
    if m:
        st, _, page = s.request("GET", m.group(1))
        check(st == 200 and 'class="log"' in page, "a job's page opens with its log", st)
    st, _, html = s.request("GET", "/audit")
    check(st == 200 and "<h1>Audit</h1>" in html, "Audit opens", st)
    token = csrf_of(html)
    st, _, _ = s.request("POST", "/logout", form={"csrf": "wrong"})
    check(st == 400, "logging out needs the token", st)
    st, _, _ = s.request("POST", "/logout", form={"csrf": token})
    check(st == 303, "logging out works", st)
    st, h, _ = s.request("GET", "/sites")
    check(st == 302 and h.get("location", "").endswith("/login"), "after logging out /sites is sent to /login", st)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--ip", required=True)
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--cacert")
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--login-only", action="store_true")
    a = ap.parse_args(argv)
    s = Session(a.host, a.ip, a.port, a.cacert)
    if tls_checks(s):
        if not a.login_only:
            other_name_refused(s)
            page_checks(s)
        if a.login or a.login_only:
            password = sys.stdin.readline().rstrip("\n")
            code = sys.stdin.readline().strip()
            login_checks(s, password, code)
    return min(FAIL, 100)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
