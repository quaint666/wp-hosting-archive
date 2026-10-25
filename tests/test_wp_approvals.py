#!/usr/bin/env python3
"""Offline tests of approvals by phone: scripts/wp_approvals.py (signed one-time tokens and the ntfy message) and the glue in scripts/wp_site_import.py (notify_report, `resolve`).

The signature, the expiry, the payload shape, every way a token can be wrong, and the HTTP post (against a real server on 127.0.0.1) are tested directly. The import runs on the same stand-in host as
tests/test_wp_import.py; ntfy is a stand-in that records what it was sent. They prove that a token can approve only a clean import and deny any, that it works once, only for the import and report it was
issued for, that nothing a phone can send overrides a check, and that a notification that cannot be sent never fails the import. They do not prove that ntfy accepts this JSON, that its action buttons post
what is configured, or that the phone app shows them: the rehearsal on the real ntfy does. Run: python3 -I tests/test_wp_approvals.py"""
import base64
import hashlib
import http.server
import json
import os
import sys
import threading
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import wp_approvals as ap  # noqa: E402
import wp_site_import as imp  # noqa: E402
from test_wp_import import ANSWERS, META, Base as ImportBase, files  # noqa: E402
from test_wp_wpress import get_from, meta, plugin_php  # noqa: E402

KEY = b"k" * 40
SHA = "a" * 64


class Tokens(unittest.TestCase):
    def test_a_token_round_trips(self):
        t, n = ap.mint(KEY, "wptest", "approve", SHA, 3600, now=1000)
        d = ap.verify(KEY, t, now=1000)
        self.assertEqual(d, {"s": "wptest", "a": "approve", "n": n, "e": 4600, "r": SHA})
        self.assertTrue(ap.TOKEN_RE.fullmatch(t))
        self.assertLess(len(t), 400)

    def test_two_tokens_differ_and_the_nonce_is_random(self):
        a, na = ap.mint(KEY, "wptest", "approve", SHA, 60, now=1)
        b, nb = ap.mint(KEY, "wptest", "approve", SHA, 60, now=1)
        self.assertNotEqual((a, na), (b, nb))
        self.assertRegex(na, r"^[0-9a-f]{32}$")

    def test_every_part_is_protected_by_the_signature(self):
        t, _ = ap.mint(KEY, "wptest", "approve", SHA, 3600, now=1000)
        head, payload, mac = t.split(".")
        flipped = lambda s, i: s[:i] + ("A" if s[i] != "A" else "B") + s[i + 1:]
        for i in range(0, len(payload), 7):
            with self.assertRaises(ap.ApprovalError) as cm:
                ap.verify(KEY, "%s.%s.%s" % (head, flipped(payload, i), mac), now=1000)
            self.assertEqual(str(cm.exception), "the signature does not match")
        other = "0" if mac[0] != "0" else "1"
        with self.assertRaises(ap.ApprovalError):
            ap.verify(KEY, "%s.%s.%s" % (head, payload, other + mac[1:]), now=1000)
        with self.assertRaises(ap.ApprovalError):
            ap.verify(b"j" * 40, t, now=1000)

    def test_the_signature_is_checked_before_the_payload_is_parsed(self):
        t = "v1.%s.%s" % ("!" * 0 + "A" * 60, "0" * 64)
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.verify(KEY, t, now=1)
        self.assertEqual(str(cm.exception), "the signature does not match")

    def test_expiry_is_exact(self):
        t, _ = ap.mint(KEY, "wptest", "deny", SHA, 100, now=1000)
        self.assertEqual(ap.verify(KEY, t, now=1100)["a"], "deny")
        with self.assertRaises(ap.ApprovalError) as cm:
            ap.verify(KEY, t, now=1101)
        self.assertEqual(str(cm.exception), "the token has expired")

    def test_things_that_are_not_tokens(self):
        for bad in ("", None, 5, b"v1.a.b", "v1.a.b", "x", "v1." + "A" * 50 + "." + "0" * 63, "v1." + "A" * 50 + "." + "0" * 65, "v1." + "A" * 50 + "." + "G" * 64,
                    "v1." + "A" * 50 + "." + "0" * 64 + "\n", "\nv1." + "A" * 50 + "." + "0" * 64, "v1." + "A" * 800 + "." + "0" * 64, "v2." + "A" * 50 + "." + "0" * 64,
                    "v1." + "A" * 50 + "." + "A" * 64):
            with self.assertRaises(ap.ApprovalError) as cm:
                ap.verify(KEY, bad, now=1)
            self.assertEqual(str(cm.exception), "not a token", repr(bad))

    def test_a_validly_signed_payload_of_the_wrong_shape_is_refused(self):
        good = {"s": "wptest", "a": "approve", "n": "b" * 32, "e": 9999999999, "r": SHA}
        cases = [dict(good, a="reset"), dict(good, a="APPROVE"), dict(good, s="../x"), dict(good, s="WPTEST"), dict(good, s="wptest\n"), dict(good, n="b" * 31), dict(good, n="B" * 32),
                 dict(good, e=True), dict(good, e="9999999999"), dict(good, e=1.5), dict(good, r="a" * 63), dict(good, x=1), {k: v for k, v in good.items() if k != "n"}, [1, 2], "text", 5, None]
        for payload in cases:
            text = ap.b64(json.dumps(payload).encode())
            token = "v1.%s.%s" % (text, ap.sign(KEY, text))
            if not ap.TOKEN_RE.fullmatch(token):
                continue
            with self.assertRaises(ap.ApprovalError) as cm:
                ap.verify(KEY, token, now=1)
            self.assertIn(str(cm.exception), ("the payload has the wrong shape", "the payload is not readable"), payload)
        text = ap.b64(json.dumps(good).encode())
        self.assertEqual(ap.verify(KEY, "v1.%s.%s" % (text, ap.sign(KEY, text)), now=1)["s"], "wptest")

    def test_a_signed_payload_that_is_not_json_is_refused(self):
        for raw in (b"\xff\xfe not json", b"{", b"\x00" * 40):
            text = ap.b64(raw).ljust(40, "A")
            token = "v1.%s.%s" % (text, ap.sign(KEY, text))
            with self.assertRaises(ap.ApprovalError):
                ap.verify(KEY, token, now=1)

    def test_mint_refuses_what_it_should_never_sign(self):
        for args in (("../x", "approve", SHA), ("wptest", "reset", SHA), ("wptest", "approve", "short"), ("WPTEST", "approve", SHA), ("wptest\n", "approve", SHA), ("wptest", "approve", SHA.upper())):
            with self.assertRaises(ap.ApprovalError):
                ap.mint(KEY, *args, 60)


