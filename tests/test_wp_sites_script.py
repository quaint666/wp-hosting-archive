#!/usr/bin/env python3
"""Offline tests of scripts/wp_sites.py (list, create, modify the WordPress sites on dellwp).

A throwaway git repo holds a copy of host_vars/dellwp/vars.yml; the answers to the prompts are scripted; the commands (ansible-playbook, ssh, the verifier) are a recording stand-in
that returns what a test tells it to. They prove the editing (only the intended lines change; the YAML is re-read), the rules (the play's own assert is applied, so a list the play would
refuse is refused before anything is written), the safety questions (nothing is written or run without an answer; going public needs its own), the order and shape of the commands (the password
is never in one), and the failure paths. They do not prove that ansible or ssh work: scripts/wp_site_verify_dellwp.sh and a real create of a throwaway site do. Run: python3 -I tests/test_wp_sites_script.py"""
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wp_sites as w  # noqa: E402
from test_wp_sites_render import fixture_vars_text  # noqa: E402

GOOD_PW = "correct-horse-battery-staple"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wpsites-"))
        (self.tmp / "host_vars/dellwp").mkdir(parents=True)
        self.vars = self.tmp / "host_vars/dellwp/vars.yml"
        # the real variables with one fixed live site (tests/test_wp_sites_render.py FIXTURE_SITE): the repo's own sites come and go, and every expectation here (ports, ids, names) is about
        # a list that starts with exactly this one
        self.vars.write_text(fixture_vars_text())
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.tmp), *cmd], check=True, capture_output=True)
        self.original = self.vars.read_text()
        self.calls, self.said, self.rc = [], [], {}
        self.answers = []
        self.stdins, self.reply = [], {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, msg):
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-am", msg], check=True, capture_output=True)

    # the stand-ins
    def ask(self, prompt):
        self.said.append("PROMPT " + prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def fake_run(self, cmd, tee=False, stdin=None):
        self.calls.append(cmd)
        self.stdins.append(stdin)
        key = " ".join(cmd[:4])
        rc = next((v for k, v in self.rc.items() if key.startswith(k) or k in " ".join(cmd)), 0)
        out = "dellwp : ok=57 changed=%s unreachable=0 failed=0\n" % self.rc.get("changed", "0") if tee else ""
        for k, v in self.reply.items():
            if k in " ".join(cmd):
                out = v
        return rc, out

    def out(self, *a):
        self.said.append(" ".join(str(x) for x in a))

    def sites(self, **kw):
        kw.setdefault("judge", self.judge)
        return w.Sites(self.tmp, ask=self.ask, run=self.fake_run, out=self.out, **kw)

    verdict = None

    def with_state(self, state):
        """The fixture's vars.yml with the one site in the given state (the site's own line: four spaces; the defaults block has two)."""
        text = fixture_vars_text()
        assert "\n    state: live\n" in text
        return text.replace("\n    state: live\n", "\n    state: %s\n" % state, 1)

    def judge(self, path, repo):
        """The archive reader's answer, scripted: (rows, notes, nogo, facts). self.verdict may be an exception to raise."""
        self.judged = getattr(self, "judged", []) + [path]
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict or ([], [], [], {"archive_wp": "7.1.3", "our_wp": "7.1.3", "archive_php": "8.5.12", "our_php": "8.5", "entries": 9, "size_mb": 1})

    def text(self):
        return "\n".join(self.said)

    def names(self, text=None):
        return [s["name"] for s in yaml.safe_load(text or self.vars.read_text())["wp_sites"]]

    def kinds(self):
        return [c[0] if c[0] != "ansible-playbook" else ("play-check" if "--check" in c else "play") for c in self.calls]

    def site(self, name):
        return next(s for s in yaml.safe_load(self.vars.read_text())["wp_sites"] if s["name"] == name)

    def create_args(self, **over):
        a = dict(name="alpha", state="hold", policy="notify", title="Alpha's site", user="alphaadmin", email="alpha@example.org", sets=(), yes=True)
        a.update(over)
        return a


class Editing(Base):
    def test_a_new_site_is_appended_after_the_last_block_and_nothing_else_changes(self):
        new = w.add_site(self.original, {"name": "alpha", "port": 9102, "quota_id": 1002, "state": "hold", "update_policy": "notify"})
        added = [l for l in new.splitlines() if l not in self.original.splitlines()]
        self.assertEqual(added, ["  - name: alpha", "    port: 9102", "    quota_id: 1002", "    state: hold", "    update_policy: notify"])
        self.assertEqual([l for l in self.original.splitlines() if l not in new.splitlines()], [], "no existing line changed or went")
        self.assertEqual(self.names(new), ["wptest", "alpha"])
        self.assertLess(new.index("  - name: alpha"), new.index("# The operator's tools on the VM"), "inside the list, before the next comment block")

    def test_a_field_edit_changes_one_line_keeps_a_trailing_comment_and_adds_a_missing_key(self):
        text = self.original.replace("    update_policy: auto\n", "    update_policy: auto   # nightly\n", 1)
        new = w.set_site_field(text, "wptest", "update_policy", "notify")
        self.assertIn("    update_policy: notify   # nightly\n", new)
        self.assertEqual(len([l for l in new.splitlines() if l not in text.splitlines()]), 1)
        added = w.set_site_field(text, "wptest", "upload_mb", 128)
        self.assertEqual([l for l in added.splitlines() if l not in text.splitlines()], ["    upload_mb: 128"])
        self.assertEqual(w.set_site_field(text, "wptest", "allow_xmlrpc", True).count("allow_xmlrpc: true"), 1)

    def test_a_field_edit_on_a_second_site_does_not_touch_the_first(self):
        two = w.add_site(self.original, {"name": "alpha", "port": 9102, "quota_id": 1002, "state": "hold", "update_policy": "notify"})
        new = w.set_site_field(two, "alpha", "state", "live")
        self.assertEqual(yaml.safe_load(new)["wp_sites"][0], yaml.safe_load(two)["wp_sites"][0])
        self.assertEqual(yaml.safe_load(new)["wp_sites"][1]["state"], "live")
        with self.assertRaises(w.Refuse):
            w.set_site_field(two, "nosuch", "state", "live")

    def test_values_are_checked(self):
        good = {"state": "live", "update_policy": "freeze", "quota_mb": "4096", "php_mem_mb": "768", "upload_mb": "128", "allow_xmlrpc": "true"}
        for k, v in good.items():
            self.assertIsNone(w.validate_value(k, v)[1], (k, v))
        self.assertIs(w.validate_value("allow_xmlrpc", "false")[0], False); self.assertEqual(w.validate_value("quota_mb", " 2048 ")[0], 2048)
        for k, v in (("state", "paused"), ("update_policy", "sometimes"), ("quota_mb", "10"), ("quota_mb", "-5"), ("quota_mb", "1e3"), ("quota_mb", "99999999"), ("php_mem_mb", "64"),
                     ("upload_mb", "0"), ("upload_mb", "1000"), ("allow_xmlrpc", "yes"), ("allow_xmlrpc", "True"), ("quota_mb", ""), ("quota_mb", "12 ; id")):
            self.assertIsNotNone(w.validate_value(k, v)[1], (k, v))

    def test_names_must_be_hostname_labels(self):
        for ok in ("ab", "alpha", "my-site", "a1", "x" * 24):
            self.assertTrue(w.NAME_RE.fullmatch(ok), ok)
        for bad in ("a", "My", "my_site", "1abc", "-abc", "my-", "x" * 25, "a b", "a.b", "", "ab\n"):
            self.assertFalse(w.NAME_RE.fullmatch(bad), repr(bad))

    def test_ids_are_above_everything_the_file_ever_held(self):
        t = w.add_site(self.original, {"name": "gone", "port": 9150, "quota_id": 1077, "state": "hold", "update_policy": "notify"})
        self.vars.write_text(t); self.commit("add gone")
        self.vars.write_text(self.original); self.commit("remove gone")
        self.assertEqual(self.names(), ["wptest"])
        used = w.ever_used(self.tmp, self.original, "quota_id")
        self.assertTrue({1001, 1077} <= used)
        self.sites().create(**self.create_args(name="alpha", yes=True, sets=()))
        self.assertEqual(self.site("alpha")["quota_id"], 1078, "a removed site's quota id is never reused")
        self.assertEqual(self.site("alpha")["port"], 9151)


class Create(Base):
    def test_an_interactive_create_asks_edits_runs_the_play_installs_and_verifies(self):
        self.answers = ["alpha", "", "", "n", "Alpha's site", "alphaadmin", "alpha@example.org", "y"]   # name, state, policy, no limits, title, user, email, write and run
        rc = self.sites().create()
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.names(), ["wptest", "alpha"])
        self.assertEqual(self.site("alpha"), {"name": "alpha", "port": 9102, "quota_id": 1002, "state": "hold", "update_policy": "notify"})
        self.assertEqual(self.kinds(), ["play", "ssh", "bash", "python3"], "one play run, no check run, no second run")
        play, ssh = self.calls[0], self.calls[1]
        self.assertEqual(play, ["ansible-playbook", "-i", "inventory.yml", "dellwp/wp_sites_dellwp.yml"])
        self.assertEqual(ssh[:3], ["ssh", "-t", "dellwp"])
        self.assertEqual(shlex.split(ssh[3]), ["sudo", "wp-site-install", "alpha", "--title=Alpha's site", "--user=alphaadmin", "--email=alpha@example.org"], "one string, quoted for the remote shell")
        self.assertEqual(self.calls[2], ["bash", "scripts/wp_site_verify_dellwp.sh", "alpha"])
        self.assertIn("hostname: alpha.example.com", self.text())
        self.assertIn("hidden prompt", self.text()); self.assertIn("git add host_vars/dellwp/vars.yml", self.text())
        self.assertIn("on hold", self.text())
        self.assertEqual([x for x in self.said if x.startswith("PROMPT Write")], ["PROMPT Write this change to host_vars/dellwp/vars.yml and run the play? [y/N] "], "one question covers the write and the play")

    def test_the_password_is_never_asked_or_passed_anywhere(self):
        self.answers = ["alpha", "", "", "n", "T", "alphaadmin", "alpha@example.org", "y", "y"]
        self.sites().create()
        everything = self.text() + "\n".join(" ".join(c) for c in self.calls)
        self.assertNotIn("assword:", "\n".join(s for s in self.said if s.startswith("PROMPT")), "this script never prompts for a password")
        self.assertNotIn("--password", everything)
        code = Path(w.__file__).read_text().split('"""', 2)[2]   # after the docstring, which explains why there is no password option
        for forbidden in ("getpass", "--password", "pw=", "password="):
            self.assertNotIn(forbidden, code)
        self.assertNotIn("--password", [a for c in self.calls for a in c])

    def test_yes_skips_the_question_but_not_the_work(self):
        rc = self.sites().create(**self.create_args())
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.kinds(), ["play", "ssh", "bash", "python3"])
        self.assertEqual([s for s in self.said if s.startswith("PROMPT")], [])

    def test_going_live_asks_a_question_that_yes_does_not_answer(self):
        s = self.sites()
        with self.assertRaises(w.Refuse):
            s.create(**self.create_args(state="live"))
        self.assertEqual(self.names(), ["wptest"], "nothing was written"); self.assertEqual(self.calls, [])
        self.assertIn("PUBLIC", self.text())
        self.answers = ["y"]
        self.assertEqual(s.create(**self.create_args(state="live")), 0)
        self.assertEqual(self.site("alpha")["state"], "live"); self.assertIn("It is PUBLIC: https://alpha.example.com", self.text())

    def test_yes_public_answers_it_for_scripts(self):
        self.assertEqual(self.sites().create(**self.create_args(state="live", yes_public=True)), 0)
        self.assertEqual(self.site("alpha")["state"], "live")

    def test_limits_can_be_set_and_only_non_defaults_are_written(self):
        self.assertEqual(self.sites().create(**self.create_args(sets=("quota_mb=2048", "upload_mb=64"))), 0, self.text())
        s = self.site("alpha")
        self.assertEqual(s["quota_mb"], 2048); self.assertEqual(s["upload_mb"], 64)
        self.commit("alpha")
        self.answers = ["beta", "", "", "y", "2048", "512", "256", "64", "Beta", "betaadmin", "beta@example.org", "y", "y"]
        self.sites().create()
        b = self.site("beta")
        self.assertEqual({k: b[k] for k in ("quota_mb", "php_mem_mb", "db_mem_mb") if k in b}, {"quota_mb": 2048}, "values equal to the default are not written")

    def test_bad_input_is_refused_before_anything_is_written(self):
        for kw in (dict(name="Bad_Name"), dict(name="a"), dict(name="my-"), dict(name="wptest"), dict(name="bloomy"), dict(name="www"), dict(name="a--b"),
                   dict(user="bad user!"), dict(email="not-an-email"), dict(title=""), dict(title="x" * 101), dict(title="a\x07b"),
                   dict(sets=("port=9999",)), dict(sets=("name=x",)), dict(sets=("quota_mb=5",)), dict(sets=("secret_rev=2",)), dict(sets=("state=live",))):
            with self.subTest(kw):
                with self.assertRaises(w.Refuse):
                    self.sites().create(**self.create_args(**kw))
                self.assertEqual(self.vars.read_text(), self.original); self.assertEqual(self.calls, [])

    def test_a_site_list_the_play_would_refuse_is_refused_here_and_the_rule_is_named(self):
        with self.assertRaises(w.Refuse) as cm:
            self.sites().create(**self.create_args(sets=("quota_mb=100000",)))
        self.assertIn("quota_mb", str(cm.exception)); self.assertEqual(self.vars.read_text(), self.original); self.assertEqual(self.calls, [])
        with self.assertRaises(w.Refuse):
            self.sites().create(**self.create_args(sets=("php_mem_mb=4096", "db_mem_mb=4096")))   # RAM does not fit
        self.assertEqual(self.vars.read_text(), self.original)

    def test_uncommitted_changes_stop_everything(self):
        self.vars.write_text(self.original + "# stray\n")
        with self.assertRaises(w.Refuse) as cm:
            self.sites().create(**self.create_args())
        self.assertIn("uncommitted", str(cm.exception)); self.assertEqual(self.calls, [])

    def test_declining_the_diff_writes_and_runs_nothing(self):
        self.answers = ["n"]
        rc = self.sites().create(**self.create_args(yes=False, sets=("quota_mb=2048",)))
        self.assertEqual(rc, 1); self.assertEqual(self.vars.read_text(), self.original); self.assertEqual(self.calls, [])
        self.assertIn("Nothing was written", self.text())

    def test_declining_the_one_question_writes_nothing_and_runs_nothing(self):
        self.answers = ["n"]
        rc = self.sites().create(**self.create_args(yes=False, sets=("quota_mb=2048",)))
        self.assertEqual(rc, 1); self.assertEqual(self.kinds(), [], "nothing ran"); self.assertEqual(self.vars.read_text(), self.original); self.assertIn("Nothing was written", self.text())

    def test_a_failing_play_leaves_the_file_edited_says_how_to_undo_it_and_runs_no_verifier(self):
        self.rc = {"ansible-playbook": 2}
        self.answers = ["y"]
        rc = self.sites().modify("wptest", sets=("update_policy=notify",), yes=False)
        self.assertEqual(rc, 2); self.assertEqual(self.kinds(), ["play"]); self.assertIn("git checkout -- host_vars/dellwp/vars.yml", self.text()); self.assertIn("half-way", self.text())
        self.assertEqual(self.site("wptest")["update_policy"], "notify")

    def test_one_question_covers_the_write_and_the_play_and_there_is_no_check_run(self):
        self.answers = ["y"]
        rc = self.sites().modify("wptest", sets=("update_policy=notify",), yes=False)
        self.assertEqual(rc, 0, self.text()); self.assertEqual(self.kinds(), ["play", "bash", "python3"])
        self.assertEqual([c for c in self.calls if "--check" in c], [])
        self.assertEqual(len([x for x in self.said if x.startswith("PROMPT")]), 1)

    def test_a_failing_play_stops_before_the_install(self):
        self.rc = {"ansible-playbook": 4}
        rc = self.sites().create(**self.create_args())
        self.assertEqual(rc, 4); self.assertNotIn("ssh", self.kinds()); self.assertIn("half-way", self.text())

    def test_a_failing_install_says_how_to_run_it_by_hand(self):
        self.rc = {"ssh": 2}
        rc = self.sites().create(**self.create_args())
        self.assertEqual(rc, 2); self.assertIn("ssh -t dellwp sudo wp-site-install alpha", self.text()); self.assertNotIn("bash", self.kinds(), "no verifier on a site with no WordPress")

    def test_the_play_runs_once_with_no_check_flag_and_no_second_run_or_warning(self):
        self.sites().create(**self.create_args())
        plays = [c for c in self.calls if c[0] == "ansible-playbook"]
        self.assertEqual(plays, [["ansible-playbook", "-i", "inventory.yml", "dellwp/wp_sites_dellwp.yml"]])
        self.assertNotIn("WARNING", self.text()); self.assertNotIn("second run", self.text())

    def test_no_run_only_edits(self):
        self.assertEqual(self.sites().create(**self.create_args(no_run=True)), 0)
        self.assertEqual(self.calls, []); self.assertIn("alpha", self.names()); self.assertIn("Edited only", self.text())

    def test_with_no_terminal_and_a_missing_answer_it_stops_instead_of_guessing(self):
        with self.assertRaises(w.Refuse) as cm:
            self.sites().create()
        self.assertIn("no terminal", str(cm.exception)); self.assertEqual(self.vars.read_text(), self.original)

    def test_a_wrong_answer_is_asked_again(self):
        self.answers = ["Bad Name", "wptest", "alpha", "", "", "n", "T", "ab", "alphaadmin", "nope", "a@example.org", "y", "y"]
        self.assertEqual(self.sites().create(), 0, self.text())
        self.assertEqual(self.site("alpha")["name"], "alpha")
        self.assertIn("'wptest' already exists", self.text()); self.assertIn("3 to 30 characters", self.text()); self.assertIn("does not look like an email", self.text())

    def test_the_ssh_command_is_one_quoted_string_whatever_the_title_holds(self):
        for title in ("it's; rm -rf /", "a $(id) `id` b", 'say "hi"', "back\\slash *glob* ~ #"):
            with self.subTest(title):
                self.vars.write_text(self.original); self.calls.clear()
                self.sites().create(**self.create_args(title=title, name="alpha", no_run=False))
                parts = shlex.split(self.calls[[c[0] for c in self.calls].index("ssh")][3])
                self.assertEqual(parts[3], "--title=" + title); self.assertEqual(len(parts), 6)


