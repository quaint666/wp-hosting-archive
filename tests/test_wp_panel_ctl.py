#!/usr/bin/env python3
"""Offline tests of app/wp_panel/ctl.py, the app's one way to dellwp.

A stand-in `run` records what would be executed and answers like wp-ctl does; a second group of tests runs a real stand-in `ssh` program to see the request on its standard input and the environment it is
given. They prove: only the read verbs go out, with exactly the arguments and ranges wp-ctl has (cross-checked against the dispatcher's own table); every value is checked before anything is sent and a
value cannot carry a second request; ssh is called with an argument list, no remote command and a clean environment; every way an answer can go wrong becomes a CtlError of the right kind, with no
traceback and no path of the key in the message. They do not prove the real ssh, the forced command or dellwp: the rehearsal on the VM does. Run: python3 -I tests/test_wp_panel_ctl.py"""
import hashlib
import json
import time
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

    def test_no_verb_that_changes_anything_is_in_the_read_list(self):
        reads = {v for v, spec in wp_ctl.VERBS.items() if spec.kind == "read"}
        self.assertTrue(set(ctl.READ_VERBS) <= reads)
        self.assertFalse(set(ctl.READ_VERBS) & set(ctl.ACTION_VERBS))

    def test_the_action_verbs_are_exactly_these_and_each_is_a_job_or_inline_verb_with_the_dispatchers_argument_names(self):
        self.assertEqual(set(ctl.ACTION_VERBS), {"create", "set", "ensure-state", "delete", "import-run", "import-approve", "import-reset", "import-discard", "import-new", "update-check", "update-apply"},
                         "a new action verb is a decision of its batch: add it here on purpose")
        for verb, spec in ctl.ACTION_VERBS.items():
            theirs = wp_ctl.VERBS[verb]
            self.assertIn(theirs.kind, ("job", "inline"), verb)
            self.assertEqual(set(spec), set(theirs.args), verb)
            for name, (required, _) in spec.items():
                optional = theirs.args[name].kind == "flag" or name == "confirm_name" and verb in ("ensure-state", "import-run", "import-approve")      # a flag is false when absent; these confirmations are asked for by another argument
                self.assertEqual(required, theirs.args[name].required and not optional, (verb, name))
        self.assertEqual(ctl.ALLOWED.keys(), set(ctl.READ_VERBS) | set(ctl.ACTION_VERBS) | set(ctl.SECRET_VERBS) | set(ctl.STREAM_VERBS))
        self.assertEqual(set(ctl.STREAM_VERBS), {"import-receive"})
        self.assertEqual(wp_ctl.VERBS["import-receive"].kind, "stream")
        self.assertEqual(set(ctl.STREAM_VERBS["import-receive"]), set(wp_ctl.VERBS["import-receive"].args))
        for verb in ("import-report", "import-list", "vuln-check"):
            self.assertEqual(ctl.READ_VERBS[verb].keys(), wp_ctl.VERBS[verb].args.keys(), verb)
            self.assertEqual(wp_ctl.VERBS[verb].kind, "read", verb)

    def test_the_import_limits_and_patterns_are_the_dispatchers_and_the_intake_quotas(self):
        self.assertEqual(ctl.REJECT_RE.pattern, wp_ctl.REJECT_RE.pattern)
        self.assertEqual(ctl.FILE_RE.pattern, wp_ctl.IMPORT_FILE_RE.pattern)
        import yaml
        self.assertEqual(ctl.UPLOAD_MAX, yaml.safe_load((ROOT / "host_vars/dellwp/vars.yml").read_text())["wp_intake_quota_mb"] * 1024 * 1024)
        limits = yaml.safe_load((ROOT / "host_vars/dellwp/vars.yml").read_text())["wp_ctl_limits"]
        self.assertGreater(ctl.STREAM_TIMEOUT_S, limits["import-receive"]["timeout_s"])

    def test_every_verb_that_is_in_neither_list_is_refused(self):
        for verb in set(wp_ctl.VERBS) - set(ctl.ALLOWED):
            with self.assertRaises(ctl.CtlError) as cm:
                client().request(verb, {"site": "alpha", "confirm_name": "alpha", "confirm_again": "alpha", "file": "x", "user": "u", "name": "alpha"})
            self.assertEqual(cm.exception.kind, "not_allowed", verb)
            self.assertIn("may not ask", str(cm.exception), verb)

    def test_the_editable_keys_the_states_and_the_limits_are_the_dispatchers(self):
        self.assertEqual(ctl.SET_KEYS, wp_ctl.SET_KEYS)
        self.assertEqual(ctl.STATES, wp_ctl.STATES)
        self.assertEqual(ctl.SET_VALUE_MAX, wp_ctl.SET_VALUE_MAX)
        self.assertEqual(ctl.LABEL_RE.pattern, wp_ctl.LABEL_RE.pattern)
        self.assertEqual(ctl.REQ_RE.pattern, wp_ctl.REQ_RE.pattern)
        self.assertEqual(ctl.CONTROL_RE.pattern, wp_ctl.CONTROL_RE.pattern)
        self.assertEqual(ctl.NAME_RE.pattern, wp_ctl.NAME_RE.pattern)

    def test_the_names_that_are_never_editable_are_not_keys(self):
        for k in ("name", "port", "quota_id", "state", "secret_rev", "created", "schema", "cpus"):
            self.assertNotIn(k, ctl.SET_KEYS)

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
        for verb in ("delete", "password", "import-run", "apply", "install", "import-receive", "import-approve", "import-reset", "import-discard", "backup", "check-name", "", "LIST", "list ", None, 5, ["list"], {"verb": "list"}):
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
        self.assertEqual(cmd[cmd.index("-F") + 1], "/dev/null", "no ssh_config of the system or the user is read: every option is on this command line")
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
        self.assertLessEqual(imports, {"hashlib", "json", "re", "subprocess", "threading"})