class Files(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.TemporaryDirectory()
        self.t = Path(self.d.name)

    def tearDown(self):
        self.d.cleanup()

    def test_the_key(self):
        p = self.t / "key"
        p.write_text("x" * 64 + "\n")
        self.assertEqual(ap.read_key(p), b"x" * 64)
        p.write_text("short\n")
        with self.assertRaises(ap.ApprovalError):
            ap.read_key(p)
        with self.assertRaises(ap.ApprovalError):
            ap.read_key(self.t / "missing")

    def test_the_conf(self):
        p = self.t / "conf"
        p.write_text('NTFY_BASE="http://10.0.0.2:8089"\nNTFY_FALLBACK_URL="https://ntfy-acer.example.org"\nNTFY_FALLBACK_TOPIC="acer-alerts"\n')
        self.assertEqual(ap.read_conf(p), ("https://ntfy-acer.example.org", "acer-alerts"))
        for bad in ('NTFY_FALLBACK_URL="http://x.org"\nNTFY_FALLBACK_TOPIC="t"\n', 'NTFY_FALLBACK_URL="https://x.org/path"\nNTFY_FALLBACK_TOPIC="t"\n', 'NTFY_FALLBACK_URL="https://x.org"\n',
                    'NTFY_FALLBACK_URL="https://x.org"\nNTFY_FALLBACK_TOPIC="a b"\n', "", 'NTFY_FALLBACK_URL="https://x.org evil"\nNTFY_FALLBACK_TOPIC="t"\n'):
            p.write_text(bad)
            with self.assertRaises(ap.ApprovalError, msg=bad):
                ap.read_conf(p)
        with self.assertRaises(ap.ApprovalError):
            ap.read_conf(self.t / "missing")

    def test_the_credentials(self):
        n = self.t / "netrc"
        n.write_text("machine ntfy-acer.example.org login hosts password s3cret\n")
        self.assertEqual(ap.sender_auth(n, "https://ntfy-acer.example.org"), "Basic " + base64.b64encode(b"hosts:s3cret").decode())
        for bad_url in ("https://other.example.org",):
            with self.assertRaises(ap.ApprovalError):
                ap.sender_auth(n, bad_url)
        n.write_text("garbage garbage")
        with self.assertRaises(ap.ApprovalError):
            ap.sender_auth(n, "https://ntfy-acer.example.org")
        a = self.t / "approver"
        a.write_text("approver:pa:ss\n")
        self.assertEqual(ap.approver_auth(a), "Basic " + base64.b64encode(b"approver:pa:ss").decode())
        for bad in ("", "nocolon", ":nologin", "nopass:"):
            a.write_text(bad)
            with self.assertRaises(ap.ApprovalError, msg=bad):
                ap.approver_auth(a)
        with self.assertRaises(ap.ApprovalError):
            ap.approver_auth(self.t / "missing")


class Message(unittest.TestCase):
    def test_the_json_ntfy_gets(self):
        m = ap.build_message("https://n.example.org", "alerts", "wp-approvals", "Basic QQ==", "wptest", "body text", {"approve": "TOKEN-A", "deny": "TOKEN-D"})
        self.assertEqual((m["topic"], m["title"], m["message"]), ("alerts", "Import ready: wptest", "body text"))
        self.assertEqual([a["label"] for a in m["actions"]], ["Approve", "Deny"])
        for a, tok in zip(m["actions"], ("TOKEN-A", "TOKEN-D")):
            self.assertEqual((a["action"], a["method"], a["url"], a["body"], a["clear"]), ("http", "POST", "https://n.example.org/wp-approvals", tok, True))
            self.assertEqual(a["headers"], {"Authorization": "Basic QQ=="})

    def test_only_the_tokens_given_become_buttons_and_a_long_body_is_cut(self):
        m = ap.build_message("https://n.example.org", "t", "r", "Basic QQ==", "s", "x" * 5000, {"deny": "D"})
        self.assertEqual([a["label"] for a in m["actions"]], ["Deny"])
        self.assertEqual(len(m["message"]), ap.BODY_MAX)


class Wire(unittest.TestCase):
    """post_json against a real HTTP server on loopback."""
    def serve(self, status):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)          # cleanups run last-in first-out: shutdown, then close
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1], seen

    def test_it_posts_json_with_the_credentials(self):
        url, seen = self.serve(200)
        self.assertEqual(ap.post_json(url, "Basic abc", {"topic": "t", "message": "m"}), 200)
        path, headers, body = seen[0]
        self.assertEqual((path, headers["Authorization"], headers["Content-Type"]), ("/", "Basic abc", "application/json"))
        self.assertEqual(json.loads(body), {"topic": "t", "message": "m"})

    def test_the_error_response_is_closed(self):
        closed = []

        class Err(urllib.error.HTTPError):
            def close(self):
                closed.append(True)
        real = ap.urllib.request.urlopen
        ap.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(Err("https://x", 500, "x", {}, None))
        try:
            with self.assertRaises(ap.ApprovalError):
                ap.post_json("https://x", "Basic x", {})
        finally:
            ap.urllib.request.urlopen = real
        self.assertEqual(closed, [True])

    def test_a_refusal_and_an_unreachable_server_are_errors_not_crashes(self):
        for status in (401, 403, 500):
            url, _ = self.serve(status)
            with self.assertRaises(ap.ApprovalError) as cm:
                ap.post_json(url, "Basic x", {})
            self.assertIn(str(status), str(cm.exception))
        with self.assertRaises(ap.ApprovalError):
            ap.post_json("http://127.0.0.1:1", "Basic x", {}, timeout=2)


class Phone(ImportBase):
    """The import on the stand-in host, with approvals configured and ntfy as a recorder."""
    def setUp(self):
        super().setUp()
        d = self.tmp / "etc"
        d.mkdir()
        (d / "key").write_text("s" * 64 + "\n")
        (d / "approver").write_text("approver:APPROVERPW\n")
        (d / "conf").write_text('NTFY_FALLBACK_URL="https://ntfy.example.org"\nNTFY_FALLBACK_TOPIC="alerts"\n')
        (d / "netrc").write_text("machine ntfy.example.org login hosts password SENDERPW\n")
        self.key = b"s" * 64
        self.cfg["approvals"] = {"key_file": str(d / "key"), "approver_file": str(d / "approver"), "ntfy_conf": str(d / "conf"), "netrc": str(d / "netrc"), "reply_topic": "wp-approvals", "ttl_s": 3600}
        self.sent, self.now, self.post_error = [], 1_000_000.0, None

    def post(self, url, auth, payload):
        if self.post_error:
            raise self.post_error
        self.sent.append((url, auth, payload))
        return 200

    def run_main(self, argv, **kw):
        h = self.host
        self.out.clear()
        code = imp.main(self.cfg, argv, get=kw.pop("get", get_from(ANSWERS)), run=h.run, ask=kw.pop("ask", lambda p: ""), out=self.out.append, popen=h.popen, chown=h.chown, which=h.which,
                        stdin=kw.pop("stdin", None), clock=lambda: self.now, post=self.post)
        return code, "\n".join(self.out)

    def tokens(self):
        return {a["label"].lower(): a["body"] for a in self.sent[-1][2]["actions"]}

    def record(self):
        return json.loads((self.intake / "work-wptest/PLAN.json").read_text())