class Modify(Base):
    def test_a_policy_change_edits_one_line_and_runs_the_play_and_the_verifier(self):
        rc = self.sites().modify("wptest", sets=("update_policy=notify",), yes=True)
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.site("wptest")["update_policy"], "notify")
        self.assertEqual(len([l for l in self.vars.read_text().splitlines() if l not in self.original.splitlines()]), 1)
        self.assertEqual(self.kinds(), ["play", "bash", "python3"]); self.assertIn("modify site wptest: update_policy=notify", self.text())

    def test_the_menu_flow_changes_several_fields(self):
        self.answers = ["update_policy", "freeze", "upload_mb", "128", "", "y"]
        rc = self.sites().modify("wptest")
        self.assertEqual(rc, 0, self.text())
        s = self.site("wptest")
        self.assertEqual((s["update_policy"], s["upload_mb"]), ("freeze", 128))
        self.assertEqual(self.kinds()[0], "play")

    def test_going_live_asks_its_own_question_and_yes_does_not_answer_it(self):
        self.sites().modify("wptest", sets=("state=hold",), yes=True)
        self.commit("hold")
        s = self.sites()
        with self.assertRaises(w.Refuse):
            s.modify("wptest", sets=("state=live",), yes=True)
        self.assertEqual(self.site("wptest")["state"], "hold")
        self.answers = ["n"]
        with self.assertRaises(w.Refuse):
            s.modify("wptest", sets=("state=live",), yes=True)
        self.answers = ["y"]
        self.assertEqual(s.modify("wptest", sets=("state=live",), yes=True), 0)
        self.assertEqual(self.site("wptest")["state"], "live")
        self.commit("live")
        self.assertEqual(s.modify("wptest", sets=("state=hold",), yes=True), 0); self.assertIn("stops being public", self.text())
        self.commit("hold again")
        self.assertEqual(s.modify("wptest", sets=("state=live",), yes=True, yes_public=True), 0)

    def test_what_a_change_costs_is_said_before_it_is_made(self):
        self.sites().modify("wptest", sets=("php_mem_mb=768",), yes=True)
        self.assertIn("restarts the whole stack", self.text())

    def test_unknown_fields_bad_values_and_unchanged_values_are_handled(self):
        for sets in (("port=9999",), ("name=x",), ("quota_id=1",), ("secret_rev=2",), ("state=paused",), ("quota_mb=abc",), ("quota_mb=",), ("novalue",)):
            with self.subTest(sets):
                with self.assertRaises(w.Refuse):
                    self.sites().modify("wptest", sets=sets, yes=True)
                self.assertEqual(self.vars.read_text(), self.original)
        with self.assertRaises(w.Refuse):
            self.sites().modify("nosuch", sets=("state=hold",), yes=True)
        self.assertEqual(self.sites().modify("wptest", sets=("update_policy=auto",), yes=True), 0)   # already auto
        self.assertIn("Nothing to change", self.text()); self.assertEqual(self.calls, [])

    def test_a_change_the_play_would_refuse_is_refused_here(self):
        with self.assertRaises(w.Refuse) as cm:
            self.sites().modify("wptest", sets=("php_mem_mb=4096", "db_mem_mb=4096"), yes=True)
        self.assertIn("php_mem_mb", str(cm.exception)); self.assertEqual(self.vars.read_text(), self.original); self.assertEqual(self.calls, [])

    def test_a_dirty_file_stops_modify_too(self):
        self.vars.write_text(self.original + "# x\n")
        with self.assertRaises(w.Refuse):
            self.sites().modify("wptest", sets=("update_policy=notify",), yes=True)


