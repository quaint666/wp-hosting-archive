#!/usr/bin/env python3
"""Offline tests of scripts/wp_monitor_rehearsal_dellwp.sh.

The script drives a real VM, so it cannot be run here; what can be checked is everything around that. Every snippet it sends to the VM (and the library they share) is syntax-checked by bash, and every
variable a snippet uses must be one the ssh line passes. Its arguments and refusals, the ORDER of its steps and the exact wp-ctl requests it makes are exercised in a throwaway git repo with a stand-in for
ssh. They prove that the policy is set to freeze before it is set back, that a failed job or a bad preflight stops the run, that the stand-in for ntfy, the throwaway record and the fill file are removed on
every way out (the end, a fatal stop and an exit that never reached the end), that the broken record is only written after the quota step, and that the script changes nothing in the repo. They do not prove
that any check inside a VM snippet is right: the rehearsal on dellwp does. Run: python3 -I tests/test_wp_monitor_script.py"""
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

SCRIPT = ROOT / "scripts/wp_monitor_rehearsal_dellwp.sh"
TEXT = SCRIPT.read_text()
JOB = "20260101T000000Z-aaaaaaaa"
SNIPPET = re.compile(r"(?:vmrun|vmtest)[^\n]*<<'EOF'[^\n]*\n(.*?)\nEOF\n", re.S)
HEADER = "\t".join(["site", "deployed", "actual", "unit", "containers", "core", "plugins", "backup_age_h", "backup_stamp", "quota_pct", "backup_quota_pct", "cron_timer", "backup_timer", "frozen"])
LINE = "\t".join(["provtest2", "hold", "hold", "active", "2/2", "6.8", "-", "3", "20260101T000000Z", "12", "4", "inactive", "active", "no"])


def snippets():
    """(label, body) of the library and of every heredoc sent to the VM."""
    out = [("LIB", re.search(r"<<'LIB'\n(.*?)\nLIB\n", TEXT, re.S).group(1))]
    for i, m in enumerate(SNIPPET.finditer(TEXT)):
        out.append(("snippet %d" % i, m.group(1)))
    return out


class Syntax(unittest.TestCase):
    def test_the_script_parses_and_prints_its_usage(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("MONITORS", r.stdout)
        self.assertIn("--site NAME", r.stdout)
        self.assertIn("Paste the whole output", r.stdout, "the usage covers the whole header")

    def test_every_snippet_sent_to_the_vm_parses_with_the_library_in_front(self):
        lib = snippets()[0][1]
        parts = snippets()
        self.assertGreaterEqual(len(parts), 6)
        for label, body in parts:
            r = subprocess.run(["bash", "-n"], input=lib + "\n" + body + "\n", capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, "%s: %s" % (label, r.stderr))

    def test_the_embedded_python_parses(self):
        found = 0
        for m in re.finditer(r"<<'PY'\n(.*?)\nPY\n", TEXT, re.S):
            compile(m.group(1), "embedded", "exec")
            found += 1
        self.assertGreaterEqual(found, 1)

    def test_a_snippet_that_asks_for_a_variable_has_it_in_the_env_line(self):
        env_line = re.search(r'vmtest\(\)[^\n]*sudo -n env ([^"]*) bash -s', TEXT).group(1)
        passed = set(re.findall(r"(\w+)=", env_line))
        used = set()
        for label, body in snippets():
            used |= set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", body))
        provided_inside = {"FAIL", "DIR", "R", "FILL", "PATH", "MOUNT", "QID"}      # set by the library, by the snippet itself or by bash
        self.assertEqual(sorted(used - passed - provided_inside), [])

    def test_every_snippet_that_reports_ends_in_finish(self):
        for label, body in snippets()[1:]:
            if "check " in body or "ok " in body or "bad " in body:
                self.assertEqual(body.rstrip().splitlines()[-1], "finish", "%s does not end in finish" % label)

    def test_the_heredocs_are_quoted_so_the_thinkpad_does_not_expand_the_vms_variables(self):
        for m in SNIPPET.finditer(TEXT):
            self.assertIn("'EOF'", m.group(0).splitlines()[0])

    def test_ntfy_is_a_stand_in_so_nothing_reaches_the_phone(self):
        lib = snippets()[0][1]
        self.assertIn("ntfy-notify", lib)
        self.assertIn('export PATH="$R/bin:$PATH"', lib)
        code = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("ntfy.sh", code)
        self.assertNotIn("curl", code)

    def test_the_script_never_commits_or_edits_the_repo(self):
        code = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))
        for word in ("git commit", "git add", "wp_sites.py", "--commit", "sed -i", "ansible-playbook"):
            self.assertNotIn(word, code, word)

    def test_nothing_secret_is_in_the_script(self):
        code = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))
        for word in ("vault", "master_secret", "read -s", "read -r -s", "PW="):
            self.assertNotIn(word, code, word)

    def test_the_registry_is_only_broken_after_the_quota_step_and_the_record_is_always_removed(self):
        self.assertGreater(TEXT.index("printf '{ not json'"), TEXT.index('say "4. disk check'))
        self.assertGreater(TEXT.index("printf '{ not json'"), TEXT.index('say "2. update policy'))
        cleanup = re.search(r"cleanup_vm\(\) \{.*?\n\}\n", TEXT, re.S).group(0)
        for needle in ('"$SITES/$BROKEN"', "/run/wp-monitor-rehearsal", ".monitor-rehearsal-fill", "backup-stale-$S.flag", "site-$S.level", "registry-unreadable.flag"):
            self.assertIn(needle, cleanup, needle)
        self.assertIn("trap '[ \"$CLEANED\" = yes ] || cleanup_vm", TEXT)


