#!/usr/bin/env python3
"""Offline tests of scripts/wp_plugin_requests.py, the operator's side of "Install" in a site's wp-admin: collecting what the sites ask for, the phone notification, approving, denying, and the token that
a button on the phone carries. Every tool it calls (wp-site-cli, wp-vulnfeed, wp-site-ext, logger) and the ntfy server are stand-ins that record what they were asked.
Run: python3 -I tests/test_wp_plugin_requests.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import wp_approvals as ap  # noqa: E402
import wp_plugin_requests as pr  # noqa: E402

NOW = 1_800_000_000
KEY = "k" * 40
RID1, RID2, RID3 = "aabbccdd0011", "aabbccdd0022", "aabbccdd0033"


def entry(rid=RID1, slug="akismet", t=NOW - 60, user="owner"):
    return {"id": rid, "slug": slug, "t": t, "user": user}


def row(slug="akismet", name="Akismet Anti-spam", version="5.3", installs=5000000, rating=90, updated="2026-10-01"):
    return {"slug": slug, "name": name, "version": version, "rating": rating, "active_installs": installs, "last_updated": updated}


class Fake:
    """The tools and the server, as stand-ins. `requests` is the option each site holds, `results` the answers the host wrote."""

    def __init__(self):
        self.calls, self.requests, self.results, self.posted, self.journal = [], {}, {}, [], []
        self.search = {"akismet": [row()], "jetpack": [row("jetpack", "Jetpack", "13.0")], "hello-dolly": [row("hello-dolly", "Hello Dolly", "1.7.2")]}
        self.search_rc, self.vuln, self.ext_rc, self.ext_text, self.down, self.on_ext, self.update_rc = 0, {}, 0, "Success: Installed 1 of 1 plugins.", set(), None, 0

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        tool = os.path.basename(cmd[0])
        done = lambda rc, out="", err="": subprocess.CompletedProcess(cmd, rc, out, err)
        if tool == "logger":
            self.journal.append(cmd[-1])
            return done(0)
        if tool == "wp-vulnfeed":
            rc, line = self.vuln.get(cmd[2], (0, "plugin %s %s checked against 2 records; none known" % (cmd[2], cmd[3])))
            return done(rc, line + "\n")
        if tool == "wp-site-ext":
            if self.on_ext:
                self.on_ext()
            return done(self.ext_rc, self.ext_text + "\n")
        assert tool == "wp-site-cli", cmd
        site, args = cmd[1], [a for a in cmd[2:] if not a.startswith("--")]
        if site in self.down:
            return done(1, "", "Error: the site's database is down")
        if args[:2] == ["option", "get"]:
            data = (self.requests if args[2] == "wph_plugin_requests" else self.results)
            if site not in data:
                return done(1, "", "Error: Could not get '%s' option. Does it exist?" % args[2])
            return done(0, "Deprecated: noise\n" + json.dumps(data[site]) + "\n")
        if args[:2] == ["option", "update"]:
            assert args[2] == "wph_plugin_request_results", cmd
            if self.update_rc:
                return done(self.update_rc, "", "Error")
            self.results[site] = json.loads(args[3])
            return done(0, "Success: Updated 'wph_plugin_request_results' option.\n")
        if args[:2] == ["plugin", "search"]:
            return done(self.search_rc, "Notice: x\n" + json.dumps(self.search.get(args[2], [])) + "\n")
        raise AssertionError(cmd)

    def post(self, url, auth, payload, timeout=15):
        if getattr(self, "post_error", None):
            raise ap.ApprovalError(self.post_error)
        self.posted.append((url, payload))
        return 200

    def tools(self):
        return [os.path.basename(c[0]) for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        t = Path(self.d.name)
        for site, state in (("alpha", "live"), ("beta", "hold"), ("gamma", "stopped")):
            (t / "data" / site).mkdir(parents=True)
            (t / "data" / site / "site.json").write_text(json.dumps({"state": state}))
        (t / "cache").mkdir()
        for f in ("a", "b"):
            (t / "cache" / f).write_text("page")
        (t / "key").write_text(KEY + "\n")
        (t / "conf").write_text('NTFY_FALLBACK_URL="https://ntfy.example.org"\nNTFY_FALLBACK_TOPIC="alerts"\n')
        (t / "netrc").write_text("machine ntfy.example.org login sender password SENDERPW\n")
        (t / "approver").write_text("approver:APPROVERPW\n")
        self.cfg = {"data_dir": str(t / "data"), "tools_bin": "/opt/bin", "state_dir": str(t / "state"), "page_cache_dir": str(t / "cache"), "logger": "/opt/bin/logger", "cli_timeout_s": 30,
                    "install_timeout_s": 600, "expire_days": 7, "max_pending_per_site": 3,
                    "approvals": {"key_file": str(t / "key"), "approver_file": str(t / "approver"), "ntfy_conf": str(t / "conf"), "netrc": str(t / "netrc"), "reply_topic": "wp-approvals", "ttl_s": 86400}}
        self.fake, self.out, self.t = Fake(), [], t
        self.now = NOW

    def go(self, *argv):
        self.out.clear()
        rc = pr.main(self.cfg, list(argv), out=self.out.append, run=self.fake, clock=lambda: self.now, post=self.fake.post)
        return rc, "\n".join(self.out)

    def store(self):
        return pr.load(self.cfg)

    def cache_files(self):
        return sorted(p.name for p in (self.t / "cache").iterdir())

    def key(self, site="alpha", rid=RID1):
        return pr.key_of(site, rid)

    def collect_one(self, **kw):
        self.fake.requests["alpha"] = [entry(**kw)]
        return self.go("collect")


class Collect(Base):
    def test_live_and_held_sites_are_asked_and_a_stopped_one_is_not(self):
        self.fake.requests = {"alpha": [entry()], "beta": [entry(RID2, "jetpack")], "gamma": [entry(RID3, "hello-dolly")]}
        rc, text = self.go("collect")
        self.assertEqual(rc, 0, text)
        self.assertEqual(sorted(self.store()), [self.key("alpha", RID1), self.key("beta", RID2)])
        self.assertIn("alpha: 1 request read, 1 new", text); self.assertIn("beta: 1 request read, 1 new", text); self.assertNotIn("gamma", text)
        self.assertIn("2 requests waiting", text)

    def test_wp_cli_runs_as_the_web_app_with_no_plugin_code(self):
        self.fake.requests["alpha"] = [entry()]
        self.go("collect", "alpha")
        for c in self.fake.calls:
            if os.path.basename(c[0]) == "wp-site-cli":
                self.assertNotIn("--code", c)
                self.assertIn("--skip-plugins", c); self.assertIn("--skip-themes", c)
        self.assertEqual(self.fake.calls[0][:4], ["/opt/bin/wp-site-cli", "alpha", "option", "get"])

    def test_a_new_request_is_looked_up_checked_against_the_vulnerability_index_and_notified(self):
        rc, text = self.collect_one()
        e = self.store()[self.key()]
        self.assertEqual((e["state"], e["found"], e["name"], e["version"], e["vuln"], e["notified"]), ("pending", True, "Akismet Anti-spam", "5.3", "clean", True))
        self.assertEqual(len(self.fake.posted), 1)
        url, msg = self.fake.posted[0]
        self.assertEqual((url, msg["topic"], msg["title"]), ("https://ntfy.example.org", "alerts", "Plugin request: alpha"))
        self.assertEqual([a["label"] for a in msg["actions"]], ["Approve", "Deny"])
        self.assertTrue(all(a["body"].startswith("p1.") for a in msg["actions"]))
        self.assertIn("owner asks to install akismet on alpha", msg["message"]); self.assertIn("5000000+ installs", msg["message"]); self.assertIn("none known", msg["message"])
        self.assertEqual(sorted(e["tokens"].values()), ["approve", "deny"])
        self.assertIn("alpha akismet: notification sent (Approve and Deny)", text)
        self.assertIn(["/opt/bin/wp-vulnfeed", "check-plugin", "akismet", "5.3"], self.fake.calls)

    def test_nothing_secret_reaches_the_output_or_the_store_file_in_the_clear(self):
        rc, text = self.collect_one()
        raw = (Path(self.cfg["state_dir"]) / "requests.json").read_text()
        for secret in (KEY, "SENDERPW", "APPROVERPW"):
            self.assertNotIn(secret, text + raw)
        self.assertEqual(oct((Path(self.cfg["state_dir"]) / "requests.json").stat().st_mode & 0o777), "0o600")
        for tok in [a["body"] for a in self.fake.posted[0][1]["actions"]]:
            self.assertNotIn(tok, text)

    def test_the_same_request_is_not_stored_or_told_again(self):
        self.collect_one()
        rc, text = self.go("collect")
        self.assertIn("1 request read, 0 new", text)
        self.assertEqual((len(self.store()), len(self.fake.posted)), (1, 1))

    def test_a_site_asking_twice_for_one_plugin_waits_once_and_the_limit_per_site_holds(self):
        self.fake.requests["alpha"] = [entry(RID1, "akismet"), entry(RID2, "akismet"), entry("aabbccdd0044", "a"), entry("aabbccdd0055", "b"), entry("aabbccdd0066", "c")]
        self.go("collect")
        self.assertEqual(sorted(e["slug"] for e in self.store().values()), ["a", "akismet", "b"], "one akismet, and three in all (the limit in the test config)")

    def test_what_is_not_the_shape_the_plugin_writes_is_dropped(self):
        junk = [entry(rid="zz"), entry(slug="../x"), entry(slug="Akismet"), entry(slug="a" * 101), entry(t="5"), entry(t=0), entry(t=True), entry(rid=RID1 + "\n"), "str", None, {"slug": "x"}, entry(RID3, "hello-dolly", user="a b\x1b[31m<script>")]
        self.fake.requests["alpha"] = junk
        self.go("collect")
        self.assertEqual([(e["slug"], e["user"]) for e in self.store().values()], [("hello-dolly", "a b31mscript")], "only the well formed one; its user name loses everything but plain characters")

    def test_old_and_future_requests_are_ignored(self):
        self.fake.requests["alpha"] = [entry(RID1, t=NOW - 8 * 86400), entry(RID2, "jetpack", t=NOW + 2 * 86400), entry(RID3, "hello-dolly", t=NOW - 6 * 86400)]
        self.go("collect")
        self.assertEqual([e["slug"] for e in self.store().values()], ["hello-dolly"])

    def test_a_plugin_wordpress_org_does_not_have_gets_a_deny_button_only(self):
        self.fake.search["nothing-here"] = []
        self.fake.requests["alpha"] = [entry(slug="nothing-here")]
        self.go("collect")
        e = next(iter(self.store().values()))
        self.assertEqual((e["found"], e["vuln"]), (False, "unchecked"))
        self.assertEqual([a["label"] for a in self.fake.posted[0][1]["actions"]], ["Deny"])
        self.assertIn("NOT FOUND on wordpress.org", self.fake.posted[0][1]["message"])

    def test_a_similar_name_is_not_the_plugin(self):
        self.fake.search["akismet"] = [row("akismet-pro", "Akismet Pro"), row("akismet-lite", "Lite")]
        self.fake.requests["alpha"] = [entry()]
        self.go("collect")
        self.assertFalse(next(iter(self.store().values()))["found"], "the slug must match exactly")

    def test_a_known_vulnerability_or_an_unchecked_one_has_no_approve_button(self):
        for rc, word in ((1, "found"), (3, "unchecked"), (2, "unchecked")):
            with self.subTest(rc=rc):
                self.setUp()
                self.fake.vuln["akismet"] = (rc, "plugin akismet 5.3: Akismet - XSS (CVE-2026-1, CVSS 7.1, fixed in 5.4)" if rc == 1 else "NOT RUN: the index is old")
                self.collect_one()
                e = next(iter(self.store().values()))
                self.assertEqual(e["vuln"], word)
                self.assertEqual([a["label"] for a in self.fake.posted[0][1]["actions"]], ["Deny"])
                self.assertIn("No Approve button", self.fake.posted[0][1]["message"])

    def test_a_lookup_that_fails_is_tried_again_next_time_and_nothing_is_told_meanwhile(self):
        self.fake.search_rc = 1
        rc, text = self.collect_one()
        e = next(iter(self.store().values()))
        self.assertNotIn("found", e); self.assertFalse(e["notified"]); self.assertIn("wordpress.org could not be asked", e["note"]); self.assertEqual(self.fake.posted, [])
        self.fake.search_rc = 0
        self.go("collect")
        self.assertEqual((len(self.fake.posted), next(iter(self.store().values()))["found"]), (1, True))

    def test_a_notification_that_fails_leaves_no_tokens_and_is_sent_next_time(self):
        self.fake.post_error = "ntfy answered 503"
        rc, text = self.collect_one()
        e = next(iter(self.store().values()))
        self.assertEqual((e["notified"], e["tokens"]), (False, {}))
        self.assertIn("akismet: phone approval is off or failed: ntfy answered 503", text)
        del self.fake.post_error
        self.go("collect")
        self.assertEqual(len(self.fake.posted), 1)
        self.assertTrue(next(iter(self.store().values()))["notified"])

    def test_without_the_approval_files_the_request_still_waits_for_the_control_page(self):
        (self.t / "key").unlink()
        rc, text = self.collect_one()
        self.assertEqual(rc, 0)
        self.assertIn("phone approval is off or failed", text); self.assertEqual(len(self.store()), 1)

    def test_no_notify_stores_and_tells_nothing(self):
        self.fake.requests["alpha"] = [entry()]
        self.go("collect", "--no-notify")
        self.assertEqual((len(self.store()), self.fake.posted), (1, []))

    def test_a_site_that_cannot_be_read_is_said_and_the_others_go_on(self):
        self.fake.requests = {"alpha": [entry()], "beta": [entry(RID2, "jetpack")]}
        self.fake.down = {"alpha"}
        rc, text = self.go("collect")
        self.assertEqual(rc, 0)
        self.assertIn("alpha: could not be read (status 1)", text)
        self.assertEqual([e["site"] for e in self.store().values()], ["beta"])
        self.fake.down = {"alpha", "beta"}
        self.assertEqual(self.go("collect")[0], 3, "nothing could be read at all")

    def test_a_site_with_no_option_yet_has_no_requests(self):
        rc, text = self.go("collect")
        self.assertEqual(rc, 0); self.assertIn("alpha: 0 requests read, 0 new", text)

    def test_one_named_site_and_an_unknown_one(self):
        self.fake.requests = {"alpha": [entry()], "beta": [entry(RID2, "jetpack")]}
        self.go("collect", "beta")
        self.assertEqual([e["site"] for e in self.store().values()], ["beta"])
        for bad in ("gamma", "nosuch", "../x", "Alpha"):
            rc, text = self.go("collect", bad)
            self.assertEqual(rc, 2, bad)

    def test_a_request_nobody_answered_expires_and_a_finished_one_is_forgotten_much_later(self):
        self.collect_one()
        self.now = NOW + 8 * 86400
        self.fake.requests["alpha"] = []
        self.go("collect")
        e = self.store()[self.key()]
        self.assertEqual((e["state"], e["tokens"]), ("expired", {}))
        self.now = NOW + 70 * 86400
        self.go("collect")
        self.assertEqual(self.store(), {})

    def test_an_expired_request_cannot_be_decided(self):
        self.collect_one()
        self.now = NOW + 8 * 86400
        self.go("collect")
        rc, text = self.go("decide", "alpha", RID1, "approve")
        self.assertEqual(rc, 2); self.assertIn("expired, not waiting", text)

    def test_list_is_one_json_line_without_tokens_waiting_first(self):
        self.fake.requests["alpha"] = [entry(RID1, "akismet"), entry(RID2, "jetpack")]
        self.go("collect")
        self.go("decide", "alpha", RID1, "deny")
        rc, text = self.go("list")
        data = json.loads(text)
        self.assertEqual([(r["slug"], r["state"]) for r in data["requests"]], [("jetpack", "pending"), ("akismet", "denied")])
        self.assertNotIn("tokens", text); self.assertNotIn("p1.", text)


class Deciding(Base):
    def setUp(self):
        super().setUp()
        self.collect_one()

    def test_deny_records_it_tells_the_site_and_installs_nothing(self):
        rc, text = self.go("decide", "alpha", RID1, "deny")
        self.assertEqual((rc, text), (0, "denied akismet for alpha"))
        e = self.store()[self.key()]
        self.assertEqual((e["state"], e["tokens"]), ("denied", {}))
        self.assertEqual(self.fake.results["alpha"], {RID1: {"status": "denied", "note": ""}})
        self.assertNotIn("wp-site-ext", self.fake.tools())
        self.assertEqual(self.cache_files(), ["a", "b"])
        self.assertEqual(self.fake.journal, ["site=alpha request=%s slug=akismet decision=deny by=panel" % RID1])

    def test_approve_installs_by_slug_inactive_clears_the_page_cache_and_tells_the_site(self):
        rc, text = self.go("decide", "alpha", RID1, "approve")
        self.assertEqual(rc, 0, text)
        self.assertIn(["/opt/bin/wp-site-ext", "alpha", "plugin", "install", "akismet"], self.fake.calls)
        self.assertNotIn("--activate", " ".join(" ".join(c) for c in self.fake.calls))
        self.assertIn("installed akismet on alpha, inactive; page cache: 2 files cleared", text)
        self.assertEqual(self.cache_files(), [])
        self.assertEqual((self.store()[self.key()]["state"], self.fake.results["alpha"][RID1]["status"]), ("installed", "installed"))
        self.assertIn("decision=approve by=panel", self.fake.journal[0])

    def test_the_request_is_marked_installing_before_the_install_starts(self):
        seen = []
        self.fake.on_ext = lambda: seen.append(self.store()[self.key()]["state"])
        self.go("decide", "alpha", RID1, "approve")
        self.assertEqual(seen, ["installing"], "a second approval that arrives meanwhile finds it taken")

    def test_a_second_decision_is_refused(self):
        self.go("decide", "alpha", RID1, "approve")
        for what in ("approve", "deny"):
            rc, text = self.go("decide", "alpha", RID1, what)
            self.assertEqual(rc, 2); self.assertIn("installed, not waiting", text)
        self.assertEqual(self.fake.tools().count("wp-site-ext"), 1)

    def test_a_failed_install_is_a_failure_with_its_reason_and_the_cache_is_kept(self):
        self.fake.ext_rc, self.fake.ext_text = 1, "Warning: x\nError: plugin 'akismet' did not match wordpress.org's checksums; it was removed"
        rc, text = self.go("decide", "alpha", RID1, "approve")
        self.assertEqual(rc, 1)
        e = self.store()[self.key()]
        self.assertEqual(e["state"], "failed"); self.assertIn("did not match", e["note"]); self.assertIn("did not match", text)
        self.assertEqual(self.cache_files(), ["a", "b"])
        self.assertEqual(self.fake.results["alpha"][RID1]["status"], "failed")

    def test_a_result_that_cannot_be_written_to_the_site_does_not_undo_the_decision(self):
        self.fake.update_rc = 1
        rc, text = self.go("decide", "alpha", RID1, "approve")
        self.assertEqual(rc, 0)
        self.assertIn("could not be written to the site", text); self.assertEqual(self.store()[self.key()]["state"], "installed")

    def test_a_plugin_that_is_not_on_wordpress_org_cannot_be_approved_at_all(self):
        Base.setUp(self)
        self.fake.search["ghost"] = []
        self.fake.requests["alpha"] = [entry(slug="ghost")]
        self.go("collect")
        for extra in ((), ("--accept-risk", "ghost")):
            rc, text = self.go("decide", "alpha", RID1, "approve", *extra)
            self.assertEqual(rc, 2); self.assertIn("not on wordpress.org", text)
        self.assertNotIn("wp-site-ext", self.fake.tools())

    def test_a_known_vulnerability_is_approved_only_on_purpose_by_typing_the_name(self):
        Base.setUp(self)
        self.fake.vuln["akismet"] = (1, "plugin akismet 5.3: Akismet - XSS (CVE-2026-1)")
        self.collect_one()
        rc, text = self.go("decide", "alpha", RID1, "approve")
        self.assertEqual(rc, 2); self.assertIn("a known vulnerability", text)
        for wrong in ("Akismet", "akismet ", "jetpack", ""):
            self.assertEqual(self.go("decide", "alpha", RID1, "approve", "--accept-risk", wrong)[0], 2, wrong)
        self.assertNotIn("wp-site-ext", self.fake.tools())
        rc, text = self.go("decide", "alpha", RID1, "approve", "--accept-risk", "akismet")
        self.assertEqual(rc, 0, text)
        self.assertIn("accept-risk", self.fake.journal[-1])

    def test_an_unchecked_one_is_the_same(self):
        Base.setUp(self)
        self.fake.vuln["akismet"] = (3, "NOT RUN: the index is old")
        self.collect_one()
        rc, text = self.go("decide", "alpha", RID1, "approve")
        self.assertEqual(rc, 2); self.assertIn("no vulnerability check", text)
        self.assertEqual(self.go("decide", "alpha", RID1, "approve", "--accept-risk", "akismet")[0], 0)

    def test_unknown_requests_and_odd_arguments_are_refused_before_anything_runs(self):
        before = len(self.fake.calls)
        for argv in (("alpha", "ffffffffffff", "approve"), ("nosuch", RID1, "approve"), ("../x", RID1, "approve"), ("alpha", "../x", "approve"), ("alpha", RID1, "install"), ("alpha", RID1, "approve", "--accept-risk"),
                     ("alpha", RID1, "approve", "--force", "akismet"), ("alpha", RID1 + "\n", "approve"), ("alpha", RID1)):
            rc, text = self.go("decide", *argv)
            self.assertEqual(rc, 2, argv)
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(self.store()[self.key()]["state"], "pending")

    def test_the_slug_that_is_installed_is_the_stored_one_not_anything_typed(self):
        self.go("decide", "alpha", RID1, "approve", "--accept-risk", "jetpack")
        installs = [c for c in self.fake.calls if os.path.basename(c[0]) == "wp-site-ext"]
        self.assertEqual(installs, [["/opt/bin/wp-site-ext", "alpha", "plugin", "install", "akismet"]])


class Tokens(Base):
    def setUp(self):
        super().setUp()
        self.collect_one()
        self.tokens = {a["label"].lower(): a["body"] for a in self.fake.posted[0][1]["actions"]}

    def resolve(self, token):
        return self.go("resolve", token)

    def test_the_approve_button_installs_and_the_token_works_once(self):
        rc, text = self.resolve(self.tokens["approve"])
        self.assertEqual(rc, 0, text)
        self.assertIn(["/opt/bin/wp-site-ext", "alpha", "plugin", "install", "akismet"], self.fake.calls)
        self.assertIn("by=phone", self.fake.journal[0])
        rc, text = self.resolve(self.tokens["approve"])
        self.assertEqual(rc, 2); self.assertIn("token refused", text)
        self.assertEqual(self.fake.tools().count("wp-site-ext"), 1)

    def test_the_deny_button_denies(self):
        rc, text = self.resolve(self.tokens["deny"])
        self.assertEqual((rc, self.store()[self.key()]["state"]), (0, "denied"))
        self.assertNotIn("wp-site-ext", self.fake.tools())

    def test_using_one_button_cancels_the_other(self):
        self.resolve(self.tokens["deny"])
        rc, text = self.resolve(self.tokens["approve"])
        self.assertEqual(rc, 2)
        self.assertEqual(self.store()[self.key()]["tokens"], {})

    def test_a_token_is_bound_to_its_request(self):
        self.fake.requests["alpha"].append(entry(RID2, "jetpack"))
        self.go("collect")
        other = next(a["body"] for p in self.fake.posted[1:] for a in p[1]["actions"] if a["label"] == "Approve")
        before = self.store()[self.key()]["tokens"]
        rc, text = self.resolve(other)                      # jetpack's token: acts on jetpack only
        self.assertEqual(self.store()[self.key(rid=RID2)]["state"], "installed")
        self.assertEqual((self.store()[self.key()]["state"], self.store()[self.key()]["tokens"]), ("pending", before))

    def test_an_expired_forged_or_foreign_token_does_nothing(self):
        self.now = NOW + 86400 + 1
        rc, text = self.resolve(self.tokens["approve"])
        self.assertEqual(rc, 2); self.assertIn("expired", text)
        self.now = NOW
        t = self.tokens["approve"]
        flipped = t[:-1] + ("0" if t[-1] != "0" else "1")
        imp, _ = ap.mint(KEY.encode(), "alpha", "approve", pr.identity_sha("alpha", "akismet", RID1), 3600, now=NOW)
        other_key, _ = ap.mint_plugin(b"x" * 40, "alpha", "approve", pr.identity_sha("alpha", "akismet", RID1), 3600, now=NOW)
        for bad in (flipped, imp, other_key, "garbage", t + "\n", "v1" + t[2:]):
            rc, text = self.resolve(bad)
            self.assertEqual(rc, 2, bad[:12])
        self.assertNotIn("wp-site-ext", self.fake.tools())

    def test_a_signed_token_for_a_nonce_never_issued_is_refused(self):
        tok, nonce = ap.mint_plugin(KEY.encode(), "alpha", "approve", pr.identity_sha("alpha", "akismet", RID1), 3600, now=NOW)
        rc, text = self.resolve(tok)
        self.assertEqual(rc, 2); self.assertIn("not issued for this request, or it was used", text)

    def test_an_approve_token_cannot_approve_a_request_that_is_not_clean_even_if_one_was_signed(self):
        e = self.store()
        e[self.key()]["vuln"] = "found"
        tok, nonce = ap.mint_plugin(KEY.encode(), "alpha", "approve", pr.identity_sha("alpha", "akismet", RID1), 3600, now=NOW)
        e[self.key()]["tokens"] = {nonce: "approve"}
        pr.save(self.cfg, e)
        rc, text = self.resolve(tok)
        self.assertEqual(rc, 2); self.assertIn("not clean", text)
        self.assertNotIn("wp-site-ext", self.fake.tools())

    def test_a_token_for_a_request_that_was_decided_in_the_meantime_is_refused(self):
        self.go("decide", "alpha", RID1, "deny")
        rc, text = self.resolve(self.tokens["approve"])
        self.assertEqual(rc, 2); self.assertIn("denied, not waiting", text)

    def test_the_phone_cannot_pass_a_risk_override(self):
        for argv in (("resolve", self.tokens["approve"], "--accept-risk", "akismet"), ("resolve",), ("resolve", "a", "b")):
            self.assertEqual(self.go(*argv)[0], 2)
        self.assertNotIn("wp-site-ext", self.fake.tools())

    def test_without_approval_files_a_token_does_nothing(self):
        self.cfg.pop("approvals")
        rc, text = self.resolve(self.tokens["approve"])
        self.assertEqual(rc, 2); self.assertIn("not configured", text)


class CommandLine(Base):
    def test_usage(self):
        for argv in ((), ("nonsense",), ("collect", "a", "b"), ("list", "x"), ("decide",)):
            rc, text = self.go(*argv)
            self.assertEqual(rc, 2, argv); self.assertIn("usage", text)


if __name__ == "__main__":
    unittest.main()
