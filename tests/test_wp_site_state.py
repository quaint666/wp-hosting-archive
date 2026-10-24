#!/usr/bin/env python3
"""Offline tests of scripts/wp_site_state.py: the line edit (comments kept, only one site's state changes, a missing state line is added), its refusals, and the
orchestration against stand-ins for ansible-playbook and the verifier in a throwaway git repository. Run: python3 -I tests/test_wp_site_state.py"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import wp_site_state as S  # noqa: E402

SAMPLE = """---
# comment at the top
wp_site_defaults:
  state: hold
  upload_mb: 64
wp_sites:
  # a comment between the key and the first entry
  - name: alpha
    port: 9101
    quota_id: 1001
    state: live   # open to the public

  - name: beta
    port: 9102
    quota_id: 1002
  - name: gamma
    port: 9103
    state: hold
    quota_id: 1003

# after the list
wp_tools_bin: /usr/local/sbin
"""


class Edit(unittest.TestCase):
    def test_changes_only_that_sites_state_and_keeps_every_comment(self):
        new = S.set_state(SAMPLE, "alpha", "hold")
        self.assertIn("    state: hold   # open to the public", new)
        self.assertEqual(new.replace("state: hold   # open", "state: live   # open"), SAMPLE)

    def test_adds_a_state_line_when_the_site_has_none(self):
        new = S.set_state(SAMPLE, "beta", "live")
        d = yaml.safe_load(new)
        self.assertEqual([s.get("state") for s in d["wp_sites"]], ["live", "live", "hold"])
        self.assertIn("    quota_id: 1002\n    state: live\n  - name: gamma", new)
        self.assertEqual(d["wp_site_defaults"]["state"], "hold", "the defaults block must not change")

    def test_the_defaults_blocks_state_is_never_touched(self):
        for site in ("alpha", "beta", "gamma"):
            for state in ("hold", "live"):
                self.assertEqual(yaml.safe_load(S.set_state(SAMPLE, site, state))["wp_site_defaults"], {"state": "hold", "upload_mb": 64})

    def test_round_trip_is_byte_identical(self):
        self.assertEqual(S.set_state(S.set_state(SAMPLE, "gamma", "live"), "gamma", "hold"), SAMPLE)

    def test_refuses_unknown_sites_and_states(self):
        for site, state in (("nosuch", "live"), ("alph", "live"), ("alpha", "paused"), ("", "live")):
            with self.subTest((site, state)):
                with self.assertRaises(ValueError):
                    S.set_state(SAMPLE, site, state)
        with self.assertRaises(ValueError):
            S.set_state("wp_sites: []\n", "alpha", "live")

    def test_a_site_whose_name_is_a_prefix_of_another_is_not_confused(self):
        text = SAMPLE.replace("name: beta", "name: alpha2")
        d = yaml.safe_load(S.set_state(text, "alpha2", "live"))
        self.assertEqual([s.get("state") for s in d["wp_sites"]], ["live", "live", "hold"])

    def test_the_real_file_round_trips(self):
        text = (ROOT / S.VARS).read_text()
        for site in [s["name"] for s in yaml.safe_load(text)["wp_sites"]]:
            now = S.current_state(text, site)
            other = "hold" if now == "live" else "live"
            flipped = S.set_state(text, site, other)
            self.assertEqual(S.current_state(flipped, site), other)
            if "state:" in text.split("wp_sites:")[1]:
                self.assertEqual(S.set_state(flipped, site, now), text, "flip and flip back must leave the file as it was")


class Orchestration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wpstate-"))
        self.repo = self.tmp / "repo"
        (self.repo / "host_vars/dellwp").mkdir(parents=True)
        (self.repo / S.VARS).write_text(SAMPLE)
        for c in (["init", "-q"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(self.repo), *c], check=True, capture_output=True)
        shim = self.tmp / "shim"
        shim.mkdir()
        self.calls = self.tmp / "calls"
        (shim / "ansible-playbook").write_text('#!/bin/bash\necho "ansible-playbook $*" >> "%s"\nexit ${SHIM_RC:-0}\n' % self.calls)
        (shim / "bash").write_text('#!/bin/bash\n[ "$1" = scripts/wp_site_verify_dellwp.sh ] && { echo "verify $*" >> "%s"; exit 0; }\nexec /usr/bin/bash "$@"\n' % self.calls)
        for f in shim.iterdir():
            f.chmod(0o755)
        self.env = dict(os.environ, PATH="%s:%s" % (shim, os.environ["PATH"]))

    def run_main(self, *args, stdin=""):
        return subprocess.run([sys.executable, "-I", str(ROOT / "scripts/wp_site_state.py"), *args], input=stdin, capture_output=True, text=True, env=self.env,
                              cwd=str(self.repo), timeout=30)

    def run_in_repo(self, *args, stdin=""):
        code = "import sys; sys.path.insert(0, %r); import wp_site_state as S; sys.exit(S.main(%r, repo=%r))" % (str(ROOT / "scripts"), list(args), str(self.repo))
        return subprocess.run([sys.executable, "-I", "-c", code], input=stdin, capture_output=True, text=True, env=self.env, timeout=30)

    def called(self):
        return self.calls.read_text() if self.calls.exists() else ""

    def test_yes_edits_runs_the_play_then_the_verifier_and_prints_the_commit(self):
        r = self.run_in_repo("beta", "live", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.called().splitlines()
        self.assertEqual(c[0], "ansible-playbook -i inventory.yml dellwp/wp_sites_dellwp.yml")
        self.assertEqual(c[1], "verify scripts/wp_site_verify_dellwp.sh beta")
        self.assertIn("git add host_vars/dellwp/vars.yml && git commit", r.stdout)
        self.assertIn("+    state: live", r.stdout)
        self.assertEqual(S.current_state((self.repo / S.VARS).read_text(), "beta"), "live")

    def test_no_run_edits_only(self):
        r = self.run_in_repo("alpha", "hold", "--no-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.called(), "")
        self.assertEqual(S.current_state((self.repo / S.VARS).read_text(), "alpha"), "hold")

    def test_answering_no_runs_nothing(self):
        r = self.run_in_repo("alpha", "hold", stdin="n\n")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.called(), "")

    def test_a_failed_play_stops_before_the_verifier_and_says_so(self):
        self.env["SHIM_RC"] = "2"
        r = self.run_in_repo("alpha", "hold", "--yes")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("verify", self.called())
        self.assertIn("live state may not match", r.stderr)

    def test_dirty_file_unknown_site_and_no_change_are_refused_or_noops(self):
        (self.repo / S.VARS).write_text(SAMPLE + "# a stray edit\n")
        r = self.run_in_repo("alpha", "hold", "--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("uncommitted", r.stderr)
        self.assertEqual(self.called(), "")
        subprocess.run(["git", "-C", str(self.repo), "checkout", "--", S.VARS], check=True)
        self.assertEqual(self.run_in_repo("nosuch", "live", "--yes").returncode, 2)
        r = self.run_in_repo("alpha", "live", "--yes")
        self.assertEqual(r.returncode, 0)
        self.assertIn("already live", r.stdout)
        self.assertEqual(self.called(), "")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
