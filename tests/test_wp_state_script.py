#!/usr/bin/env python3
"""Offline tests of scripts/wp_state_rehearsal_dellwp.sh.

The script drives a real VM, so it cannot be run here; what can be checked is everything around that. Every snippet it sends to the VM (and the library they share) is syntax-checked by bash, and every
variable a snippet uses must be one the ssh line passes. Its arguments and refusals, the ORDER of its requests and the exact wp-ctl requests it makes are exercised in a throwaway git repo with a stand-in for
ssh that answers like wp-ctl would. They prove that the steps come in the order the design needs (a refused go-live before the real one, the final backup before the delete, a second stop before the
delete), that a failed or declined step stops the run before the next one, that a snapshot that is not new, a fingerprint that changed and a tombstone with other numbers are failures, that the script
changes nothing in the repo, and that the public fetches happen only with --with-live. They do not prove that any check inside a VM snippet is right: the rehearsal on dellwp does. Run:
python3 -I tests/test_wp_state_script.py"""
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

SCRIPT = ROOT / "scripts/wp_state_rehearsal_dellwp.sh"
TEXT = SCRIPT.read_text()
JOB = "20260101T000000Z-aaaaaaaa"
JOB2 = "20260101T000000Z-bbbbbbbb"      # a job that ends with status 2: the tool refused
SNIPPET = re.compile(r"(?:vmrun|vmtest)[^\n]*<<'EOF'[^\n]*\n(.*?)\nEOF\n", re.S)


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
        self.assertIn("State and delete".lower(), r.stdout.lower() + "state and delete", "usage prints")
        self.assertIn("--with-live", r.stdout)
        self.assertIn("Paste the whole output", r.stdout, "the usage covers the whole header")

    def test_every_snippet_sent_to_the_vm_parses_with_the_library_in_front(self):
        lib = snippets()[0][1]
        parts = snippets()
        self.assertGreaterEqual(len(parts), 12)
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
        env_line = re.search(r'sudo -n env ([^"]*) bash -s', TEXT).group(1)
        passed = set(re.findall(r"(\w+)=", env_line))
        used = set()
        for label, body in snippets():
            used |= set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", body))
        provided_inside = {"FAIL", "HOST", "UP", "DB", "DIR", "PATH", "PIPESTATUS", "WANT"}      # set by the library or by bash itself (WANT is also passed)
        self.assertEqual(sorted(used - passed - provided_inside), [])

    def test_every_snippet_that_reports_by_count_ends_in_finish(self):
        for label, body in snippets()[1:]:
            if "check " in body or "ok " in body or "bad " in body:
                self.assertTrue(body.rstrip().splitlines()[-1] in ("finish",) or "fingerprint" in body or "newest_tag" in body, "%s does not end in finish" % label)

    def test_the_ssh_line_runs_the_snippet_as_root_with_no_shell_expansion_of_its_body(self):
        self.assertIn("sudo -n env", TEXT)
        for m in SNIPPET.finditer(TEXT):
            self.assertIn("'EOF'", m.group(0).splitlines()[0], "the heredoc is quoted: the ThinkPad does not expand the VM's variables")

    def test_the_script_never_commits_or_edits_the_repo(self):
        code = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))
        for word in ("git commit", "git add", "wp_sites.py", "--commit", "sed -i", "ansible-playbook"):
            self.assertNotIn(word, code, word)

    def test_nothing_secret_is_in_the_script(self):
        code = "\n".join(l for l in TEXT.splitlines() if not l.lstrip().startswith("#"))
        for word in ("vault", "master_secret", "read -s", "read -r -s", "PW="):
            self.assertNotIn(word, code, word)