class Notifying(Phone):
    def test_a_clean_import_sends_approve_and_deny(self):
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 0, text)
        self.assertIn("phone notification sent (Approve and Deny)", text)
        url, auth, payload = self.sent[0]
        self.assertEqual((url, payload["topic"]), ("https://ntfy.example.org", "alerts"))
        self.assertEqual(auth, ap.basic("hosts", "SENDERPW"))
        self.assertEqual([a["label"] for a in payload["actions"]], ["Approve", "Deny"])
        self.assertTrue(all(a["headers"] == {"Authorization": ap.basic("approver", "APPROVERPW")} and a["url"] == "https://ntfy.example.org/wp-approvals" for a in payload["actions"]))
        sha = hashlib.sha256((self.intake / "work-wptest/REPORT.txt").read_bytes()).hexdigest()
        for action, token in self.tokens().items():
            d = ap.verify(self.key, token, now=self.now)
            self.assertEqual((d["s"], d["a"], d["r"], d["e"]), ("wptest", action, sha, int(self.now) + 3600))
            self.assertEqual(self.record()["tokens"][d["n"]], action)
        self.assertEqual(len(self.record()["tokens"]), 2)

    def test_the_summary_says_what_was_held_back_and_that_it_is_clean(self):
        self.archive()
        self.run_main(["wptest", "friend.wpress"])
        body = self.sent[0][2]["message"]
        for needle in ("wptest:", "failed.", "Held back for you:", "plugin notorg", "mu-plugins", "ClamAV: NOT RUN", "Vulnerabilities: NOT RUN", "Clean. Approve copies the held-back code in"):
            self.assertIn(needle, body)

    def test_each_reason_to_distrust_an_import_removes_the_approve_button(self):
        def failed_step(host):
            host.fail["cli:core update-db"] = (1, "Error: no")

        def core_checksums(host):
            host.fail["cli:core verify-checksums"] = (1, "Error: File should not exist")

        def plugin_checksums(host):
            host.fail["cli:plugin verify-checksums"] = (1, "Error: checksum mismatch")

        def clam_found(host):
            host.fail["clam"] = (1, "/x/evil.php: Php.Trojan FOUND")

        def clam_broken(host):
            host.fail["clam"] = (2, "")

        def vuln_found(host):
            host.fail["vuln"] = (1, "plugin elementor 4.2.3: Elementor <= 4.2.4 - Stored XSS (CVE-2026-1111, fixed in 4.2.5)")

        def vuln_broken(host):
            host.fail["vuln"] = (2, "the feed answered 429")
        for name, arrange in (("failed step", failed_step), ("core checksums", core_checksums), ("plugin checksums", plugin_checksums), ("clam find", clam_found), ("clam broken", clam_broken),
                              ("vulnerability found", vuln_found), ("vulnerability check broken", vuln_broken)):
            self.setUp()
            arrange(self.host)
            self.archive()
            code, text = self.run_main(["wptest", "friend.wpress"])
            self.assertIn("Deny only", text, name)
            self.assertEqual([a["label"] for a in self.sent[0][2]["actions"]], ["Deny"], name)
            self.assertIn("NOT CLEAN", self.sent[0][2]["message"], name)
            self.assertIn("wp_sites.py approve wptest", self.sent[0][2]["message"], name)
            self.tearDown()

    def test_suspicious_code_in_the_held_back_files_removes_the_approve_button(self):
        fl = [f for f in files() if f[1] != "m.php"] + [("mu-plugins", "m.php", b"<?php eval($_POST['x']);")]
        self.archive(files=fl)
        self.run_main(["wptest", "friend.wpress"])
        self.assertEqual([a["label"] for a in self.sent[0][2]["actions"]], ["Deny"])
        self.assertIn("1 held-back file(s) match suspicious patterns", self.sent[0][2]["message"])

    def test_the_rule_for_clean_read_from_a_record(self):
        ok = {"steps": [{"name": "a", "ok": True}], "scan": {"core": [True, ""], "plugins": [True, ""], "clam": "NOT RUN: x", "vuln": "NOT RUN: no key", "held_total": 0, "uploads_hits_total": 0}}
        self.assertEqual(imp.unclean_reasons(ok), [])
        self.assertEqual(imp.unclean_reasons(dict(ok, scan=dict(ok["scan"], core=(True, "")))), [], "a tuple, as it is in memory before the record is saved")
        self.assertEqual(imp.unclean_reasons(dict(ok, scan=dict(ok["scan"], clam="clean"))), [])
        self.assertEqual(imp.unclean_reasons(dict(ok, scan=dict(ok["scan"], vuln="none known"))), [])
        bad = {"failed step": dict(ok, steps=[{"name": "a", "ok": False}]), "no scan": {k: v for k, v in ok.items() if k != "scan"}, "scan not a dict": dict(ok, scan="x"),
               "core": dict(ok, scan=dict(ok["scan"], core=[False, ""])), "core missing": dict(ok, scan={k: v for k, v in ok["scan"].items() if k != "core"}),
               "core empty": dict(ok, scan=dict(ok["scan"], core=[])), "core truthy but not True": dict(ok, scan=dict(ok["scan"], core=["yes", ""])),
               "plugins": dict(ok, scan=dict(ok["scan"], plugins=[False, ""])), "clam found": dict(ok, scan=dict(ok["scan"], clam="FOUND: x")),
               "clam failed": dict(ok, scan=dict(ok["scan"], clam="FAILED to run")), "vuln found": dict(ok, scan=dict(ok["scan"], vuln="FOUND: plugin x 1.0: XSS")),
               "vuln failed": dict(ok, scan=dict(ok["scan"], vuln="FAILED to run (exit 2): 429")), "vuln missing": dict(ok, scan={k: v for k, v in ok["scan"].items() if k != "vuln"}), "clam missing": dict(ok, scan={k: v for k, v in ok["scan"].items() if k != "clam"}),
               "held hits": dict(ok, scan=dict(ok["scan"], held_total=1)), "held missing": dict(ok, scan={k: v for k, v in ok["scan"].items() if k != "held_total"}),
               "uploads hits": dict(ok, scan=dict(ok["scan"], uploads_hits_total=2)), "uploads missing": dict(ok, scan={k: v for k, v in ok["scan"].items() if k != "uploads_hits_total"})}
        for name, record in bad.items():
            self.assertTrue(imp.unclean_reasons(record), name)

    def test_without_the_files_the_import_goes_on_and_says_phone_approval_is_off(self):
        for victim, needle in (("key", "signing key"), ("approver", "button credentials"), ("conf", "conf cannot be read"), ("netrc", "sender's credentials")):
            self.setUp()
            os.unlink(self.tmp / "etc" / victim)
            self.archive()
            code, text = self.run_main(["wptest", "friend.wpress"])
            self.assertEqual(code, 0, victim)
            self.assertIn("phone approval is off or failed", text, victim)
            self.assertIn(needle, text, victim)
            self.assertEqual(self.sent, [], victim)
            self.assertNotIn("tokens", self.record(), victim)
            self.tearDown()

    def test_not_configured_at_all(self):
        del self.cfg["approvals"]
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 0)
        self.assertIn("phone approval is not configured", text)

    def test_a_notification_that_cannot_be_sent_never_fails_the_import(self):
        for exc in (ap.ApprovalError("ntfy answered 403"), RuntimeError("boom"), OSError("down")):
            self.setUp()
            self.post_error = exc
            self.archive()
            code, text = self.run_main(["wptest", "friend.wpress"])
            self.assertEqual(code, 0, repr(exc))
            self.assertRegex(text, r"phone (approval is off or failed|notification failed)")
            self.tearDown()

    def test_no_secret_reaches_the_terminal_the_report_or_the_record(self):
        self.archive()
        code, text = self.run_main(["wptest", "friend.wpress"])
        blobs = [text, (self.intake / "work-wptest/REPORT.txt").read_text(), (self.intake / "work-wptest/PLAN.json").read_text(), (self.intake / "ledger.log").read_text()]
        for secret in ("APPROVERPW", "SENDERPW", "s" * 64, ap.basic("approver", "APPROVERPW")):
            for b in blobs:
                self.assertNotIn(secret, b)
        self.assertNotIn("v1.", blobs[1] + blobs[2])