class Actions(unittest.TestCase):
    def line(self, verb, **args):
        return json.loads(client().request(verb, args))

    def refused(self, verb, **args):
        with self.assertRaises(ctl.CtlError) as cm:
            client().request(verb, args)
        self.assertEqual(cm.exception.kind, "not_allowed", (verb, args))
        return str(cm.exception)

    def test_create_set_and_ensure_state_are_sent_as_one_json_line_with_the_request_id(self):
        self.assertEqual(self.line("create", name="delta", label="Delta", req="pa1"), {"verb": "create", "name": "delta", "label": "Delta", "req": "pa1"})
        self.assertEqual(self.line("create", name="delta"), {"verb": "create", "name": "delta"})
        self.assertEqual(self.line("set", site="alpha", key="note", value="", req="pa1"), {"verb": "set", "site": "alpha", "key": "note", "value": "", "req": "pa1"})
        self.assertEqual(self.line("ensure-state", site="alpha", state="hold", req="pa1"), {"verb": "ensure-state", "site": "alpha", "state": "hold", "req": "pa1"})
        self.assertEqual(self.line("ensure-state", site="alpha", state="live", confirm_name="alpha"), {"verb": "ensure-state", "site": "alpha", "state": "live", "confirm_name": "alpha"})
        self.assertEqual(client().request("create", {"name": "delta"}).count("\n"), 1)

    def test_the_request_id_is_only_for_actions_and_must_look_like_wp_ctls(self):
        self.refused("show", site="alpha", req="p1")
        self.refused("jobs", req="p1")
        for req in ("", " ", "a b", "x" * 65, "a/b", "a\n", "日本", None, 5, ["p"]):
            with self.subTest(req=req):
                self.refused("create", name="delta", req=req)
        self.line("create", name="delta", req="x" * 64)
        self.line("create", name="delta", req="a.b_c-D9")

    def test_create_refuses_a_name_that_is_not_a_site_name_and_a_label_that_is_not_a_label(self):
        for name in ("", "A", "a", "a--b", "-ab", "ab-", "a_b", "x" * 25, "../x", "ab\n", 'a","verb":"delete', "日本", None, 5, True, ["delta"]):
            with self.subTest(name=name):
                self.refused("create", name=name)
        for label in ("", " x", "x ", "x" * 101, "a\nb", "a\x00b", "a\x7fb", None, 5, ["x"]):
            with self.subTest(label=label):
                self.refused("create", name="delta", label=label)
        self.line("create", name="delta", label="x")
        self.line("create", name="delta", label="x" * 100)
        self.refused("create")
        self.refused("create", name="delta", site="alpha")
        self.refused("create", name="delta", note="x")

    def test_set_refuses_a_key_that_is_not_editable_and_a_value_that_is_long_or_has_control_characters(self):
        for key in ("name", "port", "state", "", "Note", "note ", None, 5, ["note"]):
            with self.subTest(key=key):
                self.refused("set", site="alpha", key=key, value="x")
        for value in ("x" * 501, "a\nb", "a\x00b", "a\x1fb", "a\x7fb", None, 5, True, ["x"]):
            with self.subTest(value=value):
                self.refused("set", site="alpha", key="note", value=value)
        for key in ctl.SET_KEYS:
            self.line("set", site="alpha", key=key, value="x" * 500)
        self.refused("set", site="alpha", key="note")
        self.refused("set", key="note", value="x")
        self.refused("set", site="Alpha", key="note", value="x")
        self.refused("set", site="alpha", key="note", value="x", state="hold")

    def test_the_longest_set_request_fits_the_size_limit_even_when_every_character_is_escaped(self):
        line = client().request("set", {"site": "a" * 24, "key": "note", "value": "é" * 500, "req": "x" * 64})
        self.assertLessEqual(len(line.encode()), ctl.MAX_REQUEST_BYTES)

    def test_ensure_state_wants_one_of_the_three_states_and_the_name_typed_only_for_live(self):
        for state in ("", "closed", "LIVE", "live,hold", "stopped ", None, 5):
            with self.subTest(state=state):
                self.refused("ensure-state", site="alpha", state=state)
        self.assertIn("name", self.refused("ensure-state", site="alpha", state="live"))
        for typed in ("", "bravo", "Alpha", "alpha ", None, 5):
            with self.subTest(typed=typed):
                self.refused("ensure-state", site="alpha", state="live", confirm_name=typed)
        for state in ("hold", "stopped"):
            self.refused("ensure-state", site="alpha", state=state, confirm_name="alpha")
        self.refused("ensure-state", site="alpha")
        self.refused("ensure-state", state="hold")

    def test_an_action_is_sent_to_the_same_forced_command_and_a_refusal_comes_back_as_refused(self):
        fake = Fake(stdout=(json.dumps({"ok": False, "error": "name_reserved", "detail": "hub is reserved"}) + "\n").encode(), returncode=2)
        with self.assertRaises(ctl.CtlError) as cm:
            client(run=fake).call("create", name="hub", req="p1")
        self.assertEqual((cm.exception.kind, cm.exception.code), ("refused", "name_reserved"))
        cmd, kw = fake.calls[0]
        self.assertEqual(json.loads(kw["input"]), {"verb": "create", "name": "hub", "req": "p1"})
        self.assertEqual(cmd[-1], "wpctl@dellwp", "the remote command is never named: the key is forced to wp-ctl")


