#!/usr/bin/env python3
"""Offline tests of the control dispatcher: scripts/wp_ctl.py (the logic) and templates/wp/wp_ctl.py.j2 (the wrapper that carries the repo's values), the access tasks in tasks/wp_ctl_common.yml
and the registry rule that templates/wp/wp_tools_common.sh.j2 (require_site) now follows.

The filesystem is real (a temporary tree standing in for /srv/wp, /var/lib/wp-ctl and /var/log/wp-ctl), so are the file locks, the pipes and the job directories; the tools (wp-site-backup, wp-site-delete,
wp-site-password, wp-site-import, wp-sites-status), systemd-run and logger are stand-ins that log what they were given (their argv, what they read from their terminal, their environment) and can fail
or sleep on demand. They prove that every request is checked before anything runs and that a refusal runs nothing and leaves nothing behind (no job, no terminal file); that the tools get exactly
the argument list the verb defines and a clean environment; that the audit line is written before the action and again after, that nothing runs when it cannot be written, that a refusal is
recorded, and that a password is in no argument, no audit line, no log and no reply; that the bytes after the request reach the tool whole; that a job runs once, keeps its log and exit code, and
that two verbs cannot hold one site; and that the forced-command key lines, the sudoers line and the registry rule are what they should be. They do not prove that sshd honours the forced command, that
sudo takes the entry, that systemd-run starts a unit, that the filesystem accepts the append-only attribute or that the real tools behave behind the verbs: scripts/wp_ctl_rehearsal_dellwp.sh does
those on the host. Run: python3 -I tests/test_wp_ctl.py"""
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import jinja2  # noqa: E402
import yaml  # noqa: E402
import wp_ctl  # noqa: E402
from test_wp_sites_render import ROOT, base_vars, env as render_env  # noqa: E402

CANARY = "Canary-Pass-9f3a!xyz"
TOOL = """#!/bin/bash
D='%(dir)s'
name=$(basename "$0")
{ printf '%%s' "$name"; for a in "$@"; do printf ' [%%s]' "$a"; done; printf '\\n'; } >> "$D/calls.log"
env | sort > "$D/env.$name"
if [ -n "${WP_SITE_TTY:-}" ]; then cat "$WP_SITE_TTY" > "$D/tty.seen.$$"; stat -c %%a "$WP_SITE_TTY" > "$D/tty.mode.$$"; fi
if [ "$name" = wp-site-import ] && [ "${1:-}" = receive ]; then cat > "$D/received.bin"; fi
[ -f "$D/sleep" ] && exec sleep "$(cat "$D/sleep")"
[ -f "$D/leak" ] && echo "leaked: $(head -1 "$WP_SITE_TTY")"
[ -f "$D/stdout.$name" ] && { cat "$D/stdout.$name"; exit "$(cat "$D/rc" 2>/dev/null || echo 0)"; }
echo "$name ran"
exit "$(cat "$D/rc" 2>/dev/null || echo 0)"
"""
STATUS = """#!/bin/bash
echo wp-sites-status >> '%(dir)s/calls.log'
[ -f '%(dir)s/sleep' ] && exec sleep "$(cat '%(dir)s/sleep')"
printf 'site\\tdeployed\\tactual\\tunit\\n'
printf 'wptest\\tlive\\tlive\\tactive\\n'
printf 'held\\thold\\thold\\tactive\\n'
[ -f '%(dir)s/status_rc' ] && exit "$(cat '%(dir)s/status_rc')"
exit 0
"""
SYSTEMD_RUN = "#!/bin/bash\n{ printf 'systemd-run'; for a in \"$@\"; do printf ' [%%s]' \"$a\"; done; printf '\\n'; } >> '%(dir)s/calls.log'\nexit \"$(cat '%(dir)s/sd_rc' 2>/dev/null || echo 0)\"\n"
LOGGER = "#!/bin/bash\nprintf '%%s\\n' \"$*\" >> '%(dir)s/journal'\nexit \"$(cat '%(dir)s/logger_rc' 2>/dev/null || echo 0)\"\n"
LIMITS = {"default": {"timeout_s": 30, "memory_mb": 512, "tasks": 256}, "password": {"timeout_s": 30}, "backup": {"timeout_s": 600}, "delete": {"timeout_s": 600, "memory_mb": 384},
          "create": {"timeout_s": 700}, "apply": {"timeout_s": 710}, "update-check": {"timeout_s": 750}, "update-apply": {"timeout_s": 760, "memory_mb": 1024}, "import-new": {"timeout_s": 770, "memory_mb": 1024}, "set": {"timeout_s": 720}, "ensure-state": {"timeout_s": 730}, "install": {"timeout_s": 40},
          "import-receive": {"timeout_s": 60}, "import-run": {"timeout_s": 900, "memory_mb": 1024}, "import-approve": {"timeout_s": 700, "memory_mb": 1024}, "import-reset": {"timeout_s": 800, "memory_mb": 1024, "tasks": 64}, "import-rescan": {"timeout_s": 810, "memory_mb": 1024}}