class Resolving(Phone):
    def reported(self, **kw):
        self.archive(**kw)
        code, text = self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(code, 0, text)
        self.host.calls.clear()

    def resolve(self, token):
        return self.run_main(["resolve", token])

    def test_approve_copies_the_held_code_in_and_consumes_everything(self):
        self.reported()
        t = self.tokens()["approve"]
        code, text = self.resolve(t)
        self.assertEqual(code, 0, text)
        self.assertTrue((self.html / "wp-content/plugins/notorg/notorg.php").exists())
        self.assertTrue((self.html / "wp-content/mu-plugins/m.php").exists())
        self.assertFalse((self.intake / "work-wptest").exists())
        self.assertFalse((self.intake / "friend.wpress").exists())
        events = [l.split("\t")[2] for l in (self.intake / "ledger.log").read_text().splitlines()]
        self.assertEqual(events[-2:], ["token-approve", "approved"])
        self.assertEqual(self.cfg["sites"]["wptest"]["state"], "hold")

    def test_deny_undoes_the_import_without_a_typed_name_and_keeps_the_file(self):
        self.reported()
        code, text = self.resolve(self.tokens()["deny"])
        self.assertEqual(code, 0, text)
        self.assertFalse((self.intake / "work-wptest").exists())
        self.assertTrue((self.intake / "friend.wpress").exists())
        self.assertIn("`wp_options`", self.host.dropped)
        self.assertFalse((self.html / "wp-content/plugins/good").exists())
        self.assertEqual([l.split("\t")[2] for l in (self.intake / "ledger.log").read_text().splitlines()][-2:], ["token-deny", "reset"])

    def test_a_token_works_once(self):
        self.reported()
        t = self.tokens()["approve"]
        self.assertEqual(self.resolve(t)[0], 0)
        code, text = self.resolve(t)
        self.assertEqual(code, 2)
        self.assertIn("no import of wptest is waiting", text)

    def test_using_one_token_cancels_the_other(self):
        self.reported()
        toks = self.tokens()
        self.assertEqual(self.resolve(toks["deny"])[0], 0)
        self.assertEqual(self.resolve(toks["approve"])[0], 2)
        self.reported()
        toks2 = self.tokens()
        self.assertEqual(self.resolve(toks2["approve"])[0], 0)
        self.assertEqual(self.resolve(toks2["deny"])[0], 2)

    def test_a_token_of_an_earlier_import_of_the_same_file_is_refused(self):
        self.reported()
        old = self.tokens()["approve"]
        self.run_main(["reset", "wptest"], ask=lambda p: "wptest\n")
        self.host.sql = b""
        self.reported()
        code, text = self.resolve(old)
        self.assertEqual(code, 2)
        self.assertIn("not issued for the import now waiting", text)
        self.assertTrue((self.intake / "work-wptest").exists(), "nothing was done")

    def test_a_token_for_a_changed_report_is_refused(self):
        self.reported()
        t = self.tokens()["approve"]
        r = self.intake / "work-wptest/REPORT.txt"
        r.write_text(r.read_text() + "\\nedited\\n")
        code, text = self.resolve(t)
        self.assertEqual(code, 2)
        self.assertIn("not the one the token was issued for", text)
        self.assertTrue((self.intake / "work-wptest").exists())

    def test_an_expired_token_does_nothing(self):
        self.reported()
        t = self.tokens()["approve"]
        self.now += 3601
        code, text = self.resolve(t)
        self.assertEqual(code, 2)
        self.assertIn("expired", text)
        self.assertTrue((self.intake / "work-wptest").exists())
        self.assertEqual(len(self.record()["tokens"]), 2, "a refused token is not consumed")

    def test_a_forged_or_foreign_token_does_nothing(self):
        self.reported()
        t = self.tokens()["approve"]
        head, payload, mac = t.split(".")
        for bad in ("%s.%s.%s" % (head, payload, "0" * 64), t[:-1] + ("0" if t[-1] != "0" else "1"), ap.mint(b"x" * 64, "wptest", "approve", SHA, 60, now=self.now)[0], "garbage", "v1.a.b"):
            code, text = self.resolve(bad)
            self.assertEqual(code, 2, bad)
            self.assertIn("token refused", text)
        self.assertTrue((self.intake / "work-wptest").exists())
        self.assertEqual(len(self.record()["tokens"]), 2)

    def test_a_flag_that_belongs_to_another_command_is_a_usage_error_not_ignored(self):
        self.reported()
        for argv in (["approve", "wptest", "--plan"], ["reset", "wptest", "--plan"], ["report", "wptest", "--plan"], ["reset", "wptest", "--reject", "x"], ["reset", "wptest", "--accept-failures"],
                     ["report", "wptest", "--reject", "x"], ["wptest", "friend.wpress", "--reject", "x"], ["wptest", "friend.wpress", "--accept-failures"]):
            code, text = self.run_main(argv, ask=lambda p: "wptest\n")
            self.assertEqual(code, 2, argv)
            self.assertIn("usage", text, argv)
        self.assertTrue((self.intake / "work-wptest").exists())
        self.assertEqual(self.host.dropped, "")

    def test_a_token_with_a_newline_or_extra_arguments(self):
        self.reported()
        t = self.tokens()["approve"]
        self.assertEqual(self.resolve(t + "\n")[0], 2)
        self.assertEqual(self.run_main(["resolve"])[0], 2)
        self.assertEqual(self.run_main(["resolve", t, "--accept-failures"])[0], 2)
        self.assertTrue((self.intake / "work-wptest").exists())

    def test_nothing_waiting_or_the_wrong_stage_or_a_site_that_is_not_on_hold(self):
        self.reported()
        t = self.tokens()["approve"]
        deny = self.tokens()["deny"]
        rec = self.record()
        rec["stage"] = "unpacked"
        (self.intake / "work-wptest/PLAN.json").write_text(json.dumps(rec))
        for token in (t, deny):
            code, text = self.resolve(token)
            self.assertEqual(code, 2)
            self.assertIn("token refused: the import of wptest is at stage 'unpacked'", text)
        self.assertTrue((self.intake / "work-wptest").exists(), "a deny must not reset an import that has not finished")
        self.assertEqual(self.host.dropped, "")
        rec["stage"] = "reported"
        (self.intake / "work-wptest/PLAN.json").write_text(json.dumps(rec))
        self.cfg["sites"]["wptest"]["state"] = "live"
        self.assertIn("on hold", self.resolve(t)[1])
        self.cfg["sites"]["wptest"]["state"] = "hold"
        self.assertEqual(self.resolve(t)[0], 0)

    def test_an_approve_token_cannot_approve_an_unclean_import_even_if_one_was_signed(self):
        self.host.fail["cli:core update-db"] = (1, "Error: no")
        self.archive()
        self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(list(self.tokens()), ["deny"], "no approve token is issued")
        nonce = "c" * 32
        forged, _ = ap.mint(self.key, "wptest", "approve", hashlib.sha256((self.intake / "work-wptest/REPORT.txt").read_bytes()).hexdigest(), 3600, now=self.now, nonce=nonce)
        rec = self.record()
        rec["tokens"][nonce] = "approve"
        (self.intake / "work-wptest/PLAN.json").write_text(json.dumps(rec))
        code, text = self.resolve(forged)
        self.assertEqual(code, 2)
        self.assertIn("not approved: 1 step(s) failed", text)
        self.assertTrue((self.intake / "work-wptest").exists())
        self.assertFalse((self.html / "wp-content/plugins/notorg").exists())
        self.assertEqual(self.record()["tokens"], {}, "it was consumed")

    def test_deny_works_on_an_unclean_import(self):
        self.host.fail["cli:core update-db"] = (1, "Error: no")
        self.archive()
        self.run_main(["wptest", "friend.wpress"])
        self.assertEqual(self.resolve(self.tokens()["deny"])[0], 0)
        self.assertFalse((self.intake / "work-wptest").exists())

    def test_the_action_in_the_token_must_match_what_was_issued_under_that_nonce(self):
        self.reported()
        toks = self.tokens()
        d = ap.verify(self.key, toks["deny"], now=self.now)
        swapped, _ = ap.mint(self.key, "wptest", "approve", d["r"], 3600, now=self.now, nonce=d["n"])
        code, text = self.resolve(swapped)
        self.assertEqual(code, 2)
        self.assertIn("not issued for the import now waiting", text)
        self.assertTrue((self.intake / "work-wptest").exists())

    def test_a_damaged_record_cannot_be_used_to_get_past_the_checks(self):
        self.reported()
        t = self.tokens()["approve"]
        rec = self.record()
        for bad in (None, [], "x", 5, {"d" * 32: "approve"}):
            rec["tokens"] = bad
            (self.intake / "work-wptest/PLAN.json").write_text(json.dumps(rec))
            self.assertEqual(self.resolve(t)[0], 2, repr(bad))
        (self.intake / "work-wptest/PLAN.json").write_text("{not json")
        self.assertEqual(self.resolve(t)[0], 2)

    def test_not_configured(self):
        self.reported()
        t = self.tokens()["approve"]
        del self.cfg["approvals"]
        self.assertIn("not configured", self.resolve(t)[1])
        self.cfg["approvals"] = {"key_file": str(self.tmp / "nokey"), "approver_file": "x", "ntfy_conf": "x", "netrc": "x", "reply_topic": "r", "ttl_s": 1}
        self.assertIn("token refused", self.resolve(t)[1])

    def test_nothing_a_phone_sends_can_pass_an_override(self):
        # the only way in is resolve <token>: it takes no flag, and approve through it is run with no rejects and no accept-failures
        self.reported()
        seen = {}
        real = imp.run_approve

        def spy(cfg, site, reject, accept, tools, out):
            seen.update(reject=reject, accept=accept)
            return real(cfg, site, reject, accept, tools, out)
        imp.run_approve = spy
        try:
            self.resolve(self.tokens()["approve"])
        finally:
            imp.run_approve = real
        self.assertEqual(seen, {"reject": [], "accept": False})

    def test_going_live_is_not_reachable_from_a_token(self):
        self.reported()
        self.resolve(self.tokens()["approve"])
        self.assertFalse([c for c in self.host.calls if "live" in c])
        self.assertEqual(self.cfg["sites"]["wptest"]["state"], "hold")