class Harness(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.repo = Path(self.d.name)
        (self.repo / "scripts").mkdir()
        (self.repo / "host_vars/dellwp").mkdir(parents=True)
        (self.repo / "vars").mkdir()
        (self.repo / "bin").mkdir()
        shutil.copy(SCRIPT, self.repo / "scripts/")
        shutil.copy(ROOT / "scripts/wp_sites_status.py", self.repo / "scripts/")
        shutil.copy(ROOT / "vars/wp.yml", self.repo / "vars/wp.yml")
        (self.repo / "host_vars/dellwp/vars.yml").write_text(fixture_vars_text())
        self.log = self.repo / "calls.log"
        # ssh answers like the VM would. A VM snippet ("bash -s") swallows its input and succeeds unless told otherwise; the cleanup snippet prints "cleaned" like the real one; the row file holds the
        # site's quota id, quota and update policy.
        (self.repo / "bin/ssh").write_text(r'''#!/bin/bash
echo "ssh $*" >> "%(log)s"
in=$(cat 2>/dev/null || true)
case "$*" in
  *"bash -s"*)
    case "$in" in
      *'rm -rf "$SITES/$BROKEN" /run/wp-monitor-rehearsal'*) echo "$in" >> "%(log)s.cleanup"; [ -n "${SSH_NO_CLEAN:-}" ] || echo cleaned; exit 0 ;;
    esac
    exit ${SSH_BASH_RC:-0} ;;
  *"sudo -n true"*) exit ${SSH_SUDO_RC:-0} ;;
  *"wp-monitor-rehearsal/row"*) echo "1002 512 ${SSH_POLICY:-notify}"; exit 0 ;;
  *"wp-sites-status"*) printf '%%s\n%%s\n' "%(header)s" "%(line)s"; exit 0 ;;
  *"wp-ctl set "*) echo '{"job": "%(job)s", "ok": true}'; exit 0 ;;
  *"job-status"*) echo '{"ok": true, "state": "done", "rc": '"${SSH_JOB_RC:-0}"'}'; exit 0 ;;
  *"job-log"*) echo '{"ok": true, "data": "the log of the job"}'; exit 0 ;;
esac
exit ${SSH_RC:-0}
''' % {"log": self.log, "job": JOB, "header": HEADER, "line": LINE})
        (self.repo / "bin/ssh").chmod(0o755)
        for cmd in (["init", "-q", "-b", "wp-test"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, capture_output=True)
        self.env = dict(os.environ, PATH="%s:%s" % (self.repo / "bin", os.environ["PATH"]))

    def tearDown(self):
        self.d.cleanup()

    def run_script(self, *args, env=None, stdin=""):
        return subprocess.run(["bash", str(self.repo / "scripts/wp_monitor_rehearsal_dellwp.sh"), *args], capture_output=True, text=True, env=env or self.env, input=stdin, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def ctl(self):
        """The wp-ctl requests that are not job polling, in order, as typed."""
        return [re.search(r"wp-ctl (.*?)\"?$", c).group(1) for c in self.calls() if "wp-ctl " in c and c.startswith("ssh") and "job-" not in c]

    def cleanups(self):
        p = Path(str(self.log) + ".cleanup")
        return p.read_text().count('rm -rf "$SITES/$BROKEN"') if p.exists() else 0

    def untouched(self):
        git = lambda *x: subprocess.run(["git", "-C", str(self.repo), *x], capture_output=True, text=True).stdout
        self.assertEqual(git("status", "--short", "--untracked-files=no"), "")
        self.assertEqual(git("log", "--oneline").count("\n"), 1, "no commit was made")


class Arguments(Harness):
    def test_bad_arguments_are_refused_before_anything_runs(self):
        for args, needle in ((("--bogus",), "unknown option"), (("--site", "Bad Name"), "a site name is"), (("--site", "a"), "a site name is"), (("--site", ""), "a site name is")):
            r = self.run_script(*args, "--yes")
            self.assertEqual(r.returncode, 2, args)
            self.assertIn(needle, r.stderr, args)
        self.assertEqual(self.calls(), [])

    def test_not_confirming_does_nothing(self):
        r = self.run_script(stdin="n\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was done.", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_problems_found_on_the_vm_stop_the_run_before_anything_is_changed(self):
        r = self.run_script("--yes", env=dict(self.env, SSH_BASH_RC="3"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the preflight found problems", r.stdout)
        self.assertEqual(self.ctl(), [])
        self.assertEqual(self.cleanups(), 1, "even a refused preflight removes what it put on the VM")

    def test_sudo_that_does_not_work_and_no_ssh_are_their_own_stops(self):
        r = self.run_script("--yes", env=dict(self.env, SSH_SUDO_RC="1"))
        self.assertIn("sudo -n does not work", r.stdout)
        r = self.run_script("--yes", env=dict(self.env, SSH_RC="255"))
        self.assertIn("cannot ssh to dellwp", r.stdout)
        self.assertEqual(self.ctl(), [])

    def test_a_site_already_on_freeze_is_refused_because_the_policy_step_would_prove_nothing(self):
        r = self.run_script("--yes", env=dict(self.env, SSH_POLICY="freeze"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("freeze already", r.stdout)
        self.assertEqual(self.ctl(), [])


class Flow(Harness):
    def go(self, *args, env=None):
        return self.run_script("--yes", *args, env=env)

    def test_the_policy_is_set_to_freeze_then_back_to_what_it_was(self):
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout[-2500:])
        self.assertIn("All checks passed.", r.stdout)
        self.assertEqual(self.ctl(), ["set site=provtest2 key=update_policy value=freeze req=mon-provtest2-freeze", "set site=provtest2 key=update_policy value=notify"])
        self.untouched()

    def test_the_policy_that_is_set_back_is_the_one_the_row_had(self):
        r = self.go(env=dict(self.env, SSH_POLICY="auto"))
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        self.assertEqual(self.ctl()[-1], "set site=provtest2 key=update_policy value=auto")

    def test_the_site_name_is_the_one_asked_for(self):
        r = self.go("--site", "montest9", env=dict(self.env))
        self.assertIn("Site: montest9", r.stdout)
        self.assertTrue(all("montest9" in c for c in self.ctl()))

    def test_the_cleanup_runs_once_at_the_end_and_not_again_at_exit(self):
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        self.assertEqual(self.cleanups(), 1)
        self.assertIn("the stand-in, the throwaway record, the fill file and this run's alert state are gone", r.stdout)

    def test_a_cleanup_that_did_not_confirm_is_a_failure_and_is_tried_again_at_exit(self):
        r = self.go(env=dict(self.env, SSH_NO_CLEAN="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAIL: the stand-in", r.stdout)
        self.assertEqual(self.cleanups(), 2, "the exit trap tries again")

    def test_a_job_that_fails_stops_the_run_and_cleans_up(self):
        r = self.go(env=dict(self.env, SSH_JOB_RC="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("set update_policy failed", r.stdout)
        self.assertEqual(self.ctl(), ["set site=provtest2 key=update_policy value=freeze req=mon-provtest2-freeze"], "nothing after the failed step, and no set back")
        self.assertEqual(self.cleanups(), 1)

    def test_a_failing_snippet_is_counted_and_the_run_goes_on_to_the_cleanup(self):
        r = self.go(env=dict(self.env, SSH_BASH_RC="2"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("check(s) failed", r.stdout)

    def test_the_status_step_runs_the_repos_status_script_on_what_the_host_printed(self):
        r = self.go()
        self.assertIn("provtest2", r.stdout)
        self.assertIn("deployed=hold", r.stdout)
        self.assertIn("does not call provtest2 a stray", r.stdout)

    def test_a_host_report_that_calls_the_site_a_stray_fails_the_step(self):
        # the real status script no longer says "does not declare it"; this checks that the grep in the rehearsal would still catch such a line if it came back
        fake = self.repo / "scripts/wp_sites_status.py"
        fake.write_text("import sys\nprint('Needs attention:')\nprint('  provtest2: the host reports it but the repo does not declare it')\nsys.exit(1)\n")
        r = self.go()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAIL: the ThinkPad half does not call provtest2 a stray", r.stdout)

    def test_it_changes_nothing_in_the_repo(self):
        self.go()
        self.untouched()


if __name__ == "__main__":
    unittest.main()