class Secrets(unittest.TestCase):
    PW = "correct horse battery 7"

    def test_the_password_is_the_second_line_of_the_input_and_nowhere_else(self):
        fake = Fake(stdout=ok(rc=0, ran=True, out="Success\n"))
        client(run=fake).call("password", secret=self.PW, site="alpha", user="admin")
        cmd, kw = fake.calls[0]
        first, second, rest = kw["input"].decode().split("\n")
        self.assertEqual((json.loads(first), second, rest), ({"verb": "password", "site": "alpha", "user": "admin"}, self.PW, ""))
        self.assertNotIn(self.PW, " ".join(cmd) + str(kw["env"]) + first)

    def test_install_sends_its_arguments_in_the_first_line_and_the_password_in_the_second(self):
        fake = Fake(stdout=ok(rc=0, ran=True, out="done\n"))
        client(run=fake).call("install", secret=self.PW, site="alpha", title="Alpha shop", user="owner", email="o@example.com")
        first, second, _ = fake.calls[0][1]["input"].decode().split("\n")
        self.assertEqual(json.loads(first), {"verb": "install", "site": "alpha", "title": "Alpha shop", "user": "owner", "email": "o@example.com"})
        self.assertEqual(second, self.PW)

    def test_a_password_that_is_short_long_not_text_or_has_a_control_character_is_refused_without_being_sent(self):
        for pw in (None, "", "short", "x" * 13, "x" * 201, "a" * 14 + "\n", "a" * 14 + "\nb", "a" * 14 + "\x00", "a" * 14 + "\x7f", 12345678901234, [self.PW], b"x" * 20):
            fake = Fake(stdout=ok())
            with self.subTest(pw=repr(pw)[:12]), self.assertRaises(ctl.CtlError) as cm:
                client(run=fake).call("password", secret=pw, site="alpha", user="admin")
            self.assertEqual((cm.exception.kind, fake.calls), ("not_allowed", []))
            if isinstance(pw, str) and len(pw) > 5:
                self.assertNotIn(pw, str(cm.exception))
        for pw in ("a" * 14, "a" * 200, "pässwörd with spaces 99"):
            client(run=Fake(stdout=ok(rc=0, ran=True, out=""))).call("password", secret=pw, site="alpha", user="admin")

    def test_only_a_secret_verb_takes_a_password_and_it_must_have_one(self):
        for verb, args in (("show", {"site": "alpha"}), ("set", {"site": "alpha", "key": "note", "value": "x"}), ("jobs", {})):
            fake = Fake(stdout=ok())
            with self.subTest(verb=verb), self.assertRaises(ctl.CtlError) as cm:
                client(run=fake).call(verb, secret=self.PW, **args)
            self.assertEqual((cm.exception.kind, fake.calls), ("not_allowed", []))
            self.assertNotIn(self.PW, str(cm.exception))
        with self.assertRaises(ctl.CtlError):
            client(run=Fake(stdout=ok())).call("password", site="alpha", user="admin")

    def test_the_password_cannot_be_smuggled_into_an_argument(self):
        for args in ({"site": "alpha", "user": "admin", "password": self.PW}, {"site": "alpha", "user": "admin\n" + self.PW}):
            fake = Fake(stdout=ok())
            with self.assertRaises(ctl.CtlError):
                client(run=fake).call("password", secret=self.PW, **args)
            self.assertEqual(fake.calls, [])

    def test_the_user_and_the_install_arguments_must_look_the_way_wp_ctl_wants(self):
        for user in ("", "a b", "a;b", "x" * 61, "日本", "a\nb", None, 5):
            with self.subTest(user=user), self.assertRaises(ctl.CtlError):
                client().request("password", {"site": "alpha", "user": user})
        for user in ("ab", "x" * 31, "a b", "a@b", "a\nb"):
            with self.subTest(admin=user), self.assertRaises(ctl.CtlError):
                client().request("install", {"site": "alpha", "title": "T", "user": user, "email": "o@example.com"})
        for email in ("", "nope", "a@b", "a b@c.d", "a@b@c.d", "a@b.c\n", "x" * 250 + "@a.bc"):
            with self.subTest(email=email), self.assertRaises(ctl.CtlError):
                client().request("install", {"site": "alpha", "title": "T", "user": "owner", "email": email})
        for title in ("", " x", "x ", "x" * 101, "a\nb"):
            with self.subTest(title=title), self.assertRaises(ctl.CtlError):
                client().request("install", {"site": "alpha", "title": title, "user": "owner", "email": "o@example.com"})

    def test_a_tool_that_failed_is_reported_with_the_end_of_its_words_and_without_the_password(self):
        out = "wp-site-password: the user nobody does not exist (REDACTED)"
        fake = Fake(stdout=(json.dumps({"ok": False, "rc": 1, "ran": True, "out": out + " " + self.PW}) + "\n").encode(), returncode=1)
        with self.assertRaises(ctl.CtlError) as cm:
            client(run=fake).call("password", secret=self.PW, site="alpha", user="nobody")
        self.assertEqual(cm.exception.kind, "failed")
        self.assertIn("does not exist", str(cm.exception))
        self.assertNotIn(self.PW, str(cm.exception))

    def test_a_password_with_spaces_is_redacted_even_when_the_tool_spreads_it_over_a_long_text(self):
        pw = "two  spaces   and   more words 12"
        out = "x " * 400 + "failed with " + pw + " in it"
        fake = Fake(stdout=(json.dumps({"ok": False, "rc": 1, "ran": True, "out": out}) + "\n").encode(), returncode=1)
        with self.assertRaises(ctl.CtlError) as cm:
            client(run=fake).call("password", secret=pw, site="alpha", user="admin")
        self.assertNotIn("spaces", str(cm.exception).replace("REDACTED", ""))
        self.assertIn("REDACTED", str(cm.exception))

    def test_install_and_password_wait_longer_than_the_other_calls_and_nothing_else_does(self):
        fake = Fake(stdout=ok(rc=0, ran=True, out=""))
        client(run=fake, timeout_s=30).call("install", secret=self.PW, site="alpha", title="T", user="owner", email="o@example.com")
        client(run=fake, timeout_s=30).call("password", secret=self.PW, site="alpha", user="admin")
        client(run=fake, timeout_s=30).call("set", site="alpha", key="note", value="x")
        self.assertEqual([kw["timeout"] for _, kw in fake.calls], [660, 330, 30])
        slow = Fake(raises=subprocess.TimeoutExpired("ssh", 660))
        with self.assertRaises(ctl.CtlError) as cm:
            client(run=slow).call("install", secret=self.PW, site="alpha", title="T", user="owner", email="o@example.com")
        self.assertIn("660 seconds", str(cm.exception))

    def test_delete_needs_the_name_typed_twice_and_exactly(self):
        line = json.loads(client().request("delete", {"site": "alpha", "confirm_name": "alpha", "confirm_again": "alpha", "req": "pd1"}))
        self.assertEqual(line, {"verb": "delete", "site": "alpha", "confirm_name": "alpha", "confirm_again": "alpha", "req": "pd1"})
        for a, b in (("alpha", "bravo"), ("bravo", "alpha"), ("bravo", "bravo"), ("Alpha", "alpha"), ("alpha", "alpha "), ("alpha", None), (None, "alpha")):
            with self.subTest(a=a, b=b), self.assertRaises(ctl.CtlError) as cm:
                client().request("delete", {"site": "alpha", "confirm_name": a, "confirm_again": b})
            self.assertEqual(cm.exception.kind, "not_allowed")
        for missing in ("confirm_name", "confirm_again", "site"):
            args = {"site": "alpha", "confirm_name": "alpha", "confirm_again": "alpha"}
            del args[missing]
            with self.assertRaises(ctl.CtlError):
                client().request("delete", args)