class FakeStream:
    """What urlopen returns: a context manager that yields ndjson lines."""
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.lines)


def event(**kw):
    d = {"id": "id%d" % event.n, "event": "message", "topic": "wp-approvals"}
    event.n += 1
    d.update(kw)
    return json.dumps(d).encode() + b"\n"


event.n = 0
TOK = "v1." + "A" * 50 + "." + "0" * 64


class Listening(unittest.TestCase):
    def setUp(self):
        import tempfile
        import wp_approval_listener as lis
        self.lis = lis
        self.d = tempfile.TemporaryDirectory()
        self.t = Path(self.d.name)
        (self.t / "conf").write_text('NTFY_FALLBACK_URL="https://ntfy.example.org"\nNTFY_FALLBACK_TOPIC="alerts"\n')
        (self.t / "listener").write_text("wpvm:LISTENERPW\n")
        self.cfg = {"ntfy_conf": str(self.t / "conf"), "listener_file": str(self.t / "listener"), "reply_topic": "wp-approvals", "state_file": str(self.t / "last-id"), "tool": "/opt/wp-site-import",
                    "notify_cmd": "/opt/ntfy-notify", "result_topic": "homeserver", "resolve_timeout_s": 600, "max_per_minute": 3, "read_timeout_s": 120}
        self.calls, self.requests, self.slept, self.said, self.streams = [], [], [], [], []
        self.now, self.tool_rc, self.tool_out, self.tool_exc = 1000.0, 0, "wptest approved", None

    def tearDown(self):
        self.d.cleanup()

    def fake_run(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd[0] == self.cfg["tool"]:
            if self.tool_exc:
                raise self.tool_exc
            return type("P", (), {"returncode": self.tool_rc, "stdout": self.tool_out, "stderr": ""})()
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def opener(self, req, timeout=None):
        self.requests.append((req, timeout))
        nxt = self.streams.pop(0) if self.streams else FakeStream([])
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def listener(self):
        return self.lis.Listener(self.cfg, opener=self.opener, run=self.fake_run, sleep=self.slept.append, clock=lambda: self.now, out=self.said.append)

    def tool_calls(self):
        return [c for c, _ in self.calls if c[0] == self.cfg["tool"]]

    def tells(self):
        return [c[2] for c, _ in self.calls if c[0] == self.cfg["notify_cmd"]]

    def test_a_token_is_handed_to_the_tool_as_one_argument_and_the_result_goes_to_the_phone(self):
        l = self.listener()
        self.streams = [FakeStream([event(message=TOK)])]
        l.serve(rounds=1)
        self.assertEqual(self.tool_calls(), [["/opt/wp-site-import", "resolve", TOK]])
        self.assertTrue(all(kw.get("stdin") == subprocess.DEVNULL and "shell" not in kw for _, kw in self.calls))
        tool_kw = [kw for c, kw in self.calls if c[0] == self.cfg["tool"]][0]
        self.assertEqual(tool_kw["timeout"], 600, "a tool that hangs must be stopped")
        self.assertEqual(self.tells(), ["Phone decision done: wptest approved"])
        self.assertEqual([c[1] for c, _ in self.calls if c[0] == self.cfg["notify_cmd"]], ["homeserver"], "the result goes to the topic from the config")

    def test_the_request_is_a_read_only_stream_with_the_listeners_login(self):
        self.streams = [FakeStream([])]
        self.listener().serve(rounds=1)
        req, timeout = self.requests[0]
        self.assertEqual(req.full_url, "https://ntfy.example.org/wp-approvals/json?since=10m")
        self.assertEqual(req.get_header("Authorization"), ap.basic("wpvm", "LISTENERPW"))
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(timeout, 120)

    def test_it_resumes_after_the_last_message_it_handled_even_after_a_restart(self):
        self.streams = [FakeStream([event(id="abc123", message=TOK)]), FakeStream([])]
        l = self.listener()
        l.serve(rounds=2)
        self.assertTrue(self.requests[1][0].full_url.endswith("since=abc123"))
        self.assertEqual((self.t / "last-id").read_text().strip(), "abc123")
        self.streams = [FakeStream([])]
        self.listener().serve(rounds=1)
        self.assertTrue(self.requests[2][0].full_url.endswith("since=abc123"), "a new process reads the state file")

    def test_a_message_delivered_twice_is_handled_once(self):
        m = event(id="same1", message=TOK)
        self.streams = [FakeStream([m, m])]
        self.listener().serve(rounds=1)
        self.assertEqual(len(self.tool_calls()), 1)

    def test_only_a_message_whose_whole_body_is_a_token_is_acted_on(self):
        junk = ["hello", "", TOK + "\n", "\n" + TOK, TOK + " ; rm -rf /", "$(" + TOK + ")", "x " + TOK, TOK[:-1], "v1." + "A" * 900 + "." + "0" * 64, None, 5, ["x"], {"a": TOK}, "v1." + "A" * 50 + "." + "A" * 64]
        lines = [event(message=m) for m in junk] + [b"not json\n", b"\n", b"[1,2]\n", json.dumps({"event": "open"}).encode() + b"\n", json.dumps({"event": "keepalive"}).encode() + b"\n",
                                                  json.dumps({"event": "message_delete", "message": TOK}).encode() + b"\n", json.dumps({"id": "z", "message": TOK}).encode() + b"\n"]
        self.streams = [FakeStream(lines)]
        self.listener().serve(rounds=1)
        self.assertEqual(self.tool_calls(), [])
        self.assertEqual(self.tells(), [])

    def test_a_flood_cannot_start_a_process_per_message(self):
        self.streams = [FakeStream([event(message=TOK) for _ in range(10)])]
        self.listener().serve(rounds=1)
        self.assertEqual(len(self.tool_calls()), 3, "max_per_minute is 3 here")
        self.assertTrue([s for s in self.said if "more than 3 a minute" in s])
        self.now += 61
        self.streams = [FakeStream([event(message=TOK)])]
        l = self.listener()
        l.recent = []
        l.serve(rounds=1)
        self.assertEqual(len(self.tool_calls()), 4)

    def test_the_window_slides(self):
        l = self.listener()
        for i in range(3):
            self.now += 10
            self.assertEqual(l.handle(json.loads(event(message=TOK))), "done")
        self.assertEqual(l.handle(json.loads(event(message=TOK))), "ignored (too many)")
        self.now += 55           # the first one is now more than 60 s old
        self.assertEqual(l.handle(json.loads(event(message=TOK))), "done")

    def test_a_refusal_by_the_tool_is_reported_not_hidden(self):
        self.tool_rc, self.tool_out = 2, "wp-site-import: token refused: the token has expired"
        self.streams = [FakeStream([event(message=TOK)])]
        self.listener().serve(rounds=1)
        self.assertEqual(self.tells(), ["Phone decision refused: wp-site-import: token refused: the token has expired"])

    def test_what_the_tool_prints_is_filtered_before_it_reaches_the_phone(self):
        self.tool_out = "first\nlast line \x1b[2J\x07 \u202e end"
        self.streams = [FakeStream([event(message=TOK)])]
        self.listener().serve(rounds=1)
        self.assertEqual(len(self.tells()), 1)
        for bad in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(bad, self.tells()[0])
        self.assertIn("last line", self.tells()[0])
        self.assertNotIn("first", self.tells()[0])

    def test_a_tool_that_hangs_or_cannot_run_is_told_to_the_phone_and_the_loop_survives(self):
        for exc, needle in ((subprocess.TimeoutExpired("x", 600), "did not finish in time"), (FileNotFoundError("no tool"), "could not be run")):
            self.setUp()
            self.tool_exc = exc
            self.streams = [FakeStream([event(message=TOK), event(message=TOK)])]
            self.listener().serve(rounds=1)
            self.assertIn(needle, self.tells()[0])
            self.tearDown()

    def test_the_token_is_never_logged_in_full(self):
        self.streams = [FakeStream([event(message=TOK)])]
        self.listener().serve(rounds=1)
        self.assertTrue(self.said)
        self.assertFalse([s for s in self.said if TOK in s or TOK[10:30] in s])
        self.assertFalse([t for t in self.tells() if TOK[10:30] in t])

    def test_it_reconnects_with_a_growing_pause_and_starts_over_after_a_good_connection(self):
        err = urllib.error.URLError("down")
        self.streams = [err, err, err, err, FakeStream([event(message="hi")]), err]
        self.listener().serve(rounds=6)
        self.assertEqual(self.slept, [1, 2, 4, 8, 1, 1])

    def test_the_pause_is_capped(self):
        self.streams = [OSError("x")] * 12
        self.listener().serve(rounds=12)
        self.assertEqual(max(self.slept), 60)
        self.assertEqual(self.slept[:7], [1, 2, 4, 8, 16, 32, 60])

    def test_a_refused_login_pauses_for_five_minutes_and_tells_you_once_an_hour(self):
        denied = urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)
        self.streams = [denied, denied, denied]
        l = self.listener()
        l.serve(rounds=2)
        self.assertEqual(self.slept, [300, 300])
        self.assertEqual(len(self.tells()), 1)
        self.assertIn("refused by ntfy (401)", self.tells()[0])
        self.now += 3601
        l.serve(rounds=1)
        self.assertEqual(len(self.tells()), 2)

    def test_a_server_error_backs_off_like_any_other_failure(self):
        self.streams = [urllib.error.HTTPError("https://x", 502, "Bad Gateway", {}, None)]
        self.listener().serve(rounds=1)
        self.assertEqual(self.slept, [1])
        self.assertEqual(self.tells(), [])

    def test_missing_files_do_not_crash_it(self):
        for victim in ("conf", "listener"):
            self.setUp()
            os.unlink(self.t / victim)
            self.listener().serve(rounds=2)
            self.assertEqual(len(self.slept), 2, victim)
            self.assertTrue([s for s in self.said if "cannot follow the topic" in s] or [s for s in self.said if "login:password" in s], victim)
            self.tearDown()
        self.setUp()
        (self.t / "listener").write_text("nocolon")
        self.listener().serve(rounds=1)
        self.assertEqual(self.requests, [])

    def test_a_damaged_state_file_is_ignored(self):
        for bad in ("../../etc", "a b", "x" * 80, ""):
            (self.t / "last-id").write_text(bad)
            self.streams = [FakeStream([])]
            self.listener().serve(rounds=1)
            self.assertTrue(self.requests[-1][0].full_url.endswith("since=10m"), repr(bad))

    def test_an_unwritable_id_is_not_stored(self):
        l = self.listener()
        for bad in ("../x", "a b", "", None, 5, "x" * 80):
            l.save_id(bad)
        self.assertFalse((self.t / "last-id").exists())

    def test_a_notification_that_cannot_be_sent_does_not_stop_it(self):
        real = self.fake_run

        def run(cmd, **kw):
            if cmd[0] == self.cfg["notify_cmd"]:
                raise OSError("no ntfy-notify")
            return real(cmd, **kw)
        l = self.lis.Listener(self.cfg, opener=self.opener, run=run, sleep=self.slept.append, clock=lambda: self.now, out=self.said.append)
        self.streams = [FakeStream([event(message=TOK), event(message=TOK)])]
        l.serve(rounds=1)
        self.assertTrue([s for s in self.said if "could not notify" in s])


