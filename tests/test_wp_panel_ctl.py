#!/usr/bin/env python3
"""Offline tests of app/wp_panel/ctl.py, the app's one way to dellwp.

A stand-in `run` records what would be executed and answers like wp-ctl does; a second group of tests runs a real stand-in `ssh` program to see the request on its standard input and the environment it is
given. They prove: only the read verbs go out, with exactly the arguments and ranges wp-ctl has (cross-checked against the dispatcher's own table); every value is checked before anything is sent and a
value cannot carry a second request; ssh is called with an argument list, no remote command and a clean environment; every way an answer can go wrong becomes a CtlError of the right kind, with no
traceback and no path of the key in the message. They do not prove the real ssh, the forced command or dellwp: the rehearsal on the VM does. Run: python3 -I tests/test_wp_panel_ctl.py"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "scripts"))
from wp_panel import ctl  # noqa: E402
import wp_ctl  # noqa: E402

JOB = "20261029T122103Z-2cf66a88"


class Fake:
    """A stand-in for subprocess.run: records the call, answers with what the test set."""

    def __init__(self, stdout=b"", returncode=0, stderr=b"", raises=None):
        self.stdout, self.returncode, self.stderr, self.raises = stdout, returncode, stderr, raises
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def client(run=None, **kw):
    return ctl.Ctl("dellwp", "wpctl", "/run/secrets/panel_key", "/run/secrets/known_hosts", run=run or Fake(), **kw)


def ok(**more):
    return (json.dumps(dict({"ok": True}, **more)) + "\n").encode()


class Allowlist(unittest.TestCase):
    def test_the_verbs_are_exactly_the_dispatchers_read_verbs_with_its_argument_names(self):
        for verb, spec in ctl.READ_VERBS.items():
            theirs = wp_ctl.VERBS[verb]
            self.assertEqual(theirs.kind, "read", verb)
            self.assertEqual(set(spec), set(theirs.args), verb)
            for name, (required, _) in spec.items():
                self.assertEqual(required, theirs.args[name].required, (verb, name))

    def test_no_verb_that_changes_anything_is_in_the_list(self):
        reads = {v for v, spec in wp_ctl.VERBS.items() if spec.kind == "read"}
        self.assertTrue(set(ctl.READ_VERBS) <= reads)
        for verb in set(wp_ctl.VERBS) - reads:
            with self.assertRaises(ctl.CtlError) as cm:
                client().request(verb, {})
            self.assertEqual(cm.exception.kind, "not_allowed", verb)

    def test_the_ranges_are_the_dispatchers(self):
        a = wp_ctl.VERBS["job-log"].args["offset"]
        self.assertTrue(ctl.READ_VERBS["job-log"]["offset"][1](a.lo) and ctl.READ_VERBS["job-log"]["offset"][1](a.hi))
        self.assertFalse(ctl.READ_VERBS["job-log"]["offset"][1](a.lo - 1) or ctl.READ_VERBS["job-log"]["offset"][1](a.hi + 1))
        b = wp_ctl.VERBS["audit-tail"].args["lines"]
        self.assertTrue(ctl.READ_VERBS["audit-tail"]["lines"][1](b.lo) and ctl.READ_VERBS["audit-tail"]["lines"][1](b.hi))
        self.assertFalse(ctl.READ_VERBS["audit-tail"]["lines"][1](b.lo - 1) or ctl.READ_VERBS["audit-tail"]["lines"][1](b.hi + 1))
        self.assertEqual(ctl.JOB_RE.pattern, wp_ctl.JOB_RE.pattern)
        self.assertEqual(ctl.NAME_RE.pattern, wp_ctl.NAME_RE.pattern)


class Request(unittest.TestCase):
    def test_each_read_verb_makes_one_json_line(self):
        c = client()
        self.assertEqual(json.loads(c.request("list", {})), {"verb": "list"})
        self.assertEqual(json.loads(c.request("show", {"site": "alpha"})), {"verb": "show", "site": "alpha"})
        self.assertEqual(json.loads(c.request("job-log", {"job": JOB, "offset": 65536})), {"verb": "job-log", "job": JOB, "offset": 65536})
        self.assertEqual(json.loads(c.request("audit-tail", {"lines": 50})), {"verb": "audit-tail", "lines": 50})
        line = c.request("show", {"site": "alpha"})
        self.assertTrue(line.endswith("\n") and line.count("\n") == 1, "one line")

    def test_a_verb_that_is_not_listed_or_not_text_is_refused(self):
        for verb in ("delete", "create", "set", "ensure-state", "password", "import-run", "apply", "install", "", "LIST", "list ", None, 5, ["list"], {"verb": "list"}):
            with self.subTest(repr(verb)), self.assertRaises(ctl.CtlError) as cm:
                client().request(verb, {})
            self.assertEqual(cm.exception.kind, "not_allowed")

    def test_an_argument_the_verb_does_not_take_or_misses_is_refused(self):
        for verb, args in (("list", {"site": "alpha"}), ("show", {}), ("show", {"site": "alpha", "x": 1}), ("jobs", {"job": JOB}), ("job-log", {}), ("job-log", {"job": JOB, "lines": 5}), ("audit-tail", {"site": "a"})):
            with self.subTest(verb=verb, args=args), self.assertRaises(ctl.CtlError) as cm:
                client().request(verb, args)
            self.assertEqual(cm.exception.kind, "not_allowed")

    def test_values_that_do_not_look_right_are_refused_and_none_can_carry_a_second_request(self):
        bad = {"show": [("site", v) for v in ("", "A", "a", "ab-", "-ab", "a--b", "a_b", "../x", "x/y", "a b", "a" * 25, "alpha\n", 'alpha","verb":"delete', 'alpha\n{"verb":"delete"}', "日本", None, 5, ["alpha"], True)],
               "job-status": [("job", v) for v in ("", JOB + "\n", JOB[:-1], JOB.upper(), "../" + JOB, None, 5)],
               "job-log": [("job", JOB)]}
        for verb, pairs in bad.items():
            for name, value in pairs:
                args = {name: value}
                if verb == "job-log":
                    continue
                with self.subTest(verb=verb, value=value), self.assertRaises(ctl.CtlError) as cm:
                    client().request(verb, args)
                self.assertEqual(cm.exception.kind, "not_allowed")
        for value in (-1, 2 ** 40 + 1, True, False, "5", 1.0, None):
            with self.subTest(what="offset", value=value), self.assertRaises(ctl.CtlError):
                client().request("job-log", {"job": JOB, "offset": value})
        for value in (0, 201, True, "5", 1.5, None):
            with self.subTest(what="lines", value=value), self.assertRaises(ctl.CtlError):
                client().request("audit-tail", {"lines": value})

    def test_a_request_that_is_too_long_is_refused(self):
        c = client()
        self.assertLess(len(c.request("show", {"site": "a" * 24}).encode()), ctl.MAX_REQUEST_BYTES)
        orig = ctl.MAX_REQUEST_BYTES
        try:
            ctl.MAX_REQUEST_BYTES = 10
            with self.assertRaises(ctl.CtlError):
                c.request("show", {"site": "alpha"})
        finally:
            ctl.MAX_REQUEST_BYTES = orig

    def test_nothing_is_sent_when_the_request_is_refused(self):
        run = Fake()
        c = client(run)
        for verb, args in (("delete", {"site": "alpha"}), ("show", {"site": "../x"}), ("list", {"x": 1})):
            with self.assertRaises(ctl.CtlError):
                c.call(verb, **args)
        self.assertEqual(run.calls, [])


class Command(unittest.TestCase):
    def test_ssh_gets_an_argument_list_with_no_remote_command_and_no_prompts_or_forwarding(self):
        cmd = client().command()
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], "ssh")
        self.assertEqual(cmd[-1], "wpctl@dellwp", "the destination is last: nothing follows it, so no remote command")
        for need in ("BatchMode=yes", "IdentitiesOnly=yes", "PasswordAuthentication=no", "StrictHostKeyChecking=yes", "UserKnownHostsFile=/run/secrets/known_hosts", "GlobalKnownHostsFile=/dev/null",
                     "ForwardAgent=no", "ClearAllForwardings=yes", "ConnectTimeout=10"):
            self.assertIn(need, cmd)
        self.assertIn("-T", cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], "/run/secrets/panel_key")
        self.assertNotIn("StrictHostKeyChecking=no", cmd)
        self.assertNotIn("StrictHostKeyChecking=accept-new", cmd)

    def test_a_host_or_user_that_is_not_a_plain_name_is_refused(self):
        for bad in ("", "-oProxyCommand=x", "a b", "a;b", "a@b", "x" * 80, None, "$(id)", "a\n"):
            with self.subTest(repr(bad)), self.assertRaises(ValueError):
                ctl.Ctl(bad, "wpctl", "k", "kh")
            with self.subTest("user " + repr(bad)), self.assertRaises(ValueError):
                ctl.Ctl("dellwp", bad, "k", "kh")


class Call(unittest.TestCase):
    def test_a_good_answer_is_returned_and_the_call_is_made_the_safe_way(self):
        run = Fake(stdout=ok(sites=[{"site": "alpha"}]))
        reply = client(run, timeout_s=12).call("list")
        self.assertEqual(reply, {"ok": True, "sites": [{"site": "alpha"}]})
        (cmd, kw), = run.calls
        self.assertEqual(cmd, client().command())
        self.assertEqual(kw["input"], b'{"verb":"list"}\n')
        self.assertEqual(kw["timeout"], 12)
        self.assertEqual(kw["env"], {"PATH": "/usr/bin:/bin"}, "ssh sees nothing of the app's environment")
        self.assertTrue(kw["capture_output"])
        self.assertNotIn("shell", kw)

    def test_the_arguments_travel_in_the_request_not_on_the_command_line(self):
        run = Fake(stdout=ok(job="x"))
        client(run).call("job-log", job=JOB, offset=0)
        (cmd, kw), = run.calls
        self.assertNotIn(JOB, " ".join(cmd))
        self.assertEqual(json.loads(kw["input"]), {"verb": "job-log", "job": JOB, "offset": 0})

    def test_no_answer_in_time_and_no_ssh_are_unreachable(self):
        for exc in (subprocess.TimeoutExpired("ssh", 30), FileNotFoundError(2, "No such file or directory"), PermissionError(13, "Permission denied")):
            with self.subTest(type(exc).__name__), self.assertRaises(ctl.CtlError) as cm:
                client(Fake(raises=exc)).call("list")
            self.assertEqual(cm.exception.kind, "unreachable")

    def test_status_255_is_unreachable_and_shows_one_clean_line_of_sshs_own_message(self):
        run = Fake(returncode=255, stderr=b"ssh: connect to host dellwp port 22: Connection timed out\nsecond line\n")
        with self.assertRaises(ctl.CtlError) as cm:
            client(run).call("list")
        self.assertEqual(cm.exception.kind, "unreachable")
        self.assertIn("Connection timed out", str(cm.exception))
        self.assertNotIn("second line", str(cm.exception))
        run = Fake(returncode=255, stderr=b"\x1b[31mboom\x00\nx")
        with self.assertRaises(ctl.CtlError) as cm:
            client(run).call("list")
        self.assertNotRegex(str(cm.exception), r"[\x00-\x08\x0b-\x1f\x7f]")
        self.assertNotIn("/run/secrets", str(cm.exception), "the key's path is never in a message")

    def test_wp_ctl_saying_no_is_refused_with_its_code_and_failing_is_failed(self):
        run = Fake(stdout=json.dumps({"ok": False, "error": "unknown_site", "detail": "there is no site called x"}).encode() + b"\n", returncode=2)
        with self.assertRaises(ctl.CtlError) as cm:
            client(run).call("show", site="alpha")
        self.assertEqual((cm.exception.kind, cm.exception.code), ("refused", "unknown_site"))
        self.assertIn("there is no site called x", str(cm.exception))
        run = Fake(stdout=json.dumps({"ok": False, "error": "tool_failed", "ran": True}).encode() + b"\n", returncode=1)
        with self.assertRaises(ctl.CtlError) as cm:
            client(run).call("list")
        self.assertEqual((cm.exception.kind, cm.exception.code), ("failed", "tool_failed"))

    def test_an_error_code_or_detail_that_is_not_text_is_ignored_not_trusted(self):
        run = Fake(stdout=b'{"ok": false, "error": {"x": 1}, "detail": [1]}\n', returncode=2)
        with self.assertRaises(ctl.CtlError) as cm:
            client(run).call("list")
        self.assertEqual((cm.exception.kind, cm.exception.code), ("refused", None))

    def test_anything_that_is_not_exactly_one_json_object_is_a_bad_reply(self):
        for out in (b"", b"\n", b"hello\n", b"[1]\n", b'"ok"\n', b'{"ok": true}\n{"ok": true}\n', b'{"ok": true}\ngarbage\n', b'{"x": 1}\n', b'{"ok": "yes"}\n', b'{"ok": 1}\n', b"\xff\xfe\n", b"{not json}\n"):
            with self.subTest(out), self.assertRaises(ctl.CtlError) as cm:
                client(Fake(stdout=out)).call("list")
            self.assertEqual(cm.exception.kind, "bad_reply")

    def test_an_answer_and_an_exit_status_that_disagree_are_a_bad_reply(self):
        for out, rc in ((ok(), 1), (ok(), 2), (b'{"ok": false, "error": "x"}\n', 0)):
            with self.subTest(out=out, rc=rc), self.assertRaises(ctl.CtlError) as cm:
                client(Fake(stdout=out, returncode=rc)).call("list")
            self.assertEqual(cm.exception.kind, "bad_reply")

    def test_an_answer_larger_than_the_limit_is_refused(self):
        big = ok(blob="x" * (ctl.MAX_REPLY_BYTES + 10))
        with self.assertRaises(ctl.CtlError) as cm:
            client(Fake(stdout=big)).call("list")
        self.assertEqual(cm.exception.kind, "bad_reply")
        self.assertIn("larger than this page accepts", str(cm.exception), "said so, not mistaken for a broken answer")

    def test_the_message_of_a_bad_reply_is_one_clean_line(self):
        with self.assertRaises(ctl.CtlError) as cm:
            client(Fake(stdout=b"\x1b[2J<script>alert(1)</script>\nmore\n")).call("list")
        self.assertNotIn("\n", str(cm.exception))
        self.assertNotIn("\x1b", str(cm.exception))


class RealProcess(unittest.TestCase):
    """The same client against a real program standing in for ssh: the request on its standard input, the environment it gets, and the exit statuses."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.tmp = Path(self.d.name)
        self.ssh = self.tmp / "ssh"
        self.ssh.write_text(f"""#!{sys.executable}
import os, sys
open({str(self.tmp / 'argv')!r}, 'w').write(repr(sys.argv[1:]))
open({str(self.tmp / 'stdin')!r}, 'wb').write(sys.stdin.buffer.read())
open({str(self.tmp / 'env')!r}, 'w').write(repr(sorted(os.environ)))
sys.stdout.write(open({str(self.tmp / 'reply')!r}).read())
sys.exit(int(open({str(self.tmp / 'rc')!r}).read()))
""")
        self.ssh.chmod(self.ssh.stat().st_mode | stat.S_IXUSR)
        (self.tmp / "reply").write_text('{"ok": true, "sites": []}\n')
        (self.tmp / "rc").write_text("0")
        self.addCleanup(self.d.cleanup)

    def test_the_request_is_the_whole_standard_input_and_the_environment_holds_only_a_path(self):
        os.environ["PANEL_CANARY"] = "must-not-reach-ssh"
        os.environ["SSH_AUTH_SOCK"] = "/tmp/agent"
        self.addCleanup(os.environ.pop, "PANEL_CANARY", None)
        self.addCleanup(os.environ.pop, "SSH_AUTH_SOCK", None)
        c = ctl.Ctl("dellwp", "wpctl", "/k", "/kh", ssh=str(self.ssh), path=os.environ["PATH"])
        self.assertEqual(c.call("show", site="alpha"), {"ok": True, "sites": []})
        self.assertEqual((self.tmp / "stdin").read_bytes(), b'{"site":"alpha","verb":"show"}\n')
        env = eval((self.tmp / "env").read_text())
        self.assertNotIn("PANEL_CANARY", env)
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertLessEqual(set(env) - {"PATH", "LC_CTYPE", "LANG"}, set())
        argv = eval((self.tmp / "argv").read_text())
        self.assertEqual(argv[-1], "wpctl@dellwp")

    def test_exit_statuses_of_the_real_program_map_to_the_kinds(self):
        c = ctl.Ctl("dellwp", "wpctl", "/k", "/kh", ssh=str(self.ssh), path=os.environ["PATH"])
        (self.tmp / "reply").write_text('{"ok": false, "error": "unknown_site"}\n')
        (self.tmp / "rc").write_text("2")
        with self.assertRaises(ctl.CtlError) as cm:
            c.call("show", site="alpha")
        self.assertEqual(cm.exception.kind, "refused")
        (self.tmp / "reply").write_text("")
        (self.tmp / "rc").write_text("255")
        with self.assertRaises(ctl.CtlError) as cm:
            c.call("list")
        self.assertEqual(cm.exception.kind, "unreachable")

    def test_a_program_that_never_answers_is_cut_off(self):
        slow = self.tmp / "slow"
        slow.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        slow.chmod(slow.stat().st_mode | stat.S_IXUSR)
        c = ctl.Ctl("dellwp", "wpctl", "/k", "/kh", ssh=str(slow), timeout_s=1, path=os.environ["PATH"])
        with self.assertRaises(ctl.CtlError) as cm:
            c.call("list")
        self.assertEqual(cm.exception.kind, "unreachable")


class Source(unittest.TestCase):
    def test_the_module_uses_the_standard_library_only_and_never_a_shell(self):
        text = (ROOT / "app/wp_panel/ctl.py").read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for bad in ("shell=True", "os.system", "os.popen", "import flask", "import requests", "eval(", "exec("):
            self.assertNotIn(bad, code, bad)
        imports = {l.split()[1].split(".")[0] for l in text.splitlines() if l.startswith(("import ", "from "))}
        self.assertLessEqual(imports, {"json", "re", "subprocess"})


if __name__ == "__main__":
    unittest.main()
