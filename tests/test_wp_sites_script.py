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
import wp_sites as w  # noqa: E402

GOOD_PW = "correct-horse-battery-staple"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wpsites-"))
        (self.tmp / "host_vars/dellwp").mkdir(parents=True)
        self.vars = self.tmp / "host_vars/dellwp/vars.yml"
        # the committed file, not the working copy: an uncommitted edit (a site this script just added, say) must not change what the tests start from
        head = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:host_vars/dellwp/vars.yml"], capture_output=True, text=True)
        text = head.stdout if head.returncode == 0 else (ROOT / "host_vars/dellwp/vars.yml").read_text()
        # ...and only its first site: the repo gains sites (helloworld is committed), and every expectation here (ports, ids, names) is about a list that starts with one
        lines = text.split("\n")
        for extra in [x["name"] for x in yaml.safe_load(text)["wp_sites"]][1:]:
            start, end = w.site_span(lines, extra)
            del lines[start:end]
        self.vars.write_text("\n".join(lines))
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.tmp), *cmd], check=True, capture_output=True)
        self.original = self.vars.read_text()
        self.calls, self.said, self.rc = [], [], {}
        self.answers = []

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

    def fake_run(self, cmd, tee=False):
        self.calls.append(cmd)
        key = " ".join(cmd[:4])
        rc = next((v for k, v in self.rc.items() if key.startswith(k) or k in " ".join(cmd)), 0)
        out = "dellwp : ok=57 changed=%s unreachable=0 failed=0\n" % self.rc.get("changed", "0") if tee else ""
        return rc, out

    def out(self, *a):
        self.said.append(" ".join(str(x) for x in a))

    def sites(self, **kw):
        return w.Sites(self.tmp, ask=self.ask, run=self.fake_run, out=self.out, **kw)

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
        self.answers = ["alpha", "", "", "n", "Alpha's site", "alphaadmin", "alpha@example.org", "y", "y"]   # name, state, policy, no limits, title, user, email, write, run for real
        rc = self.sites().create()
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.names(), ["wptest", "alpha"])
        self.assertEqual(self.site("alpha"), {"name": "alpha", "port": 9102, "quota_id": 1002, "state": "hold", "update_policy": "notify"})
        self.assertEqual(self.kinds(), ["play", "play", "ssh", "bash", "python3"], "no check run for a new site: check mode cannot get past a directory that does not exist yet")
        self.assertIn("No check run", self.text())
        play, ssh = self.calls[0], self.calls[2]
        self.assertEqual(play, ["ansible-playbook", "-i", "inventory.yml", "dellwp/wp_sites_dellwp.yml"])
        self.assertEqual(ssh[:3], ["ssh", "-t", "dellwp"])
        self.assertEqual(shlex.split(ssh[3]), ["sudo", "wp-site-install", "alpha", "--title=Alpha's site", "--user=alphaadmin", "--email=alpha@example.org"], "one string, quoted for the remote shell")
        self.assertEqual(self.calls[3], ["bash", "scripts/wp_site_verify_dellwp.sh", "alpha"])
        self.assertIn("hostname: alpha.example.com", self.text())
        self.assertIn("hidden prompt", self.text()); self.assertIn("git add host_vars/dellwp/vars.yml", self.text())
        self.assertIn("on hold", self.text())

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

    def test_yes_skips_the_questions_and_the_check_run_but_not_the_work(self):
        rc = self.sites().create(**self.create_args())
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.kinds(), ["play", "play", "ssh", "bash", "python3"])
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

    def test_declining_the_real_run_leaves_the_edit_and_says_how_to_undo_it(self):
        self.answers = ["y", "n"]
        rc = self.sites().create(**self.create_args(yes=False, sets=("quota_mb=2048",)))
        self.assertEqual(rc, 1); self.assertEqual(self.kinds(), [], "nothing ran"); self.assertIn("alpha", self.names())
        self.assertIn("git checkout -- host_vars/dellwp/vars.yml", self.text()); self.assertIn("it builds the new site", "\n".join(self.said))

    def test_a_failing_check_run_of_a_change_stops_before_the_real_one(self):
        # modify keeps the check run (its directories exist); create has none
        self.rc = {"ansible-playbook -i inventory.yml dellwp/wp_sites_dellwp.yml --check": 2}
        self.answers = ["y"]
        rc = self.sites().modify("wptest", sets=("update_policy=notify",), yes=False)
        self.assertEqual(rc, 2); self.assertEqual(self.kinds(), ["play-check"]); self.assertIn("Nothing on the host changed", self.text())

    def test_a_good_check_run_of_a_change_asks_before_the_real_one(self):
        self.answers = ["y", "y"]
        rc = self.sites().modify("wptest", sets=("update_policy=notify",), yes=False)
        self.assertEqual(rc, 0, self.text()); self.assertEqual(self.kinds()[:3], ["play-check", "play", "play"]); self.assertNotIn("No check run", self.text())

    def test_a_failing_play_stops_before_the_install(self):
        self.rc = {"ansible-playbook": 4}
        rc = self.sites().create(**self.create_args())
        self.assertEqual(rc, 4); self.assertNotIn("ssh", self.kinds()); self.assertIn("half-way", self.text())

    def test_a_failing_install_says_how_to_run_it_by_hand(self):
        self.rc = {"ssh": 2}
        rc = self.sites().create(**self.create_args())
        self.assertEqual(rc, 2); self.assertIn("ssh -t dellwp sudo wp-site-install alpha", self.text()); self.assertNotIn("bash", self.kinds(), "no verifier on a site with no WordPress")

    def test_a_second_run_that_changes_something_is_warned_about(self):
        self.rc = {"changed": "3"}
        self.sites().create(**self.create_args())
        self.assertIn("WARNING: the second run should end changed=0", self.text())
        self.commit("alpha"); self.said.clear(); self.rc = {}
        self.sites().create(**self.create_args(name="beta"))
        self.assertNotIn("WARNING", self.text())

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
        self.assertEqual(self.kinds(), ["play", "play", "bash", "python3"]); self.assertIn("modify site wptest: update_policy=notify", self.text())

    def test_the_menu_flow_changes_several_fields(self):
        self.answers = ["update_policy", "freeze", "upload_mb", "128", "", "y", "y"]
        rc = self.sites().modify("wptest")
        self.assertEqual(rc, 0, self.text())
        s = self.site("wptest")
        self.assertEqual((s["update_policy"], s["upload_mb"]), ("freeze", 128))
        self.assertEqual(self.kinds()[0], "play-check")

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
        for needle in ("list", "create", "modify", "examples:", "wp_sites.py modify shop --set update_policy=auto", "hidden prompt", "Not here yet: delete, rename."):
            self.assertIn(needle, top)
        self.assertIn("wp-site-update", top); self.assertIn("ssh -t dellwp sudo", top)
        for cmd, needles in (("create", ("--name", "--state", "--policy", "--title", "--user", "--email", "--set", "--yes", "--yes-public", "--no-run", "example.com", "second run that must end changed=0")),
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

    def test_stop_takes_the_final_backup_after_every_answer_and_immediately_before_the_real_play(self):
        self.answers = ["y", "y"]   # write the change, run the play for real
        rc = self.sites().stop("wptest")
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.kinds(), ["play-check", "ssh", "play", "play", "bash", "python3"], "the backup waits for the answers: a person must not decide how stale it is")
        self.assertEqual(self.calls[1], ["ssh", "dellwp", "sudo -n wp-site-backup wptest --force --tag=final"], "forced (a frozen site too), and tagged final: it quiets the site and is what a delete archives")
        self.assertEqual(self.site("wptest")["state"], "stopped")
        self.assertIn("'This site is paused.'", self.text()); self.assertIn("It is LIVE", self.text()); self.assertIn("wp_sites.py start wptest", self.text())
        self.assertEqual(self.calls[4], ["bash", "scripts/wp_site_verify_dellwp.sh", "wptest"])

    def test_declining_the_diff_or_the_real_run_takes_no_backup(self):
        self.answers = ["n"]
        self.assertEqual(self.sites().stop("wptest"), 1)
        self.assertEqual(self.calls, []); self.assertEqual(self.vars.read_text(), self.original)
        self.answers = ["y", "n"]   # write, then decline the real run: the check ran, the backup did not
        self.assertEqual(self.sites().stop("wptest"), 1)
        self.assertEqual(self.kinds(), ["play-check"]); self.assertNotIn("final", " ".join(" ".join(c) for c in self.calls))

    def test_a_failed_final_backup_puts_the_file_back_and_the_play_never_runs(self):
        self.rc = {"ssh": 3}
        rc = self.sites().stop("wptest", yes=True)
        self.assertEqual(rc, 3); self.assertEqual(self.kinds(), ["ssh"]); self.assertEqual(self.vars.read_text(), self.original, "the edit is undone, byte for byte")
        self.assertIn("nothing was stopped", self.text()); self.assertIn("is back as it was", self.text())
        self.assertEqual(self.git_status(), "")

    def git_status(self):
        return subprocess.run(["git", "-C", str(self.tmp), "status", "--short"], capture_output=True, text=True).stdout.strip()

    def test_yes_skips_the_questions_and_the_check_run_but_not_the_backup(self):
        self.assertEqual(self.sites().stop("wptest", yes=True), 0)
        self.assertEqual(self.kinds(), ["ssh", "play", "play", "bash", "python3"])

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
        self.answers = ["y", "y"]
        rc = self.sites().start("wptest")
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.site("wptest")["state"], "hold", "always hold: going live is a separate, explicit step")
        self.assertEqual(self.kinds(), ["play-check", "play", "play", "bash", "python3"], "no backup on a start")
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
        self.answers = ["7", "6", "wptest", "y", "y", "4"]   # nothing is stopped; stop wptest; write; run
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
        # the menu keeps one Sites object for the whole session: a dirty second run in one action must not block the commit of a later, clean one
        s = self.sites()
        self.rc = {"changed": "2"}
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

    def test_a_second_run_that_changed_something_blocks_the_commit(self):
        self.rc = {"changed": "2"}
        before = self.commits()
        self.assertEqual(self.sites().create(**self.create_args(commit=True)), 0)
        self.assertEqual(self.commits(), before); self.assertIn("the play's second run was not changed=0", self.text())

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
            self.assertIn("--commit", flat); self.assertIn("only that file", flat); self.assertIn("second run is changed=0", flat)

    def test_it_commits_in_the_repo_the_script_is_run_against_not_somewhere_else(self):
        before = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        self.sites().modify("wptest", sets=("update_policy=notify",), yes=True, commit=True)
        self.assertEqual(subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout, before, "the real repo was not touched by a test")


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