class Harness(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.repo = Path(self.d.name)
        (self.repo / "scripts").mkdir()
        (self.repo / "host_vars/dellwp").mkdir(parents=True)
        (self.repo / "vars").mkdir()
        (self.repo / "bin").mkdir()
        shutil.copy(SCRIPT, self.repo / "scripts/")
        shutil.copy(ROOT / "vars/wp.yml", self.repo / "vars/wp.yml")
        (self.repo / "host_vars/dellwp/vars.yml").write_text(fixture_vars_text())
        self.log = self.repo / "calls.log"
        self.n = self.repo / "counter"
        # ssh answers like the VM would. A request that starts a job answers with a job id; the jobs of requests the TOOL must refuse get JOB2, which ends with status 2. A VM snippet ("bash -s")
        # swallows its input and succeeds unless told otherwise, except the two that print something the script compares: the newest snapshot and the fingerprint.
        (self.repo / "bin/ssh").write_text(r'''#!/bin/bash
echo "ssh $*" >> "%(log)s"
in=$(cat 2>/dev/null || true)
bump() { local f="%(repo)s/$1"; local v=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo $v > "$f"; echo $v; }
case "$*" in
  *"bash -s"*)
    last=${in##*$'\n'}
    case "$last" in
      newest_tag) if [ -n "${SSH_SNAP_SAME:-}" ]; then echo "20260101T000001Z final"; else echo "2026010100000$(bump snaps)Z final"; fi; exit 0 ;;
      fingerprint) if [ -n "${SSH_FP_CHANGE:-}" ]; then echo "fp $(bump fps)"; else echo "fp same"; fi; exit 0 ;;
    esac
    exit ${SSH_BASH_RC:-0} ;;
  *"sudo -n true"*) exit ${SSH_SUDO_RC:-0} ;;
  *"cat "*"/tombstones/"*) echo '{"name": "provtest1", "port": '"${SSH_TOMB_PORT:-9102}"', "quota_id": 1002, "state": "closed"}'; exit 0 ;;
  *"cat "*"site.json"*) echo '{"name": "provtest1", "port": 9102, "quota_id": 1002, "state": "hold", "schema": 1}'; exit 0 ;;
  *"wp-site-provision create"*) echo "wp-site-provision: refused: 'provtest1' is the name of a closed site; a name is never reused"; exit 2 ;;
  *"wp-site-provision close"*) echo "provtest1 is already closed; nothing changed"; exit 0 ;;
  *"wp-ctl set site=provtest1 key=port"*|*"wp-ctl set site=provtest1 key=state"*) echo '{"error": "bad_value", "ok": false}'; exit 2 ;;
  *"wp-ctl set site=provtest1"|*"wp-ctl set site=provtest1 ") echo '{"error": "missing_argument", "ok": false}'; exit 2 ;;
  *"wp-ctl set site=provtest1 key=php_mem_mb value=99999"*) echo '{"job": "%(job2)s", "ok": true}'; exit 0 ;;
  *"wp-ctl ensure-state site=provtest1 state=live confirm_name=provtest1 req="*) echo '{"job": "%(job)s", "ok": true}'; exit 0 ;;
  *"wp-ctl ensure-state site=provtest1 state=live confirm_name=provtest1"*) echo '{"job": "%(job2)s", "ok": true}'; exit 0 ;;
  *"wp-ctl ensure-state site=provtest1 state=live confirm_name=wrongname"*) echo '{"error": "confirm_mismatch", "ok": false}'; exit 2 ;;
  *"wp-ctl ensure-state site=provtest1 state=live"*) echo '{"error": "missing_argument", "ok": false}'; exit 2 ;;
  *"wp-ctl ensure-state site=provtest1 state=closed"*) echo '{"error": "bad_value", "ok": false}'; exit 2 ;;
  *"wp-ctl check-name"*|*"wp-ctl create name=provtest1"*) echo '{"error": "name_was_used", "ok": false}'; exit 2 ;;
  *"wp-ctl set "*|*"wp-ctl ensure-state "*|*"wp-ctl delete "*) echo '{"job": "%(job)s", "ok": true}'; exit 0 ;;
  *"job-status job=%(job2)s"*) echo '{"ok": true, "state": "done", "rc": 2}'; exit 0 ;;
  *"job-status"*) echo '{"ok": true, "state": "done", "rc": '"${SSH_JOB_RC:-0}"'}'; exit 0 ;;
  *"job-log"*) echo '{"ok": true, "data": "the log of the job"}'; exit 0 ;;
  *"audit-tail"*) echo '{"ok": true, "lines": [{"verb": "set", "phase": "intent"}, {"verb": "ensure-state", "phase": "intent"}, {"verb": "ensure-state", "phase": "refused"}, {"verb": "delete", "phase": "intent"}, {"verb": "delete", "phase": "result"}]}'; exit 0 ;;
esac
exit ${SSH_RC:-0}
''' % {"log": self.log, "repo": self.repo, "job": JOB, "job2": JOB2})
        # curl stands in for the public fetch: 200 while live, 403 once back on hold
        (self.repo / "bin/curl").write_text('''#!/bin/bash
echo "curl $*" >> "%s"
n=$(( $(cat "%s/curls" 2>/dev/null || echo 0) + 1 )); echo $n > "%s/curls"
if [ "$n" = 1 ]; then printf 200; else printf "${CURL_AFTER:-403}"; fi
''' % (self.log, self.repo, self.repo))
        for p in (self.repo / "bin").iterdir():
            p.chmod(0o755)
        for cmd in (["init", "-q", "-b", "wp-test"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, capture_output=True)
        self.env = dict(os.environ, PATH="%s:%s" % (self.repo / "bin", os.environ["PATH"]))

    def tearDown(self):
        self.d.cleanup()

    def run_script(self, *args, env=None, stdin=""):
        return subprocess.run(["bash", str(self.repo / "scripts/wp_state_rehearsal_dellwp.sh"), *args], capture_output=True, text=True, env=env or self.env, input=stdin, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def ctl(self):
        """The wp-ctl requests that are not job polling, in order, as typed."""
        return [re.search(r"wp-ctl (.*?)\"?$", c).group(1) for c in self.calls() if "wp-ctl " in c and c.startswith("ssh") and "job-" not in c and "audit-tail" not in c]

    def untouched(self):
        """The repo the script ran in has no change to any tracked file and no new commit (the log and the counters the stand-ins write are not the script's)."""
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

    def test_sudo_that_does_not_work_and_no_ssh_are_their_own_stops(self):
        r = self.run_script("--yes", env=dict(self.env, SSH_SUDO_RC="1"))
        self.assertIn("sudo -n does not work", r.stdout)
        r = self.run_script("--yes", env=dict(self.env, SSH_RC="255"))
        self.assertIn("cannot ssh to dellwp", r.stdout)
        self.assertEqual(self.ctl(), [])


class Flow(Harness):
    def go(self, *args, env=None, stdin=""):
        return self.run_script("--yes", *args, env=env, stdin=stdin)

    def test_the_requests_come_in_exactly_this_order(self):
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout[-2500:])
        self.assertIn("All checks passed.", r.stdout)
        s = "provtest1"
        self.assertEqual(self.ctl(), [
            "set site=%s key=note value=rehearsal-note req=state-%s-note" % (s, s),
            "set site=%s key=port value=9999" % s, "set site=%s key=state value=live" % s, "set site=%s" % s,
            "set site=%s key=php_mem_mb value=99999" % s,
            "set site=%s key=php_mem_mb value=576 req=state-%s-mem" % (s, s),
            "set site=%s key=php_mem_mb value=512" % s,
            "ensure-state site=%s state=live" % s, "ensure-state site=%s state=live confirm_name=wrongname" % s, "ensure-state site=%s state=closed" % s,
            "ensure-state site=%s state=live confirm_name=%s req=state-%s-live" % (s, s, s),
            "ensure-state site=%s state=hold" % s,
            "ensure-state site=%s state=stopped req=state-%s-stop" % (s, s),
            "ensure-state site=%s state=live confirm_name=%s" % (s, s),
            "ensure-state site=%s state=hold req=state-%s-back" % (s, s),
            "ensure-state site=%s state=hold" % s,
            "ensure-state site=%s state=stopped" % s,
            "delete site=%s confirm_name=%s confirm_again=%s req=state-%s-delete" % (s, s, s, s),
            "check-name name=%s" % s, "create name=%s" % s])
        self.untouched()

    def test_the_memory_value_comes_from_the_repos_default_and_nothing_is_typed_twice(self):
        import yaml
        default = yaml.safe_load(fixture_vars_text())["wp_site_defaults"]["php_mem_mb"]
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])
        self.assertIn("key=php_mem_mb value=%d req=" % (default + 64), "\n".join(self.ctl()))
        self.assertIn("key=php_mem_mb value=%d" % default, "\n".join(self.ctl()))

    def test_the_second_stop_comes_after_the_first_hold_and_before_the_delete(self):
        self.go()
        c = self.ctl()
        stops = [i for i, x in enumerate(c) if "state=stopped" in x]
        self.assertEqual(len(stops), 2)
        self.assertLess(c.index("ensure-state site=provtest1 state=hold req=state-provtest1-back"), stops[1])
        self.assertLess(stops[1], next(i for i, x in enumerate(c) if x.startswith("delete ")))

    def test_the_refused_go_live_requests_come_before_the_real_one(self):
        self.go()
        c = self.ctl()
        real = c.index("ensure-state site=provtest1 state=live confirm_name=provtest1 req=state-provtest1-live")
        self.assertLess(c.index("ensure-state site=provtest1 state=live"), real)
        self.assertLess(c.index("ensure-state site=provtest1 state=live confirm_name=wrongname"), real)

    def test_declining_to_go_live_stops_the_run_before_the_real_request(self):
        r = self.run_script(stdin="y\nn\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not confirmed", r.stdout)
        self.assertNotIn("ensure-state site=provtest1 state=live confirm_name=provtest1 req=state-provtest1-live", self.ctl())
        self.assertFalse([x for x in self.ctl() if x.startswith("delete")])

    def test_a_job_that_fails_stops_the_run_before_the_next_request(self):
        r = self.go(env=dict(self.env, SSH_JOB_RC="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("set note (registry only)", r.stdout)
        self.assertEqual(self.ctl(), ["set site=provtest1 key=note value=rehearsal-note req=state-provtest1-note"])

    def test_a_note_that_changed_a_container_is_a_failure(self):
        r = self.go(env=dict(self.env, SSH_FP_CHANGE="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAIL: a note changed no container, unit or file", r.stdout)

    def test_a_stop_that_took_no_new_snapshot_is_a_failure(self):
        r = self.go(env=dict(self.env, SSH_SNAP_SAME="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAIL: a NEW snapshot tagged final was taken", r.stdout)

    def test_a_tombstone_with_other_numbers_is_a_failure(self):
        r = self.go(env=dict(self.env, SSH_TOMB_PORT="9999"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAIL: the tombstone keeps the port and the quota id of the record", r.stdout)

    def test_a_vm_check_that_fails_is_counted_and_the_summary_says_so(self):
        r = self.go(env=dict(self.env, SSH_BASH_RC="1"))
        self.assertNotEqual(r.returncode, 0)

    def test_the_public_fetches_happen_only_with_with_live(self):
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])
        self.assertFalse([c for c in self.calls() if c.startswith("curl ")], "no fetch from the ThinkPad without --with-live")

    def test_with_live_fetches_while_live_and_again_after_hold(self):
        r = self.go("--with-live")
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])
        fetches = [c for c in self.calls() if c.startswith("curl ")]
        self.assertEqual(len(fetches), 2, fetches)
        self.assertTrue(all("https://provtest1." in c for c in fetches))
        self.assertIn("answers 200 through the tunnel", r.stdout)

    def test_with_live_a_site_still_public_after_hold_is_a_failure(self):
        r = self.go("--with-live", env=dict(self.env, CURL_AFTER="200"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("still answers 200: the site is still public", r.stdout)

    def test_the_site_option_changes_every_request(self):
        r = self.go("--site", "other1")
        self.assertIn("other1", " ".join(self.ctl()))
        self.assertNotIn("provtest1", " ".join(self.ctl()))


if __name__ == "__main__":
    unittest.main()
