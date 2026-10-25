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
    for i, m in enumerate(re.finditer(r"(?:vmrun|vmtest) <<'EOF'\n(.*?)\nEOF\n", TEXT, re.S)):
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

    def exit_status_snippets(self):
        """Snippets sent with vmtest: their exit status is the answer, so they do not end in finish."""
        out = set()
        for i, m in enumerate(re.finditer(r"(vmrun|vmtest) <<'EOF'\n(.*?)\nEOF\n", TEXT, re.S)):
            if m.group(1) == "vmtest":
                out.add("snippet %d" % i)
        return out

    def test_every_snippet_ends_by_reporting_its_failures_as_its_exit_code(self):
        for label, body in snippets()[1:]:
            self.assertTrue(body.rstrip().endswith("finish") or label in self.exit_status_snippets() or "\nfinish" in body, label)

    def test_a_snippet_that_asks_for_a_variable_has_it_in_the_env_line(self):
        env_line = re.search(r'sudo -n env ([^"]*) bash -s', TEXT).group(1)
        passed = set(re.findall(r"(\w+)=", env_line))
        used = set()
        for label, body in snippets():
            used |= set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", body))
        local = {"FAIL", "DIR", "HOST", "WORK", "UP", "DB", "BASE", "CS", "PC", "AP", "PREFIX", "PATH", "HOME", "PIPESTATUS", "SQL", "MARIADB_DATABASE", "MARIADB_PASSWORD", "MARIADB_USER"}   # the last four are for docker exec's own shell
        self.assertEqual(sorted(used - passed - local - {"BIN"}), [], "a snippet uses a variable the VM does not receive")
        for needed in ("SITE", "DOMAIN", "BIN", "SITES", "INTAKE", "LISTEN", "BDIR", "ARCH", "ADIR", "FILENAME"):
            self.assertIn(needed, passed)

    def test_options_are_read_with_plugins_skipped_and_hidden_files_are_named(self):
        self.assertRegex(TEXT, r'wpr\(\) \{ "\$BIN/wp-site-cli" "\$SITE" "\$@" --skip-plugins --skip-themes')
        self.assertNotRegex(TEXT, r'wpc option get (blog_public|active_plugins)')
        self.assertIn("(info) hidden file:", TEXT)
        self.assertIn("(diff, < now, > before)", TEXT)

    def test_the_phone_is_told_not_to_tap_before_the_import_checks_have_read_the_import(self):
        line = "do NOT tap it yet"
        self.assertEqual(TEXT.count(line), 3, "after each import: the first, the second, and the replace rounds")
        for phase, nxt in (("PHASE=import1", "PHASE=deny"), ("PHASE=import2", "PHASE=approve")):
            a, b = TEXT.index(phase), TEXT.index(nxt)
            seg = TEXT[a:b]
            self.assertIn(line, seg, phase)
            self.assertLess(seg.index(line), seg.index("vmrun <<"), phase)

    def test_the_import_checks_show_the_scan_lines_and_fail_only_when_a_scan_tool_broke(self):
        self.assertIn("(ClamAV|known vulnerabilities):", TEXT)
        self.assertIn("'^  ClamAV: .*FAILED'", TEXT); self.assertIn("'^  known vulnerabilities: FAILED'", TEXT)
        self.assertIn("NOT RUN is allowed", TEXT)

    def test_the_replace_phase_sits_between_approve_and_operate_and_only_runs_when_asked(self):
        a, b = TEXT.index("PHASE=approve"), TEXT.index("PHASE=operate")
        self.assertTrue(a < TEXT.index("PHASE=replace") < b)
        self.assertIn('if [ "$REPLACE" = yes ]; then', TEXT)
        seg = TEXT[TEXT.index("PHASE=replace"):b]
        self.assertIn('python3 scripts/wp_sites.py import "$SITE" "$FILE" --replace --yes', seg)
        self.assertEqual(seg.count("do NOT tap it yet"), 1, "inside the loop: once per round")
        self.assertLess(seg.index("do NOT tap it yet"), seg.index("vmrun <<'EOF'\ncheck \"the work directory exists\""))
        for needle in ("tag=pre-import", "tag=pre-update", '"$(snaps)" "$(cat "$BASE.replace.snaps")"', '"$(tables)" "$(cat "$BASE.replace.tables")"', "paths, owners, modes", "stop_here replace"):
            self.assertIn(needle, seg)

    def test_a_deny_is_checked_for_the_snapshot_the_import_took(self):
        before, deny = TEXT.index('snaps > "$BASE.snaps"'), TEXT.index('check "the snapshot the import took is gone again')
        self.assertLess(TEXT.index('PHASE=create') if 'PHASE=create' in TEXT else 0, before)
        self.assertLess(before, TEXT.index('PHASE=deny'))
        self.assertGreater(deny, TEXT.index('PHASE=deny'))
        self.assertLess(deny, TEXT.index('PHASE=import2'))
        self.assertIn('"$(snaps)" "$(cat "$BASE.snaps")"', TEXT)

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
        (self.repo / "bin/ssh").write_text('#!/bin/bash\necho "ssh $*" >> "%s"\nin=$(cat 2>/dev/null || true)\n[ -n "${SSH_FAIL_ON:-}" ] && case "$in" in *"$SSH_FAIL_ON"*) exit 1 ;; esac\ncase "$*" in *"bash -s"*) exit ${SSH_BASH_RC:-${SSH_RC:-0}} ;; *"sudo -n true"*) exit ${SSH_SUDO_RC:-${SSH_RC:-0}} ;; esac\nexit ${SSH_RC:-0}\n' % self.log)
        for name in ("wp_sites.py", "wp_wpress.py"):
            body = "#!/usr/bin/env python3\nimport sys\nopen(%r, 'a').write('%s ' + ' '.join(sys.argv[1:]) + '\\n')\n" % (str(self.log), name)
            if name == "wp_wpress.py":
                body += "print('GO')\n"
            (self.repo / "scripts" / name).write_text(body)
        # curl: answers like the site would, by the state the last `modify` asked for (it reads the call log); CURL_* in the environment change what it says
        (self.repo / "bin/curl").write_text('#!/bin/bash\necho "curl $*" >> "%s"\nlast=$(grep -o "state=[a-z]*" "%s" | tail -n 1)\nurl=${@: -1}\n'
                                            'if [ "$last" = state=live ]; then\n  case "$*" in *" -w "*) case "$url" in *wp-login.php) echo "${CURL_LOGIN:-200}" ;; *) echo "${CURL_LIVE:-200}" ;; esac ;;\n'
                                            '    *) printf \'<html><link href="/wp-content/themes/x/style.css">%%s</html>\' "${CURL_BODY:-}" ;; esac\n'
                                            'else\n  case "$*" in *" -w "*) echo "${CURL_HOLD:-403}" ;; *) echo forbidden ;; esac\nfi\n' % (str(self.log), str(self.log)))
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

    def test_existing_needs_a_declared_site_on_hold(self):
        r = self.run_script("--existing", "--site", "nosuch", "--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--existing needs 'nosuch' to be in wp_sites and on hold (it is not there)", r.stderr)
        r = self.run_script("--existing", "--site", "wptest", "--yes")       # the fixture's site is live
        self.assertEqual(r.returncode, 2)
        self.assertIn("(it is live)", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_name_in_use_points_at_existing(self):
        r = self.run_script("--site", "wptest", "--yes")
        self.assertIn("use --existing for an empty site on hold", r.stderr)

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

    def test_with_replace_it_imports_twice_more_denies_then_approves_in_that_order(self):
        r = self.run_script("--yes", "--no-phone", "--with-replace", "--site", "life", "--until", "replace")
        imp = "wp_sites.py import life %s/wpress/practice.wpress" % self.repo
        self.assertEqual(self.sites_calls(), [
            "wp_sites.py create --name life --no-install --policy notify --yes --commit", imp + " --yes", "wp_sites.py reset life", imp + " --yes", "wp_sites.py approve life --yes",
            imp + " --replace --yes", "wp_sites.py reset life", imp + " --replace --yes", "wp_sites.py approve life --yes"])
        self.assertIn("Stopped after 'replace' as asked", r.stdout)
        self.assertIn("Round: deny", r.stdout); self.assertIn("Round: approve", r.stdout)

    def test_without_with_replace_the_replace_phase_never_runs(self):
        r = self.run_script("--yes", "--no-phone", "--site", "life", "--until", "approve")
        self.assertNotIn("--replace", " ".join(self.sites_calls())); self.assertNotIn("5b.", r.stdout)

    def test_existing_skips_the_create_and_goes_on_with_the_import(self):
        vars_path = self.repo / "host_vars/dellwp/vars.yml"
        text = vars_path.read_text()
        vars_path.write_text(text.replace("\nwp_sites:\n", "\nwp_sites:\n  - name: held\n    port: 9109\n    quota_id: 1009\n    state: hold\n", 1))
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "held"], check=True, capture_output=True)
        r = self.run_script("--yes", "--no-phone", "--existing", "--site", "held", "--until", "approve")
        verbs = [c.split()[1] for c in self.sites_calls()]
        self.assertEqual(verbs, ["import", "reset", "import", "approve"], r.stdout)
        self.assertIn("(--existing: the site is already declared; no create)", r.stdout)

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

    def test_every_phase_says_how_long_it_took_and_the_summary_adds_them_up(self):
        r = self.run_script("--yes", "--no-phone", "--site", "life", "--until", "approve")
        for phase in ("preflight", "create", "import1", "deny", "import2"):
            self.assertRegex(r.stdout, r"\(%s took \d+ s, your waiting" % phase)
        self.assertRegex(r.stdout, r"\(approve took \d+ s")
        self.assertRegex(r.stdout, r"== summary\n\s+took preflight: \d+ s\n(\s+took [a-z0-9]+: \d+ s\n)+\s+total: \d+ s")

    def test_with_live_asks_its_own_question_and_goes_back_to_hold(self):
        r = self.run_script("--yes", "--no-phone", "--with-live", "--site", "life", "--until", "live")
        calls = self.sites_calls()
        self.assertIn("wp_sites.py modify life --set state=live --commit", calls)
        self.assertEqual(calls[-1], "wp_sites.py modify life --set state=hold --yes --commit")
        self.assertNotIn("--yes-public", " ".join(calls))

    def live(self, **env):
        return self.run_script("--yes", "--no-phone", "--with-live", "--site", "life", "--until", "live", env=dict(self.env, **env))

    def test_going_live_checks_the_page_the_login_and_the_timers_and_then_that_the_public_is_shut_out_again(self):
        r = self.live()
        for ok in ("answers 200 through the tunnel", "the front page is WordPress's", "the front page shows no PHP error", "the login page answers 200", "back on hold the public no longer gets the site (answer: '403')"):
            self.assertIn("ok: " + ok if not ok.startswith("answers") else ok, r.stdout, ok)
        self.assertNotIn("FAIL: https://", r.stdout)
        self.assertIn("reachable by anyone", r.stdout)
        calls = self.calls()
        order = [i for i, c in enumerate(calls) if "state=" in c]
        self.assertEqual([calls[i].split("state=")[1].split()[0] for i in order], ["live", "hold"])

    def test_a_site_that_is_still_public_after_going_back_to_hold_is_a_failure(self):
        r = self.live(CURL_HOLD="200")
        self.assertIn("FAIL: back on hold, https://life.", r.stdout); self.assertIn("still answers 200: the site is still public", r.stdout)
        self.assertNotEqual(r.returncode, 0)

    def test_a_front_page_that_is_not_wordpress_or_shows_php_errors_is_a_failure(self):
        r = self.live(CURL_BODY="Fatal error: Uncaught Error in /var/www/html/wp-content/plugins/x.php")
        self.assertIn("FAIL: the front page shows a PHP error", r.stdout)
        r = self.live(CURL_LOGIN="500")
        self.assertIn("FAIL: the login page answered '500'", r.stdout)
        r = self.live(CURL_LIVE="530")
        self.assertIn("FAIL: https://life.", r.stdout); self.assertIn("is there a DNS record and a tunnel route", r.stdout)

    def test_without_with_live_nothing_public_is_asked_for(self):
        r = self.run_script("--yes", "--no-phone", "--site", "life", "--until", "operate")
        self.assertNotIn("state=live", " ".join(self.calls())); self.assertFalse([c for c in self.calls() if c.startswith("curl")])

    def test_a_phase_that_fails_stops_the_run_and_says_how_to_clean_up(self):
        (self.repo / "scripts/wp_sites.py").write_text("#!/usr/bin/env python3\nimport sys\nopen(%r, 'a').write('wp_sites.py ' + ' '.join(sys.argv[1:]) + '\\n')\nsys.exit(2 if sys.argv[1] == 'import' else 0)\n" % str(self.log))
        r = self.run_script("--yes", "--no-phone", "--site", "life")
        verbs = [c.split()[1] for c in self.sites_calls()]
        self.assertEqual(verbs, ["create", "import"], "it went no further than the failed import")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("import exited 2", r.stdout)
        self.assertIn("python3 scripts/wp_sites.py reset life", r.stdout)
        self.assertIn("delete life --commit", r.stdout)

    def test_a_site_with_no_data_after_the_approve_phase_stops_the_run_before_it_operates_on_nothing(self):
        r = self.run_script("--yes", "--no-phone", "--site", "life", env=dict(self.env, SSH_FAIL_ON='"$(tables)" -gt 0'))      # only the vmtest snippet has this text
        verbs = [c.split()[1] for c in self.sites_calls()]
        self.assertEqual(verbs[-1], "approve", "nothing after the approve phase ran")
        self.assertNotIn("stop", verbs)
        self.assertIn("the site is empty after this phase", r.stdout)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("== 6. operate", r.stdout)

    def test_no_approve_button_on_a_clean_import_is_a_failure_and_falls_back_to_the_terminal(self):
        # answers: go on; a notification arrived; Deny's message arrived; no Approve button
        r = self.run_script("--site", "life", "--until", "approve", stdin="y\ny\ny\nn\n")
        verbs = [c.split()[1] for c in self.sites_calls()]
        self.assertEqual(verbs, ["create", "import", "import", "approve"], r.stdout)
        self.assertIn("FAIL: no Approve button on a clean import", r.stdout)
        self.assertIn("NO: the second notification had no Approve button", r.stdout)
        self.assertNotIn("Tap APPROVE now", r.stdout)
        self.assertNotEqual(r.returncode, 0)

    def test_an_approve_button_that_is_there_is_waited_for_and_never_pressed_for_you(self):
        r = self.run_script("--site", "life", "--until", "approve", stdin="y\ny\ny\ny\ny\n")
        verbs = [c.split()[1] for c in self.sites_calls()]
        self.assertEqual(verbs, ["create", "import", "import"], "with the button there, the tap does the approving")
        self.assertIn("Tap APPROVE now", r.stdout)
        self.assertIn("yes: the second notification had an Approve button", r.stdout)

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
