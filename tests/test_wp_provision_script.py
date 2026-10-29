#!/usr/bin/env python3
"""Offline tests of scripts/wp_provision_rehearsal_dellwp.sh.

The script drives a real VM, so it cannot be run here; what can be checked is everything around that. Every snippet it sends to the VM (and the library they share) is syntax-checked by bash. Its
arguments and refusals, the ORDER of its steps and the exact wp-ctl requests it makes are exercised in a throwaway git repo with stand-ins for ssh (which answers like wp-ctl would) and for the python
scripts it calls. They prove that the password is typed once, reaches only the standard input of the install request and of the leak check and is in no argument list, and that a failed step stops the
run before the next one. They do not prove that any check inside a VM snippet is right: the rehearsal on dellwp does. Run: python3 -I tests/test_wp_provision_script.py"""
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

SCRIPT = ROOT / "scripts/wp_provision_rehearsal_dellwp.sh"
TEXT = SCRIPT.read_text()
CANARY = "Canary-Pass-7c1e!xyz"
JOB = "20260101T000000Z-aaaaaaaa"


def snippets():
    """(label, body) of the library and of every heredoc sent to the VM."""
    out = [("LIB", re.search(r"<<'LIB'\n(.*?)\nLIB\n", TEXT, re.S).group(1))]
    for i, m in enumerate(re.finditer(r"(?:vmrun|vmtest) <<'EOF'[^\n]*\n(.*?)\nEOF\n", TEXT, re.S)):
        out.append(("snippet %d" % i, m.group(1)))
    return out


