#!/usr/bin/env python3
"""Offline tests of scripts/wp_site_lifecycle_rehearsal_dellwp.sh.

The script drives a real VM, so it cannot be run here; what can be checked is everything around that. Every snippet it sends to the VM (and the library they share) is syntax-checked by bash. Its
arguments and its refusals are exercised in a throwaway git repo with stand-ins for ssh and for the python scripts it calls, which also proves the ORDER of the lifecycle (create, import, reset,
import, approve, stop, start, stop, delete), the exact options each call gets, and that a phone-less run never waits on a tap. They do not prove that any check inside a VM snippet is right: the
rehearsal on dellwp does. Run: python3 -I tests/test_wp_lifecycle_script.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_wp_sites_render import ROOT, fixture_vars_text  # noqa: E402

SCRIPT = ROOT / "scripts/wp_site_lifecycle_rehearsal_dellwp.sh"
TEXT = SCRIPT.read_text()


def snippets():
    """(label, body) of the library and of every heredoc sent to the VM."""
    out = [("LIB", re.search(r"<<'LIB'\n(.*?)\nLIB\n", TEXT, re.S).group(1))]
    for i, m in enumerate(re.finditer(r"vmrun <<'EOF'\n(.*?)\nEOF\n", TEXT, re.S)):
        out.append(("snippet %d" % i, m.group(1)))
    return out


class Syntax(unittest.TestCase):
    def test_the_script_parses(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_snippet_sent_to_the_vm_parses_with_the_library_in_front(self):
        lib = snippets()[0][1]
        parts = snippets()
        self.assertGreaterEqual(len(parts), 10)
        for label, body in parts:
            r = subprocess.run(["bash", "-n"], input=lib + "\n" + body + "\n", capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, "%s: %s" % (label, r.stderr))

    def test_the_embedded_python_parses(self):
        for label, body in snippets():
            for m in re.finditer(r"python3 -I - [^\n]*<<'PY'\n(.*?)\nPY\n", body, re.S):
                compile(m.group(1), label, "exec")

    def test_every_snippet_ends_by_reporting_its_failures_as_its_exit_code(self):
        for label, body in snippets()[1:]:
            self.assertTrue(body.rstrip().endswith("finish") or body.rstrip().endswith("finish\n") or "\nfinish" in body, label)

    def test_a_snippet_that_asks_for_a_variable_has_it_in_the_env_line(self):
        env_line = re.search(r'sudo -n env ([^"]*) bash -s', TEXT).group(1)
        passed = set(re.findall(r"(\w+)=", env_line))
        used = set()
        for label, body in snippets():
            used |= set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", body))
        local = {"FAIL", "DIR", "HOST", "WORK", "UP", "DB", "BASE", "CS", "PC", "AP", "PATH", "HOME", "PIPESTATUS", "SQL", "MARIADB_DATABASE", "MARIADB_PASSWORD", "MARIADB_USER"}   # the last four are for docker exec's own shell
        self.assertEqual(sorted(used - passed - local - {"BIN"}), [], "a snippet uses a variable the VM does not receive")
        for needed in ("SITE", "DOMAIN", "BIN", "SITES", "INTAKE", "LISTEN", "BDIR", "ARCH", "ADIR", "FILENAME"):
            self.assertIn(needed, passed)

    def test_nothing_secret_is_in_the_script_and_the_password_prompts_are_not_bypassed(self):
        self.assertNotRegex(TEXT, r"(?i)password\s*=\s*['\"]\w")
        self.assertIn("wp_sites.py delete", TEXT)
        delete_line = [l for l in TEXT.splitlines() if "wp_sites.py delete" in l and not l.lstrip().startswith("#")][0]
        self.assertNotIn("--yes", delete_line, "the delete keeps its two typed names")
        live_line = [l for l in TEXT.splitlines() if "state=live" in l and "wp_sites.py" in l][0]
        self.assertNotIn("--yes", live_line)
        self.assertNotIn("--yes-public", TEXT)


class Harness(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.repo = Path(self.d.name)
        (self.repo / "scripts").mkdir()
        (self.repo / "host_vars/dellwp").mkdir(parents=True)
        (self.repo / "wpress").mkdir()
        (self.repo / "bin").mkdir()
        shutil.copy(SCRIPT, self.repo / "scripts/")
        (self.repo / "host_vars/dellwp/vars.yml").write_text(fixture_vars_text())
        (self.repo / "wpress/practice.wpress").write_bytes(b"x" * 10)
        self.log = self.repo / "calls.log"
        # stand-ins: ssh swallows its input and succeeds; the python scripts record their arguments
        (self.repo / "bin/ssh").write_text('#!/bin/bash\necho "ssh $*" >> "%s"\ncat >/dev/null 2>&1 || true\ncase "$*" in *"bash -s"*) exit ${SSH_BASH_RC:-${SSH_RC:-0}} ;; *"sudo -n true"*) exit ${SSH_SUDO_RC:-${SSH_RC:-0}} ;; esac\nexit ${SSH_RC:-0}\n' % self.log)
        for name in ("wp_sites.py", "wp_wpress.py"):
            body = "#!/usr/bin/env python3\nimport sys\nopen(%r, 'a').write('%s ' + ' '.join(sys.argv[1:]) + '\\n')\n" % (str(self.log), name)
            if name == "wp_wpress.py":
                body += "print('GO')\n"
            (self.repo / "scripts" / name).write_text(body)
        (self.repo / "scripts/wp_site_verify_dellwp.sh").write_text('#!/bin/bash\necho "verify $*" >> "%s"\necho "5 checks, 0 failed"\n' % self.log)
        for p in (self.repo / "bin").iterdir():
            p.chmod(0o755)
        for cmd in (["init", "-q", "-b", "wp-test"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, capture_output=True)
        self.env = dict(os.environ, PATH="%s:%s" % (self.repo / "bin", os.environ["PATH"]))

    def tearDown(self):
        self.d.cleanup()

    def run_script(self, *args, env=None, stdin=""):
        return subprocess.run(["bash", str(self.repo / "scripts/wp_site_lifecycle_rehearsal_dellwp.sh"), *args], capture_output=True, text=True, env=env or self.env, input=stdin, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class Arguments(Harness):
    def test_help_prints_the_header(self):
        r = self.run_script("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("ONE WHOLE LIFE of a WordPress site", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_bad_arguments_are_refused_before_anything_runs(self):
        for args, needle in ((("--bogus",), "unknown option"), (("--site", "Bad Name"), "--site must be"), (("--site", ""), "--site must be"), (("--site", "a"), "--site must be"), (("--until", "never"), "--until must be"),
                             (("--file", "/nonexistent.wpress"), "no .wpress to import")):
            r = self.run_script(*args, "--yes")
            self.assertEqual(r.returncode, 2, args)
            self.assertIn(needle, r.stderr, args)
        self.assertEqual(self.calls(), [])

    def test_it_will_not_run_on_main(self):
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", "main"], check=True, capture_output=True)
        r = self.run_script("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("you are on main", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_it_will_not_run_over_uncommitted_changes_to_vars(self):
        (self.repo / "host_vars/dellwp/vars.yml").write_text((self.repo / "host_vars/dellwp/vars.yml").read_text() + "# x\n")
        r = self.run_script("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("uncommitted changes", r.stderr)

    def test_a_name_that_exists_or_was_closed_is_refused(self):
        r = self.run_script("--site", "wptest", "--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("already in wp_sites", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_not_confirming_does_nothing(self):
        r = self.run_script(stdin="n\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was done.", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_failures_found_on_the_vm_stop_the_run_before_anything_is_created(self):
        r = self.run_script("--yes", "--no-phone", env=dict(self.env, SSH_BASH_RC="3"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the preflight found problems", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("wp_sites.py")])

    def test_ssh_that_works_with_sudo_that_does_not_is_its_own_stop(self):
        r = self.run_script("--yes", "--no-phone", env=dict(self.env, SSH_SUDO_RC="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sudo -n does not work", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("wp_sites.py")])

    def test_no_ssh_means_a_fatal_with_clean_up_advice_and_no_change(self):
        r = self.run_script("--yes", env=dict(self.env, SSH_RC="255"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot ssh to dellwp", r.stdout)
        self.assertIn("To clean up:", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("wp_sites.py")], "nothing was created")


class Flow(Harness):
    def sites_calls(self):
        return [c for c in self.calls() if c.startswith("wp_sites.py")]

    def test_the_whole_lifecycle_without_the_phone_in_order_with_the_right_options(self):
        r = self.run_script("--yes", "--no-phone", "--site", "life")
        self.assertEqual(self.sites_calls(), [
            "wp_sites.py create --name life --no-install --policy notify --yes --commit",
            "wp_sites.py import life %s/wpress/practice.wpress --yes" % self.repo,
            "wp_sites.py reset life",
            "wp_sites.py import life %s/wpress/practice.wpress --yes" % self.repo,
            "wp_sites.py approve life --yes",
            "wp_sites.py stop life --yes --commit",
            "wp_sites.py start life --yes --commit",
            "wp_sites.py stop life --yes --commit",
            "wp_sites.py delete life --commit"])
        self.assertIn("wp_wpress.py preflight %s/wpress/practice.wpress" % self.repo, self.calls())
        self.assertIn("verify life", self.calls())
        self.assertNotIn("state=live", " ".join(self.calls()), "no public step without --with-live")
        self.assertIn("(7. live: skipped", r.stdout)
        self.assertNotIn("Tap DENY", r.stdout)
        self.assertNotIn("Tap APPROVE", r.stdout)
        # the stand-in cannot make the repo say closed, so that last check is the one that fails, and only it
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL: the repo says 'life' is closed", r.stdout)

    def test_the_snippets_receive_the_repos_values_and_the_site_name(self):
        self.run_script("--yes", "--no-phone", "--site", "life", "--until", "create")
        env_lines = [c for c in self.calls() if "sudo -n env" in c]
        self.assertGreaterEqual(len(env_lines), 2)
        for line in env_lines:
            for needle in ("SITE=life", "DOMAIN=example.com", "BIN=/usr/local/sbin", "SITES=/srv/wp/sites", "INTAKE=/srv/wp/intake", "BDIR=/srv/wp/backups", "ARCH=/srv/wp/archive",
                           "ADIR=/etc/wp-approvals", "FILENAME=practice.wpress"):
                self.assertIn(needle, line)

    def test_until_stops_after_that_phase_and_leaves_the_site(self):
        for until, last in (("create", "create"), ("import", None), ("deny", "reset"), ("approve", "approve"), ("operate", "approve")):
            self.log.write_text("")
            r = self.run_script("--yes", "--no-phone", "--site", "life", "--until", until)
            verbs = [c.split()[1] for c in self.sites_calls()]
            self.assertNotIn("stop", verbs, until)
            self.assertNotIn("delete", verbs, until)
            if last:
                self.assertEqual(verbs[-1], last, until)
            self.assertIn("Stopped after '%s' as asked" % until, r.stdout)

    def test_with_live_asks_its_own_question_and_goes_back_to_hold(self):
        r = self.run_script("--yes", "--no-phone", "--with-live", "--site", "life", "--until", "live")
        calls = self.sites_calls()
        self.assertIn("wp_sites.py modify life --set state=live --commit", calls)
        self.assertEqual(calls[-1], "wp_sites.py modify life --set state=hold --yes --commit")
        self.assertNotIn("--yes-public", " ".join(calls))

    def test_a_phase_that_fails_stops_the_run_and_says_how_to_clean_up(self):
        (self.repo / "scripts/wp_sites.py").write_text("#!/usr/bin/env python3\nimport sys\nopen(%r, 'a').write('wp_sites.py ' + ' '.join(sys.argv[1:]) + '\\n')\nsys.exit(2 if sys.argv[1] == 'import' else 0)\n" % str(self.log))
        r = self.run_script("--yes", "--no-phone", "--site", "life")
        verbs = [c.split()[1] for c in self.sites_calls()]
        self.assertEqual(verbs, ["create", "import"], "it went no further than the failed import")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("import exited 2", r.stdout)
        self.assertIn("python3 scripts/wp_sites.py reset life", r.stdout)
        self.assertIn("delete life --commit", r.stdout)

    def test_the_phone_path_waits_for_a_tap_and_reports_a_missing_one(self):
        # a stand-in ssh that always says the work directory is still there makes the wait time out; shorten the wait by patching a copy of the script
        script = (self.repo / "scripts/wp_site_lifecycle_rehearsal_dellwp.sh")
        text = script.read_text().replace("waitgone 600", "waitgone 0")
        script.write_text(text)
        r = self.run_script("--yes", "--site", "life", stdin="y\ny\ny\ny\ny\n")
        self.assertIn("Tap DENY on the notification now", r.stdout)
        self.assertIn("no decision arrived", r.stdout)
        self.assertNotIn("reset life", " ".join(self.calls()), "with the phone, the deny is the phone's")


if __name__ == "__main__":
    unittest.main()