HOSTILE_SITES = ["../x", "Site", "a;id", "x y", "", "a" * 25, "-a", "a-", "wp test", "wptest\n", "wptest/../held", "*", "wptest\x00", "WPTEST", "a", "nosuch", "wptest ", "$(id)"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wpctl-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin, self.data, self.state, self.stdin_dir, self.run = self.tmp / "bin", self.tmp / "data", self.tmp / "state", self.tmp / "in", self.tmp / "run"
        for d in (self.bin, self.data, self.tmp / "log", self.stdin_dir, self.tmp / "seen"):
            d.mkdir()
        for site in ("wptest", "held"):
            (self.data / site).mkdir()
            (self.data / site / "site.json").write_text("{}")          # what makes a directory a site: a registry record
        self.shim = self.tmp / "seen"
        for name in ("wp-site-backup", "wp-site-delete", "wp-site-password", "wp-site-import", "wp-site-provision", "wp-site-install", "wp-site-update", "wp-vulnfeed", "wp-site-cli", "wp-site-ext", "wp-ctl"):
            self.script(name, TOOL)
        self.script("wp-sites-status", STATUS)
        self.script("systemd-run", SYSTEMD_RUN)
        self.script("logger", LOGGER)
        self.t = time.time()
        self.audit_file = self.tmp / "log" / "audit.jsonl"
        self.cfg = {"state_dir": str(self.state), "run_dir": str(self.run), "audit_file": str(self.audit_file), "data_dir": str(self.data), "tools_bin": str(self.bin), "page_cache_dir": str(self.tmp / "pagecache"), "path": "/usr/bin:/bin",
                    "logger": str(self.bin / "logger"), "systemd_run": str(self.bin / "systemd-run"), "panel_user": "wpctl", "job_keep_days": 14, "max_request_bytes": 65536,
                    "limits": LIMITS, "reserved_names": ["hub", "mail", "bloomy"]}
        self.env = {"SUDO_USER": "wpctl", "LEAK_ME": "must-not-reach-the-tools", "SUDO_COMMAND": "/usr/local/sbin/wp-ctl"}

    def script(self, name, text):
        p = self.bin / name
        p.write_text(text % {"dir": str(self.shim)})
        p.chmod(0o755)

    clock_delay = 0

    def clock(self):
        if self.clock_delay:
            time.sleep(self.clock_delay)          # start_job reads the clock between looking for the request id and writing the job: a slow clock widens that window so a missing lock shows
        return self.t

    def limits(self, **own):
        """The test limits with some verbs' limits replaced: limits(**{"import-report": {"timeout_s": 1}})."""
        return dict(self.cfg, limits=dict(LIMITS, **own))

    def control(self, name, value):
        (self.shim / name).write_text(str(value))

    def call(self, req, extra=b"", argv=(), env=None, cfg=None):
        """Run one request as the dispatcher would see it: the JSON line (and the raw bytes after it) on a file descriptor. Returns (exit status, reply)."""
        line = (json.dumps(req) + "\n").encode() if isinstance(req, (dict, list)) else (req if isinstance(req, bytes) else req.encode())
        handle, name = tempfile.mkstemp(dir=self.stdin_dir, prefix="req-")
        os.write(handle, line + extra)
        os.close(handle)
        fd = os.open(name, os.O_RDONLY)
        out = io.StringIO()
        try:
            rc = wp_ctl.main(cfg or self.cfg, list(argv), stdin_fd=fd, out=out, env=self.env if env is None else env, now=self.clock)
        finally:
            os.close(fd)
        text = out.getvalue()
        return rc, (json.loads(text) if text else None)

    def calls(self):
        p = self.shim / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def audit(self):
        return [json.loads(l) for l in self.audit_file.read_text().splitlines()] if self.audit_file.exists() else []

    def jobs(self):
        d = self.state / "jobs"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def tty_seen(self):
        return sorted(p.read_text() for p in self.shim.glob("tty.seen.*"))

    def spec(self, job):
        return json.loads((self.state / "jobs" / job / "spec.json").read_text())

    def run_job(self, job):
        return wp_ctl.main(self.cfg, ["--run-job", job], env=self.env, now=self.clock)

    def assert_nothing_happened(self, label=""):
        self.assertEqual(self.calls(), [], "a refused request ran something: " + label)
        self.assertEqual(self.jobs(), [], "a refused request left a job: " + label)
        self.assertEqual(sorted(p.name for p in self.run.glob("*")), [], "a terminal file was left: " + label)


class Validation(Base):
    def test_unknown_verbs_and_odd_requests_are_refused_and_run_nothing(self):
        for label, req in (("unknown verb", {"verb": "rm"}), ("no verb", {"site": "wptest"}), ("verb not text", {"verb": 5}), ("verb a list", {"verb": ["list"]}),
                           ("not an object", [1, 2]), ("empty line", b"\n"), ("empty input", b""), ("garbage", b"{not json\n"), ("a string", b'"list"\n'),
                           ("duplicate key", b'{"verb":"list","verb":"jobs"}\n'), ("duplicate site", b'{"verb":"show","site":"held","site":"wptest"}\n'), ("verb with a path", {"verb": "../list"})):
            with self.subTest(label):
                rc, reply = self.call(req)
                self.assertEqual(rc, 2, label)
                self.assertFalse(reply["ok"], label)
                self.assert_nothing_happened(label)

    def test_a_request_line_longer_than_the_limit_is_refused(self):
        cfg = dict(self.cfg, max_request_bytes=100)
        rc, reply = self.call({"verb": "show", "site": "wptest", "pad": "x" * 300}, cfg=cfg)
        self.assertEqual((rc, reply["error"]), (2, "request_too_large"))
        self.assert_nothing_happened()

    def test_unknown_arguments_are_refused_for_every_verb(self):
        for verb in wp_ctl.VERBS:
            with self.subTest(verb):
                rc, reply = self.call({"verb": verb, "surprise": 1})
                self.assertEqual((rc, reply["error"]), (2, "unknown_argument"), verb)
        self.assert_nothing_happened()

    def test_hostile_site_names_are_refused_by_every_verb_that_takes_one(self):
        for verb, extra in (("show", {}), ("backup", {}), ("delete", {"confirm_name": "wptest"}), ("password", {"user": "admin"}), ("import-run", {"file": "a.wpress"}),
                            ("import-approve", {}), ("import-reset", {"confirm_name": "wptest"}), ("import-report", {})):
            for site in HOSTILE_SITES:
                with self.subTest(verb=verb, site=site):
                    rc, reply = self.call(dict({"verb": verb, "site": site}, **extra), extra=(CANARY + "\n").encode())
                    self.assertEqual(rc, 2)
                    self.assertFalse(reply["ok"])
        self.assert_nothing_happened()

    def test_wrong_types_are_refused(self):
        for label, req in (("site a number", {"verb": "show", "site": 5}), ("site a list", {"verb": "show", "site": ["wptest"]}), ("site null", {"verb": "show", "site": None}),
                           ("flag as text", {"verb": "backup", "site": "wptest", "force": "true"}), ("flag as number", {"verb": "backup", "site": "wptest", "force": 1}),
                           ("int as text", {"verb": "audit-tail", "lines": "5"}), ("int as bool", {"verb": "audit-tail", "lines": True}), ("int as float", {"verb": "audit-tail", "lines": 5.5}),
                           ("tag a number", {"verb": "backup", "site": "wptest", "tag": 1})):
            with self.subTest(label):
                rc, reply = self.call(req)
                self.assertEqual((rc, reply["error"]), (2, "bad_type"), label)
        self.assert_nothing_happened()

    def test_values_outside_their_set_or_range_are_refused(self):
        for label, req, code in (("tag not in the list", {"verb": "backup", "site": "wptest", "tag": "post-update"}, "bad_value"),
                                 ("lines zero", {"verb": "audit-tail", "lines": 0}, "out_of_range"), ("lines too many", {"verb": "audit-tail", "lines": 201}, "out_of_range"),
                                 ("offset negative", {"verb": "job-log", "job": "20260101T000000Z-abcd1234", "offset": -1}, "out_of_range"),
                                 ("job id odd", {"verb": "job-status", "job": "../../etc"}, "bad_value"), ("job id short", {"verb": "job-log", "job": "x"}, "bad_value"),
                                 ("file not .wpress", {"verb": "import-discard", "file": "a.zip"}, "bad_value"), ("file with a path", {"verb": "import-discard", "file": "../a.wpress"}, "bad_value"),
                                 ("file hidden", {"verb": "import-discard", "file": ".a.wpress"}, "bad_value"), ("file with a space", {"verb": "import-discard", "file": "a b.wpress"}, "bad_value"),
                                 ("reject with a space", {"verb": "import-approve", "site": "wptest", "reject": "a, b"}, "bad_value"), ("reject with a semicolon", {"verb": "import-approve", "site": "wptest", "reject": "a;id"}, "bad_value"),
                                 ("user with a semicolon", {"verb": "password", "site": "wptest", "user": "a;id"}, "bad_value"), ("user too long", {"verb": "password", "site": "wptest", "user": "u" * 61}, "bad_value"),
                                 ("user with a control character", {"verb": "password", "site": "wptest", "user": "ad\x00min"}, "bad_value"), ("user empty", {"verb": "password", "site": "wptest", "user": ""}, "bad_value"),
                                 ("text too long", {"verb": "import-discard", "file": "a" * 300 + ".wpress"}, "bad_value"), ("request id odd", {"verb": "list", "req": "a b"}, "bad_request_id"),
                                 ("request id not text", {"verb": "list", "req": 7}, "bad_request_id")):
            with self.subTest(label):
                rc, reply = self.call(req, extra=(CANARY + "\n").encode())
                self.assertEqual((rc, reply["error"]), (2, code), label)
        self.assert_nothing_happened()

    def test_missing_arguments_and_the_typed_confirmation(self):
        for label, req, code in (("no site", {"verb": "backup"}, "missing_argument"), ("delete without the name", {"verb": "delete", "site": "wptest"}, "missing_argument"),
                                 ("delete with another name", {"verb": "delete", "site": "wptest", "confirm_name": "held"}, "confirm_mismatch"),
                                 ("delete typed once", {"verb": "delete", "site": "wptest", "confirm_name": "wptest"}, "missing_argument"),
                                 ("delete, the second typing is another name", {"verb": "delete", "site": "wptest", "confirm_name": "wptest", "confirm_again": "held"}, "confirm_mismatch"),
                                 ("delete, the second typing in capitals", {"verb": "delete", "site": "wptest", "confirm_name": "wptest", "confirm_again": "WPTEST"}, "bad_value"),
                                 ("delete, only the second typing", {"verb": "delete", "site": "wptest", "confirm_again": "wptest"}, "missing_argument"),
                                 ("delete, name in capitals", {"verb": "delete", "site": "wptest", "confirm_name": "WPTEST"}, "bad_value"),
                                 ("delete, name with a space", {"verb": "delete", "site": "wptest", "confirm_name": "wptest "}, "bad_value"),
                                 ("reset without the name", {"verb": "import-reset", "site": "wptest"}, "missing_argument"),
                                 ("replace without the name", {"verb": "import-run", "site": "wptest", "file": "a.wpress", "replace": True}, "missing_argument"),
                                 ("replace with another name", {"verb": "import-run", "site": "wptest", "file": "a.wpress", "replace": True, "confirm_name": "held"}, "confirm_mismatch"),
                                 ("no file", {"verb": "import-run", "site": "wptest"}, "missing_argument"), ("password without a user", {"verb": "password", "site": "wptest"}, "missing_argument"),
                                 ("check-name without a name", {"verb": "check-name"}, "missing_argument")):
            with self.subTest(label):
                rc, reply = self.call(req, extra=(CANARY + "\n").encode())
                self.assertEqual((rc, reply["error"]), (2, code), label)
        self.assert_nothing_happened()

    def test_a_plain_import_needs_no_typed_name_but_a_wrong_one_is_still_refused(self):
        rc, reply = self.call({"verb": "import-run", "site": "wptest", "file": "a.wpress"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        self.assertEqual(self.spec(reply["job"])["argv"][-2:], ["wptest", "a.wpress"])
        rc, reply = self.call({"verb": "import-run", "site": "wptest", "file": "a.wpress", "confirm_name": "held"})
        self.assertEqual((rc, reply["error"]), (2, "confirm_mismatch"))

    def test_a_refusal_changes_nothing_even_after_a_valid_request(self):
        self.call({"verb": "backup", "site": "wptest"})
        before = self.jobs()
        self.call({"verb": "delete", "site": "wptest", "confirm_name": "held"})
        self.assertEqual(self.jobs(), before)


class Argv(Base):
    def test_the_job_verbs_get_exactly_the_argument_list_the_verb_defines(self):
        b = str(self.bin)
        for label, req, argv, expected_tty in (
                ("backup", {"verb": "backup", "site": "wptest"}, [b + "/wp-site-backup", "wptest"], None),
                ("backup, all options", {"verb": "backup", "site": "wptest", "force": True, "tag": "final"}, [b + "/wp-site-backup", "wptest", "--force", "--tag=final"], None),
                ("delete", {"verb": "delete", "site": "wptest", "confirm_name": "wptest", "confirm_again": "wptest"}, [b + "/wp-site-delete", "wptest"], "wptest\n"),
                ("import", {"verb": "import-run", "site": "wptest", "file": "friend-1.wpress"}, [b + "/wp-site-import", "wptest", "friend-1.wpress"], None),
                ("import, plan", {"verb": "import-run", "site": "wptest", "file": "f.wpress", "plan": True}, [b + "/wp-site-import", "wptest", "f.wpress", "--plan"], None),
                ("import, replace", {"verb": "import-run", "site": "wptest", "file": "f.wpress", "replace": True, "confirm_name": "wptest"},
                 [b + "/wp-site-import", "wptest", "f.wpress", "--replace"], "wptest\n"),
                ("approve", {"verb": "import-approve", "site": "wptest"}, [b + "/wp-site-import", "approve", "wptest"], None),
                ("approve, all options", {"verb": "import-approve", "site": "wptest", "reject": "a,b-c", "accept_failures": True, "confirm_name": "wptest"},
                 [b + "/wp-site-import", "approve", "wptest", "--reject", "a,b-c", "--accept-failures"], None),
                ("rescan", {"verb": "import-rescan", "site": "wptest"}, [b + "/wp-site-import", "rescan", "wptest"], None),
                ("reset", {"verb": "import-reset", "site": "wptest", "confirm_name": "wptest"}, [b + "/wp-site-import", "reset", "wptest"], "wptest\n"),
                ("update, check", {"verb": "update-check", "site": "wptest"}, [b + "/wp-site-update", "wptest", "--check"], None),
                ("update, apply", {"verb": "update-apply", "site": "wptest"}, [b + "/wp-site-update", "wptest", "--apply"], None),
                ("import into a new site", {"verb": "import-new", "name": "newshop", "file": "f.wpress"},
                 [[b + "/wp-site-provision", "create", "newshop"], [b + "/wp-site-import", "newshop", "f.wpress"]], None),
                ("import into a new site, with a label", {"verb": "import-new", "name": "newshop", "file": "f.wpress", "label": "My shop"},
                 [[b + "/wp-site-provision", "create", "newshop", "--set", "label=My shop"], [b + "/wp-site-import", "newshop", "f.wpress"]], None)):
            with self.subTest(label):
                rc, reply = self.call(req)
                self.assertEqual((rc, reply["ok"]), (0, True), reply)
                self.assertEqual(self.spec(reply["job"])["argv"], argv)
                tty = self.state / "jobs" / reply["job"] / "tty"
                self.assertEqual(tty.read_text() if tty.exists() else None, expected_tty, "what the tool will read from its terminal")
        self.assertEqual([c for c in self.calls() if not c.startswith("systemd-run")], [], "starting a job must not run the tool itself")

    def test_the_quick_verbs_get_exactly_the_argument_list_the_verb_defines(self):
        for req, line in (({"verb": "import-discard", "file": "f.wpress"}, "wp-site-import [discard] [f.wpress]"), ({"verb": "import-discard", "file": "f.wpress", "if_present": True}, "wp-site-import [discard] [f.wpress] [--if-present]"),
                          ({"verb": "import-report", "site": "wptest"}, "wp-site-import [report] [wptest]"), ({"verb": "list"}, "wp-sites-status")):
            with self.subTest(req):
                (self.shim / "calls.log").unlink(missing_ok=True)
                rc, reply = self.call(req)
                self.assertEqual(rc, 0, reply)
                self.assertEqual(self.calls(), [line])

    def test_values_that_look_odd_but_are_allowed_reach_the_tool_as_one_argument(self):
        rc, reply = self.call({"verb": "password", "site": "wptest", "user": "first.last@host-1_x"}, extra=(CANARY + "\n").encode())
        self.assertEqual((rc, reply["ok"]), (0, True), reply)
        self.assertEqual(self.calls(), ["wp-site-password [wptest] [first.last@host-1_x]"])

    def test_the_tools_get_a_clean_environment_and_no_terminal_of_their_own(self):
        self.call({"verb": "import-report", "site": "wptest"})
        env = dict(l.split("=", 1) for l in (self.shim / "env.wp-site-import").read_text().splitlines() if "=" in l)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertNotIn("LEAK_ME", env)
        self.assertFalse([k for k in env if k.startswith("SUDO_")], "sudo's variables must not reach the tools")
        self.assertNotIn("WP_SITE_TTY", env, "a verb with no typed answer has no terminal file")

    def test_a_tool_that_fails_is_a_result_not_a_crash(self):
        self.control("rc", 3)
        rc, reply = self.call({"verb": "import-report", "site": "wptest"})
        self.assertEqual((rc, reply["ok"], reply["rc"], reply["ran"]), (1, False, 3, True))
        self.assertIn("ran", reply["out"])

    def test_a_tool_that_is_missing_is_reported(self):
        (self.bin / "wp-site-import").unlink()
        rc, reply = self.call({"verb": "import-report", "site": "wptest"})
        self.assertEqual((rc, reply["rc"]), (1, 127))
        self.assertIn("could not be started", reply["out"])

    def test_a_quick_tool_that_runs_too_long_is_stopped(self):
        self.control("sleep", 30)
        rc, reply = self.call({"verb": "import-report", "site": "wptest"}, cfg=self.limits(**{"import-report": {"timeout_s": 1}}))
        self.assertEqual((rc, reply["rc"]), (1, 124))
        self.assertIn("stopped after 1 s", reply["out"])


class Audit(Base):
    def test_a_change_writes_an_intent_line_then_a_result_line(self):
        rc, reply = self.call({"verb": "backup", "site": "wptest", "force": True, "req": "r-1"})
        lines = self.audit()
        self.assertEqual([l["phase"] for l in lines], ["intent", "started"])
        self.assertEqual(lines[1]["job"], reply["job"])
        self.run_job(reply["job"])
        lines = self.audit()
        self.assertEqual([l["phase"] for l in lines], ["intent", "started", "result"])
        intent, started, result = lines
        for l in lines:
            self.assertEqual((l["verb"], l["site"], l["req"], l["caller"]), ("backup", "wptest", "r-1", "panel"))
            self.assertRegex(l["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(intent["args"], {"force": True})
        self.assertEqual((result["ok"], result["rc"], result["job"]), (True, 0, reply["job"]))

    def test_a_quick_change_writes_both_lines_before_it_returns(self):
        self.call({"verb": "import-discard", "file": "f.wpress"})
        self.assertEqual([l["phase"] for l in self.audit()], ["intent", "result"])

    def test_the_intent_line_exists_before_the_tool_runs(self):
        # the stand-in tool copies the audit file as it finds it
        self.script("wp-site-import", TOOL.replace('echo "$name ran"', 'cp \'%s\' "$D/audit.at.run" 2>/dev/null; echo "$name ran"' % self.audit_file))
        self.call({"verb": "import-discard", "file": "f.wpress"})
        seen = [json.loads(l) for l in (self.shim / "audit.at.run").read_text().splitlines()]
        self.assertEqual([l["phase"] for l in seen], ["intent"], "the record of the attempt must exist before the action")

    def test_the_caller_is_the_panel_account_or_a_named_cli_user(self):
        self.call({"verb": "import-discard", "file": "f.wpress"})
        self.call({"verb": "import-discard", "file": "g.wpress"}, env={"SUDO_USER": "admin"})
        self.call({"verb": "import-discard", "file": "h.wpress"}, env={})
        self.assertEqual([l["caller"] for l in self.audit() if l["phase"] == "intent"], ["panel", "cli:admin", "cli:root"])

    def test_a_refusal_is_recorded_with_its_code_and_the_reads_are_not_recorded_when_they_succeed(self):
        self.call({"verb": "list"})
        self.call({"verb": "jobs"})
        self.assertEqual(self.audit(), [], "a successful read is not audited")
        self.call({"verb": "delete", "site": "wptest", "confirm_name": "held"})
        self.call({"verb": "show", "site": "../x"})
        self.call({"verb": "drop-tables"})
        self.assertEqual([(l["phase"], l["error"]) for l in self.audit()], [("refused", "confirm_mismatch"), ("refused", "unknown_site"), ("refused", "unknown_verb")])
        self.assertEqual(self.audit()[0]["site"], "wptest")
        self.assertNotIn("site", self.audit()[1], "a name that is not even plausible is not copied into the audit")

    def test_nothing_runs_when_the_audit_cannot_be_written(self):
        cfg = dict(self.cfg, audit_file=str(self.tmp / "no-such-dir" / "audit.jsonl"))
        for req in ({"verb": "backup", "site": "wptest"}, {"verb": "import-discard", "file": "f.wpress"}, {"verb": "password", "site": "wptest", "user": "admin"}):
            with self.subTest(req["verb"]):
                rc, reply = self.call(req, cfg=cfg, extra=(CANARY + "\n").encode())
                self.assertEqual((rc, reply["error"]), (2, "audit_unavailable"))
        self.assert_nothing_happened()
        rc, reply = self.call({"verb": "list"}, cfg=cfg)
        self.assertEqual((rc, reply["ok"]), (0, True), "reading does not need the audit")

    def test_the_audit_file_is_only_ever_appended_to(self):
        self.audit_file.write_text('{"old":"line"}\n')
        os_mode = self.audit_file.stat().st_mode
        self.call({"verb": "import-discard", "file": "f.wpress"})
        text = self.audit_file.read_text()
        self.assertTrue(text.startswith('{"old":"line"}\n'), "an existing record must survive")
        self.assertEqual(len(text.splitlines()), 3)
        self.assertEqual(self.audit_file.stat().st_mode, os_mode)

    def test_every_line_also_goes_to_the_journal_and_a_broken_logger_stops_nothing(self):
        self.call({"verb": "import-discard", "file": "f.wpress"})
        journal = (self.shim / "journal").read_text().splitlines()
        self.assertEqual(len(journal), 2)
        self.assertTrue(journal[0].startswith("-t wp-ctl-audit -- {"))
        self.assertEqual(json.loads(journal[0].split(" -- ", 1)[1]), self.audit()[0])
        self.control("logger_rc", 1)
        rc, reply = self.call({"verb": "import-discard", "file": "g.wpress"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        (self.bin / "logger").unlink()
        rc, reply = self.call({"verb": "import-discard", "file": "h.wpress"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        self.assertEqual(len(self.audit()), 6, "the file is the record that must not be lost")

    def test_a_result_line_that_cannot_be_written_is_reported_to_the_caller(self):
        self.script("wp-site-import", TOOL.replace('echo "$name ran"', 'rm -f \'%s\'; mkdir \'%s\'; echo "$name ran"' % (self.audit_file, self.audit_file)))
        rc, reply = self.call({"verb": "import-discard", "file": "f.wpress"})
        self.assertEqual(rc, 0)
        self.assertIn("could not be written", reply["audit"])

    def test_audit_tail_returns_the_last_lines_parsed_and_keeps_garbage_as_raw(self):
        self.audit_file.write_text("".join('{"n":%d}\n' % i for i in range(10)) + "not json\n")
        rc, reply = self.call({"verb": "audit-tail", "lines": 3})
        self.assertEqual(reply["lines"], [{"n": 8}, {"n": 9}, {"raw": "not json"}])
        rc, reply = self.call({"verb": "audit-tail"})
        self.assertEqual(len(reply["lines"]), 11)
        self.audit_file.unlink()
        self.assertEqual(self.call({"verb": "audit-tail"})[1]["lines"], [])


class Password(Base):
    def req(self, **kw):
        return dict({"verb": "password", "site": "wptest", "user": "admin"}, **kw)

    def test_the_password_reaches_the_tool_only_through_its_terminal_file(self):
        rc, reply = self.call(self.req(), extra=(CANARY + "\n").encode())
        self.assertEqual((rc, reply["ok"]), (0, True), reply)
        self.assertEqual(self.tty_seen(), ["%s\n%s\n" % (CANARY, CANARY)], "asked twice, as the tool does")
        self.assertEqual(self.calls(), ["wp-site-password [wptest] [admin]"], "never in an argument")
        self.assertEqual([p.read_text().strip() for p in self.shim.glob("tty.mode.*")], ["600"], "the file holding the password is readable by root only")
        self.assertEqual(sorted(p.name for p in self.run.glob("*")), [], "the terminal file is removed")

    def test_the_password_is_in_no_audit_line_log_journal_job_or_reply(self):
        self.control("leak", 1)
        rc, reply = self.call(self.req(), extra=(CANARY + "\n").encode())
        self.assertNotIn(CANARY, json.dumps(reply))
        self.assertIn("REDACTED", reply["out"], "a tool that echoes it is scrubbed")
        self.call({"verb": "backup", "site": "wptest"})
        for p in self.tmp.rglob("*"):
            if p.is_file() and p.parent != self.stdin_dir and not p.name.startswith("tty.seen."):
                self.assertNotIn(CANARY.encode(), p.read_bytes(), "the password is in " + str(p))

    def test_a_bad_password_is_refused_before_the_tool_and_is_still_recorded(self):
        for label, extra in (("no line", b""), ("empty line", b"\n"), ("too short", b"short\n"), ("13 characters", b"0123456789abc\n"), ("no newline", CANARY.encode()), ("control character", b"abcdefghijklmnop\x07\n"),
                             ("not text", b"\xff\xfe" * 10 + b"\n"), ("too long", b"x" * 300 + b"\n")):
            with self.subTest(label):
                rc, reply = self.call(self.req(), extra=extra)
                self.assertEqual((rc, reply["error"]), (2, "bad_password"), label)
        self.assertEqual(self.calls(), [])
        self.assertEqual([l["error"] for l in self.audit() if l["phase"] == "refused"], ["bad_password"] * 8)
        self.assertEqual(sorted(p.name for p in self.run.glob("*")), [])

    def test_exactly_14_characters_and_spaces_inside_are_accepted(self):
        rc, reply = self.call(self.req(), extra=b"0123456789ab  \n")
        self.assertEqual((rc, reply["ok"]), (0, True))
        self.assertEqual(self.tty_seen(), ["0123456789ab  \n0123456789ab  \n"])

    def test_a_busy_site_refuses_the_password_change(self):
        fd = wp_ctl.take_lock(wp_ctl.Ctl(self.cfg, self.env, self.clock, subprocess.run), "wptest")
        self.addCleanup(os.close, fd)
        rc, reply = self.call(self.req(), extra=(CANARY + "\n").encode())
        self.assertEqual((rc, reply["error"]), (2, "busy"))
        self.assertEqual(self.calls(), [])
        rc, reply = self.call(self.req(site="held"), extra=(CANARY + "\n").encode())
        self.assertTrue(reply["ok"], "another site is not blocked")


class Stream(Base):
    def test_the_bytes_after_the_request_reach_the_tool_whole(self):
        blob = bytes(range(256)) * 1000 + b"\n\n\x00end\r\n" + os.urandom(5000)
        rc, reply = self.call({"verb": "import-receive", "file": "friend.wpress"}, extra=blob)
        self.assertEqual((rc, reply["ok"]), (0, True), reply)
        self.assertEqual((self.shim / "received.bin").read_bytes(), blob)
        self.assertEqual(self.calls(), ["wp-site-import [receive] [friend.wpress]"])
        self.assertEqual([l["phase"] for l in self.audit()], ["intent", "result"])

    def test_an_empty_stream_and_a_stream_that_starts_with_a_newline_are_not_confused_with_the_request(self):
        rc, reply = self.call({"verb": "import-receive", "file": "a.wpress"}, extra=b"\n\nX")
        self.assertEqual((self.shim / "received.bin").read_bytes(), b"\n\nX")
        rc, reply = self.call({"verb": "import-receive", "file": "b.wpress"})
        self.assertEqual((self.shim / "received.bin").read_bytes(), b"")

    def test_the_file_name_is_checked_before_a_single_byte_is_passed(self):
        rc, reply = self.call({"verb": "import-receive", "file": "../../etc/passwd"}, extra=b"data")
        self.assertEqual((rc, reply["error"]), (2, "bad_value"))
        self.assert_nothing_happened()


class Jobs(Base):
    def start(self, **kw):
        rc, reply = self.call(dict({"verb": "delete", "site": "wptest", "confirm_name": "wptest", "confirm_again": "wptest"}, **kw))
        self.assertEqual((rc, reply["ok"]), (0, True), reply)
        return reply["job"]

    def test_starting_a_job_writes_its_request_and_starts_one_unit_and_runs_nothing(self):
        job = self.start()
        self.assertRegex(job, r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
        self.assertEqual(self.calls(), ["systemd-run [--unit=wpjob-%s] [--collect] [--quiet] [--description=wp-ctl job %s (delete wptest)] [--property=MemoryMax=384M] [--property=MemorySwapMax=0] "
                                       "[--property=TasksMax=256] [--property=RuntimeMaxSec=900] [%s/wp-ctl] [--run-job] [%s]" % (job, job, self.bin, job)])
        spec = self.spec(job)
        self.assertEqual((spec["verb"], spec["site"], spec["caller"]), ("delete", "wptest", "panel"))
        for p in ((self.state / "jobs" / job), (self.state / "jobs" / job / "spec.json"), (self.state / "jobs" / job / "tty")):
            self.assertEqual(p.stat().st_mode & 0o077, 0, "%s must be root-only" % p)
        self.assertEqual((self.state / "jobs" / job / "tty").read_text(), "wptest\n")

    def test_a_delete_needs_the_name_typed_twice_and_the_tool_is_answered_with_the_second_typing(self):
        rc, reply = self.call({"verb": "delete", "site": "wptest", "confirm_name": "wptest", "confirm_again": "wptest"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        self.assertEqual((self.state / "jobs" / reply["job"] / "tty").read_text(), "wptest\n")
        self.assertEqual(self.audit()[0]["args"], {"confirm_again": "wptest", "confirm_name": "wptest"}, "both typings are in the record of the attempt")
        # an import's reset and replace ask once, as their tools do; only a delete asks twice
        rc, reply = self.call({"verb": "import-reset", "site": "held", "confirm_name": "held"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        rc, reply = self.call({"verb": "import-reset", "site": "wptest", "confirm_name": "wptest", "confirm_again": "wptest"})
        self.assertEqual((rc, reply["error"]), (2, "unknown_argument"))

    def test_running_a_job_keeps_its_log_and_exit_code_and_removes_the_typed_answer(self):
        job = self.start()
        self.assertEqual(self.call({"verb": "job-status", "job": job})[1]["state"], "running")
        self.assertEqual(self.run_job(job), 0)
        jd = self.state / "jobs" / job
        self.assertEqual((jd / "log").read_text(), "wp-site-delete ran\n")
        self.assertEqual((jd / "exit").read_text(), "0\n")
        self.assertFalse((jd / "tty").exists(), "the typed answer is removed once the tool has run")
        self.assertEqual(self.tty_seen(), ["wptest\n"])
        self.assertEqual(self.calls()[-1], "wp-site-delete [wptest]")
        self.assertEqual(self.call({"verb": "job-status", "job": job})[1], {"ok": True, "job": job, "verb": "delete", "site": "wptest", "state": "done", "rc": 0}, "the status names what the job is for, so the page can say what finished")
        result = [l for l in self.audit() if l["phase"] == "result"][0]
        self.assertEqual((result["verb"], result["site"], result["job"], result["ok"], result["rc"], result["caller"]), ("delete", "wptest", job, True, 0, "panel"))

    def test_a_failing_tool_leaves_its_exit_code(self):
        job = self.start()
        self.control("rc", 3)
        self.run_job(job)
        self.assertEqual(self.call({"verb": "job-status", "job": job})[1], {"ok": True, "job": job, "verb": "delete", "site": "wptest", "state": "done", "rc": 3})
        self.assertEqual([l["ok"] for l in self.audit() if l["phase"] == "result"], [False])

    def test_a_job_runs_once_whatever_restarts_the_unit(self):
        job = self.start()
        self.run_job(job)
        n = len(self.calls())
        self.assertEqual(self.run_job(job), 0)
        self.assertEqual(len(self.calls()), n, "the tool must not run a second time")

    def test_a_finished_job_releases_its_site(self):
        job = self.start()
        self.run_job(job)
        rc, reply = self.call({"verb": "backup", "site": "wptest"})
        self.assertEqual((rc, reply["ok"]), (0, True), "the lock must not outlive the job")
        probe = wp_ctl.take_lock(wp_ctl.Ctl(self.cfg, self.env, self.clock, subprocess.run), "wptest")
        self.assertIsNotNone(probe)
        os.close(probe)

    def test_the_job_log_can_be_read_in_pieces(self):
        job = self.start()
        self.run_job(job)
        rc, a = self.call({"verb": "job-log", "job": job})
        self.assertEqual((a["data"], a["offset"], a["state"]), ("wp-site-delete ran\n", 19, "done"))
        rc, b = self.call({"verb": "job-log", "job": job, "offset": 8})
        self.assertEqual((b["data"], b["offset"]), ("delete ran\n", 19))
        rc, c = self.call({"verb": "job-log", "job": job, "offset": 19})
        self.assertEqual((c["data"], c["offset"]), ("", 19))

    def test_a_site_that_is_busy_refuses_a_second_job_and_a_job_that_finds_it_locked_does_not_run(self):
        job = self.start()
        fd = wp_ctl.take_lock(wp_ctl.Ctl(self.cfg, self.env, self.clock, subprocess.run), "wptest")
        rc, reply = self.call({"verb": "backup", "site": "wptest"})
        self.assertEqual((rc, reply["error"]), (2, "busy"))
        self.assertEqual(self.jobs(), [job], "the refused job left no directory")
        self.run_job(job)
        os.close(fd)
        self.assertEqual(self.call({"verb": "job-status", "job": job})[1]["rc"], wp_ctl.BUSY)
        self.assertNotIn("wp-site-delete [wptest]", self.calls())
        self.assertIn("another verb holds this site", (self.state / "jobs" / job / "log").read_text())
        rc, reply = self.call({"verb": "backup", "site": "held"})
        self.assertTrue(reply["ok"], "another site is not blocked")

    def test_a_unit_that_cannot_be_started_leaves_nothing_behind(self):
        self.control("sd_rc", 1)
        rc, reply = self.call({"verb": "delete", "site": "wptest", "confirm_name": "wptest", "confirm_again": "wptest"})
        self.assertEqual((rc, reply["error"]), (2, "job_not_started"))
        self.assertEqual(self.jobs(), [])
        self.assertEqual([l["phase"] for l in self.audit()], ["intent", "refused"])

    def test_a_job_that_runs_too_long_is_stopped_and_says_so(self):
        cfg = self.limits(delete={"timeout_s": 1})
        rc, reply = self.call({"verb": "delete", "site": "wptest", "confirm_name": "wptest", "confirm_again": "wptest"}, cfg=cfg)
        job = reply["job"]
        self.assertEqual(self.spec(job)["timeout_s"], 1, "the limit is written into the job when it starts")
        self.control("sleep", 30)
        self.run_job(job)
        self.assertEqual(self.call({"verb": "job-status", "job": job})[1]["rc"], 124)
        self.assertIn("stopped after 1 s", (self.state / "jobs" / job / "log").read_text())

    def test_a_job_without_an_exit_code_past_its_time_is_lost_not_running_for_ever(self):
        job = self.start()
        self.t += self.spec(job)["timeout_s"] + wp_ctl.LOST_GRACE_S - 5
        self.assertEqual(self.call({"verb": "job-status", "job": job})[1]["state"], "running", "inside the time limit and the unit's grace the job may still be finishing")
        self.t += 10
        self.assertEqual(self.call({"verb": "job-status", "job": job})[1]["state"], "lost")

    def test_odd_run_job_arguments_run_nothing(self):
        for argv in ([], ["x"], ["../../etc"], ["20260101T000000Z-abcd1234"], ["a", "b"]):
            with self.subTest(argv):
                self.assertEqual(wp_ctl.main(self.cfg, ["--run-job"] + argv, env=self.env, now=self.clock), 2)
        self.assertEqual(self.calls(), [])

    def test_unknown_jobs_are_reported(self):
        rc, reply = self.call({"verb": "job-status", "job": "20260101T000000Z-abcd1234"})
        self.assertEqual((rc, reply["error"]), (2, "unknown_job"))

    def test_the_job_list_is_newest_first_and_shows_the_state(self):
        a = self.start()
        self.t += 5
        b = self.call({"verb": "backup", "site": "held"})[1]["job"]
        self.run_job(a)
        rc, reply = self.call({"verb": "jobs"})
        self.assertEqual([(j["job"], j["verb"], j["site"], j["state"]) for j in reply["jobs"]], [(b, "backup", "held", "running"), (a, "delete", "wptest", "done")])

    def test_old_job_directories_are_removed_and_nothing_else_is(self):
        job = self.start()
        old = self.state / "jobs" / "20200101T000000Z-aaaaaaaa"
        old.mkdir()
        (old / "log").write_text("x")
        os.utime(old, (self.t - 15 * 86400, self.t - 15 * 86400))
        fresh = self.state / "jobs" / "20200101T000000Z-bbbbbbbb"
        fresh.mkdir()
        os.utime(fresh, (self.t - 13 * 86400, self.t - 13 * 86400))
        stray = self.state / "jobs" / "not-a-job"
        stray.mkdir()
        os.utime(stray, (self.t - 99 * 86400, self.t - 99 * 86400))
        target = self.tmp / "precious"
        target.mkdir()
        (target / "keep").write_text("k")
        link = self.state / "jobs" / "20200101T000000Z-cccccccc"
        link.symlink_to(target)
        self.call({"verb": "list"})
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists() and stray.exists() and (self.state / "jobs" / job).exists())
        self.assertTrue((target / "keep").exists(), "a link named like a job must not be followed")


class Idempotency(Base):
    def job(self, req=None, **kw):
        body = dict({"verb": "delete", "site": "wptest", "confirm_name": "wptest", "confirm_again": "wptest"}, **kw)
        if req:
            body["req"] = req
        return self.call(body)

    def systemd_runs(self):
        return [c for c in self.calls() if c.startswith("systemd-run")]

    def test_the_same_request_returns_the_job_it_started(self):
        rc, first = self.job("click-1")
        rc, again = self.job("click-1")
        self.assertEqual((rc, again["ok"], again["job"], again.get("duplicate")), (0, True, first["job"], True))
        self.assertNotIn("duplicate", first)
        self.assertEqual(self.jobs(), [first["job"]])
        self.assertEqual(len(self.systemd_runs()), 1, "one unit, however many times the button was pressed")
        self.assertEqual([l["phase"] for l in self.audit()], ["intent", "started", "intent", "duplicate"])
        self.assertEqual(self.audit()[-1]["job"], first["job"])

    def test_a_repeat_while_the_job_is_running_is_the_same_job_not_a_busy_refusal(self):
        rc, first = self.job("click-2")
        fd = wp_ctl.take_lock(wp_ctl.Ctl(self.cfg, self.env, self.clock, subprocess.run), "wptest")
        self.addCleanup(os.close, fd)
        rc, again = self.job("click-2")
        self.assertEqual((rc, again["job"], again["duplicate"]), (0, first["job"], True))
        rc, other = self.job("click-3")
        self.assertEqual((rc, other["error"]), (2, "busy"), "a different request still finds the site busy")

    def test_a_repeat_after_the_job_finished_is_still_the_same_job(self):
        rc, first = self.job("click-4")
        self.run_job(first["job"])
        rc, again = self.job("click-4")
        self.assertEqual((again["job"], again["duplicate"]), (first["job"], True))
        self.assertEqual(self.call({"verb": "job-status", "job": again["job"]})[1]["rc"], 0)

    def test_the_same_id_for_anything_else_is_refused_and_starts_nothing(self):
        rc, first = self.job("click-5")
        for label, req in (("another verb", {"verb": "backup", "site": "wptest", "req": "click-5"}), ("another site", {"verb": "delete", "site": "held", "confirm_name": "held", "confirm_again": "held", "req": "click-5"}),
                           ("other arguments", {"verb": "backup", "site": "wptest", "force": True, "req": "click-5"})):
            with self.subTest(label):
                rc, reply = self.call(req)
                self.assertEqual((rc, reply["error"]), (2, "req_reused"), label)
        self.assertEqual(self.jobs(), [first["job"]])
        self.assertEqual(len(self.systemd_runs()), 1)
        self.assertEqual([l["error"] for l in self.audit() if l["phase"] == "refused"], ["req_reused"] * 3)

    def test_a_request_without_an_id_is_never_a_duplicate_and_different_ids_are_different_jobs(self):
        rc, a = self.call({"verb": "backup", "site": "held"})
        self.run_job(a["job"])
        rc, b = self.call({"verb": "backup", "site": "held"})
        self.assertNotEqual(a["job"], b["job"])
        self.assertNotIn("duplicate", b)
        self.run_job(b["job"])
        self.t += 2
        rc, c = self.call({"verb": "backup", "site": "held", "req": "x-1"})
        self.run_job(c["job"])
        self.t += 2
        rc, d = self.call({"verb": "backup", "site": "held", "req": "x-2"})
        self.assertNotEqual(c["job"], d["job"])
        self.assertEqual(len(self.jobs()), 4)

    def test_a_request_id_is_not_remembered_for_quick_verbs(self):
        for _ in range(2):
            rc, reply = self.call({"verb": "import-discard", "file": "f.wpress", "req": "same-id"})
            self.assertEqual((rc, reply["ok"]), (0, True))
        self.assertEqual([c for c in self.calls() if c.startswith("wp-site-import")].__len__(), 2, "a quick verb runs every time: it has no job to return")

    def test_simultaneous_identical_requests_start_one_job(self):
        self.clock_delay = 0.05
        results, errors = [], []

        def go():
            try:
                results.append(self.job("race-1"))
            except Exception as e:      # a failure of the harness itself must not pass as a pass
                errors.append(e)

        threads = [threading.Thread(target=go) for _ in range(8)]
        [th.start() for th in threads]
        [th.join(30) for th in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(r[1]["job"] for r in results)), 1, "one job id for all of them")
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(len(self.systemd_runs()), 1)
        self.assertEqual(sorted(bool(r[1].get("duplicate")) for r in results), [False] + [True] * 7)


class Limits(Base):
    def test_each_job_unit_gets_the_memory_process_and_time_limits_of_its_verb(self):
        for req, props in (({"verb": "backup", "site": "wptest"}, "[--property=MemoryMax=512M] [--property=MemorySwapMax=0] [--property=TasksMax=256] [--property=RuntimeMaxSec=900]"),
                           ({"verb": "delete", "site": "held", "confirm_name": "held", "confirm_again": "held"}, "[--property=MemoryMax=384M] [--property=MemorySwapMax=0] [--property=TasksMax=256] [--property=RuntimeMaxSec=900]"),
                           ({"verb": "import-run", "site": "wptest", "file": "a.wpress"}, "[--property=MemoryMax=1024M] [--property=MemorySwapMax=0] [--property=TasksMax=256] [--property=RuntimeMaxSec=1200]"),
                           ({"verb": "import-approve", "site": "held"}, "[--property=MemoryMax=1024M] [--property=MemorySwapMax=0] [--property=TasksMax=256] [--property=RuntimeMaxSec=1000]"),
                           ({"verb": "import-reset", "site": "wptest", "confirm_name": "wptest"}, "[--property=MemoryMax=1024M] [--property=MemorySwapMax=0] [--property=TasksMax=64] [--property=RuntimeMaxSec=1100]")):
            with self.subTest(req["verb"]):
                (self.shim / "calls.log").unlink(missing_ok=True)
                rc, reply = self.call(req)
                self.assertEqual(rc, 0, reply)
                self.assertIn(" " + props + " ", self.calls()[0], req["verb"])
                self.assertEqual(self.spec(reply["job"])["timeout_s"], wp_ctl.Ctl(self.cfg, {}, self.clock, None).limit(req["verb"], "timeout_s"))
                self.run_job(reply["job"])          # one job at a time on a site

    def test_a_quick_verb_uses_the_default_time_limit_unless_it_has_its_own(self):
        ctl = wp_ctl.Ctl(self.cfg, {}, self.clock, None)
        self.assertEqual((ctl.limit("list", "timeout_s"), ctl.limit("password", "timeout_s"), ctl.limit("import-receive", "timeout_s")), (30, 30, 60))
        self.assertEqual((ctl.limit("delete", "memory_mb"), ctl.limit("delete", "tasks"), ctl.limit("nosuch", "memory_mb")), (384, 256, 512))
        self.control("sleep", 30)
        rc, reply = self.call({"verb": "list"}, cfg=self.limits(default={"timeout_s": 1, "memory_mb": 512, "tasks": 256}))
        self.assertEqual((rc, reply["error"]), (2, "tool_failed"))
        self.assertIn("stopped after 1 s", reply["detail"])

    def test_the_upload_has_its_own_longer_limit(self):
        self.control("sleep", 30)
        rc, reply = self.call({"verb": "import-receive", "file": "a.wpress"}, extra=b"x", cfg=self.limits(**{"import-receive": {"timeout_s": 1}}))
        self.assertEqual((rc, reply["rc"]), (1, 124))

    def test_a_bad_limits_table_stops_everything_loudly(self):
        base = dict(LIMITS)
        for label, limits in (("no default", {k: v for k, v in base.items() if k != "default"}), ("default without tasks", dict(base, default={"timeout_s": 30, "memory_mb": 512})),
                              ("default with a stray key", dict(base, default={"timeout_s": 30, "memory_mb": 512, "tasks": 256, "cpu": 1})),
                              ("a verb that does not exist", dict(base, **{"backups": {"timeout_s": 5}})), ("a stray key", dict(base, backup={"timeout_s": 5, "ram": 5})),
                              ("zero", dict(base, backup={"timeout_s": 0})), ("negative", dict(base, backup={"timeout_s": -5})), ("a bool", dict(base, backup={"timeout_s": True})),
                              ("a number as text", dict(base, backup={"timeout_s": "600"})), ("a float", dict(base, backup={"timeout_s": 5.5})), ("not a table", dict(base, backup=600)),
                              ("a job without a timeout of its own", {k: v for k, v in base.items() if k != "import-run"}), ("the upload without a timeout of its own", {k: v for k, v in base.items() if k != "import-receive"}),
                              ("a job with other keys but no timeout", dict(base, delete={"memory_mb": 384})), ("limits missing", None), ("limits not a table", [1, 2])):
            with self.subTest(label):
                cfg = dict(self.cfg, limits=limits)
                if limits is None:
                    del cfg["limits"]
                for req in ({"verb": "list"}, {"verb": "backup", "site": "wptest"}, {"verb": "audit-tail"}):
                    rc, reply = self.call(req, cfg=cfg)
                    self.assertEqual((rc, reply["error"]), (2, "bad_config"), label)
        self.assert_nothing_happened()
        self.assertEqual(self.audit(), [])

    def test_a_job_started_before_the_table_went_bad_still_runs_with_what_it_was_given(self):
        rc, reply = self.call({"verb": "backup", "site": "wptest"})
        self.assertEqual(self.spec(reply["job"])["timeout_s"], 600)
        rc = wp_ctl.main(dict(self.cfg, limits={"default": {}}), ["--run-job", reply["job"]], out=io.StringIO(), env=self.env, now=self.clock)
        self.assertEqual(rc, 0)
        self.assertEqual(self.call({"verb": "job-status", "job": reply["job"]}, cfg=self.cfg)[1]["rc"], 0)
        self.assertIn("wp-site-backup [wptest]", self.calls())


class Intake(Base):
    """import-list, vuln-check, the update verbs, the approval's typed name and the job of several steps (import-new)."""

    LISTING = {"files": [{"name": "friend.wpress", "bytes": 5, "mtime": 1, "site": "held"}], "imports": [{"site": "held", "file": "friend.wpress", "stage": "reported", "problems": ["7 vulnerabilities"]}]}

    def test_import_list_passes_the_tools_one_json_line_on(self):
        self.control("stdout.wp-site-import", json.dumps(self.LISTING) + "\n")
        rc, reply = self.call({"verb": "import-list"})
        self.assertEqual((rc, reply), (0, dict(self.LISTING, ok=True)))
        self.assertEqual(self.calls(), ["wp-site-import [list]"])
        self.assertEqual(self.audit(), [], "a read is not audited")

    def test_import_list_refuses_what_is_not_the_tools_one_line(self):
        for text, rc_ in (("not json\n", 0), ("", 0), ("[1]\n", 0), (json.dumps({"files": 1, "imports": []}) + "\n", 0), (json.dumps({"files": [], "imports": "x"}) + "\n", 0), (json.dumps({"files": []}) + "\n", 0), (json.dumps(self.LISTING) + "\n", 3)):
            with self.subTest(text=text[:20], rc=rc_):
                self.control("stdout.wp-site-import", text)
                self.control("rc", rc_)
                rc, reply = self.call({"verb": "import-list"})
                self.assertEqual((rc, reply["ok"], reply["error"]), (2, False, "tool_output"))
        self.assertEqual(self.jobs(), [])

    def test_import_list_takes_no_arguments(self):
        for extra in ({"site": "wptest"}, {"file": "a.wpress"}):
            rc, reply = self.call(dict({"verb": "import-list"}, **extra))
            self.assertEqual((rc, reply["error"]), (2, "unknown_argument"))

    def test_vuln_check_returns_the_answer_whether_something_was_found_or_not_and_fails_only_on_a_real_error(self):
        self.control("stdout.wp-vulnfeed", "core wordpress 7.1.3: 7 findings\n")
        for rc_, ok in ((0, True), (1, True), (3, True), (2, False), (124, False), (127, False)):
            with self.subTest(rc=rc_):
                self.control("rc", rc_)
                rc, reply = self.call({"verb": "vuln-check", "site": "wptest"})
                self.assertEqual((reply["ok"], reply["rc"]), (ok, rc_))
                self.assertEqual(rc, 0 if ok else 1)
                self.assertIn("7 findings", reply["out"])
        self.assertEqual(set(self.calls()), {"wp-vulnfeed [check] [wptest]"})

    USERS = [{"user_login": "admin", "roles": "administrator"}, {"user_login": "ed.itor@x", "roles": "editor,author"}]

    def test_users_lists_logins_and_roles_from_the_tools_json_and_audits_nothing(self):
        self.control("stdout.wp-site-cli", "Notice: something\n" + json.dumps(self.USERS) + "\n")
        rc, reply = self.call({"verb": "users", "site": "wptest"})
        self.assertEqual((rc, reply), (0, {"ok": True, "site": "wptest", "users": [{"user": "admin", "roles": "administrator"}, {"user": "ed.itor@x", "roles": "editor,author"}]}))
        self.assertEqual(self.calls(), ["wp-site-cli [wptest] [user] [list] [--fields=user_login,roles] [--format=json]"])
        self.assertEqual(self.audit(), [], "a read is not audited")

    def test_users_drops_rows_that_are_not_plausible_users_and_refuses_what_is_not_the_tools_json(self):
        junk = [{"user_login": "a b", "roles": "x"}, {"user_login": "../x"}, {"roles": "x"}, "str", {"user_login": "ok", "roles": ["nope"]}]
        self.control("stdout.wp-site-cli", json.dumps(junk) + "\n")
        rc, reply = self.call({"verb": "users", "site": "wptest"})
        self.assertEqual(reply["users"], [{"user": "ok", "roles": ""}])
        for text, rc_ in (("not json\n", 0), ("", 0), ("{}\n", 0), (json.dumps(self.USERS) + "\n", 1), ("Error: wptest is stopped\n", 1)):
            with self.subTest(text=text[:20], rc=rc_):
                self.control("stdout.wp-site-cli", text)
                self.control("rc", rc_)
                rc, reply = self.call({"verb": "users", "site": "wptest"})
                self.assertEqual((rc, reply["ok"], reply["error"]), (2, False, "tool_output"))

    def test_users_needs_an_existing_site(self):
        for site in HOSTILE_SITES:
            rc, reply = self.call({"verb": "users", "site": site})
            self.assertEqual(rc, 2)
        self.assert_nothing_happened()

    def test_vuln_check_needs_an_existing_site(self):
        for site in HOSTILE_SITES:
            rc, reply = self.call({"verb": "vuln-check", "site": site})
            self.assertEqual(rc, 2)
        self.assert_nothing_happened()

    def test_the_update_verbs_are_jobs_on_an_existing_site_only(self):
        for verb in ("update-check", "update-apply"):
            for site in HOSTILE_SITES:
                rc, reply = self.call({"verb": verb, "site": site})
                self.assertEqual(rc, 2, (verb, site))
            rc, reply = self.call({"verb": verb, "site": "wptest"})
            self.assertEqual((rc, reply["ok"], "job" in reply), (0, True, True))
            self.assertEqual(self.spec(reply["job"])["verb"], verb)
        self.assertEqual(len(self.jobs()), 2)

    def test_the_update_job_runs_the_tool_and_keeps_its_exit_code(self):
        rc, reply = self.call({"verb": "update-apply", "site": "wptest"})
        self.control("rc", 3)
        self.run_job(reply["job"])
        self.assertEqual(self.call({"verb": "job-status", "job": reply["job"]})[1]["rc"], 3)
        self.assertIn("wp-site-update [wptest] [--apply]", self.calls())

    def test_accepting_the_failures_of_an_import_needs_the_name_typed_and_nothing_else_does(self):
        for req in ({"verb": "import-approve", "site": "wptest", "accept_failures": True}, {"verb": "import-approve", "site": "wptest", "accept_failures": True, "confirm_name": "held"},
                    {"verb": "import-approve", "site": "wptest", "accept_failures": True, "confirm_name": "WPTEST"}, {"verb": "import-approve", "site": "wptest", "accept_failures": True, "confirm_name": ""}):
            rc, reply = self.call(req)
            self.assertEqual(rc, 2, req)
            self.assertIn(reply["error"], ("missing_argument", "confirm_mismatch", "bad_value"), req)
        self.assert_nothing_happened()
        rc, reply = self.call({"verb": "import-approve", "site": "wptest", "accept_failures": True, "confirm_name": "wptest"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        rc, reply = self.call({"verb": "import-approve", "site": "held"})
        self.assertEqual((rc, reply["ok"]), (0, True), "a clean approval asks for no name")

    def test_import_new_refuses_a_bad_taken_or_reserved_name_before_anything_starts(self):
        for name, code in (("Bad_Name", "bad_value"), ("a", "bad_value"), ("hub", "reserved_name"), ("wptest", "name_taken"), ("held", "name_taken"), ("-x", "bad_value")):
            rc, reply = self.call({"verb": "import-new", "name": name, "file": "f.wpress"})
            self.assertEqual(rc, 2, name)
            self.assertIn(reply["error"], (code, "invalid_name"), name)
        self.assert_nothing_happened()

    def test_import_new_is_one_job_whose_two_steps_run_in_order_and_whose_log_names_each(self):
        rc, reply = self.call({"verb": "import-new", "name": "newshop", "file": "f.wpress", "label": "New shop", "req": "pn1"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        job = reply["job"]
        self.assertEqual(self.run_job(job), 0)
        self.assertEqual([c for c in self.calls() if not c.startswith("systemd-run")], ["wp-site-provision [create] [newshop] [--set] [label=New shop]", "wp-site-import [newshop] [f.wpress]"])
        log = (self.state / "jobs" / job / "log").read_text()
        self.assertLess(log.index("== step 1 of 2: wp-site-provision create newshop"), log.index("== step 2 of 2: wp-site-import newshop f.wpress"))
        self.assertEqual((self.state / "jobs" / job / "exit").read_text(), "0\n")
        self.assertEqual(self.call({"verb": "job-status", "job": job})[1]["rc"], 0)
        result = [l for l in self.audit() if l["phase"] == "result"][0]
        self.assertEqual((result["verb"], result["site"], result["rc"], result["caller"]), ("import-new", "newshop", 0, "panel"))

    def test_the_first_step_that_fails_ends_the_job_with_its_status_and_the_next_does_not_run(self):
        rc, reply = self.call({"verb": "import-new", "name": "newshop", "file": "f.wpress"})
        self.control("rc", 2)
        self.run_job(reply["job"])
        self.assertEqual([c for c in self.calls() if not c.startswith("systemd-run")], ["wp-site-provision [create] [newshop]"])
        self.assertEqual((self.state / "jobs" / reply["job"] / "exit").read_text(), "2\n")
        self.assertIn("step 1 of 2 ended with status 2; the next step was not run", (self.state / "jobs" / reply["job"] / "log").read_text())

    def test_a_failing_second_step_leaves_its_status_too(self):
        rc, reply = self.call({"verb": "import-new", "name": "newshop", "file": "f.wpress"})
        (self.bin / "wp-site-import").write_text("#!/bin/bash\necho second step failed\nexit 7\n")
        (self.bin / "wp-site-import").chmod(0o755)
        self.run_job(reply["job"])
        self.assertEqual((self.state / "jobs" / reply["job"] / "exit").read_text(), "7\n")
        self.assertIn("second step failed", (self.state / "jobs" / reply["job"] / "log").read_text())

    def test_a_chain_shares_one_time_limit_and_a_slow_step_is_stopped(self):
        cfg = self.limits(**{"import-new": {"timeout_s": 2, "memory_mb": 1024}})
        rc, reply = self.call({"verb": "import-new", "name": "newshop", "file": "f.wpress"}, cfg=cfg)
        self.control("sleep", 1.3)                    # each tool takes 1.3 s: either alone fits in the 2 s of the job, the two together do not
        t0 = time.time()
        wp_ctl.main(cfg, ["--run-job", reply["job"]], env=self.env, now=self.clock)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual((self.state / "jobs" / reply["job"] / "exit").read_text(), "124\n", "the second step had only what the first left")
        self.assertEqual([c for c in self.calls() if not c.startswith("systemd-run")], ["wp-site-provision [create] [newshop]", "wp-site-import [newshop] [f.wpress]"])

    def test_the_same_request_again_is_the_same_job_and_the_site_it_makes_is_locked_meanwhile(self):
        req = {"verb": "import-new", "name": "newshop", "file": "f.wpress", "req": "pn2"}
        rc, first = self.call(req)
        rc, again = self.call(req)
        self.assertEqual((again["job"], again.get("duplicate")), (first["job"], True))
        self.assertEqual(len(self.jobs()), 1)
        rc, other = self.call({"verb": "import-new", "name": "newshop", "file": "g.wpress", "req": "pn2"})
        self.assertEqual((rc, other["error"]), (2, "req_reused"))


PLUGIN_LIST = [{"name": "woocommerce", "title": "WooCommerce", "status": "active", "update": "none", "version": "9.3.3"},
               {"name": "old-thing", "title": "Old Thing", "status": "inactive", "update": "available", "version": "1.0"},
               {"name": "Odd_Name", "title": "Odd", "status": "inactive", "update": "none", "version": "2"},
               {"name": "hosting-mail", "title": "Hosting mail", "status": "must-use", "update": "none", "version": "1"}]
# a stand-in for wp-site-cli that lists plugins, activates and deactivates; a deactivation that loads the other plugins fails while the file `plugin_fails` exists
CLI = """#!/bin/bash
D='%(dir)s'
{ printf 'wp-site-cli'; for a in "$@"; do printf ' [%%s]' "$a"; done; printf '\\n'; } >> "$D/calls.log"
case " $* " in
  *" plugin list "*) cat "$D/plugin_list"; exit "$(cat "$D/list_rc" 2>/dev/null || echo 0)" ;;
  *" plugin deactivate "*) if [ -f "$D/deactivate_needs_skip" ] && [[ " $* " != *" --skip-plugins "* ]]; then echo "PHP Fatal error in a plugin"; exit 1; fi ;;
esac
[ -f "$D/action_rc" ] && { echo "tool failed"; exit "$(cat "$D/action_rc")"; }
echo "Success: done."
"""


class Plugins(Base):
    def setUp(self):
        super().setUp()
        self.script("wp-site-cli", CLI)
        self.control("plugin_list", "Notice: a plugin said something\n" + json.dumps(PLUGIN_LIST) + "\n")
        self.cache = self.tmp / "pagecache"
        (self.cache / "a" / "bc").mkdir(parents=True)
        for f in ("a/bc/one", "a/two", "three"):
            (self.cache / f).write_text("page")

    def cache_files(self):
        return sorted(str(p.relative_to(self.cache)) for p in self.cache.rglob("*") if p.is_file())

    def test_plugins_lists_the_rows_the_tool_reports_with_no_plugin_code_and_audits_nothing(self):
        rc, reply = self.call({"verb": "plugins", "site": "wptest"})
        self.assertEqual(rc, 0)
        self.assertEqual([(r["name"], r["status"], r["update"]) for r in reply["plugins"]], [("woocommerce", "active", False), ("old-thing", "inactive", True), ("Odd_Name", "inactive", False), ("hosting-mail", "must-use", False)])
        self.assertEqual(self.calls(), ["wp-site-cli [wptest] [plugin] [list] [--fields=name,title,status,update,version] [--format=json] [--skip-plugins] [--skip-themes]"])
        self.assertEqual(self.audit(), [], "a read is not audited")

    def test_plugins_drops_odd_rows_and_refuses_what_is_not_the_tools_json(self):
        junk = [{"name": "../x", "status": "active"}, {"name": "ok", "status": "weird"}, {"status": "active"}, "str", {"name": "fine", "status": "active", "title": "A\x1bB", "version": 5}]
        self.control("plugin_list", json.dumps(junk) + "\n")
        rc, reply = self.call({"verb": "plugins", "site": "wptest"})
        self.assertEqual(reply["plugins"], [{"name": "fine", "title": "A B", "status": "active", "version": "", "update": False}])
        for text, rc_ in (("not json\n", 0), ("{}\n", 0), (json.dumps(PLUGIN_LIST) + "\n", 1), ("Error: stopped\n", 1)):
            with self.subTest(text=text[:12], rc=rc_):
                self.control("plugin_list", text)
                self.control("list_rc", rc_)
                rc, reply = self.call({"verb": "plugins", "site": "wptest"})
                self.assertEqual((rc, reply["error"]), (2, "tool_output"))

    def test_plugins_needs_an_existing_site(self):
        for site in HOSTILE_SITES:
            self.assertEqual(self.call({"verb": "plugins", "site": site})[0], 2, site)
        self.assert_nothing_happened()

    def test_activate_runs_the_tool_as_the_web_apps_identity_audits_and_clears_the_page_cache(self):
        rc, reply = self.call({"verb": "plugin-set", "site": "wptest", "plugin": "old-thing", "action": "activate"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        self.assertEqual(self.calls()[-1], "wp-site-cli [wptest] [plugin] [activate] [old-thing]")
        self.assertIn("3 files cleared", reply["out"])
        self.assertEqual(self.cache_files(), [])
        self.assertTrue(self.cache.is_dir(), "the directory stays: nginx keeps using it")
        self.assertEqual([a["phase"] for a in self.audit()], ["intent", "result"])
        self.assertEqual(self.audit()[0]["args"], {"plugin": "old-thing", "action": "activate"})

    def test_deactivate_is_tried_again_without_the_other_plugins_when_the_first_try_fails(self):
        self.control("deactivate_needs_skip", "1")
        rc, reply = self.call({"verb": "plugin-set", "site": "wptest", "plugin": "woocommerce", "action": "deactivate"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        self.assertEqual(self.calls()[-2:], ["wp-site-cli [wptest] [plugin] [deactivate] [woocommerce]", "wp-site-cli [wptest] [plugin] [deactivate] [woocommerce] [--skip-plugins] [--skip-themes]"])
        self.assertEqual(self.cache_files(), [])

    def test_a_delete_goes_to_wp_site_ext_after_the_name_is_typed_and_clears_the_cache(self):
        rc, reply = self.call({"verb": "plugin-set", "site": "wptest", "plugin": "old-thing", "action": "delete", "confirm_plugin": "old-thing"})
        self.assertEqual((rc, reply["ok"]), (0, True))
        self.assertEqual(self.calls()[-1], "wp-site-ext [wptest] [plugin] [delete] [old-thing]")
        self.assertEqual(self.cache_files(), [])

    def test_a_delete_without_the_exact_name_is_refused_before_anything_runs(self):
        for extra in ({}, {"confirm_plugin": "old"}, {"confirm_plugin": "OLD-THING"}):
            with self.subTest(extra=extra):
                rc, reply = self.call(dict({"verb": "plugin-set", "site": "wptest", "plugin": "old-thing", "action": "delete"}, **extra))
                self.assertEqual(rc, 2)
        self.assert_nothing_happened()
        self.assertEqual(len(self.cache_files()), 3)

    def test_the_state_rules_and_the_hostings_own_plugins(self):
        for plugin, action, code in (("woocommerce", "activate", "wrong_state"), ("old-thing", "deactivate", "wrong_state"), ("woocommerce", "delete", "wrong_state"),
                                     ("hosting-mail", "deactivate", "wrong_state"), ("hosting-mail", "activate", "wrong_state"), ("nope", "activate", "unknown_plugin"), ("Odd_Name", "delete", "not_deletable")):
            with self.subTest(plugin=plugin, action=action):
                req = {"verb": "plugin-set", "site": "wptest", "plugin": plugin, "action": action}
                if action == "delete":
                    req["confirm_plugin"] = plugin
                rc, reply = self.call(req)
                self.assertEqual((rc, reply["error"]), (2, code))
        self.assertEqual([c for c in self.calls() if "plugin list" not in c and "[list]" not in c], [], "only listings ran")
        self.assertEqual(len(self.cache_files()), 3, "a refusal clears nothing")

    def test_a_tool_that_fails_is_a_failure_and_the_cache_is_kept(self):
        self.control("action_rc", 1)
        rc, reply = self.call({"verb": "plugin-set", "site": "wptest", "plugin": "old-thing", "action": "activate"})
        self.assertEqual((rc, reply["ok"]), (1, False))
        self.assertEqual(len(self.cache_files()), 3)

    def test_hostile_plugin_names_and_actions_are_refused(self):
        for plugin in ("../x", "a b", "-x", "a;id", "", "x" * 101, "a\nb"):
            rc, _ = self.call({"verb": "plugin-set", "site": "wptest", "plugin": plugin, "action": "activate"})
            self.assertEqual(rc, 2, repr(plugin))
        for action in ("install", "update", "Activate", "", None):
            rc, _ = self.call({"verb": "plugin-set", "site": "wptest", "plugin": "old-thing", "action": action})
            self.assertEqual(rc, 2, repr(action))
        self.assert_nothing_happened()

    def test_the_purge_removes_files_only_and_follows_no_link(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep").write_text("x")
        (self.cache / "link").symlink_to(outside)
        (self.cache / "lnk-file").symlink_to(outside / "keep")
        self.assertEqual(wp_ctl.purge_page_cache(self.cfg), 4)           # three files and the link to a file; the link to a directory is not entered
        self.assertTrue((outside / "keep").exists())
        self.assertTrue((self.cache / "link").is_symlink())
        linked = self.tmp / "linked-cache"
        linked.symlink_to(outside)
        self.assertIsNone(wp_ctl.purge_page_cache(dict(self.cfg, page_cache_dir=str(linked))), "a cache directory that is itself a link is not entered")
        self.assertTrue((outside / "keep").exists())
        self.assertIsNone(wp_ctl.purge_page_cache(dict(self.cfg, page_cache_dir=str(self.tmp / "absent"))))
        self.assertIsNone(wp_ctl.purge_page_cache({k: v for k, v in self.cfg.items() if k != "page_cache_dir"}))


class Handlers(Base):
    def test_every_verb_is_one_handler_taking_one_call(self):
        for name, verb in wp_ctl.VERBS.items():
            with self.subTest(name):
                self.assertIn(verb.kind, ("read", "inline", "secret", "stream", "job"))
                self.assertTrue(callable(verb.handler))
                params = list(inspect.signature(verb.handler).parameters)
                self.assertEqual(params, ["call"], "a handler takes the call and nothing else")
                self.assertTrue(all(isinstance(a, wp_ctl.Arg) for a in verb.args.values()))
                self.assertRegex(name, r"^[a-z]+(-[a-z]+)*$")
        self.assertEqual(set(wp_ctl.VERBS), {"list", "show", "check-name", "jobs", "job-status", "job-log", "audit-tail", "import-report", "backup", "delete", "password", "import-receive", "import-discard", "import-run",
                                             "import-approve", "import-reset", "create", "apply", "set", "ensure-state", "install", "import-list", "vuln-check", "users", "plugins", "plugin-set", "import-new", "update-check", "update-apply", "import-rescan"})

    def test_every_job_and_stream_verb_has_a_limit_of_its_own_in_the_tests_table_and_no_limit_names_a_missing_verb(self):
        wp_ctl.check_limits(self.cfg, wp_ctl.VERBS)
        self.assertTrue(set(LIMITS) - {"default"} <= set(wp_ctl.VERBS))

    def test_result_lines_carry_how_long_the_action_took(self):
        self.call({"verb": "import-discard", "file": "f.wpress"})
        rc, reply = self.call({"verb": "backup", "site": "wptest"})
        self.run_job(reply["job"])
        lines = [l for l in self.audit() if l["phase"] in ("result", "started")]
        self.assertEqual(len(lines), 3)
        for l in lines:
            self.assertIsInstance(l["duration_ms"], int, l)
            self.assertGreaterEqual(l["duration_ms"], 0)

    def test_a_reading_handler_cannot_be_given_what_it_does_not_declare(self):
        rc, reply = self.call({"verb": "list", "site": "wptest"})
        self.assertEqual((rc, reply["error"]), (2, "unknown_argument"))


class Reads(Base):
    def test_list_turns_the_status_table_into_rows(self):
        rc, reply = self.call({"verb": "list"})
        self.assertEqual(reply["sites"], [{"site": "wptest", "deployed": "live", "actual": "live", "unit": "active"}, {"site": "held", "deployed": "hold", "actual": "hold", "unit": "active"}])

    def test_a_failing_status_tool_is_reported_not_guessed(self):
        self.control("status_rc", 4)
        rc, reply = self.call({"verb": "list"})
        self.assertEqual((rc, reply["error"]), (2, "tool_failed"))

    def test_show_adds_the_registry_record_and_never_guesses_a_broken_one(self):
        rc, reply = self.call({"verb": "show", "site": "wptest"})
        self.assertEqual((reply["status"]["deployed"], reply["registry"]), ("live", {}))
        (self.data / "wptest" / "site.json").write_text('{"label": "Friend"}')
        self.assertEqual(self.call({"verb": "show", "site": "wptest"})[1]["registry"], {"label": "Friend"})
        (self.data / "wptest" / "site.json").write_text("{broken")
        reply = self.call({"verb": "show", "site": "wptest"})[1]
        self.assertIsNone(reply["registry"])
        self.assertIn("cannot be read", reply["registry_error"])
        self.assertEqual(self.call({"verb": "show", "site": "nosuch"})[1]["error"], "unknown_site")

    def test_a_site_with_only_a_registry_record_exists_and_a_lookalike_does_not(self):
        (self.data / "newsite").mkdir()
        self.assertEqual(self.call({"verb": "show", "site": "newsite"})[1]["error"], "unknown_site", "a directory alone is not a site")
        (self.data / "newsite" / "site.json").write_text("{}")
        self.assertEqual(self.call({"verb": "backup", "site": "newsite"})[0], 0)
        real = self.tmp / "elsewhere"
        real.mkdir()
        (real / "site.json").write_text("{}")
        (self.data / "linked").symlink_to(real)
        self.assertEqual(self.call({"verb": "show", "site": "linked"})[1]["error"], "unknown_site", "a linked directory is not followed")
        (self.data / "Bad_Name").mkdir()
        (self.data / "Bad_Name" / "site.json").write_text("{}")
        self.assertNotIn("Bad_Name", wp_ctl.known_sites(self.cfg))
        self.assertEqual(wp_ctl.known_sites(dict(self.cfg, data_dir=str(self.tmp / "missing"))), set(), "no sites directory: no sites")

    def test_check_name_applies_every_rule_of_a_new_name(self):
        (self.data / "reg").mkdir()
        (self.data / "reg" / "site.json").write_text("{}")
        for name, ok, code in (("fresh-site", True, None), ("ab", True, None), ("a" * 24, True, None), ("hub", False, "reserved_name"), ("mail", False, "reserved_name"), ("wptest", False, "name_taken"),
                               ("reg", False, "name_taken"), ("oldsite", True, None), ("Fresh", False, "invalid_name"), ("a", False, "invalid_name"), ("a" * 25, False, "invalid_name"),
                               ("-fresh", False, "invalid_name"), ("fresh-", False, "invalid_name"), ("fresh_site", False, "invalid_name"), ("fre.sh", False, "invalid_name"), ("1fresh", False, "invalid_name")):
            with self.subTest(name):
                rc, reply = self.call({"verb": "check-name", "name": name})
                if ok:
                    self.assertEqual((rc, reply), (0, {"ok": True}))
                else:
                    self.assertEqual((rc, reply["error"]), (2, code))
        self.assert_nothing_happened()


class Provisioning(Base):
    """create, apply and install: the verbs over wp-site-provision and wp-site-install."""

    def registered(self, name="regsite"):
        (self.data / name).mkdir(exist_ok=True)
        (self.data / name / "site.json").write_text("{}")

    def install_req(self, **kw):
        return dict({"verb": "install", "site": "wptest", "title": "My shop", "user": "owner", "email": "owner@example.org"}, **kw)

    def test_create_and_apply_start_jobs_with_exactly_the_tools_argument_list(self):
        self.registered()
        b = str(self.bin)
        for label, req, argv in (("create", {"verb": "create", "name": "fresh"}, [b + "/wp-site-provision", "create", "fresh"]),
                                 ("create with a label", {"verb": "create", "name": "fresh2", "label": "Aunt May's shop"}, [b + "/wp-site-provision", "create", "fresh2", "--set", "label=Aunt May's shop"]),
                                 ("apply", {"verb": "apply", "site": "regsite"}, [b + "/wp-site-provision", "apply", "regsite"])):
            with self.subTest(label):
                rc, reply = self.call(req)
                self.assertEqual((rc, reply["ok"]), (0, True), reply)
                spec = self.spec(reply["job"])
                self.assertEqual(spec["argv"], argv)
                self.assertEqual(spec["site"], req.get("name") or req["site"], "the job is about the new site, so it takes that name's lock")
                self.assertFalse((self.state / "jobs" / reply["job"] / "tty").exists())
        self.assertEqual([c for c in self.calls() if not c.startswith("systemd-run")], [], "starting a job runs no tool")

    def test_the_audit_names_the_new_site_and_the_job_unit_gets_the_verbs_own_limits(self):
        rc, reply = self.call({"verb": "create", "name": "fresh", "label": "Shop"})
        first = self.audit()[0]
        self.assertEqual((first["phase"], first["verb"], first["site"], first["args"]), ("intent", "create", "fresh", {"label": "Shop"}))
        self.assertIn("[--property=RuntimeMaxSec=1000]", self.calls()[0], "700 s plus the unit grace")
        self.registered()
        self.call({"verb": "apply", "site": "regsite"})
        self.assertIn("[--property=RuntimeMaxSec=1010]", self.calls()[1])

    def test_a_create_that_breaks_a_naming_rule_or_carries_anything_else_is_refused_and_runs_nothing(self):
        self.registered("reg")
        for label, req, code in (("reserved", {"name": "hub"}, "reserved_name"), ("a site", {"name": "wptest"}, "name_taken"), ("a registry-only site", {"name": "reg"}, "name_taken"),
                                 ("upper case", {"name": "Fresh"}, "invalid_name"), ("one letter", {"name": "a"}, "invalid_name"),
                                 ("no name", {}, "missing_argument"), ("a setting the verb does not take", {"name": "fresh", "quota_mb": 5}, "unknown_argument"),
                                 ("--set smuggled in the name", {"name": "fresh --set x=1"}, "invalid_name"), ("a label with a leading space", {"name": "fresh", "label": " x"}, "bad_value"),
                                 ("a label with a control character", {"name": "fresh", "label": "a\nb"}, "bad_value"), ("a label of 101 characters", {"name": "fresh", "label": "x" * 101}, "bad_value"),
                                 ("an empty label", {"name": "fresh", "label": ""}, "bad_value"), ("a label that is a number", {"name": "fresh", "label": 5}, "bad_type")):
            with self.subTest(label):
                rc, reply = self.call(dict({"verb": "create"}, **req))
                self.assertEqual((rc, reply["error"]), (2, code))
        self.assertEqual((self.calls(), self.jobs()), ([], []))

    def test_a_closed_sites_name_can_be_used_again_the_dispatcher_does_not_look_at_tombstones(self):
        self.assertFalse(hasattr(wp_ctl, "closed_names"))
        self.assertEqual(self.call({"verb": "check-name", "name": "gone"})[0], 0)
        self.assertEqual(self.call({"verb": "create", "name": "gone"})[0], 0)
        self.assertEqual(len(self.jobs()), 1)

    def test_a_label_of_one_or_a_hundred_characters_is_accepted(self):
        for n, label in enumerate(("x", "y" * 100, "two words")):
            rc, reply = self.call({"verb": "create", "name": "fresh%d" % n, "label": label})
            self.assertEqual((rc, reply["ok"]), (0, True), label)

    def test_apply_needs_a_registry_record_and_an_existing_site(self):
        self.assertNotIn("not_registered", (ROOT / "scripts/wp_ctl.py").read_text(), "a site is known only if it has a record, so there is no site without one to refuse")
        rc, reply = self.call({"verb": "apply", "site": "nosuch"})
        self.assertEqual((rc, reply["error"]), (2, "unknown_site"))
        link = self.tmp / "elsewhere"
        link.mkdir()
        (link / "site.json").write_text("{}")
        (self.data / "viasymlink").symlink_to(link)
        self.assertEqual(self.call({"verb": "apply", "site": "viasymlink"})[1]["error"], "unknown_site")
        self.assertEqual((self.calls(), self.jobs()), ([], []))

    def test_set_and_ensure_state_start_jobs_with_exactly_the_tools_argument_list(self):
        self.registered()
        b = str(self.bin) + "/wp-site-provision"
        for label, req, argv in (("a limit", {"verb": "set", "site": "regsite", "key": "php_mem_mb", "value": "640"}, [b, "set", "regsite", "php_mem_mb=640"]),
                                 ("a switch", {"verb": "set", "site": "regsite", "key": "allow_xmlrpc", "value": "true"}, [b, "set", "regsite", "allow_xmlrpc=true"]),
                                 ("a note with spaces and an equals sign", {"verb": "set", "site": "regsite", "key": "note", "value": "a = b; $(id) `x`"}, [b, "set", "regsite", "note=a = b; $(id) `x`"]),
                                 ("clearing a note", {"verb": "set", "site": "regsite", "key": "note", "value": ""}, [b, "set", "regsite", "note="]),
                                 ("hold", {"verb": "ensure-state", "site": "regsite", "state": "hold"}, [b, "state", "regsite", "hold"]),
                                 ("stopped", {"verb": "ensure-state", "site": "regsite", "state": "stopped"}, [b, "state", "regsite", "stopped"]),
                                 ("live, confirmed", {"verb": "ensure-state", "site": "regsite", "state": "live", "confirm_name": "regsite"}, [b, "state", "regsite", "live"])):
            with self.subTest(label):
                rc, reply = self.call(req)
                self.assertEqual((rc, reply["ok"]), (0, True), reply)
                self.assertEqual(self.spec(reply["job"])["argv"], argv)
                self.assertEqual(self.spec(reply["job"])["site"], "regsite")
        self.assertEqual([c for c in self.calls() if not c.startswith("systemd-run")], [], "starting a job runs no tool")

    def test_going_live_needs_the_sites_name_and_the_other_states_do_not(self):
        self.registered()
        for label, req, code in (("no confirmation", {"state": "live"}, "missing_argument"), ("the wrong name", {"state": "live", "confirm_name": "wptest"}, "confirm_mismatch"),
                                 ("an empty name", {"state": "live", "confirm_name": ""}, "bad_value"), ("the name in capitals", {"state": "live", "confirm_name": "REGSITE"}, "bad_value")):
            with self.subTest(label):
                rc, reply = self.call(dict({"verb": "ensure-state", "site": "regsite"}, **req))
                self.assertEqual((rc, reply["error"]), (2, code))
        self.assertEqual((self.calls(), self.jobs()), ([], []))
        for state in ("hold", "stopped"):
            self.assertEqual(self.call({"verb": "ensure-state", "site": "regsite", "state": state})[0], 0, state)
        rc, reply = self.call({"verb": "ensure-state", "site": "regsite", "state": "hold", "confirm_name": "wptest"})
        self.assertEqual((rc, reply["error"]), (2, "confirm_mismatch"), "a confirmation that is given must still be right")

    def test_set_and_ensure_state_refuse_what_they_must_before_anything_starts(self):
        self.registered()
        s = {"verb": "set", "site": "regsite"}
        e = {"verb": "ensure-state", "site": "regsite"}
        for label, req, code in (("the port", dict(s, key="port", value="9999"), "bad_value"), ("the state", dict(s, key="state", value="live"), "bad_value"), ("the name", dict(s, key="name", value="x"), "bad_value"),
                                 ("the quota id", dict(s, key="quota_id", value="5"), "bad_value"), ("a hidden key", dict(s, key="fpm_max_children", value="5"), "bad_value"), ("no key", dict(s, value="5"), "missing_argument"),
                                 ("no value", dict(s, key="note"), "missing_argument"), ("a value with a control character", dict(s, key="note", value="a\nb"), "bad_value"),
                                 ("a note of 501 characters", dict(s, key="note", value="x" * 501), "bad_value"), ("a value that is a list", dict(s, key="note", value=["x"]), "bad_type"), ("a number where text is wanted (the page sends text; the command line coerces)", dict(s, key="php_mem_mb", value=640), "bad_type"),
                                 ("two keys at once", dict(s, key="note", value="x", upload_mb=5), "unknown_argument"),
                                 ("a site that does not exist", dict(s, site="nosuch", key="note", value="x"), "unknown_site"), ("a state that does not exist", dict(e, state="paused"), "bad_value"),
                                 ("closing by state", dict(e, state="closed"), "bad_value"), ("no state", e, "missing_argument"), ("a state in capitals", dict(e, state="HOLD"), "bad_value"),
                                 ("a site that does not exist", dict(e, site="nosuch", state="hold"), "unknown_site")):
            with self.subTest(label):
                rc, reply = self.call(req)
                self.assertEqual((rc, reply["error"]), (2, code), reply)
        self.assertEqual((self.calls(), self.jobs()), ([], []))

    def test_a_note_of_the_longest_allowed_size_and_a_value_that_looks_like_an_option_are_accepted_as_values(self):
        self.registered()
        for value in ("x" * 500, "--yes", "-1", "=", "plain"):
            rc, reply = self.call({"verb": "set", "site": "regsite", "key": "note", "value": value})
            self.assertEqual((rc, reply["ok"]), (0, True), value[:20])
            self.assertEqual(self.spec(reply["job"])["argv"][-1], "note=" + value, "one argument: the tool splits at the first equals sign")

    def test_set_and_ensure_state_are_audited_with_their_arguments_and_get_their_own_limits(self):
        self.registered()
        self.call({"verb": "set", "site": "regsite", "key": "php_mem_mb", "value": "640"})
        self.call({"verb": "ensure-state", "site": "regsite", "state": "live", "confirm_name": "regsite"})
        first = self.audit()[0]
        self.assertEqual((first["phase"], first["verb"], first["site"], first["args"]), ("intent", "set", "regsite", {"key": "php_mem_mb", "value": "640"}))
        live = [a for a in self.audit() if a["verb"] == "ensure-state" and a["phase"] == "intent"][0]
        self.assertEqual(live["args"], {"state": "live", "confirm_name": "regsite"})
        self.assertIn("[--property=RuntimeMaxSec=1020]", self.calls()[0], "720 s plus the unit grace")
        self.assertIn("[--property=RuntimeMaxSec=1030]", self.calls()[1], "730 s plus the unit grace")

    def test_two_changes_to_one_site_do_not_overlap_and_a_repeat_is_the_same_job(self):
        self.registered()
        req = {"verb": "ensure-state", "site": "regsite", "state": "hold", "req": "r1"}
        first = self.call(req)[1]
        again = self.call(req)[1]
        self.assertEqual((again["job"], again.get("duplicate")), (first["job"], True))
        fd = wp_ctl.take_lock(wp_ctl.Ctl(self.cfg, self.env, self.clock, subprocess.run), "regsite")
        self.addCleanup(os.close, fd)
        rc, reply = self.call({"verb": "set", "site": "regsite", "key": "note", "value": "x"})
        self.assertEqual((rc, reply["error"]), (2, "busy"))

    def test_a_repeated_create_is_the_same_job_and_two_creates_of_one_name_do_not_overlap(self):
        req = {"verb": "create", "name": "fresh", "req": "r1"}
        first = self.call(req)[1]
        again = self.call(req)[1]
        self.assertEqual((again["job"], again.get("duplicate")), (first["job"], True))
        fd = wp_ctl.take_lock(wp_ctl.Ctl(self.cfg, self.env, self.clock, subprocess.run), "other")
        self.addCleanup(os.close, fd)
        rc, reply = self.call({"verb": "create", "name": "other"})
        self.assertEqual((rc, reply["error"]), (2, "busy"))
        rc, reply = self.call({"verb": "create", "name": "fresh", "req": "r1", "label": "different"})
        self.assertEqual((rc, reply["error"]), (2, "req_reused"))

    def test_a_repeat_of_a_create_after_the_site_exists_is_still_the_same_job_but_a_new_request_is_refused(self):
        req = {"verb": "create", "name": "fresh", "req": "r1"}
        first = self.call(req)[1]
        self.registered("fresh")                       # the build has made the site by now
        rc, again = self.call(req)
        self.assertEqual((rc, again["job"], again.get("duplicate")), (0, first["job"], True), again)
        rc, other = self.call({"verb": "create", "name": "fresh", "req": "r2"})
        self.assertEqual((rc, other["error"]), (2, "name_taken"), "another request for the same name is still refused")
        rc, other = self.call({"verb": "create", "name": "fresh"})
        self.assertEqual((rc, other["error"]), (2, "name_taken"), "and so is one without a request id")
        rc, other = self.call({"verb": "create", "name": "hub", "req": "r1"})
        self.assertEqual((rc, other["error"]), (2, "req_reused"), "the id of a job cannot be reused for another name, whether or not the name would pass")

    def test_a_create_job_runs_the_tool_and_records_its_exit_code(self):
        rc, reply = self.call({"verb": "create", "name": "fresh"})
        self.assertEqual(self.run_job(reply["job"]), 0)
        self.assertEqual(self.calls()[-1], "wp-site-provision [create] [fresh]")
        self.assertEqual((self.state / "jobs" / reply["job"] / "exit").read_text(), "0\n")
        self.assertEqual(self.audit()[-1]["site"], "fresh")
        self.control("rc", 1)
        rc, reply = self.call({"verb": "create", "name": "fresh2"})
        self.run_job(reply["job"])
        self.assertEqual((self.state / "jobs" / reply["job"] / "exit").read_text(), "1\n")

    def test_install_gives_the_tool_its_options_and_the_password_only_through_the_terminal_file(self):
        rc, reply = self.call(self.install_req(), extra=(CANARY + "\n").encode())
        self.assertEqual((rc, reply["ok"]), (0, True), reply)
        self.assertEqual(self.calls(), ["wp-site-install [wptest] [--title=My shop] [--user=owner] [--email=owner@example.org]"])
        self.assertEqual(self.tty_seen(), ["%s\n%s\n" % (CANARY, CANARY)], "asked twice, as the tool does")
        self.assertEqual([p.read_text().strip() for p in self.shim.glob("tty.mode.*")], ["600"])
        self.assertEqual(sorted(p.name for p in self.run.glob("*")), [], "the terminal file is removed")

    def test_the_install_password_is_in_no_audit_line_journal_log_or_reply(self):
        self.control("leak", 1)
        rc, reply = self.call(self.install_req(), extra=(CANARY + "\n").encode())
        self.assertNotIn(CANARY, json.dumps(reply))
        self.assertIn("REDACTED", reply["out"])
        for p in self.tmp.rglob("*"):
            if p.is_file() and p.parent != self.stdin_dir and not p.name.startswith("tty.seen."):
                self.assertNotIn(CANARY.encode(), p.read_bytes(), "the password is in " + str(p))

    def test_a_bad_install_request_is_refused_before_the_tool(self):
        for label, req, extra, code in (("no password", {}, b"", "bad_password"), ("short password", {}, b"short\n", "bad_password"), ("control character", {}, b"abcdefghijklmnop\x07\n", "bad_password"),
                                        ("user too short", {"user": "ab"}, CANARY.encode() + b"\n", "bad_value"), ("user with a space", {"user": "my user"}, CANARY.encode() + b"\n", "bad_value"),
                                        ("not an email", {"email": "owner"}, CANARY.encode() + b"\n", "bad_value"), ("email with a space", {"email": "a b@example.org"}, CANARY.encode() + b"\n", "bad_value"),
                                        ("no title", {"title": ""}, CANARY.encode() + b"\n", "bad_value"), ("no site", {"site": "nosuch"}, CANARY.encode() + b"\n", "unknown_site"),
                                        ("a password as an argument", {"password": CANARY}, CANARY.encode() + b"\n", "unknown_argument")):
            with self.subTest(label):
                rc, reply = self.call(self.install_req(**req), extra=extra)
                self.assertEqual((rc, reply["error"]), (2, code))
        self.assertEqual(self.calls(), [])
        self.assertEqual(sorted(p.name for p in self.run.glob("*")), [])
        self.assertNotIn(CANARY, self.audit_file.read_text(), "not even the refused attempt that carried it as an argument")

    def test_install_holds_the_sites_lock(self):
        fd = wp_ctl.take_lock(wp_ctl.Ctl(self.cfg, self.env, self.clock, subprocess.run), "wptest")
        self.addCleanup(os.close, fd)
        rc, reply = self.call(self.install_req(), extra=(CANARY + "\n").encode())
        self.assertEqual((rc, reply["error"]), (2, "busy"))
        self.assertEqual(self.calls(), [])


class CommandLine(Base):
    def test_the_command_line_form_is_the_same_request(self):
        rc, reply = self.call(b"", argv=["backup", "site=wptest", "force=true", "tag=final", "req=cli-1"])
        self.assertEqual((rc, reply["ok"]), (0, True), reply)
        self.assertEqual(self.spec(reply["job"])["argv"], [str(self.bin / "wp-site-backup"), "wptest", "--force", "--tag=final"])
        self.assertEqual(self.audit()[0]["req"], "cli-1")

    def test_the_command_line_form_is_checked_as_hard_as_the_json_form(self):
        for argv in (["backup"], ["backup", "site=../x"], ["backup", "site=wptest", "force=maybe"], ["backup", "site=wptest", "site=held"], ["backup", "wptest"], ["backup", "=x"], ["backup", "site=wptest", "x=1"],
                     ["audit-tail", "lines=abc"], ["audit-tail", "lines=0"], ["audit-tail", "lines=-1"], ["--help"], ["delete", "site=wptest", "confirm_name=held"]):
            with self.subTest(argv):
                rc, reply = self.call(b"", argv=argv)
                self.assertEqual((rc, reply["ok"]), (2, False), argv)
        self.assert_nothing_happened()
        self.assertEqual(self.call(b"", argv=["audit-tail", "lines=5"])[0], 0, "a number typed on the command line is a number")

    def test_the_password_on_the_command_line_form_still_comes_from_the_input_not_the_arguments(self):
        rc, reply = self.call(CANARY.encode() + b"\n", argv=["password", "site=wptest", "user=admin"])
        self.assertEqual((rc, reply["ok"]), (0, True), reply)
        self.assertEqual(self.calls(), ["wp-site-password [wptest] [admin]"])


class Wrapper(Base):
    """The Jinja wrapper with the repo's real values, run as a program: the exit status, one JSON line on standard output, the library found by its path."""

    def render(self, sites=None):
        v = base_vars()
        if sites is not None:
            v["wp_sites"] = sites
        return render_env().get_template("wp/wp_ctl.py.j2").render(**v), v

    def test_the_config_carries_the_repo_values(self):
        text, v = self.render(sites=[{"name": "alive"}, {"name": "gone", "state": "closed"}, {"name": "held", "state": "hold"}, {"name": "tomb", "state": "closed"}])
        ns = {}
        exec(compile(text.replace("sys.exit(wp_ctl.main(CONFIG, sys.argv[1:]))", "RESULT = CONFIG").replace('sys.path.insert(0, "%s")' % v["wp_tools_lib"], 'sys.path.insert(0, %r)' % str(ROOT / "scripts")),
                     "wp_ctl.py.j2", "exec"), ns)
        cfg = ns["RESULT"]
        self.assertNotIn("sites", cfg, "the sites are the registry's: no list is baked in")
        self.assertNotIn("closed_sites", cfg, "a closed site's name may be used again: the dispatcher does not keep the list")
        self.assertEqual(cfg["reserved_names"], v["wp_reserved_names"])
        for key, var in (("state_dir", "wp_ctl_state_dir"), ("run_dir", "wp_ctl_run_dir"), ("audit_file", "wp_ctl_audit_file"), ("tools_bin", "wp_tools_bin"), ("page_cache_dir", "wp_page_cache_path"), ("panel_user", "wp_ctl_user"), ("job_keep_days", "wp_ctl_job_keep_days"),
                         ("max_request_bytes", "wp_ctl_max_request_bytes"), ("limits", "wp_ctl_limits")):
            self.assertEqual(cfg[key], v[var], key)
        wp_ctl.check_limits(cfg, wp_ctl.VERBS)       # the repo's own table passes the dispatcher's check: every verb named exists and every job and the upload have a timeout of their own
        self.assertEqual(cfg["data_dir"], v["wp_data_mount"] + "/sites")
        needed = {"state_dir", "run_dir", "audit_file", "data_dir", "tools_bin", "page_cache_dir", "path", "logger", "systemd_run", "panel_user", "job_keep_days", "max_request_bytes", "limits",
                  "reserved_names"}
        self.assertEqual(set(cfg), needed, "every key the dispatcher reads, and no stray one")

    def test_it_runs_as_a_program_and_refuses_a_hostile_request_with_status_2(self):
        text, v = self.render(sites=[{"name": "wptest"}])
        for old, new in ((v["wp_tools_lib"], str(ROOT / "scripts")), (v["wp_ctl_state_dir"], str(self.state)), (v["wp_ctl_run_dir"], str(self.run)), (v["wp_ctl_audit_file"], str(self.audit_file)), (v["wp_data_mount"] + "/sites", str(self.data)),
                         (v["wp_tools_bin"], str(self.bin)), ("/usr/bin/logger", str(self.bin / "logger")), ("/usr/bin/systemd-run", str(self.bin / "systemd-run"))):
            text = text.replace(old, new)
        prog = self.tmp / "wp-ctl"
        prog.write_text(text)

        def go(payload):
            return subprocess.run([sys.executable, "-I", str(prog)], input=payload, capture_output=True, env={"SUDO_USER": "wpctl"}, timeout=60)

        r = go(b'{"verb":"list"}\n')
        self.assertEqual((r.returncode, json.loads(r.stdout)["ok"]), (0, True), r.stderr)
        self.assertEqual(r.stdout.count(b"\n"), 1, "one JSON line, nothing else")
        r = go(b'{"verb":"delete","site":"../../etc","confirm_name":"../../etc"}\n')
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]), (2, "unknown_site"))
        r = go(b'{"verb":"backup","site":"wptest"}\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([l["phase"] for l in self.audit()], ["refused", "intent", "started"])
        self.assertEqual(self.audit()[-1]["caller"], "panel")
        r = go(b"")
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]), (2, "empty_request"))


class Access(unittest.TestCase):
    def setUp(self):
        self.tasks = yaml.safe_load((ROOT / "tasks/wp_ctl_common.yml").read_text())
        self.vars = base_vars()

    def task(self, name_part):
        return [t for t in self.tasks if name_part in t["name"]][0]

    def test_a_key_line_is_forced_to_the_program_with_restrict_and_an_empty_list_lets_nobody_in(self):
        content = self.task("keys that may log in")["ansible.builtin.copy"]["content"]
        render = lambda keys: jinja2.Environment(trim_blocks=True).from_string(content).render(wp_ctl_panel_keys=keys, wp_tools_bin="/usr/local/sbin", wp_ctl_user="wpctl", wp_ctl_panel_from="10.8.0.68")
        render_user = lambda keys: render(keys).replace("{{ wp_ctl_user }}", "wpctl")
        self.assertEqual([l for l in render([]).splitlines() if not l.startswith("#")], [], "no key line: sshd lets nobody in")
        two = [l for l in render(["ssh-ed25519 AAAAC3Nza one@panel", "ssh-ed25519 AAAAC3Nzb two"]).splitlines() if not l.startswith("#")]
        self.assertEqual(two, ['from="10.8.0.68",restrict,command="sudo -n /usr/local/sbin/wp-ctl" ssh-ed25519 AAAAC3Nza one@panel', 'from="10.8.0.68",restrict,command="sudo -n /usr/local/sbin/wp-ctl" ssh-ed25519 AAAAC3Nzb two'])
        self.assertTrue(render_user([]).startswith("#"), "the file starts with a comment so the content is never empty")
        keys = self.vars["wp_ctl_panel_keys"]
        self.assertEqual(len(keys), 1, "exactly one key may log in: the control app's")
        self.assertRegex(keys[0], r"^ssh-ed25519 AAAA[0-9A-Za-z+/]+=* wp-panel@aopenpanel$")
        self.assertEqual(yaml.safe_load((ROOT / "host_vars/dellwp/vars.yml").read_text())["wp_ctl_panel_from"], "{{ hostvars['aopenpanel']['overlay_ip'] }}", "the one address the keys may log in from is the control app's VM")

    def test_the_from_address_must_be_one_plain_ipv4_address(self):
        that = self.task("not a plain ed25519")["ansible.builtin.assert"]["that"][1]
        rx = re.fullmatch(r"wp_ctl_panel_from is match\('(.*)'\)", that).group(1)
        for good in ("10.8.0.68", "1.2.3.4", "192.168.100.200"):
            self.assertTrue(re.match(rx, good), good)
        trim = self.task("not a plain ed25519")["ansible.builtin.assert"]["that"][2]
        self.assertEqual(trim, "wp_ctl_panel_from == (wp_ctl_panel_from | trim)", "a trailing newline would pass the pattern above, because $ matches before it")
        for bad in ("*", "10.8.0.0/24", "10.8.0.68,10.8.0.69", '10.8.0.68",command="id', "10.8.0.68 ", "", "a.b.c.d", "10.8.0", "10.8.0.68.1", "!10.8.0.68", "10.8.0.68\nfrom=*"):
            self.assertFalse(re.match(rx, bad), repr(bad))

    def test_no_copy_task_can_render_an_empty_content_which_ansible_reads_as_no_content(self):
        # "src (or content) is required" failed the real run on dellwp (2026-10-28) when the key list was empty: plain Jinja rendered "" and called it fine, Ansible reads "" as missing
        seen = 0
        for t in self.tasks:
            body = t.get("ansible.builtin.copy")
            if body and "content" in body:
                seen += 1
                text = jinja2.Environment(trim_blocks=True).from_string(body["content"]).render(wp_ctl_panel_keys=[], wp_tools_bin="/usr/local/sbin", wp_ctl_user="wpctl")
                self.assertNotEqual(text.strip(), "", "%s renders empty with the default variables" % t["name"])
                self.assertNotEqual(body["content"], "", t["name"])
        self.assertGreaterEqual(seen, 3, "the key file, the sudoers entry and the audit file")
        raw = (ROOT / "tasks/wp_ctl_common.yml").read_text()
        self.assertNotRegex(raw, r"content:\s*[\"']{2}\s*$", "an empty literal content is read the same way")
        self.assertNotIn("ignore_errors", raw)

    def test_the_audit_file_starts_with_a_record_that_audit_tail_can_read(self):
        content = self.task("Make the audit file")["ansible.builtin.copy"]["content"]
        first = json.loads(content.splitlines()[0])
        self.assertEqual(first["phase"], "created")

    def test_only_plain_ed25519_keys_pass_the_assertion(self):
        that = self.task("not a plain ed25519")["ansible.builtin.assert"]["that"][0]
        rx = re.fullmatch(r"wp_ctl_panel_keys \| reject\('match', '(.*)'\) \| list \| length == 0", that).group(1)
        good = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc+/0=", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc panel@vm-1.lab", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc=="]
        bad = ['ssh-ed25519 AAAAC3 x" , command="id"', "ssh-ed25519 AAAAC3\nssh-rsa AAAAB3", "ssh-rsa AAAAB3NzaC1yc2E", 'command="id" ssh-ed25519 AAAAC3', "ssh-ed25519 AAAAC3 a b", "", "ssh-ed25519", "ssh-ed25519 AAAAC3 x\x00y",
               "ssh-ed25519 AAAAC3 from=\"0.0.0.0\"", "restrict ssh-ed25519 AAAAC3", "ssh-ed25519 notbase64"]
        for k in good:
            self.assertTrue(re.match(rx, k), k)
        for k in bad:
            self.assertFalse(re.match(rx, k), repr(k))

    def test_the_sudoers_entry_allows_the_program_with_no_arguments_and_is_validated(self):
        # the forced command is `sudo -n wp-ctl` with no arguments and the entry allows none: a client's text never becomes argv
        sudoers = self.task("Let the control account run wp-ctl")["ansible.builtin.copy"]
        line = jinja2.Environment(trim_blocks=True).from_string(sudoers["content"]).render(wp_ctl_user="wpctl", wp_tools_bin="/usr/local/sbin").strip()
        self.assertEqual(line, 'wpctl ALL=(root) NOPASSWD: /usr/local/sbin/wp-ctl ""')
        self.assertIn("visudo -cf", sudoers["validate"])
        self.assertEqual(sudoers["mode"], "0440")

    def test_the_account_home_and_ssh_directory_belong_to_root(self):
        t = self.task("home and .ssh directories")
        d = t["ansible.builtin.file"]
        self.assertEqual((d["owner"], d["mode"]), ("root", "0755"))
        self.assertTrue(any(str(i).endswith("/.ssh") for i in t["loop"]))

    def test_the_account_and_its_files_belong_to_root_and_the_audit_file_is_made_append_only(self):
        user = self.task("Make the control account")["ansible.builtin.user"]
        self.assertEqual((user["password"], user["create_home"], user["system"]), ("*", False, True))
        # useradd refuses a colon, a comma-separated GECOS surprise or a control character in the comment (it failed on dellwp with "wp-ctl: the ..."); check mode and the offline run cannot see it
        self.assertRegex(user["comment"], r"^[^:,\x00-\x1f\x7f]+$")
        for t in self.tasks:
            body = t.get("ansible.builtin.copy") or t.get("ansible.builtin.file") or {}
            if "authorized_keys" in str(body.get("dest", "")) or "/.ssh" in str(body.get("path", "")) or "sudoers" in str(body.get("dest", "")):
                self.assertEqual(body.get("owner"), "root", t["name"])
        block = self.task("Make the audit file append-only")
        self.assertEqual(block["block"][0]["ansible.builtin.file"]["attributes"], "a")
        self.assertIn("ansible.builtin.debug", block["rescue"][0], "a filesystem that refuses the attribute is reported (rescued), not a failure of the play")
        self.assertIn("ansible_failed_result", block["rescue"][0]["ansible.builtin.debug"]["msg"])
        self.assertNotIn("failed_when", json.dumps(block), "failed_when: false also clears the flag a later `when:` would test, so the warning could never fire")
        self.assertIs(self.task("Make the audit file")["ansible.builtin.copy"]["force"], False, "re-running must not rewrite a file that is already append-only")

    def test_the_program_is_staged_with_the_other_tools_and_the_access_tasks_are_imported_with_their_own_tag(self):
        tools = yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text())
        mods = [t for t in tools if t.get("name") == "Stage the Python modules of wp-site-import"][0]["loop"]
        self.assertIn("wp_ctl.py", mods)
        self.assertTrue((ROOT / "scripts/wp_ctl.py").is_file())
        stage = [t for t in tools if t.get("name") == "Stage the tools"][0]["loop"]
        self.assertIn({"src": "wp_ctl.py.j2", "dest": "wp-ctl"}, stage)
        play = yaml.safe_load((ROOT / "dellwp/wp_sites_dellwp.yml").read_text())[0]["tasks"]
        imp = [t for t in play if "wp_ctl_common" in str(t)][0]
        self.assertEqual(imp["tags"], ["wp_ctl"])

    def test_the_variables_exist_and_the_reserved_names_are_the_one_list_the_play_already_uses(self):
        v = self.vars
        for key in ("wp_ctl_user", "wp_ctl_panel_keys", "wp_ctl_state_dir", "wp_ctl_run_dir", "wp_ctl_audit_file", "wp_ctl_job_keep_days", "wp_ctl_max_request_bytes", "wp_ctl_limits", "wp_reserved_names"):
            self.assertIn(key, v)
        text = (ROOT / "host_vars/dellwp/vars.yml").read_text()
        self.assertEqual(len(re.findall(r"^wp_reserved_names:", text, re.M)), 1, "one variable in one place")
        self.assertIn("wp_reserved_names", (ROOT / "templates/wp/wp_ctl.py.j2").read_text(), "the dispatcher's wrapper carries the one list")
        self.assertNotIn("wp_ctl_inline_timeout_s", v, "the flat timeouts were replaced by the limits table; two places would disagree")


class Registry(unittest.TestCase):
    """require_site in the rendered tools library: a site exists if it has a registry record."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wpctl-lib-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        v = base_vars()
        text = render_env().get_template("wp/wp_tools_common.sh.j2").render(**v).replace("readonly WP_DATA='%s/sites'" % v["wp_data_mount"], "readonly WP_DATA='%s'" % (self.tmp / "sites"))
        self.assertIn(str(self.tmp / "sites"), text)
        self.lib = self.tmp / "common.sh"
        self.lib.write_text(text)
        (self.tmp / "sites").mkdir()

    def known(self, name):
        r = subprocess.run(["bash", "-c", '. "$1"; require_site "$2"; echo "$site|$dir"', "x", str(self.lib), name], capture_output=True, text=True)
        return r.returncode == 0, r

    def test_a_record_and_nothing_else(self):
        sites = self.tmp / "sites"
        self.assertFalse(self.known("fresh")[0])
        (sites / "fresh").mkdir()
        self.assertFalse(self.known("fresh")[0], "a directory alone is not a site")
        (sites / "fresh" / "site.json").write_text("{}")
        ok, r = self.known("fresh")
        self.assertTrue(ok, r.stderr)
        self.assertEqual(r.stdout.strip(), "fresh|%s/fresh" % sites)

    def test_a_linked_record_and_hostile_names_are_refused(self):
        sites = self.tmp / "sites"
        (sites / "linked").mkdir()
        (self.tmp / "real.json").write_text("{}")
        (sites / "linked" / "site.json").symlink_to(self.tmp / "real.json")
        self.assertFalse(self.known("linked")[0], "a record that is a link does not count")
        (sites / "x").mkdir()
        (sites / "x" / "site.json").write_text("{}")
        for name in ("../sites/x", "x/../x", "X", "x y", "", "*", "x;id", "a" * 30, "-x"):
            with self.subTest(name):
                self.assertFalse(self.known(name)[0], name)

    def test_the_message_for_an_unknown_site_names_the_registry(self):
        ok, r = self.known("nosuch")
        self.assertFalse(ok)
        self.assertIn("'nosuch' is not a site (it has no registry record: create it with wp-ctl create)", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