class Help(Base):
    def help_text(self, *argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            w.main(list(argv) + ["--help"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=self.out)
        self.assertEqual(cm.exception.code, 0)
        return buf.getvalue()

    def test_every_command_explains_itself(self):
        top = self.help_text()
        for needle in ("list", "create", "modify", "examples:", "wp_sites.py modify shop --set update_policy=auto", "hidden prompt", "Not here yet: rename."):
            self.assertIn(needle, top)
        self.assertIn("wp-site-update", top); self.assertIn("ssh -t dellwp sudo", top)
        for cmd, needles in (("create", ("--name", "--state", "--policy", "--title", "--user", "--email", "--set", "--yes", "--yes-public", "--no-run", "example.com", "(one run)")),
                             ("modify", ("site", "--set", "--yes", "--yes-public", "--no-run", "Not editable here: name, port, quota_id, secret_rev")), ("list", ("state, stack",)), ("password", ("--user", "hidden prompt", "never sees it", "Edits no file"))):
            text = self.help_text(cmd)
            for n in needles:
                self.assertIn(n, text, "%s --help lacks %r" % (cmd, n))

    def test_the_fields_in_the_help_are_the_ones_the_script_validates(self):
        text = self.help_text("modify")
        for key, spec in w.EDITABLE.items():
            line = next((l for l in text.splitlines() if l.strip().startswith(key + " ")), None)
            self.assertIsNotNone(line, key + " is not in the help")
            if spec[0] == "choice":
                self.assertIn("|".join(spec[1]), line)
            elif spec[0] == "int":
                self.assertIn("%d..%d" % (spec[1], spec[2]), line)
        for key in w.RESTARTS:
            self.assertIn(w.RESTARTS[key], text)
        for key in ("port", "quota_id", "secret_rev"):
            self.assertNotIn("\n  %s " % key, text.split("Not editable here")[0], "an uneditable field is listed as editable")

    def test_the_help_says_what_auto_really_does_about_major_updates(self):
        # the operator asked whether auto asks permission for major updates; the help must not leave that to memory
        text = self.help_text("create")
        flat = " ".join(text.split())   # argparse wraps lines
        self.assertIn("core: minor and security releases only, a new X.Y is only reported; plugins and themes: minor updates, while a major version is held for you with one ntfy per set", flat)
        self.assertIn("wp-site-update --apply applies everything", flat)

    def test_the_import_commands_explain_themselves(self):
        for cmd, needles in (("import", ("site", "file", "--yes", "no WordPress yet", "NO-GO", "Edits no file")), ("approve", ("--reject", "--accept-failures", "--yes", "stays on hold")),
                             ("reset", ("not approved yet", "type the site's name")), ("report", ("site",))):
            text = self.help_text(cmd)
            for n in needles:
                self.assertIn(n, " ".join(text.split()), "%s --help lacks %r" % (cmd, n))
        self.assertIn("--no-install", self.help_text("create"))
        top = self.help_text()
        self.assertIn("create --name friend --no-install", top)
        self.assertIn("wp-site-import", top)

    def test_the_import_tool_the_help_names_is_deployed_and_prints_a_usage_line(self):
        deployed = {i["dest"] for t in yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text()) if t.get("name") == "Deploy the tools" for i in t["loop"]}
        self.assertIn("wp-site-import", deployed)
        import wp_site_import
        self.assertIn("usage: wp-site-import", wp_site_import.USAGE)
        for sub in ("approve", "reset", "report", "receive", "discard"):
            self.assertIn("wp-site-import %s" % sub, wp_site_import.USAGE)

    def test_the_tools_the_help_names_exist_and_print_a_usage_line(self):
        deployed = {i["dest"] for t in yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text()) if t.get("name") == "Deploy the tools" for i in t["loop"]}
        top = self.help_text()
        for tool in ("wp-site-update", "wp-site-recover", "wp-site-backup", "wp-site-restore", "wp-site-install", "wp-site-password", "wp-site-ext", "wp-site-cli"):
            self.assertIn(tool, deployed, tool + " is not deployed"); self.assertIn(tool, top)
            src = (ROOT / "templates/wp" / ("wp_" + tool.replace("wp-", "", 1).replace("-", "_") + ".sh.j2")).read_text()
            self.assertIn("usage: " + tool, src, tool + " does not print a usage line")
        for script in ("wp_sites_status.py", "wp_site_state.py", "wp_site_verify_dellwp.sh"):
            self.assertTrue((ROOT / "scripts" / script).exists(), script)