import subprocess  # noqa: E402

import jinja2  # noqa: E402
import yaml  # noqa: E402
from test_wp_sites_render import ROOT, load  # noqa: E402


def render_template(name, **over):
    v = {}
    v.update(load("vars/wp.yml"))
    v.update(load("host_vars/dellwp/vars.yml"))
    v.update(over)
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates/wp")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
    return e.get_template(name).render(**v), v


class Plumbing(unittest.TestCase):
    """The play, the templates and the ntfy users, read as files: they cannot be run here, so what can go wrong in their text is checked."""
    def test_the_listener_command_carries_the_repos_values_and_not_the_ntfy_address(self):
        text, v = render_template("wp_approval_listener.py.j2")
        compile(text, "wp-approval-listener", "exec")
        self.assertTrue(text.startswith("#!/usr/bin/python3 -I\n"))
        ns = {}
        exec(text.split("import wp_approval_listener  # noqa: E402\n")[1].split("sys.exit(")[0], ns)
        c = ns["CONFIG"]
        self.assertEqual(c, {"ntfy_conf": v["wp_ntfy_conf"], "listener_file": v["wp_approvals_dir"] + "/listener", "reply_topic": v["wp_approvals_reply_topic"],
                             "state_file": v["wp_approvals_state_dir"] + "/last-id", "tool": v["wp_tools_bin"] + "/wp-site-import", "notify_cmd": v["wp_ntfy_notify_cmd"],
                             "result_topic": v["wp_ntfy_topic"], "resolve_timeout_s": v["wp_approvals_resolve_timeout_s"], "max_per_minute": v["wp_approvals_max_per_minute"],
                             "read_timeout_s": v["wp_approvals_read_timeout_s"]})
        for k in ("resolve_timeout_s", "max_per_minute", "read_timeout_s"):
            self.assertIsInstance(c[k], int)
        self.assertNotIn("ntfy-acer", text)
        self.assertNotIn("http", text.replace("https://", "").split("CONFIG")[1])

    def test_the_rendered_command_runs_the_real_listener_module(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lib = d / "lib"
            lib.mkdir()
            for m in ("wp_approval_listener.py", "wp_approvals.py", "wp_wpress.py"):
                (lib / m).write_text((ROOT / "scripts" / m).read_text())
            (d / "conf").write_text("")      # unreadable on purpose: the process must report it and not crash
            text, _ = render_template("wp_approval_listener.py.j2", wp_tools_lib=str(lib), wp_ntfy_conf=str(d / "conf"), wp_approvals_dir=str(d), wp_approvals_state_dir=str(d))
            text = text.replace("sys.exit(wp_approval_listener.main(CONFIG))", "import wp_approval_listener as m\nm.Listener(CONFIG, sleep=lambda s: None).serve(rounds=1)\nprint('survived')")
            tool = d / "cmd"
            tool.write_text(text.replace("#!/usr/bin/python3 -I", "#!%s -I" % sys.executable))
            tool.chmod(0o755)
            r = subprocess.run([str(tool)], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("cannot follow the topic", r.stdout)
            self.assertIn("survived", r.stdout)

    def test_the_unit(self):
        text, v = render_template("wp_approval_listener.service.j2")
        directives = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]       # a comment that names a setting is not the setting
        for needle in ("ExecStart=%s/wp-approval-listener" % v["wp_tools_bin"], "Restart=always", "StartLimitIntervalSec=0", "NoNewPrivileges=true", "After=network-online.target docker.service",
                       "WantedBy=multi-user.target"):
            self.assertIn(needle, directives)

    def tasks(self):
        return yaml.safe_load((ROOT / "tasks/wp_approvals_common.yml").read_text())

    def test_the_secret_files_are_root_only_and_never_logged(self):
        t = {x["name"]: x for x in self.tasks()}
        for name in ("Write the signing key", "Write the button user's login", "Write the listener's login"):
            x = t[name]
            self.assertEqual((x["ansible.builtin.copy"]["mode"], x["ansible.builtin.copy"]["owner"], x["no_log"]), ("0600", "root", True), name)
        self.assertEqual(t["Make the directories (root only)"]["ansible.builtin.file"]["mode"], "0700")
        self.assertEqual(t["Deploy the listener command"]["ansible.builtin.template"]["mode"], "0750")

    def test_the_key_is_a_function_of_the_master_secret_and_the_tool_accepts_it(self):
        t = {x["name"]: x for x in self.tasks()}
        expr = t["Write the signing key"]["ansible.builtin.copy"]["content"]
        e = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)      # Ansible keeps the trailing newline of a template
        e.filters["hash"] = lambda value, algo="sha1": hashlib.new(algo, str(value).encode()).hexdigest()
        out = e.from_string(expr).render(wp_master_secret="the-master-secret")
        self.assertEqual(out, hashlib.sha256(b"the-master-secret|wp-approvals").hexdigest() + "\n")
        import tempfile
        with tempfile.NamedTemporaryFile("w") as f:
            f.write(out)
            f.flush()
            self.assertEqual(len(ap.read_key(f.name)), 64)
        self.assertNotEqual(out, e.from_string(expr).render(wp_master_secret="another"))

    def test_the_logins_are_the_ntfy_users_the_other_play_creates(self):
        t = {x["name"]: x for x in self.tasks()}
        self.assertTrue(t["Write the button user's login"]["ansible.builtin.copy"]["content"].startswith("approver:{{ hostvars['centermc92']['centermc92_ntfy_approver_password'] }}"))
        self.assertTrue(t["Write the listener's login"]["ansible.builtin.copy"]["content"].startswith("wpvm:{{ hostvars['centermc92']['centermc92_ntfy_wpvm_password'] }}"))
        text = (ROOT / "centermc92/ntfy_centermc92.yml").read_text()
        for line in ("ensure_user approver \"$APPROVER_PW\"", "ensure_user wpvm \"$WPVM_PW\"", "ntfy access approver {{ ntfy_approvals_topic }} write-only", "ntfy access wpvm {{ ntfy_approvals_topic }} read-only",
                     "ntfy access hosts {{ ntfy_acer_topic }} write-only", "ntfy access admin {{ ntfy_acer_topic }} read-write"):
            self.assertIn(line, text)
        self.assertNotIn("access approver {{ ntfy_acer_topic }}", text)
        self.assertNotIn("access wpvm {{ ntfy_acer_topic }}", text)
        self.assertNotRegex(text, r"access (approver|wpvm) \{\{ ntfy_approvals_topic \}\} (read-write|write-only|read-only|deny)\n(?s:.*)access (approver|wpvm) \{\{ ntfy_approvals_topic \}\} read-write")
        self.assertIn("hostvars['dellwp']['wp_approvals_reply_topic']", text)

    def test_the_secrets_script_adds_exactly_the_names_the_plays_read(self):
        script = (ROOT / "scripts/wp_approvals_secrets.sh").read_text()
        self.assertIn("centermc92_ntfy_approver_password", script)
        self.assertIn("centermc92_ntfy_wpvm_password", script)
        for f in ("tasks/wp_approvals_common.yml", "centermc92/ntfy_centermc92.yml"):
            used = set(__import__("re").findall(r"centermc92_ntfy_(?:approver|wpvm)_password", (ROOT / f).read_text()))
            self.assertEqual(used, {"centermc92_ntfy_approver_password", "centermc92_ntfy_wpvm_password"}, f)

    def test_the_play_notifies_the_handler_it_defines_and_asserts_before_it_changes_anything(self):
        play = yaml.safe_load((ROOT / "dellwp/wp_approvals_dellwp.yml").read_text())[0]
        handlers = {h["name"] for h in play["handlers"]}
        notified = {x["notify"] for x in self.tasks() if "notify" in x}
        self.assertEqual(notified, handlers)
        self.assertEqual(self.tasks()[0]["ansible.builtin.assert"]["that"][0], "wp_master_secret is defined")
        self.assertEqual(play["vars"]["wp_master_secret"], "{{ dellwp_wp_master_secret }}")


