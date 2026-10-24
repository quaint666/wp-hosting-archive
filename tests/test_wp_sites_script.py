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
        for needle in ("list", "create", "modify", "examples:", "wp_sites.py modify shop --set update_policy=auto", "hidden prompt", "Not here yet: delete, rename, password reset"):
            self.assertIn(needle, top)
        self.assertIn("wp-site-update", top); self.assertIn("ssh -t dellwp sudo", top)
        for cmd, needles in (("create", ("--name", "--state", "--policy", "--title", "--user", "--email", "--set", "--yes", "--yes-public", "--no-run", "example.com", "second run that must end changed=0")),
                             ("modify", ("site", "--set", "--yes", "--yes-public", "--no-run", "Not editable here: name, port, quota_id, secret_rev")), ("list", ("state, stack",))):
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
        self.assertIn("core: minor and security releases only; plugins and themes: any newer version, majors included", flat)

    def test_the_tools_the_help_names_exist_and_print_a_usage_line(self):
        deployed = {i["dest"] for t in yaml.safe_load((ROOT / "tasks/wp_tools_common.yml").read_text()) if t.get("name") == "Deploy the tools" for i in t["loop"]}
        top = self.help_text()
        for tool in ("wp-site-update", "wp-site-recover", "wp-site-backup", "wp-site-restore", "wp-site-install", "wp-site-ext", "wp-site-cli"):
            self.assertIn(tool, deployed, tool + " is not deployed"); self.assertIn(tool, top)
            src = (ROOT / "templates/wp" / ("wp_" + tool.replace("wp-", "", 1).replace("-", "_") + ".sh.j2")).read_text()
            self.assertIn("usage: " + tool, src, tool + " does not print a usage line")
        for script in ("wp_sites_status.py", "wp_site_state.py", "wp_site_verify_dellwp.sh"):
            self.assertTrue((ROOT / "scripts" / script).exists(), script)


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