class Password(Base):
    def test_it_asks_for_the_user_confirms_and_runs_the_tool_over_ssh_with_everything_quoted(self):
        self.answers = ["adminuser", "y"]
        rc = self.sites().password("wptest")
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.calls, [["ssh", "-t", "dellwp", "sudo wp-site-password wptest adminuser"]])
        self.assertIn("never sees it", self.text()); self.assertIn("hidden prompt", self.text())
        self.assertEqual(self.vars.read_text(), self.original, "no file is edited")

    def test_it_needs_no_clean_file_and_asks_nothing_about_the_password(self):
        self.vars.write_text(self.original + "# uncommitted\n")   # create and modify refuse this; password edits nothing
        self.assertEqual(self.sites().password("wptest", user="adminuser", yes=True), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([s for s in self.said if "assword" in s and s.startswith("PROMPT")], [], "this script never prompts for a password")

    def test_declining_runs_nothing(self):
        self.answers = ["n"]
        self.assertEqual(self.sites().password("wptest", user="adminuser"), 1); self.assertEqual(self.calls, [])

    def test_bad_input_is_refused_before_anything_runs(self):
        for kw in (dict(site="nosuch", user="adminuser"), dict(site="wptest", user="bad user!"), dict(site="wptest", user="a;id"), dict(site="wptest", user="$(id)"),
                   dict(site="wptest", user="x" * 61), dict(site="../x", user="adminuser")):
            with self.subTest(kw):
                with self.assertRaises(w.Refuse):
                    self.sites().password(yes=True, **kw)
                self.assertEqual(self.calls, [])
        with self.assertRaises(w.Refuse):
            self.sites().password("wptest")   # no user and no terminal

    def test_the_user_is_one_quoted_argument_whatever_it_holds(self):
        self.sites().password("wptest", user="admin@example.org", yes=True)
        self.assertEqual(self.calls[0][3], "sudo wp-site-password wptest admin@example.org")

    def test_a_failing_tool_is_reported_with_its_exit_code(self):
        self.rc = {"ssh": 2}
        rc = self.sites().password("wptest", user="adminuser", yes=True)
        self.assertEqual(rc, 2); self.assertIn("exit 2", self.text())

    def test_main_and_the_menu_reach_it_and_no_password_option_exists(self):
        out = []
        self.assertEqual(w.main(["password", "wptest", "--user", "adminuser", "--yes"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append), 0)
        self.assertEqual(self.calls[-1][3], "sudo wp-site-password wptest adminuser")
        with self.assertRaises(SystemExit):
            w.main(["password", "wptest", "--password", "x"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append)
        self.calls.clear(); self.answers = ["5", "wptest", "adminuser", "y", "4"]
        self.assertEqual(self.sites().menu(), 0)
        self.assertEqual(self.calls, [["ssh", "-t", "dellwp", "sudo wp-site-password wptest adminuser"]])


class Lifecycle(Base):
    def stopped_repo(self, name="wptest"):
        self.vars.write_text(w.set_site_field(self.vars.read_text(), name, "state", "stopped")); self.commit("stopped")

    def crowded_repo(self):
        """Seven small sites, the last one stopped: the six running ones fill the RAM budget exactly, so starting the seventh cannot fit."""
        t = w.set_site_field(w.set_site_field(self.vars.read_text(), "wptest", "quota_mb", 512), "wptest", "backup_quota_mb", 512)
        for i in range(2, 8):
            t = w.add_site(t, {"name": "s%d" % i, "port": 9100 + i, "quota_id": 1000 + i, "state": "hold", "update_policy": "notify", "quota_mb": 512, "backup_quota_mb": 512})
        t = w.set_site_field(t, "s7", "state", "stopped")
        self.vars.write_text(t); self.commit("crowded")

    def test_stop_takes_the_final_backup_after_every_answer_and_immediately_before_the_play(self):
        self.answers = ["y"]   # write the change and run the play
        rc = self.sites().stop("wptest")
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.kinds(), ["ssh", "play", "bash", "python3"], "the backup waits for the answer and comes right before the play: a person must not decide how stale it is")
        self.assertEqual(self.calls[0], ["ssh", "dellwp", "sudo -n wp-site-backup wptest --force --tag=final"], "forced (a frozen site too), and tagged final: it quiets the site and is what a delete archives")
        self.assertEqual(self.site("wptest")["state"], "stopped")
        self.assertIn("'This site is paused.'", self.text()); self.assertIn("It is LIVE", self.text()); self.assertIn("wp_sites.py start wptest", self.text())
        self.assertEqual(self.calls[2], ["bash", "scripts/wp_site_verify_dellwp.sh", "wptest"])

    def test_declining_takes_no_backup_and_writes_nothing(self):
        self.answers = ["n"]
        self.assertEqual(self.sites().stop("wptest"), 1)
        self.assertEqual(self.calls, []); self.assertEqual(self.vars.read_text(), self.original)

    def test_a_failed_final_backup_puts_the_file_back_and_the_play_never_runs(self):
        self.rc = {"ssh": 3}
        rc = self.sites().stop("wptest", yes=True)
        self.assertEqual(rc, 3); self.assertEqual(self.kinds(), ["ssh"]); self.assertEqual(self.vars.read_text(), self.original, "the edit is undone, byte for byte")
        self.assertIn("nothing was stopped", self.text()); self.assertIn("is back as it was", self.text())
        self.assertEqual(self.git_status(), "")

    def git_status(self):
        return subprocess.run(["git", "-C", str(self.tmp), "status", "--short"], capture_output=True, text=True).stdout.strip()

    def test_yes_skips_the_question_but_not_the_backup(self):
        self.assertEqual(self.sites().stop("wptest", yes=True), 0)
        self.assertEqual(self.kinds(), ["ssh", "play", "bash", "python3"])

    def test_no_run_edits_only_and_takes_no_backup(self):
        self.assertEqual(self.sites().stop("wptest", yes=True, no_run=True), 0)
        self.assertEqual(self.calls, []); self.assertEqual(self.site("wptest")["state"], "stopped"); self.assertIn("Edited only", self.text())

    def test_stop_and_start_refuse_what_does_not_apply(self):
        with self.assertRaises(w.Refuse):
            self.sites().stop("nosuch", yes=True)
        with self.assertRaises(w.Refuse) as cm:
            self.sites().start("wptest", yes=True)
        self.assertIn("is not stopped", str(cm.exception))
        self.stopped_repo()
        with self.assertRaises(w.Refuse) as cm:
            self.sites().stop("wptest", yes=True)
        self.assertIn("already stopped", str(cm.exception)); self.assertEqual(self.calls, [])
        self.vars.write_text(self.vars.read_text() + "# stray\n")
        with self.assertRaises(w.Refuse):
            self.sites().start("wptest", yes=True)

    def test_start_brings_a_stopped_site_back_on_hold_and_says_how_to_go_live(self):
        self.stopped_repo()
        self.answers = ["y"]
        rc = self.sites().start("wptest")
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.site("wptest")["state"], "hold", "always hold: going live is a separate, explicit step")
        self.assertEqual(self.kinds(), ["play", "bash", "python3"], "no backup on a start")
        self.assertIn("modify wptest --set state=live", self.text())

    def test_a_start_that_would_not_fit_in_the_vms_ram_is_refused_by_the_plays_own_rule(self):
        self.crowded_repo()
        with self.assertRaises(w.Refuse) as cm:
            self.sites().start("s7", yes=True)
        self.assertIn("php_mem_mb", str(cm.exception)); self.assertEqual(self.calls, [])
        self.assertEqual(self.site("s7")["state"], "stopped")
        self.assertEqual(self.sites().stop("s2", yes=True), 0, "stopping never needs room")
        self.commit("stopped s2")
        self.assertEqual(self.sites().start("s7", yes=True), 0, "with one of the others stopped, it fits")

    def test_modify_cannot_move_a_stopped_site_around_the_final_backup(self):
        self.stopped_repo()
        with self.assertRaises(w.Refuse) as cm:
            self.sites().modify("wptest", sets=("state=live",), yes=True)
        self.assertIn("start wptest", str(cm.exception)); self.assertEqual(self.calls, [])
        self.assertEqual(self.sites().modify("wptest", sets=("update_policy=freeze",), yes=True), 0, "other fields can still be changed")
        with self.assertRaises(w.Refuse):
            self.sites().modify("wptest", sets=("state=stopped",), yes=True)   # stopped is not a value modify takes

    def test_the_menu_stops_and_starts_and_says_when_there_is_nothing_to_choose(self):
        self.answers = ["7", "6", "wptest", "y", "4"]   # nothing is stopped; stop wptest; write and run
        self.assertEqual(self.sites().menu(), 0)
        self.assertIn("No site is stopped.", self.text()); self.assertEqual(self.site("wptest")["state"], "stopped")
        self.assertIn(["ssh", "dellwp", "sudo -n wp-site-backup wptest --force --tag=final"], self.calls)
        self.commit("stopped"); self.calls.clear(); self.answers = ["6", "4"]
        self.sites().menu(); self.assertIn("No site is running.", self.text())

    def test_main_parses_stop_and_start_and_the_help_explains_them(self):
        out = []
        self.assertEqual(w.main(["stop", "wptest", "--yes", "--no-run"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append), 0)
        self.assertEqual(self.site("wptest")["state"], "stopped")
        self.commit("stopped")
        self.assertEqual(w.main(["start", "wptest", "--yes", "--no-run"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append), 0)
        self.assertEqual(self.site("wptest")["state"], "hold")
        import contextlib
        import io
        for cmd, needles in (("stop", ("final backup", "paused", "--no-run")), ("start", ("on hold", "separate step"))):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
                w.main([cmd, "--help"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append)
            for n in needles:
                self.assertIn(n, buf.getvalue(), "%s --help lacks %r" % (cmd, n))


class Committing(Base):
    def git_out(self, *a):
        return subprocess.run(["git", "-C", str(self.tmp), *a], capture_output=True, text=True).stdout.strip()

    def head(self):
        return self.git_out("log", "-1", "--format=%s")

    def commits(self):
        return int(self.git_out("rev-list", "--count", "HEAD"))

    def test_create_commits_only_vars_yml_with_the_message_it_prints(self):
        (self.tmp / "other.txt").write_text("unrelated"); subprocess.run(["git", "-C", str(self.tmp), "add", "other.txt"], check=True)   # something else is staged
        before = self.commits()
        rc = self.sites().create(**self.create_args(commit=True))
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.commits(), before + 1); self.assertEqual(self.head(), "ops(wp): create site alpha")
        self.assertEqual(self.git_out("show", "--name-only", "--format=", "HEAD"), "host_vars/dellwp/vars.yml", "only the declared-state file is in the commit")
        self.assertEqual(self.git_out("diff", "--cached", "--name-only"), "other.txt", "what else was staged stays staged")
        self.assertEqual(self.git_out("status", "--short", "host_vars"), ""); self.assertIn("Committed", self.text())
        self.assertNotIn("When you are satisfied, commit it", self.text())

    def test_it_stages_nothing_but_vars_yml(self):
        (self.tmp / "untracked.txt").write_text("not mine")
        self.sites().create(**self.create_args(commit=True))
        self.assertEqual(self.git_out("status", "--short"), "?? untracked.txt", "an untracked file stays untracked: the script never stages anything but the declared-state file")

    def test_one_object_used_for_several_actions_judges_each_run_on_its_own(self):
        # the menu keeps one Sites object for the whole session: a failed verifier in one action must not block the commit of a later, clean one
        s = self.sites()
        self.rc = {"bash": 2}
        s.create(**self.create_args(name="alpha", commit=True)); before = self.commits()
        subprocess.run(["git", "-C", str(self.tmp), "checkout", "--", "host_vars/dellwp/vars.yml"], check=True)
        self.rc = {}
        s.create(**self.create_args(name="beta", commit=True))
        self.assertEqual(self.commits(), before + 1); self.assertEqual(self.head(), "ops(wp): create site beta")

    def test_without_commit_nothing_is_committed(self):
        before = self.commits()
        self.sites().create(**self.create_args())
        self.assertEqual(self.commits(), before); self.assertIn("When you are satisfied, commit it", self.text()); self.assertNotEqual(self.git_out("status", "--short", "host_vars"), "")

    def test_modify_stop_and_start_commit_with_their_own_messages(self):
        self.assertEqual(self.sites().modify("wptest", sets=("update_policy=notify",), yes=True, commit=True), 0)
        self.assertEqual(self.head(), "ops(wp): modify site wptest: update_policy=notify")
        self.assertEqual(self.sites().stop("wptest", yes=True, commit=True), 0); self.assertEqual(self.head(), "ops(wp): stop site wptest")
        self.assertEqual(self.sites().start("wptest", yes=True, commit=True), 0); self.assertEqual(self.head(), "ops(wp): start site wptest")
        self.assertEqual(self.git_out("status", "--short"), "")

    def test_a_verifier_that_reports_failures_blocks_the_commit_and_says_so(self):
        self.rc = {"bash": 2}
        before = self.commits()
        rc = self.sites().create(**self.create_args(commit=True))
        self.assertEqual(rc, 2); self.assertEqual(self.commits(), before)
        self.assertIn("--commit did not commit: the verifier reported failures", self.text()); self.assertIn("git add host_vars/dellwp/vars.yml", self.text())
        self.assertNotEqual(self.git_out("status", "--short", "host_vars"), "", "the edit is still there, uncommitted")

    def test_a_failed_play_blocks_the_commit(self):
        self.rc = {"ansible-playbook": 4}
        before = self.commits()
        self.assertEqual(self.sites().create(**self.create_args(commit=True)), 4)
        self.assertEqual(self.commits(), before); self.assertNotIn("Committed", self.text()); self.assertEqual(self.kinds(), ["play"], "no installer and no verifier after a failed play")

    def test_commit_needs_the_run_and_is_refused_before_anything_is_written(self):
        with self.assertRaises(w.Refuse) as cm:
            self.sites().create(**self.create_args(commit=True, no_run=True))
        self.assertIn("--commit needs the run", str(cm.exception))
        for call in (self.sites().modify, self.sites().stop, self.sites().start):
            with self.assertRaises(w.Refuse):
                call("wptest", yes=True, no_run=True, commit=True)
        self.assertEqual(self.vars.read_text(), self.original); self.assertEqual(self.calls, [])

    def test_declining_or_a_failed_play_leaves_nothing_committed(self):
        before = self.commits()
        self.answers = ["n"]
        self.assertEqual(self.sites().modify("wptest", sets=("update_policy=notify",), commit=True), 1)
        self.rc = {"ansible-playbook": 4}
        self.vars.write_text(self.original); self.assertEqual(self.sites().modify("wptest", sets=("update_policy=notify",), yes=True, commit=True), 4)
        self.assertEqual(self.commits(), before)

    def test_main_accepts_the_flag_and_the_help_explains_it(self):
        import contextlib
        import io
        out = []
        self.assertEqual(w.main(["modify", "wptest", "--set", "update_policy=notify", "--yes", "--commit"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append), 0)
        self.assertEqual(self.head(), "ops(wp): modify site wptest: update_policy=notify")
        for cmd in ("create", "modify", "stop", "start"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
                w.main([cmd, "--help"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append)
            flat = " ".join(buf.getvalue().split())
            self.assertIn("--commit", flat); self.assertIn("only that file", flat); self.assertIn("the play succeeded and the verifier ends with 0 failed", flat)

    def test_it_commits_in_the_repo_the_script_is_run_against_not_somewhere_else(self):
        before = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        self.sites().modify("wptest", sets=("update_policy=notify",), yes=True, commit=True)
        self.assertEqual(subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout, before, "the real repo was not touched by a test")


class Tombstones(Base):
    def closed_repo(self):
        t = w.add_site(self.vars.read_text(), {"name": "gone", "port": 9150, "quota_id": 1050, "state": "closed", "update_policy": "notify"})
        self.vars.write_text(t); self.commit("a tombstone")

    def git_out(self, *a):
        return subprocess.run(["git", "-C", str(self.tmp), *a], capture_output=True, text=True).stdout.strip()

    def test_no_command_works_on_a_closed_site_and_each_says_why(self):
        self.closed_repo()
        for call, args, kw in ((self.sites().stop, ("gone",), {}), (self.sites().start, ("gone",), {}), (self.sites().modify, ("gone",), dict(sets=("update_policy=auto",))),
                               (self.sites().password, ("gone",), dict(user="adminuser"))):
            with self.assertRaises(w.Refuse) as cm:
                call(*args, yes=True, **kw)
            self.assertIn("closed", str(cm.exception))
        self.assertEqual(self.calls, []); self.assertEqual(self.git_out("status", "--short"), "")

    def test_a_closed_sites_name_cannot_be_reused_and_the_message_says_so(self):
        self.closed_repo()
        with self.assertRaises(w.Refuse) as cm:
            self.sites().create(**self.create_args(name="gone"))
        self.assertIn("name of a closed site", str(cm.exception)); self.assertIn("never reused", str(cm.exception))
        self.answers = ["gone", "alpha", "", "", "n", "T", "alphaadmin", "alpha@example.org", "y", "y"]
        self.assertEqual(self.sites().create(), 0, self.text())
        self.assertIn("closed site", self.text(), "asked again after the refusal")

    def test_the_new_sites_ids_are_above_the_tombstones(self):
        self.closed_repo()
        self.sites().create(**self.create_args(name="alpha"))
        self.assertEqual((self.site("alpha")["port"], self.site("alpha")["quota_id"]), (9151, 1051))

    def test_the_menus_do_not_offer_a_tombstone(self):
        self.closed_repo()
        self.answers = ["3", "gone", "wptest", "", "4"]   # change a site: 'gone' is not one of the choices, 'wptest' is
        self.sites().menu()
        self.assertIn("one of the sites above", self.text())
        prompts = [s for s in self.said if s.startswith("PROMPT Site (")]
        self.assertTrue(prompts and all("gone" not in p for p in prompts), prompts)
        self.said.clear(); self.answers = ["5", "wptest", "adminuser", "n", "6", "wptest", "n", "7", "4"]
        self.sites().menu()
        self.assertTrue(all("gone" not in s for s in self.said if s.startswith("PROMPT Site (")))
        self.assertIn("No site is stopped.", self.text())


class Deleting(Base):
    def stopped_repo(self, name="wptest"):
        self.vars.write_text(w.set_site_field(self.vars.read_text(), name, "state", "stopped")); self.commit("stopped")

    def git_out(self, *a):
        return subprocess.run(["git", "-C", str(self.tmp), *a], capture_output=True, text=True).stdout.strip()

    def test_the_vm_tool_runs_first_then_the_repo_says_closed_then_the_play_and_the_verifier(self):
        self.stopped_repo()
        seen = []
        orig = self.fake_run
        def spy(cmd, tee=False):
            if cmd[0] == "ssh":
                seen.append(self.vars.read_text())
            return orig(cmd, tee)
        s = self.sites(); s.run = spy
        self.answers = ["wptest", "y"]   # confirmation 1 of 2 (the name); write and run the play
        rc = s.delete("wptest")
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.kinds(), ["ssh", "play", "bash", "python3"])
        self.assertEqual(self.calls[0], ["ssh", "-t", "dellwp", "sudo wp-site-delete wptest"])
        self.assertIn("state: stopped", seen[0], "when the tool ran, vars.yml still said stopped: the repo never claims a deletion that has not happened")
        self.assertEqual(self.site("wptest")["state"], "closed")
        self.assertEqual({k: self.site("wptest")[k] for k in ("name", "port", "quota_id")}, {"name": "wptest", "port": 9101, "quota_id": 1001}, "the tombstone keeps the ids")
        self.assertEqual(self.calls[2], ["bash", "scripts/wp_site_verify_dellwp.sh", "wptest"])
        for needle in ("FOR GOOD", "14 days", "tombstone", "Proxmox", "Confirmation 1 of 2"):
            self.assertIn(needle, self.text())

    def test_only_a_stopped_site_can_be_deleted_and_the_refusal_says_how(self):
        for state, text in (("hold", "stop it"), ("live", "stop it")):
            self.vars.write_text(w.set_site_field(self.vars.read_text(), "wptest", "state", state)); self.commit(state)
            with self.assertRaises(w.Refuse) as cm:
                self.sites().delete("wptest", yes=True)
            self.assertIn("wp_sites.py stop wptest", str(cm.exception)); self.assertIn("final backup", str(cm.exception))
        with self.assertRaises(w.Refuse):
            self.sites().delete("nosuch", yes=True)
        self.assertEqual(self.calls, [])
        self.stopped_repo()
        t = w.set_site_field(self.vars.read_text(), "wptest", "state", "closed"); self.vars.write_text(t); self.commit("closed")
        with self.assertRaises(w.Refuse) as cm:
            self.sites().delete("wptest", yes=True)
        self.assertIn("already closed", str(cm.exception)); self.assertEqual(self.calls, [])
        self.vars.write_text(t + "# stray\n")
        with self.assertRaises(w.Refuse):
            self.sites().delete("wptest", yes=True)

    def test_declining_runs_nothing(self):
        self.stopped_repo(); before = self.vars.read_text()
        self.answers = ["n"]
        self.assertEqual(self.sites().delete("wptest"), 1)
        self.assertEqual(self.calls, []); self.assertEqual(self.vars.read_text(), before)

    def test_a_tool_that_fails_leaves_the_repo_alone_and_says_to_run_it_again(self):
        self.stopped_repo(); before = self.vars.read_text()
        self.rc = {"ssh": 2}; self.answers = ["wptest"]
        self.assertEqual(self.sites().delete("wptest", yes=True), 2)
        self.assertEqual(self.kinds(), ["ssh"]); self.assertEqual(self.vars.read_text(), before)
        self.assertIn("the repo was not changed", self.text()); self.assertIn("the tool resumes", self.text())

    def test_declining_after_the_tool_succeeded_says_to_run_delete_again_and_a_play_failure_says_to_run_the_play(self):
        self.stopped_repo()
        self.answers = ["wptest", "n"]   # type the name; run the tool; decline writing the change
        rc = self.sites().delete("wptest")
        self.assertEqual(rc, 1); self.assertEqual(self.site("wptest")["state"], "stopped")
        self.assertIn("already gone from the VM, but vars.yml still says stopped", self.text()); self.assertIn("delete wptest", self.text())
        self.said.clear(); self.calls.clear(); self.rc = {"ansible-playbook": 4}; self.answers = ["wptest"]
        rc = self.sites().delete("wptest", yes=True)
        self.assertEqual(rc, 4); self.assertEqual(self.site("wptest")["state"], "closed")
        self.assertIn("vars.yml now says closed, but the play did not finish", self.text()); self.assertIn("ansible-playbook -i inventory.yml dellwp/wp_sites_dellwp.yml", self.text())

    def test_yes_skips_the_question_before_the_play_but_never_the_typed_name(self):
        self.stopped_repo()
        self.answers = ["wptest"]
        self.assertEqual(self.sites().delete("wptest", yes=True), 0)
        self.assertEqual(self.kinds(), ["ssh", "play", "bash", "python3"])
        self.assertEqual(len([x for x in self.said if x.startswith("PROMPT")]), 1); self.assertIn("Confirmation 1 of 2", [x for x in self.said if x.startswith("PROMPT")][0])
        self.assertEqual(self.calls[0][2:], ["dellwp", "sudo wp-site-delete wptest"], "the second confirmation, the name typed on the VM, is the tool's own and cannot be skipped from here")

    def test_a_wrong_or_missing_name_changes_nothing_even_with_yes(self):
        self.stopped_repo(); before = self.vars.read_text()
        for answer in ("", "WPTEST", "wptest2", "yes", "y", "helloworld"):
            self.said.clear(); self.answers = [answer]
            self.assertEqual(self.sites().delete("wptest", yes=True), 1, repr(answer)); self.assertIn("Not confirmed", self.text())
        self.assertEqual(self.calls, []); self.assertEqual(self.vars.read_text(), before)
        self.answers = []   # no terminal at all: --yes cannot answer for a person
        with self.assertRaises(w.Refuse):
            self.sites().delete("wptest", yes=True)
        self.assertEqual(self.calls, [])

    def test_commit_makes_the_commit_when_the_verifier_ends_clean_and_not_otherwise(self):
        self.stopped_repo(); self.answers = ["wptest"]
        self.assertEqual(self.sites().delete("wptest", yes=True, commit=True), 0)
        self.assertEqual(self.git_out("log", "-1", "--format=%s"), "ops(wp): delete site wptest"); self.assertEqual(self.git_out("status", "--short"), "")
        self.vars.write_text(self.original); self.commit("back"); self.stopped_repo(); before = self.git_out("rev-list", "--count", "HEAD")
        self.rc = {"bash": 2}; self.answers = ["wptest"]
        self.assertEqual(self.sites().delete("wptest", yes=True, commit=True), 2)
        self.assertEqual(self.git_out("rev-list", "--count", "HEAD"), before); self.assertIn("--commit did not commit", self.text())

    def test_the_deleted_names_and_ids_stay_reserved(self):
        self.stopped_repo(); self.answers = ["wptest"]; self.sites().delete("wptest", yes=True); self.commit("closed")
        with self.assertRaises(w.Refuse) as cm:
            self.sites().create(**self.create_args(name="wptest"))
        self.assertIn("name of a closed site", str(cm.exception))
        self.sites().create(**self.create_args(name="alpha"))
        self.assertEqual((self.site("alpha")["port"], self.site("alpha")["quota_id"]), (9102, 1002), "above the tombstone, never on it")

    def test_the_menu_offers_only_stopped_sites_and_the_help_explains_delete(self):
        import contextlib
        import io
        self.answers = ["8", "4"]
        self.sites().menu(); self.assertIn("No site is stopped; a site is deleted only after it is stopped.", self.text())
        self.stopped_repo(); self.said.clear(); self.answers = ["8", "nosuch", "wptest", "wptest", "y", "4"]   # menu 8; a name that is not offered; the site; confirmation 1; write and run
        self.sites().menu()
        self.assertIn("one of the sites above", self.text()); self.assertEqual(self.site("wptest")["state"], "closed")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            w.main(["delete", "--help"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=lambda *a: None)
        flat = " ".join(buf.getvalue().split())
        for needle in ("for good", "TYPE the site's name", "TWICE", "tombstone", "never reused", "months", "stopped", "--commit", "--yes", "skips neither" if False else "does NOT skip the name"):
            self.assertIn(needle, flat)
        self.assertNotIn("--no-run", flat, "there is no half-way: a closed state with the data still there would be refused by the play")
        top = io.StringIO()
        with contextlib.redirect_stdout(top), self.assertRaises(SystemExit):
            w.main(["--help"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=lambda *a: None)
        self.assertIn("wp-site-delete", top.getvalue()); self.assertIn("Not here yet: rename.", top.getvalue()); self.assertIn("A destructive change asks twice", " ".join(top.getvalue().split()))

class EmptySite(Base):
    def test_no_install_makes_an_empty_site_on_hold_and_asks_for_no_administrator(self):
        rc = self.sites().create(name="alpha", policy="notify", yes=True, install=False)
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.site("alpha")["state"], "hold")
        self.assertFalse([c for c in self.calls if "wp-site-install" in " ".join(c)])
        self.assertFalse([m for m in self.said if "PROMPT" in m and any(x in m for x in ("title", "Administrator", "email"))])
        self.assertIn("EMPTY site", self.text())
        self.assertIn("import alpha <file.wpress>", self.text())
        self.assertIn(["bash", "scripts/wp_site_verify_dellwp.sh", "alpha"], self.calls)

    def test_no_install_takes_no_live_state_and_no_administrator_details(self):
        for kw in ({"state": "live"}, {"title": "T"}, {"user": "someone"}, {"email": "a@example.org"}):
            with self.assertRaises(w.Refuse) as cm:       # everything else is given, so only the guard can be what refuses (not a missing answer)
                self.sites().create(name="alpha", policy="notify", yes=True, yes_public=True, install=False, **kw)
            self.assertIn("--no-install", str(cm.exception))
        self.assertEqual(self.vars.read_text(), self.original)

    def test_no_install_with_commit_commits_it(self):
        rc = self.sites().create(name="alpha", policy="notify", yes=True, install=False, commit=True)
        self.assertEqual(rc, 0, self.text())
        log = subprocess.run(["git", "-C", str(self.tmp), "log", "--format=%s", "-1"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(log, "ops(wp): create empty site alpha")

    def test_the_flag_reaches_create_from_the_command_line(self):
        out = []
        rc = w.main(["create", "--name", "alpha", "--no-install", "--policy", "notify", "--yes"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append)
        self.assertEqual(rc, 0, out)
        self.assertFalse([c for c in self.calls if "wp-site-install" in " ".join(c)])
        self.assertEqual(w.main(["create", "--name", "beta", "--no-install", "--state", "live", "--yes"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append), 2)

    def test_a_normal_create_still_installs(self):
        self.assertEqual(self.sites().create(**self.create_args()), 0)
        self.assertTrue([c for c in self.calls if "wp-site-install" in " ".join(c)])


class Importing(Base):
    def setUp(self):
        super().setUp()
        self.vars.write_text(self.with_state("hold"))
        self.commit("hold")
        self.original = self.vars.read_text()
        self.f = self.tmp / "friend.wpress"
        self.f.write_bytes(b"archive bytes " * 1000)
        import hashlib
        self.digest = hashlib.sha256(self.f.read_bytes()).hexdigest()
        self.reply["wp-site-import receive"] = "received %s %d friend.wpress\n" % (self.digest, self.f.stat().st_size)

    def ssh(self):
        return [" ".join(c) for c in self.calls if c[0] == "ssh"]

    def test_the_whole_flow_in_order_and_the_file_goes_only_to_the_receive_command(self):
        rc = self.sites().import_site("wptest", str(self.f), yes=True)
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.judged, [str(self.f)])
        ssh = [c for c in self.calls if c[0] == "ssh"]
        self.assertEqual(ssh[0], ["ssh", "dellwp", "sudo -n wp-site-import receive friend.wpress"])
        self.assertEqual(ssh[1], ["ssh", "-t", "dellwp", "sudo wp-site-import wptest friend.wpress"])
        self.assertEqual(self.stdins[self.calls.index(ssh[0])], str(self.f))
        self.assertEqual([x for x in self.stdins if x], [str(self.f)], "the file is sent once, to receive and to nothing else")
        self.assertIn("Copied and verified", self.text())
        self.assertIn("approve wptest", self.text())
        self.assertEqual(self.vars.read_text(), self.original, "an import edits no file")

    def test_the_preflight_is_shown_and_asked_before_anything_is_copied(self):
        self.answers = ["n"]
        rc = self.sites().import_site("wptest", str(self.f))
        self.assertEqual(rc, 1)
        self.assertIn("Copy friend.wpress", self.text())
        self.assertIn("Nothing was copied.", self.text())
        self.assertIn("archive: WordPress 7.1.3", self.text())
        self.assertFalse(self.ssh())

    def test_a_yes_to_the_question_copies(self):
        self.answers = ["y"]
        self.assertEqual(self.sites().import_site("wptest", str(self.f)), 0)
        self.assertEqual(len(self.ssh()), 2)

    def test_a_no_go_stops_before_anything_is_copied(self):
        self.verdict = ([], [], ["core is newer"], {"archive_wp": "7.2.0", "our_wp": "7.1.3", "archive_php": "8.5", "our_php": "8.5", "entries": 1, "size_mb": 1})
        with self.assertRaises(w.Refuse) as cm:
            self.sites().import_site("wptest", str(self.f), yes=True)
        self.assertIn("NO-GO", str(cm.exception))
        self.assertFalse(self.ssh())

    def test_an_unreadable_archive_and_an_unreachable_wordpress_org_stop_it_too(self):
        import wp_wpress
        for exc, needle in ((wp_wpress.ArchiveError("truncated"), "NO-GO: truncated"), (urllib_error("no route"), "NOT JUDGED")):
            self.verdict = exc
            with self.assertRaises(w.Refuse) as cm:
                self.sites().import_site("wptest", str(self.f), yes=True)
            self.assertIn(needle, str(cm.exception))
        self.assertFalse(self.ssh())

    def test_the_site_must_be_declared_open_and_on_hold(self):
        with self.assertRaises(w.Refuse):
            self.sites().import_site("nosuch", str(self.f), yes=True)
        for state, needle in (("live", "on hold"), ("stopped", "start wptest")):
            self.vars.write_text(self.with_state(state))
            with self.assertRaises(w.Refuse) as cm:
                self.sites().import_site("wptest", str(self.f), yes=True)
            self.assertIn(needle, str(cm.exception))
        self.vars.write_text(self.with_state("closed"))
        with self.assertRaises(w.Refuse) as cm:
            self.sites().import_site("wptest", str(self.f), yes=True)
        self.assertIn("tombstone", str(cm.exception))
        self.assertFalse(self.ssh())

    def test_the_file_must_be_a_plain_wpress(self):
        (self.tmp / "my file.wpress").write_bytes(b"x")
        (self.tmp / "notes.txt").write_bytes(b"x")
        (self.tmp / "link.wpress").symlink_to(self.f)
        (self.tmp / ".hidden.wpress").write_bytes(b"x")
        for name in ("missing.wpress", "my file.wpress", "notes.txt", "link.wpress", ".hidden.wpress", "friend.wpress\n"):
            with self.assertRaises(w.Refuse):
                self.sites().import_site("wptest", str(self.tmp / name), yes=True)
        self.assertFalse(self.ssh())
        self.assertEqual(getattr(self, "judged", []), [], "nothing is even read before the name is accepted")

    def test_a_copy_that_does_not_match_is_removed_and_nothing_is_imported(self):
        for reply in ("received %s %d friend.wpress\n" % ("0" * 64, self.f.stat().st_size), "received %s %d friend.wpress\n" % (self.digest, 5), "garbage\n", "",
                      "received %s %d other.wpress\n" % (self.digest, self.f.stat().st_size)):
            self.calls.clear()
            self.reply["wp-site-import receive"] = reply
            with self.assertRaises(w.Refuse) as cm:
                self.sites().import_site("wptest", str(self.f), yes=True)
            self.assertIn("does not match", str(cm.exception))
            self.assertIn("ssh dellwp sudo -n wp-site-import discard friend.wpress", self.ssh())
            self.assertFalse([c for c in self.ssh() if "-t" in c.split()[:2]], "no import after a bad copy")

    def test_a_failed_copy_stops_without_an_import(self):
        self.rc["ssh dellwp sudo -n wp-site-import receive"] = 255
        self.assertEqual(self.sites().import_site("wptest", str(self.f), yes=True), 255)
        self.assertEqual(len(self.ssh()), 1)
        self.assertIn("copy to the VM failed", self.text())

    def test_what_the_exit_code_of_the_import_means(self):
        for rc, needle in ((1, "a step failed"), (2, "reset")):
            self.said.clear()
            self.rc["ssh -t dellwp sudo wp-site-import wptest"] = rc
            self.assertEqual(self.sites().import_site("wptest", str(self.f), yes=True), rc)
            self.assertIn(needle, self.text())

    def test_a_name_with_a_space_or_a_quote_cannot_reach_the_shell_line(self):
        # the only names that get here passed IMPORT_FILE_RE; the remote command is still built with shlex.quote
        self.sites().import_site("wptest", str(self.f), yes=True)
        self.assertTrue(all("'" not in c and ";" not in c for c in self.ssh()))


def urllib_error(msg):
    import urllib.error
    return urllib.error.URLError(msg)


class Approving(Base):
    def setUp(self):
        super().setUp()
        self.vars.write_text(self.with_state("hold"))
        self.commit("hold")
        self.original = self.vars.read_text()
        self.reply["wp-site-import report"] = "IMPORT REPORT for wptest\nCODE HELD BACK: plugin notorg\n"

    def ssh(self):
        return [" ".join(c) for c in self.calls if c[0] == "ssh"]

    def test_the_report_is_shown_first_then_asked_then_approved(self):
        self.answers = ["y"]
        rc = self.sites().approve("wptest")
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.ssh(), ["ssh dellwp sudo -n wp-site-import report wptest", "ssh -t dellwp sudo wp-site-import approve wptest"])
        self.assertIn("Approve this import of wptest?", self.text())
        self.assertIn(["bash", "scripts/wp_site_verify_dellwp.sh", "wptest"], self.calls)
        self.assertIn("still on hold", self.text())

    def test_not_approved_changes_nothing(self):
        for answer in ("n", "", "yes please"):
            self.calls.clear()
            self.answers = [answer]
            self.assertEqual(self.sites().approve("wptest"), 1)
            self.assertFalse([c for c in self.ssh() if "approve" in c])

    def test_reject_and_accept_failures_are_passed_on_quoted(self):
        rc = self.sites().approve("wptest", ["notorg", "my-theme"], accept_failures=True, yes=True)
        self.assertEqual(rc, 0)
        self.assertIn("ssh -t dellwp sudo wp-site-import approve wptest --reject notorg,my-theme --accept-failures", self.ssh())

    def test_a_reject_list_that_is_not_folder_names_is_refused_before_anything_runs(self):
        for bad in (["a;b"], ["../x"], ["a b"], ["x\n"], ["$(id)"], ["a", "-b"]):
            with self.assertRaises(w.Refuse):
                self.sites().approve("wptest", bad, yes=True)
        self.assertEqual(self.calls, [])

    def test_a_site_that_is_not_on_hold_or_not_declared(self):
        with self.assertRaises(w.Refuse):
            self.sites().approve("nosuch", yes=True)
        self.vars.write_text(self.with_state("live"))
        with self.assertRaises(w.Refuse):
            self.sites().approve("wptest", yes=True)
        self.assertEqual(self.calls, [])

    def test_no_report_means_no_question(self):
        self.rc["ssh dellwp sudo -n wp-site-import report"] = 2
        self.assertEqual(self.sites().approve("wptest", yes=True), 2)
        self.assertFalse([c for c in self.ssh() if "approve" in c])

    def test_a_refusal_on_the_vm_is_reported_and_nothing_else_runs(self):
        self.rc["ssh -t dellwp sudo wp-site-import approve"] = 2
        self.assertEqual(self.sites().approve("wptest", yes=True), 2)
        self.assertIn("Nothing is deleted until it succeeds", self.text())
        self.assertNotIn(["bash", "scripts/wp_site_verify_dellwp.sh", "wptest"], self.calls)

    def test_reset_and_report_go_to_the_vm(self):
        self.assertEqual(self.sites().reset("wptest"), 0)
        self.assertIn("ssh -t dellwp sudo wp-site-import reset wptest", self.ssh())
        self.assertIn(["python3", "scripts/wp_sites_status.py"], self.calls)
        self.calls.clear()
        self.assertEqual(self.sites().report("wptest"), 0)
        self.assertEqual(self.ssh(), ["ssh dellwp sudo -n wp-site-import report wptest"])
        for fn in (self.sites().reset, self.sites().report):
            with self.assertRaises(w.Refuse):
                fn("nosuch")

    def test_a_failed_reset_says_so(self):
        self.rc["ssh -t dellwp sudo wp-site-import reset"] = 2
        self.assertEqual(self.sites().reset("wptest"), 2)
        self.assertIn("did not finish", self.text())

    def test_main_parses_the_new_commands(self):
        out = []
        f = self.tmp / "friend.wpress"
        f.write_bytes(b"x" * 10)
        import hashlib
        self.reply["wp-site-import receive"] = "received %s 10 friend.wpress\n" % hashlib.sha256(b"x" * 10).hexdigest()
        kw = dict(repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append)
        self.assertEqual(w.main(["report", "wptest"], **kw), 0)
        self.assertEqual(w.main(["approve", "wptest", "--reject", "a,b", "--accept-failures", "--yes"], **kw), 0)
        self.assertIn("ssh -t dellwp sudo wp-site-import approve wptest --reject a,b --accept-failures", self.ssh())
        self.assertEqual(w.main(["reset", "wptest"], **kw), 0)
        self.assertEqual(w.main(["approve", "nosuch", "--yes"], **kw), 2)

    def test_the_menu_offers_import_approve_reset_and_report_only_for_a_site_on_hold(self):
        self.answers = ["9", "10", "11", "12", "4"]
        self.vars.write_text(self.with_state("live"))
        self.sites().menu()
        self.assertEqual(self.text().count("No site is on hold"), 4)
        self.said.clear(); self.calls.clear()
        self.vars.write_text(self.original)
        self.answers = ["11", "wptest", "12", "wptest", "4"]
        self.sites().menu()
        self.assertIn("ssh -t dellwp sudo wp-site-import reset wptest", self.ssh())
        self.assertIn("ssh dellwp sudo -n wp-site-import report wptest", self.ssh())

    def test_the_menu_approve_asks_what_to_leave_out(self):
        self.answers = ["10", "wptest", "notorg", "y", "4"]
        self.sites().menu()
        self.assertIn("ssh -t dellwp sudo wp-site-import approve wptest --reject notorg", self.ssh())


class Surface(Base):
    def test_list_runs_the_status_command(self):
        self.assertEqual(self.sites().list(), 0); self.assertEqual(self.calls, [["python3", "scripts/wp_sites_status.py"]])

    def test_the_menu_dispatches_and_survives_a_refusal(self):
        self.answers = ["1", "3", "nosuch", "wptest", "", "2", "Bad Name", "4"]
        rc = self.sites().menu()
        self.assertEqual(rc, 0); self.assertIn(["python3", "scripts/wp_sites_status.py"], self.calls)
        self.assertIn("Nothing to change", self.text())

    def test_main_parses_the_commands_and_reports_a_refusal_with_exit_2(self):
        out = []
        rc = w.main(["create", "--name", "alpha", "--title", "T", "--user", "alphaadmin", "--email", "a@example.org", "--state", "hold", "--policy", "notify", "--yes", "--no-run"],
                    repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append)
        self.assertEqual(rc, 0, out); self.assertIn("alpha", self.names())
        self.commit("alpha")
        rc = w.main(["modify", "nosuch", "--set", "state=hold", "--yes"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append)
        self.assertEqual(rc, 2); self.assertTrue(any("no site 'nosuch'" in str(o) for o in out))
        with self.assertRaises(SystemExit):
            w.main(["create", "--state", "paused"], repo=self.tmp, ask=self.ask, run=self.fake_run, out=out.append)

    def test_the_documented_rules_match_the_play(self):
        # the editable fields exist in the defaults, so an edit can never invent a key the play ignores; the policies and states are the ones the play accepts
        d = yaml.safe_load((ROOT / "host_vars/dellwp/vars.yml").read_text())
        for k in w.EDITABLE:
            self.assertIn(k, d["wp_site_defaults"], k)
        self.assertEqual(set(w.POLICIES), {"auto", "notify", "freeze"}); self.assertEqual(set(w.STATES), {"hold", "live"})
        self.assertEqual(w.HOST, "dellwp"); self.assertTrue((ROOT / w.PLAY).exists())
        for k in ("port", "quota_id", "name", "secret_rev"):
            self.assertNotIn(k, w.EDITABLE, "identity and secrets are not editable here")


if __name__ == "__main__":
    unittest.main(verbosity=1)