class Syntax(unittest.TestCase):
    def test_the_script_parses(self):
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

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
        self.assertGreaterEqual(found, 2)

    def test_a_snippet_that_asks_for_a_variable_has_it_in_the_env_line(self):
        env_line = re.search(r'sudo -n env ([^"]*) bash -s', TEXT).group(1)
        passed = set(re.findall(r"(\w+)=", env_line))
        used = set()
        for label, body in snippets():
            used |= set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", body))
        local = {"FAIL", "DIR", "HOST", "UP", "DB", "PATH", "HOME", "PIPESTATUS", "SQL", "MARIADB_DATABASE", "MARIADB_PASSWORD", "MARIADB_USER", "S"}   # the middle four are for docker exec's own shell
        self.assertEqual(sorted(used - passed - local), [], "a snippet uses a variable the VM does not receive")

    def test_every_snippet_that_reports_by_count_ends_in_finish(self):
        for label, body in snippets()[1:]:
            self.assertTrue("\nfinish" in "\n" + body or body.strip() == "fingerprint", label)

    def test_nothing_secret_is_in_the_script_and_the_password_is_never_an_argument(self):
        self.assertNotRegex(TEXT, r"(?i)password\s*=\s*['\"]\w")
        for line in TEXT.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if "$PW" in line:
                self.assertRegex(line, r"printf '%s\\n' \"\$PW\" \||\[ \"\$PW\" = \"\$PW2\" \]|\(\"\$OUT\"|printf '%s' \"\$OUT\" \| grep -qF -- \"\$PW\"|echo \"   \(password: \$\{#PW\} characters\)\"|unset|read -r -s",
                                 "the password may only be read, compared, counted, piped and unset: " + line)
        self.assertIn("read -r -s", TEXT, "the password is read hidden")

    def test_the_old_way_delete_keeps_its_two_typed_names_and_the_parity_step_comes_before_the_create(self):
        delete_line = [l for l in TEXT.splitlines() if "wp_sites.py delete" in l and not l.lstrip().startswith("#")][0]
        self.assertNotIn("--yes", delete_line)
        self.assertLess(TEXT.index('say "1. parity'), TEXT.index('say "2. create'))
        self.assertLess(TEXT.index("wp_sites.py delete"), TEXT.index('say "2. create'))
        self.assertLess(TEXT.index("--tags wp_tools,wp_ctl"), TEXT.index('say "2. create'), "the tool's list of the repo's sites is refreshed before a site is given a port")

    def test_the_preflight_looks_for_everything_rehearsal7_could_have_left(self):
        for what in ("$SITES/rehearsal7", "wp-rehearsal7.service", "wp_site_rehearsal7.conf", '$BDIR/rehearsal7', "name=^wp_rehearsal7_"):
            self.assertIn(what, TEXT)


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
        self.stdins = self.repo / "stdins.log"
        # ssh answers like the VM would: wp-ctl replies as JSON with the exit status wp-ctl gives; a VM snippet ("bash -s") swallows its input and succeeds unless told otherwise
        (self.repo / "bin/ssh").write_text(r'''#!/bin/bash
echo "ssh $*" >> "%(log)s"
in=$(cat 2>/dev/null || true)
[ -z "$in" ] || printf 'stdin of [%%s]: %%s\n' "$*" "$in" >> "%(stdins)s"
[ -z "${SSH_PARITY_RC:-}" ] || case "$in" in *" render "*) exit $SSH_PARITY_RC ;; esac
case "$*" in
  *"bash -s"*) exit ${SSH_BASH_RC:-0} ;;
  *"sudo -n true"*) exit ${SSH_SUDO_RC:-0} ;;
  *"wp-ctl create name=hub"*) echo '{"error": "reserved_name", "ok": false}'; exit 2 ;;
  *"wp-ctl create name=Bad_Name"*) echo '{"error": "invalid_name", "ok": false}'; exit 2 ;;
  *"wp-ctl create name=provtest1 label=x set=1"*) echo '{"error": "unknown_argument", "ok": false}'; exit 2 ;;
  *"wp-ctl create name=provtest1 label=Provtest req="*) if [ -e "%(repo)s/created" ]; then echo '{"duplicate": true, "job": "%(job)s", "ok": true}'; else touch "%(repo)s/created"; echo '{"job": "%(job)s", "ok": true}'; fi; exit 0 ;;
  *"wp-ctl create name=provtest1"*) echo '{"error": "name_taken", "ok": false}'; exit 2 ;;
  *"wp-ctl apply site=nosuch"*) echo '{"error": "unknown_site", "ok": false}'; exit 2 ;;
  *"wp-ctl apply site=provtest1"*) echo '{"job": "%(job)s", "ok": true}'; exit 0 ;;
  *"job-status"*) echo '{"ok": true, "state": "done", "rc": '"%(jobrc)s"'}'; exit 0 ;;
  *"job-log"*) echo '{"ok": true, "data": "the log of the job"}'; exit 0 ;;
  *"wp-ctl install"*) echo '{"ok": true, "rc": 0, "out": ""}'; exit ${SSH_INSTALL_RC:-0} ;;
  *"base64 -d"*) echo "journal: ${SSH_JOURNAL_HITS:-0}"; [ -z "${SSH_FILE_HIT:-}" ] || echo "file: $SSH_FILE_HIT"; exit 0 ;;
  *"grep -c"*) echo 1; exit 0 ;;
esac
exit ${SSH_RC:-0}
''' % {"log": self.log, "stdins": self.stdins, "repo": self.repo, "job": JOB, "jobrc": "${SSH_JOB_RC:-0}"})
        # the old-way tool: records its arguments; `create` also puts the site into wp_sites as the real one does
        (self.repo / "scripts/wp_sites.py").write_text('''#!/usr/bin/env python3
import sys
open(%r, 'a').write('wp_sites.py ' + ' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'create':
    p = %r
    t = open(p).read()
    open(p, 'w').write(t.replace('wp_sites:\\n', 'wp_sites:\\n  - name: ' + sys.argv[3] + '\\n    port: 9150\\n    quota_id: 1150\\n    state: hold\\n    update_policy: notify\\n', 1))
sys.exit(int(open(%r).read()) if sys.argv[1] in open(%r).read().split() else 0)
''' % (str(self.log), str(self.repo / "host_vars/dellwp/vars.yml"), str(self.repo / "fail_rc"), str(self.repo / "fail_rc")))
        (self.repo / "fail_rc").write_text("0")
        for p in (self.repo / "bin").iterdir():
            p.chmod(0o755)
        for cmd in (["init", "-q", "-b", "wp-test"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, capture_output=True)
        self.env = dict(os.environ, PATH="%s:%s" % (self.repo / "bin", os.environ["PATH"]))

    def tearDown(self):
        self.d.cleanup()

    def run_script(self, *args, env=None, stdin=""):
        return subprocess.run(["bash", str(self.repo / "scripts/wp_provision_rehearsal_dellwp.sh"), *args], capture_output=True, text=True, env=env or self.env, input=stdin, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def ctl(self):
        """The wp-ctl requests, in order, as typed."""
        return [re.search(r"wp-ctl (.*?)\"?$", c).group(1) for c in self.calls() if "wp-ctl " in c and c.startswith("ssh") and "job-" not in c]


class Arguments(Harness):
    def test_bad_arguments_are_refused_before_anything_runs(self):
        for args, needle in ((("--bogus",), "unknown option"), (("--site", "Bad Name"), "a site name is"), (("--site", "a"), "a site name is"), (("--parity-site", ""), "a site name is"),
                             (("--site", "same", "--parity-site", "same"), "must differ")):
            r = self.run_script(*args, "--yes")
            self.assertEqual(r.returncode, 2, args)
            self.assertIn(needle, r.stderr, args)
        self.assertEqual(self.calls(), [])

    def test_the_parity_step_will_not_run_on_main_or_over_uncommitted_changes_but_the_rest_may(self):
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", "main"], check=True, capture_output=True)
        r = self.run_script("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("you are on main", r.stderr)
        self.assertEqual(self.calls(), [])
        r = self.run_script("--yes", "--skip-parity", stdin="%s\n%s\n" % (CANARY, CANARY))
        self.assertEqual(r.returncode, 0, r.stdout[-800:])
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "wp-test"], check=True, capture_output=True)
        (self.repo / "host_vars/dellwp/vars.yml").write_text((self.repo / "host_vars/dellwp/vars.yml").read_text() + "# x\n")
        r = self.run_script("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("uncommitted changes", r.stderr)

    def test_a_name_already_in_wp_sites_is_refused(self):
        for args in (("--site", "wptest"), ("--parity-site", "wptest")):
            r = self.run_script(*args, "--yes")
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("already in wp_sites", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_not_confirming_does_nothing(self):
        r = self.run_script(stdin="n\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was done.", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_problems_found_on_the_vm_stop_the_run_before_anything_is_made(self):
        r = self.run_script("--yes", env=dict(self.env, SSH_BASH_RC="3"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the preflight found problems", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("wp_sites.py") or "wp-ctl create" in c])

    def test_sudo_that_does_not_work_and_no_ssh_are_their_own_stops(self):
        r = self.run_script("--yes", env=dict(self.env, SSH_SUDO_RC="1"))
        self.assertIn("sudo -n does not work", r.stdout)
        r = self.run_script("--yes", env=dict(self.env, SSH_RC="255"))
        self.assertIn("cannot ssh to dellwp", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("wp_sites.py") or "wp-ctl create" in c])


class Flow(Harness):
    def go(self, *args, stdin=None, env=None):
        return self.run_script("--yes", *args, stdin=stdin if stdin is not None else "%s\n%s\n" % (CANARY, CANARY), env=env)

    def test_steps_two_to_five_make_exactly_these_requests_in_this_order(self):
        r = self.go("--skip-parity")
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])
        self.assertEqual(self.ctl(), ["create name=provtest1 label=Provtest req=prov-provtest1", "create name=provtest1 label=Provtest req=prov-provtest1", "create name=provtest1",
                                      "create name=hub", "create name=Bad_Name", "apply site=nosuchprovtest1", "create name=provtest1 label=x set=1",
                                      "install site=provtest1 title=Provtest user=owner email=owner@example.org", "apply site=provtest1"])
        self.assertIn("All checks passed.", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("wp_sites.py")], "no old-way call without the parity step")

    def test_the_password_is_asked_hidden_and_reaches_only_the_install_request_and_the_leak_check_on_their_standard_input(self):
        r = self.go("--skip-parity")
        self.assertNotIn(CANARY, r.stdout + r.stderr, "never shown")
        self.assertIn("password: %d characters" % len(CANARY), r.stdout)
        self.assertEqual([c for c in self.calls() if CANARY in c], [], "in no argument list")
        seen = [l for l in self.stdins.read_text().splitlines() if CANARY in l]
        self.assertEqual(len(seen), 2, seen)
        self.assertIn("wp-ctl install site=provtest1", seen[0])
        self.assertIn("base64 -d", seen[1])

    def test_a_password_that_differs_or_is_short_stops_before_the_install(self):
        for stdin in ("%s\nother-%s\n" % (CANARY, CANARY), "short\nshort\n"):
            r = self.go("--skip-parity", stdin=stdin)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("differ or are shorter", r.stdout)
        self.assertFalse([c for c in self.ctl() if c.startswith("install")])

    def test_a_password_found_in_a_file_or_the_journal_is_a_failure(self):
        for env in (dict(SSH_FILE_HIT="/var/lib/wp-ctl/jobs/x/log"), dict(SSH_JOURNAL_HITS="2")):
            r = self.go("--skip-parity", env=dict(self.env, **env))
            self.assertNotEqual(r.returncode, 0, env)
            self.assertIn("FAIL: the password is in no file", r.stdout)

    def test_a_failed_install_stops_the_run_and_says_nothing_of_the_password(self):
        r = self.go("--skip-parity", env=dict(self.env, SSH_INSTALL_RC="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(CANARY, r.stdout + r.stderr)
        self.assertNotIn("apply site=provtest1", self.ctl()[-1:])

    def test_a_create_job_that_fails_stops_the_run_before_anything_else(self):
        r = self.go("--skip-parity", env=dict(self.env, SSH_JOB_RC="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the create job failed", r.stdout)
        self.assertEqual(self.ctl(), ["create name=provtest1 label=Provtest req=prov-provtest1"])

    def test_the_parity_step_builds_the_record_from_the_wp_sites_entry_then_deletes_the_old_way_site_and_waits_for_the_play(self):
        r = self.go("--site", "provtest1", stdin="y\n%s\n%s\n" % (CANARY, CANARY))
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])
        old = [c for c in self.calls() if c.startswith("wp_sites.py")]
        self.assertEqual(old, ["wp_sites.py create --name parity1 --no-install --policy notify --yes --commit", "wp_sites.py stop parity1 --yes --commit", "wp_sites.py delete parity1 --commit"])
        record_call = [c for c in self.calls() if "write_site" in c]
        self.assertEqual(len(record_call), 1)
        self.assertIn('"name": "parity1"', record_call[0])
        self.assertIn('"port": 9150', record_call[0])
        self.assertIn('"quota_id": 1150', record_call[0])
        self.assertIn("ansible-playbook -i inventory.yml dellwp/wp_sites_dellwp.yml --tags wp_tools,wp_ctl", r.stdout)
        order = [i for i, c in enumerate(self.calls()) if c.startswith("wp_sites.py delete")][0], [i for i, c in enumerate(self.calls()) if "wp-ctl create name=provtest1 label" in c][0]
        self.assertLess(*order)

    def test_a_play_that_was_not_re_run_stops_before_the_create(self):
        r = self.go("--site", "provtest1", stdin="n\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("re-run the tools play", r.stdout)
        self.assertFalse([c for c in self.ctl() if c.startswith("create")])

    def test_a_parity_failure_on_the_vm_stops_before_the_old_way_site_is_deleted(self):
        r = self.go("--site", "provtest1", env=dict(self.env, SSH_PARITY_RC="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the tool does not see the play", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("wp_sites.py stop") or c.startswith("wp_sites.py delete")])


if __name__ == "__main__":
    unittest.main()