STAND_IN = """#!{python}
import hashlib, json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
mode = open(os.path.join(here, "mode")).read().strip()
first = sys.stdin.buffer.readline()
request = json.loads(first)
open(os.path.join(here, "request"), "w").write(json.dumps(request))
if mode == "refuse":
    print(json.dumps({{"ok": False, "error": "already_there", "detail": "x.wpress is already in the intake directory"}}))
    sys.exit(2)
if mode == "hang":
    time.sleep(30)
h, n = hashlib.sha256(), 0
while True:
    b = sys.stdin.buffer.read(1 << 16)
    if not b:
        break
    h.update(b)
    n += len(b)
open(os.path.join(here, "eof"), "w").write("%d" % n)
digest = "0" * 64 if mode == "lie" else h.hexdigest()
print(json.dumps({{"ok": True, "rc": 0, "ran": True, "out": "received %s %d %s\\n" % (digest, n, request["file"])}}))
"""


class Stream(unittest.TestCase):
    def setUp(self):
        import stat
        import tempfile
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.ssh = os.path.join(self.d.name, "ssh")
        with open(self.ssh, "w") as f:
            f.write(STAND_IN.format(python=sys.executable))
        os.chmod(self.ssh, os.stat(self.ssh).st_mode | stat.S_IXUSR)
        self.mode("ok")

    def mode(self, m):
        with open(os.path.join(self.d.name, "mode"), "w") as f:
            f.write(m)

    def seen(self, name):
        with open(os.path.join(self.d.name, name)) as f:
            return f.read()

    def client(self):
        return ctl.Ctl("dellwp", "wpctl", "/k", "/kh", ssh=self.ssh, path=os.environ["PATH"])

    def test_every_byte_goes_through_once_in_order_and_the_hash_and_count_are_checked_against_dellwps_line(self):
        data = os.urandom(5 * 1024 * 1024 + 123)
        chunks = (data[i:i + 65536] for i in range(0, len(data), 65536))
        got = self.client().stream("import-receive", chunks, file="site-one.wpress")
        self.assertEqual(got, {"file": "site-one.wpress", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(json.loads(self.seen("request")), {"verb": "import-receive", "file": "site-one.wpress"})
        self.assertEqual(self.seen("eof"), str(len(data)))

    def test_the_chunks_are_pulled_one_at_a_time_not_collected_first(self):
        pulled = []

        def gen():
            for i in range(20):
                pulled.append(i)
                yield b"x" * 1000
        got = self.client().stream("import-receive", gen(), file="a1.wpress")
        self.assertEqual((got["bytes"], pulled[-1]), (20000, 19))

    def test_an_account_that_does_not_match_what_was_sent_is_not_trusted(self):
        self.mode("lie")
        with self.assertRaises(ctl.CtlError) as cm:
            self.client().stream("import-receive", iter([b"abc" * 100]), file="a1.wpress")
        self.assertEqual(cm.exception.kind, "bad_reply")

    def test_a_refusal_before_the_bytes_are_read_is_a_refusal_not_a_broken_pipe(self):
        self.mode("refuse")
        big = (b"x" * 65536 for _ in range(400))                    # far more than a pipe holds
        with self.assertRaises(ctl.CtlError) as cm:
            self.client().stream("import-receive", big, file="a1.wpress")
        self.assertEqual((cm.exception.kind, cm.exception.code), ("refused", "already_there"))

    def test_a_chunk_source_that_fails_stops_ssh_and_the_failure_goes_on(self):
        class Gone(Exception):
            pass

        def gen():
            yield b"x" * 1000
            raise Gone()
        with self.assertRaises(Gone):
            self.client().stream("import-receive", gen(), file="a1.wpress")

    def test_ssh_is_dead_and_collected_when_the_chunk_source_fails(self):
        seen = []

        def spy(*a, **kw):
            seen.append(subprocess.Popen(*a, **kw))
            return seen[-1]

        def gen():
            yield b"x" * 1000
            raise KeyError("gone")
        with self.assertRaises(KeyError):
            self.client().stream("import-receive", gen(), popen=spy, file="a1.wpress")
        self.assertIsNotNone(seen[0].poll(), "the process was stopped and waited for, not left running")

    def test_it_works_where_communicate_refuses_a_stdin_that_is_closed(self):
        """Python before 3.13 raises ValueError('flush of closed file') when communicate() is given a Popen whose stdin the caller closed (the CI runner has 3.12). The client must not leave it closed."""
        class Strict(subprocess.Popen):
            def communicate(self, *a, **kw):
                if self.stdin is not None and self.stdin.closed:
                    raise ValueError("flush of closed file")
                return super().communicate(*a, **kw)
        got = self.client().stream("import-receive", iter([b"abc" * 100]), popen=Strict, file="a1.wpress")
        self.assertEqual(got["bytes"], 300)
        self.mode("refuse")
        with self.assertRaises(ctl.CtlError) as cm:
            self.client().stream("import-receive", (b"x" * 65536 for _ in range(400)), popen=Strict, file="a1.wpress")
        self.assertEqual(cm.exception.kind, "refused")

    def test_a_stalled_ssh_is_stopped_after_the_time_limit(self):
        self.mode("hang")
        old = ctl.STREAM_TIMEOUT_S
        ctl.STREAM_TIMEOUT_S = 1
        self.addCleanup(setattr, ctl, "STREAM_TIMEOUT_S", old)
        t0 = time.time()
        with self.assertRaises(ctl.CtlError):
            self.client().stream("import-receive", iter([b"abc"]), file="a1.wpress")
        self.assertLess(time.time() - t0, 10)

    def test_only_a_stream_verb_streams_and_a_stream_verb_is_not_called(self):
        for verb in ("show", "set", "password", "import-run", "delete", "", None):
            with self.subTest(verb=verb), self.assertRaises(ctl.CtlError) as cm:
                self.client().stream(verb, iter([b"x"]), file="a1.wpress")
            self.assertEqual(cm.exception.kind, "not_allowed")
        with self.assertRaises(ctl.CtlError) as cm:
            self.client().call("import-receive", file="a1.wpress")
        self.assertEqual(cm.exception.kind, "not_allowed")

    def test_a_file_name_that_is_not_a_plain_wpress_name_is_refused_before_ssh_starts(self):
        for name in ("", "a", "a.zip", ".hidden.wpress", "../a.wpress", "a/b.wpress", "a b.wpress", "a.wpress\n", "x" * 100 + ".wpress", None, 5):
            with self.subTest(name=name), self.assertRaises(ctl.CtlError) as cm:
                self.client().stream("import-receive", iter([b"x"]), file=name)
            self.assertEqual(cm.exception.kind, "not_allowed")
            self.assertFalse(os.path.exists(os.path.join(self.d.name, "request")))


class Imports(unittest.TestCase):
    def line(self, verb, **args):
        return json.loads(client().request(verb, args))

    def refused(self, verb, **args):
        with self.assertRaises(ctl.CtlError) as cm:
            client().request(verb, args)
        self.assertEqual(cm.exception.kind, "not_allowed", (verb, args))

    def test_the_four_import_actions_and_the_report_are_sent_as_wp_ctl_wants_them(self):
        self.assertEqual(self.line("import-report", site="alpha"), {"verb": "import-report", "site": "alpha"})
        self.assertEqual(self.line("import-run", site="alpha", file="a1.wpress", plan=True, req="p1"), {"verb": "import-run", "site": "alpha", "file": "a1.wpress", "plan": True, "req": "p1"})
        self.assertEqual(self.line("import-run", site="alpha", file="a1.wpress"), {"verb": "import-run", "site": "alpha", "file": "a1.wpress"})
        self.assertEqual(self.line("import-run", site="alpha", file="a1.wpress", replace=True, confirm_name="alpha")["confirm_name"], "alpha")
        self.assertEqual(self.line("import-approve", site="alpha", reject="akismet,hello-dolly", accept_failures=True, confirm_name="alpha"),
                         {"verb": "import-approve", "site": "alpha", "reject": "akismet,hello-dolly", "accept_failures": True, "confirm_name": "alpha"})
        self.assertEqual(self.line("import-reset", site="alpha", confirm_name="alpha"), {"verb": "import-reset", "site": "alpha", "confirm_name": "alpha"})
        self.assertEqual(self.line("import-discard", file="a1.wpress", if_present=False), {"verb": "import-discard", "file": "a1.wpress", "if_present": False})

    def test_the_follow_up_verbs_are_sent_as_wp_ctl_wants_them(self):
        self.assertEqual(self.line("import-list"), {"verb": "import-list"})
        self.assertEqual(self.line("vuln-check", site="alpha"), {"verb": "vuln-check", "site": "alpha"})
        self.assertEqual(self.line("update-check", site="alpha", req="p1"), {"verb": "update-check", "site": "alpha", "req": "p1"})
        self.assertEqual(self.line("update-apply", site="alpha"), {"verb": "update-apply", "site": "alpha"})
        self.assertEqual(self.line("import-new", name="newshop", file="a1.wpress", label="Shop", req="p1"), {"verb": "import-new", "name": "newshop", "file": "a1.wpress", "label": "Shop", "req": "p1"})
        self.assertEqual(self.line("import-new", name="newshop", file="a1.wpress"), {"verb": "import-new", "name": "newshop", "file": "a1.wpress"})

    def test_a_read_takes_no_request_id_and_import_list_takes_no_argument(self):
        self.refused("import-list", site="alpha")
        self.refused("import-list", req="p1")
        self.refused("vuln-check", site="alpha", req="p1")
        self.refused("vuln-check")
        for site in ("", "A", "a--b", "../x", None, 5):
            self.refused("vuln-check", site=site)
            self.refused("update-check", site=site)
            self.refused("update-apply", site=site)

    def test_a_new_site_needs_a_site_name_a_file_name_and_at_most_a_label(self):
        for name in ("", "A", "a", "a--b", "-ab", "x" * 25, "../x", None, 5):
            self.refused("import-new", name=name, file="a1.wpress")
        for file in ("", "a", "a.zip", "../a.wpress", ".h.wpress", "a b.wpress", None):
            self.refused("import-new", name="newshop", file=file)
        for label in ("", " x", "x ", "x" * 101, "a\nb"):
            self.refused("import-new", name="newshop", file="a1.wpress", label=label)
        self.refused("import-new", name="newshop")
        self.refused("import-new", file="a1.wpress")
        self.refused("import-new", name="newshop", file="a1.wpress", site="alpha")

    def test_accepting_the_failures_needs_the_name_typed_and_nothing_else_takes_one(self):
        for typed in ("", "bravo", "Alpha", "alpha ", None):
            self.refused("import-approve", site="alpha", accept_failures=True, confirm_name=typed)
        self.refused("import-approve", site="alpha", accept_failures=True)
        self.refused("import-approve", site="alpha", confirm_name="alpha")
        self.refused("import-approve", site="alpha", accept_failures=False, confirm_name="alpha")
        self.line("import-approve", site="alpha")
        self.line("import-approve", site="alpha", reject="a,b")

    def test_a_replace_needs_the_name_typed_and_nothing_else_takes_one(self):
        for typed in ("", "bravo", "Alpha", "alpha ", None):
            self.refused("import-run", site="alpha", file="a1.wpress", replace=True, confirm_name=typed)
        self.refused("import-run", site="alpha", file="a1.wpress", replace=True)
        self.refused("import-run", site="alpha", file="a1.wpress", confirm_name="alpha")
        self.refused("import-run", site="alpha", file="a1.wpress", replace=False, confirm_name="alpha")

    def test_a_reset_needs_the_name_typed_exactly(self):
        for typed in ("", "bravo", "Alpha", "alpha ", None):
            self.refused("import-reset", site="alpha", confirm_name=typed)
        self.refused("import-reset", site="alpha")

    def test_flags_are_booleans_and_nothing_else(self):
        for flag in ("true", 1, 0, None, "yes", [True]):
            self.refused("import-run", site="alpha", file="a1.wpress", plan=flag)
            self.refused("import-approve", site="alpha", accept_failures=flag)
            self.refused("import-discard", file="a1.wpress", if_present=flag)

    def test_the_rejected_list_is_slugs_and_commas_and_a_file_is_a_plain_wpress_name(self):
        for reject in ("", ",", "a,", ",a", "a,,b", "a b", "a;b", "../x", "a\nb", "x" * 201, None):
            self.refused("import-approve", site="alpha", reject=reject)
        for name in ("", "a", "a.zip", ".hidden.wpress", "../a.wpress", "a/b.wpress", "a b.wpress", "a.wpress\n", "x" * 100 + ".wpress", None):
            self.refused("import-run", site="alpha", file=name)
            self.refused("import-discard", file=name)


if __name__ == "__main__":
    unittest.main()