SECRETS = ROOT / "scripts/wp_approvals_secrets.sh"
VAULT_SHIM = """#!/bin/bash
# a stand-in for ansible-vault: the "encrypted" file is a header line and the plain text
case "$1" in
  view) tail -n +2 "$2" ;;
  encrypt) out=""; src=""; shift; MODE_LOG="${SHIM_MODES:-/dev/null}"; while [ $# -gt 0 ]; do case "$1" in --output) out="$2"; shift 2 ;; *) src="$1"; shift ;; esac; done
           stat -c %a "$src" >> "$MODE_LOG"; stat -c %a "$out" >> "$MODE_LOG"
           if [ -f "$SHIM_BREAK" ]; then echo garbage > "$out"; else { echo '$ANSIBLE_VAULT;1.1;AES256'; cat "$src"; } > "$out"; fi ;;
  *) exit 2 ;;
esac
"""


class Secrets(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.TemporaryDirectory()
        self.repo = Path(self.d.name)
        (self.repo / "scripts").mkdir()
        (self.repo / "host_vars/centermc92").mkdir(parents=True)
        (self.repo / "bin").mkdir()
        (self.repo / "scripts/wp_approvals_secrets.sh").write_text(SECRETS.read_text())
        (self.repo / "bin/ansible-vault").write_text(VAULT_SHIM)
        (self.repo / "bin/ansible-vault").chmod(0o755)
        self.vault = self.repo / "host_vars/centermc92/vault_ntfy.yml"
        self.vault.write_text("$ANSIBLE_VAULT;1.1;AES256\ncentermc92_ntfy_phone_password: \"p\"\ncentermc92_ntfy_hosts_password: \"h\"\n")
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, capture_output=True)
        self.env = dict(os.environ, PATH="%s:%s" % (self.repo / "bin", os.environ["PATH"]), SHIM_BREAK=str(self.repo / "break"))

    def tearDown(self):
        self.d.cleanup()

    def run_script(self, env=None):
        return subprocess.run(["bash", str(self.repo / "scripts/wp_approvals_secrets.sh")], capture_output=True, text=True, env=env or self.env, timeout=60)

    def values(self):
        return yaml.safe_load("\n".join(self.vault.read_text().splitlines()[1:]))

    def test_it_adds_both_logins_and_shows_only_their_length(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        v = self.values()
        for name in ("centermc92_ntfy_approver_password", "centermc92_ntfy_wpvm_password"):
            self.assertRegex(v[name], r"^[0-9a-f]{48}$")
            self.assertIn("%s: generated, 48 characters" % name, r.stdout)
            self.assertNotIn(v[name], r.stdout + r.stderr)
        self.assertNotEqual(v["centermc92_ntfy_approver_password"], v["centermc92_ntfy_wpvm_password"])
        self.assertEqual((v["centermc92_ntfy_phone_password"], v["centermc92_ntfy_hosts_password"]), ("p", "h"), "what was there is kept")
        self.assertTrue(self.vault.read_text().startswith("$ANSIBLE_VAULT"))

    def test_a_second_run_changes_nothing(self):
        self.run_script()
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "x"], check=True, capture_output=True)
        before = self.vault.read_bytes()
        r = self.run_script()
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing to add", r.stdout)
        self.assertEqual(self.vault.read_bytes(), before)

    def test_a_login_that_is_already_there_is_never_replaced(self):
        self.vault.write_text(self.vault.read_text() + 'centermc92_ntfy_wpvm_password: "keepme-keepme-keepme-keepme-keepme-keepme"\n')
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "x"], check=True, capture_output=True)
        r = self.run_script()
        self.assertEqual(r.returncode, 0)
        self.assertIn("centermc92_ntfy_wpvm_password: already in the vault, left alone", r.stdout)
        v = self.values()
        self.assertEqual(v["centermc92_ntfy_wpvm_password"], "keepme-keepme-keepme-keepme-keepme-keepme")
        self.assertIn("centermc92_ntfy_approver_password", v)

    def test_it_refuses_a_vault_file_with_uncommitted_changes(self):
        self.vault.write_text(self.vault.read_text() + "x: 1\n")
        before = self.vault.read_bytes()
        r = self.run_script()
        self.assertEqual(r.returncode, 2)
        self.assertIn("uncommitted changes", r.stderr)
        self.assertEqual(self.vault.read_bytes(), before)

    def test_it_refuses_a_file_that_is_not_encrypted(self):
        self.vault.write_text("centermc92_ntfy_phone_password: plain\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "x"], check=True, capture_output=True)
        r = self.run_script()
        self.assertEqual(r.returncode, 2)
        self.assertIn("not an encrypted vault file", r.stderr)
        self.assertEqual(self.vault.read_text(), "centermc92_ntfy_phone_password: plain\n")

    def test_a_failed_encryption_leaves_the_vault_as_it_was(self):
        (self.repo / "break").write_text("")
        before = self.vault.read_bytes()
        r = self.run_script()
        self.assertEqual(r.returncode, 2)
        self.assertIn("encryption failed", r.stderr)
        self.assertEqual(self.vault.read_bytes(), before)

    def test_a_missing_command_is_named_and_nothing_changes(self):
        import shutil
        only = self.repo / "only"
        only.mkdir()
        for tool in ("git", "dirname", "head", "grep", "mktemp", "rm", "cp", "cat", "tail", "openssl", "python3", "tr", "env", "bash"):
            (only / tool).symlink_to(shutil.which(tool))        # everything the script needs except ansible-vault, which may be installed on this machine
        before = self.vault.read_bytes()
        r = self.run_script(env=dict(self.env, PATH=str(only)))
        self.assertEqual(r.returncode, 2)
        self.assertIn("missing command: ansible-vault", r.stderr)
        self.assertEqual(self.vault.read_bytes(), before)

    def test_the_plaintext_and_the_new_file_are_private_while_they_exist(self):
        log = self.repo / "modes"
        r = self.run_script(env=dict(self.env, SHIM_MODES=str(log)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text().split(), ["600", "600"])

    def test_no_temporary_file_with_a_secret_is_left_behind(self):
        tmpdir = self.repo / "tmpdir"
        tmpdir.mkdir()
        for case in ("ok", "broken"):
            if case == "broken":
                (self.repo / "break").write_text("")
            self.run_script(env=dict(self.env, TMPDIR=str(tmpdir)))
            self.assertEqual(list(tmpdir.iterdir()), [], case)


if __name__ == "__main__":
    unittest.main()
