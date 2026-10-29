#!/usr/bin/env python3
"""Offline tests of the scans on dellwp: the ClamAV tools (templates/wp/wp_clamscan.sh.j2, wp_clamav_update.sh.j2) against a stand-in for docker, the vulnerability feed wrapper
(templates/wp/wp_vulnfeed.py.j2), the tasks that install the key and the timers (tasks/wp_scans_common.yml), and the script that puts the key in the vault. What a stand-in cannot say: that freshclam reaches
its mirrors, that the image's clamscan accepts these options, or that Wordfence accepts the key; the first real run does (the rehearsal's import report shows it).
Run: python3 -I tests/test_wp_scans.py"""
import os
import pty
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wp_backup import Base  # noqa: E402
from test_wp_sites_render import ROOT, load  # noqa: E402

DOCKER_CLAM = r'''#!/bin/bash
echo "$*" >> "$SHIM/docker.calls"
case "$1" in
  image) [ -f "$SHIM/no_image" ] && exit 1; exit 0 ;;
  pull) echo pulled >> "$SHIM/pulls"; touch "$SHIM/pulled_ok"; rm -f "$SHIM/no_image"; exit "${SHIM_PULL_RC:-0}" ;;
  run)
    case " $* " in
      *" --entrypoint freshclam "*)
        for a in "$@"; do case "$a" in *:/var/lib/clamav) d=${a%%:/var/lib/clamav}; esac; done
        echo "freshclam says hello"
        [ -f "$SHIM/freshclam_ok" ] && { touch "$d/main.cvd" "$d/daily.cvd"; }
        exit "${SHIM_FRESH_RC:-0}" ;;
      *" --entrypoint clamscan "*)
        [ -f "$SHIM/clam_slow" ] && sleep 5
        [ -f "$SHIM/clam_out" ] && cat "$SHIM/clam_out"
        exit "${SHIM_CLAM_RC:-0}" ;;
    esac ;;
esac
exit 0
'''


class ScanBase(Base):
    def setUp(self):
        super().setUp()
        self.shim("docker", DOCKER_CLAM)
        self.sigs = self.tmp / "sigs"
        self.sigs.mkdir()
        self.intake = self.tmp / "data" / "intake"
        self.intake.mkdir()
        self.ctx.update(wp_clamav_dir=str(self.sigs), wp_intake_dir=str(self.intake))
        (self.tmp / "bin").mkdir(exist_ok=True)

    def calls(self):
        return [l for l in self.log("docker.calls").splitlines()]

    def sign(self):
        (self.sigs / "main.cvd").write_text("x")
        (self.sigs / "daily.cld").write_text("x")


class ClamScan(ScanBase):
    def setUp(self):
        super().setUp()
        self.script = self.install("wp_clamscan.sh.j2", "bin/wp-clamscan")
        self.sign()
        self.work = self.intake / "work-alpha" / "wp-content"
        self.work.mkdir(parents=True)
        self.uploads = self.site / "html/wp-content/uploads"

    def scan(self, *dirs, **env):
        return self.run_script(self.script, *map(str, dirs), **env)

    def test_a_clean_scan_exits_0_and_mounts_every_directory_read_only_without_a_network(self):
        r = self.scan(self.work, self.uploads)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = next(c for c in self.calls() if c.startswith("run "))
        for needle in ("--rm", "--network none", "--read-only", "--cap-drop ALL", "--cap-add DAC_READ_SEARCH", "--security-opt no-new-privileges:true", "-v %s:/var/lib/clamav:ro" % self.sigs, "-v %s:/scan/1:ro" % self.work.resolve(), "-v %s:/scan/2:ro" % self.uploads.resolve(), "--entrypoint clamscan " + self.v["wp_image_clamav"],
                       "--recursive --infected --no-summary --database=/var/lib/clamav /scan"):
            self.assertIn(needle, run)
        m = re.search(r"--memory (\d+m) --memory-swap (\d+m)", run)
        self.assertEqual(m.group(1), m.group(2)); self.assertEqual(m.group(1), "%dm" % self.v["wp_clamav_mem_mb"])
        self.assertNotIn(":rw", run); self.assertNotIn("--privileged", run); self.assertNotIn("--network host", run)

    def test_the_image_is_the_pinned_one_and_never_latest(self):
        self.assertRegex(self.v["wp_image_clamav"], r"^clamav/clamav:\d+\.\d+\.\d+$")

    def test_something_found_exits_1_and_prints_what(self):
        self.put("clam_out", "/scan/1/plugins/evil/x.php: Php.Trojan.Agent FOUND\n")
        r = self.scan(self.work, SHIM_CLAM_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Php.Trojan.Agent FOUND", r.stdout)

    def test_any_other_exit_of_clamscan_is_an_error_not_a_pass(self):
        for rc in ("2", "50", "137"):
            r = self.scan(self.work, SHIM_CLAM_RC=rc)
            self.assertEqual(r.returncode, 2, rc); self.assertIn("clamscan exited " + rc, r.stderr)

    def test_a_scan_that_runs_too_long_is_stopped_and_is_an_error(self):
        script = self.install("wp_clamscan.sh.j2", "bin/wp-clamscan", wp_clamscan_timeout_s=1)
        self.put("clam_slow", "1")
        t = time.time()
        r = self.run_script(script, str(self.work))
        self.assertEqual(r.returncode, 2, r.stderr); self.assertIn("did not finish in 1 s", r.stderr)
        self.assertLess(time.time() - t, 4)

    def test_no_signatures_or_half_of_them_is_not_set_up_and_nothing_runs(self):
        for keep in ((), ("main.cvd",), ("daily.cvd",)):
            for p in self.sigs.iterdir():
                p.unlink()
            for k in keep:
                (self.sigs / k).write_text("x")
            self.tmp.joinpath("shim/docker.calls").unlink(missing_ok=True)
            r = self.scan(self.work)
            self.assertEqual(r.returncode, 3, keep); self.assertIn("no signatures yet", r.stdout)
            self.assertFalse([c for c in self.calls() if c.startswith("run ")], keep)

    def test_either_signature_format_counts(self):
        for p in self.sigs.iterdir():
            p.unlink()
        (self.sigs / "main.cld").write_text("x"); (self.sigs / "daily.cvd").write_text("x")
        self.assertEqual(self.scan(self.work).returncode, 0)

    def test_an_image_that_is_not_on_the_vm_is_not_set_up(self):
        self.put("no_image", "1")
        r = self.scan(self.work)
        self.assertEqual(r.returncode, 3); self.assertIn("is not on this VM", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("run ")])

    def test_only_directories_under_the_sites_and_the_intake_are_scanned(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        evil_prefix = self.tmp / "data" / "intake-evil"
        evil_prefix.mkdir()
        link = self.intake / "link"
        link.symlink_to(outside)
        dotdot = self.intake / "work-alpha" / ".." / ".." / ".." / "elsewhere"
        a_file = self.work / "f.php"
        a_file.write_text("x")
        for bad in (outside, evil_prefix, link, dotdot, a_file, self.tmp / "nope", "/", "/etc", self.intake / ".." / "elsewhere"):
            self.tmp.joinpath("shim/docker.calls").unlink(missing_ok=True)
            r = self.scan(bad)
            self.assertEqual(r.returncode, 2, str(bad))
            self.assertFalse([c for c in self.calls() if c.startswith("run ")], "nothing is run for " + str(bad))

    def test_one_bad_directory_among_good_ones_stops_everything(self):
        r = self.scan(self.work, self.tmp)
        self.assertEqual(r.returncode, 2)
        self.assertFalse([c for c in self.calls() if c.startswith("run ")])

    def test_no_argument_is_a_usage_error(self):
        r = self.scan()
        self.assertEqual(r.returncode, 2); self.assertIn("usage:", r.stderr)

    def test_a_directory_name_with_spaces_or_metacharacters_is_one_argument(self):
        odd = self.intake / "work x; touch pwned $(touch pwned2)"
        odd.mkdir()
        r = self.scan(odd)
        self.assertEqual(r.returncode, 0, r.stderr)
        for where in (self.tmp, self.intake, Path.cwd()):
            self.assertFalse((where / "pwned").exists() or (where / "pwned2").exists(), where)
        self.assertIn(":/scan/1:ro", self.log("docker.calls"))


class ClamUpdate(ScanBase):
    def setUp(self):
        super().setUp()
        self.script = self.install("wp_clamav_update.sh.j2", "bin/wp-clamav-update")
        self.put("freshclam_ok", "1")

    def update(self, **env):
        return self.run_script(self.script, **env)

    def test_it_refreshes_the_signatures_in_a_container_with_nothing_mounted_but_their_directory(self):
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr)
        run = next(c for c in self.calls() if c.startswith("run "))
        for needle in ("--rm", "--cap-drop ALL", "--cap-add SETUID --cap-add SETGID", "--cap-add CHOWN", "--security-opt no-new-privileges:true", "-v %s:/var/lib/clamav " % self.sigs, "--entrypoint freshclam " + self.v["wp_image_clamav"], "--datadir=/var/lib/clamav", "--user=root"):
            self.assertIn(needle, run)
        self.assertEqual(run.count(" -v "), 1)
        m = re.search(r"--memory (\d+m) --memory-swap (\d+m)", run)
        self.assertEqual(m.group(1), m.group(2))
        self.assertEqual(m.group(1), "%dm" % self.v["wp_clamav_mem_mb"], "loading the signatures to test them needs what a scan needs")
        self.assertIn("signatures are in", r.stdout)
        self.assertTrue((self.sigs / "main.cvd").exists())

    def test_the_image_is_pulled_only_when_it_is_not_there(self):
        self.put("no_image", "1")
        self.update()
        self.assertEqual(self.log("pulls").count("pulled"), 1)
        self.assertIn("pull --quiet " + self.v["wp_image_clamav"], self.log("docker.calls"))
        (self.tmp / "shim/pulls").unlink()
        self.update()
        self.assertEqual(self.log("pulls"), "")

    def test_a_pull_that_fails_fails_the_job_before_freshclam_runs(self):
        self.put("no_image", "1")
        r = self.update(SHIM_PULL_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("run ")])

    def test_freshclam_that_fails_fails_the_job(self):
        r = self.update(SHIM_FRESH_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("signatures are in", r.stdout)

    def test_a_freshclam_that_finishes_without_leaving_signatures_is_an_error(self):
        (self.tmp / "shim/freshclam_ok").unlink()
        r = self.update()
        self.assertEqual(r.returncode, 2); self.assertIn("signatures are not in", r.stderr)

    def test_old_signatures_alone_do_not_hide_a_failed_refresh(self):
        self.sign()
        r = self.update(SHIM_FRESH_RC="1")
        self.assertNotEqual(r.returncode, 0)

    def test_a_missing_directory_is_told_to_run_the_play(self):
        shutil.rmtree(self.sigs)
        r = self.update()
        self.assertEqual(r.returncode, 2); self.assertIn("wp_scans_dellwp.yml", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_two_updates_at_once_are_refused(self):
        lock = subprocess.Popen(["flock", str(self.sigs / ".update.lock"), "sleep", "3"])
        try:
            time.sleep(0.5)
            r = self.update()
            self.assertEqual(r.returncode, 2); self.assertIn("another update is running", r.stderr)
        finally:
            lock.kill(); lock.wait()


class Wrapper(ScanBase):
    def config(self, text):
        ns = {"sys": type("S", (), {"path": [], "exit": lambda *a: None, "argv": []})}
        exec(text.split("import wp_vulnfeed  # noqa: E402\n")[1].split("sys.exit(")[0], ns)
        return ns["CONFIG"]

    def render(self, **over):
        return self.env_j.get_template("wp_vulnfeed.py.j2").render(**dict(self.ctx, **over))

    def test_the_wrapper_is_valid_python_with_the_repos_values(self):
        text = self.render()
        compile(text, "wp-vulnfeed", "exec")
        self.assertTrue(text.startswith("#!/usr/bin/python3 -I\n"))
        cfg = self.config(text)
        v = self.v
        self.assertNotIn("sites", cfg, "the sites are asked of the registry at every run, nothing is baked in")
        self.assertEqual((cfg["key_file"], cfg["index_file"], cfg["state_dir"]), (v["wp_vulnfeed_dir"] + "/key", v["wp_vulnfeed_state_dir"] + "/index.json", v["wp_vulnfeed_state_dir"]))
        self.assertEqual((cfg["feed_url"], cfg["max_age_days"], cfg["timeout_s"]), (v["wp_vulnfeed_url"], v["wp_vulnfeed_max_age_days"], v["wp_vulnfeed_cmd_timeout_s"]))
        self.assertEqual((cfg["notify_cmd"], cfg["ntfy_topic"], cfg["host_name"]), (v["wp_ntfy_notify_cmd"], v["wp_ntfy_topic"], "dellwp"))
        self.assertTrue(cfg["feed_url"].startswith("https://www.wordfence.com/api/intelligence/v3/"))
        self.assertIsInstance(cfg["max_age_days"], int)
        self.assertIn('sys.path.insert(0, "%s")' % self.ctx["wp_tools_lib"], text)

    def test_it_runs_end_to_end_and_is_not_run_without_a_key_or_an_index(self):
        shutil.copy(ROOT / "scripts/wp_vulnfeed.py", self.tmp / "lib/wp_vulnfeed.py")
        state = self.tmp / "vstate"
        state.mkdir()
        stand_in = self.tmp / "bin/wp-site-provision"
        stand_in.write_text("#!/bin/sh\necho 'alpha hold 1001 3072 2001 3072 notify'\n")
        stand_in.chmod(0o755)
        script = self.install("wp_vulnfeed.py.j2", "bin/wp-vulnfeed", wp_tools_bin=str(self.tmp / "bin"), wp_vulnfeed_dir=str(self.tmp / "vkey"), wp_vulnfeed_state_dir=str(state))
        r = subprocess.run([str(script), "check", "alpha"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr); self.assertIn("NOT RUN", r.stdout)
        r = subprocess.run([str(script), "check", "beta"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 2, r.stdout); self.assertIn("is not a site this host knows", r.stdout)
        stand_in.write_text("#!/bin/sh\necho 'the record is broken' >&2\nexit 1\n")
        r = subprocess.run([str(script), "check", "alpha"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr); self.assertIn("cannot read the site registry", r.stdout)
        r = subprocess.run([str(script), "update"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 3); self.assertIn("no feed key", r.stdout)
        r = subprocess.run([str(script), "what"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 2); self.assertIn("usage:", r.stdout)


class Tasks(unittest.TestCase):
    def setUp(self):
        self.tasks = load("tasks/wp_scans_common.yml")
        self.by = {t["name"]: t for t in self.tasks}
        self.v = __import__("test_wp_sites_render").base_vars()

    def test_the_key_is_written_root_only_from_the_vault_and_never_logged(self):
        t = self.by["Write the feed key"]
        c = t["ansible.builtin.copy"]
        self.assertEqual((c["dest"], c["owner"], c["group"], c["mode"]), ("{{ wp_vulnfeed_dir }}/key", "root", "root", "0600"))
        self.assertEqual(c["content"], "{{ dellwp_wp_wordfence_key }}\n")
        self.assertIs(t["no_log"], True)
        d = self.by["Make the feed's key directory (root only)"]["ansible.builtin.file"]
        self.assertEqual((d["path"], d["mode"]), ("{{ wp_vulnfeed_dir }}", "0700"))

    def test_the_play_stops_early_without_the_key_or_the_tools(self):
        first = self.tasks[0]
        self.assertIn("dellwp_wp_wordfence_key is defined", first["ansible.builtin.assert"]["that"])
        self.assertIn("scripts/wp_provider_key.sh wordfence", first["ansible.builtin.assert"]["fail_msg"])
        stat_t = self.by["Look for the tools the scans need"]
        wanted = {"{{ wp_tools_bin }}/wp-clamscan", "{{ wp_tools_bin }}/wp-clamav-update", "{{ wp_tools_bin }}/wp-vulnfeed", "{{ wp_tools_lib }}/wp_vulnfeed.py"}
        self.assertTrue(wanted <= set(stat_t["loop"]))
        names = [t["name"] for t in self.tasks]
        self.assertLess(names.index("Stop if the tools are not installed"), names.index("Write the feed key"))
        self.assertLess(names.index("Stop early if the key or the tools are missing"), names.index("Pull the ClamAV image"))

    def test_the_services_alert_on_failure_and_the_feed_job_is_memory_limited_and_ordered(self):
        for n in ("Deploy the signatures refresh service", "Deploy the vulnerability feed service"):
            self.assertIn("OnFailure=wp-job-failed@%N.service", self.by[n]["ansible.builtin.copy"]["content"], n)
        feed = self.by["Deploy the vulnerability feed service"]["ansible.builtin.copy"]["content"]
        self.assertIn("MemoryMax={{ wp_vulnfeed_mem_mb }}M", feed)
        starts = [l for l in feed.splitlines() if l.startswith("ExecStart=")]
        self.assertEqual(starts, ["ExecStart={{ wp_tools_bin }}/wp-vulnfeed update", "ExecStart={{ wp_tools_bin }}/wp-vulnfeed check-all"], "the check never runs on a failed update")
        self.assertIn("Type=oneshot", feed)

    def test_the_timers_are_persistent_enabled_and_use_the_variables(self):
        for n, cal in (("Deploy the signatures refresh timer", "wp_clamav_update_calendar"), ("Deploy the vulnerability feed timer", "wp_vulnfeed_calendar")):
            c = self.by[n]["ansible.builtin.copy"]["content"]
            self.assertIn("OnCalendar={{ %s }}" % cal, c); self.assertIn("Persistent=true", c); self.assertIn("WantedBy=timers.target", c)
        for n, unit in (("Enable the signatures refresh timer", "wp-clamav-update.timer"), ("Enable the vulnerability feed timer", "wp-vulnfeed.timer")):
            s = self.by[n]["ansible.builtin.systemd"]
            self.assertEqual((s["name"], s["enabled"], s["state"]), (unit, True, "started"))
        for k in ("wp_clamav_update_calendar", "wp_vulnfeed_calendar"):
            self.assertRegex(self.v[k], r"^\*-\*-\* \d\d:\d\d:\d\d$")

    def test_the_directories_have_the_right_modes(self):
        for n, mode in (("Make the signatures directory", "0755"), ("Make the feed's state directory", "0755")):
            self.assertEqual(self.by[n]["ansible.builtin.file"]["mode"], mode, n)

    def test_the_tools_are_installed_by_the_sites_play_and_the_module_travels_with_them(self):
        tools = {t["name"]: t for t in load("tasks/wp_tools_common.yml")}
        dests = {i["dest"] for i in tools["Stage the tools"]["loop"]}
        self.assertTrue({"wp-clamscan", "wp-clamav-update", "wp-vulnfeed"} <= dests)
        self.assertIn("wp_vulnfeed.py", tools["Stage the Python modules of wp-site-import"]["loop"])
        for i in tools["Stage the tools"]["loop"]:
            if i["dest"] in ("wp-clamscan", "wp-clamav-update", "wp-vulnfeed"):
                self.assertTrue((ROOT / "templates/wp" / i["src"]).is_file(), i["src"])

    def test_the_variables_exist_and_the_image_is_pinned_with_a_renovate_anchor(self):
        for k in ("wp_clamav_dir", "wp_clamav_mem_mb", "wp_clamscan_timeout_s", "wp_vulnfeed_dir", "wp_vulnfeed_state_dir", "wp_vulnfeed_url", "wp_vulnfeed_max_age_days", "wp_vulnfeed_mem_mb", "wp_vulnfeed_timeout_s"):
            self.assertIn(k, self.v, k)
        lines = (ROOT / "vars/wp.yml").read_text().splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith("wp_image_clamav:"))
        self.assertEqual(lines[i - 1], "# renovate: depName=clamav/clamav")
        self.assertNotIn("latest", lines[i])

    def test_the_play_loads_the_pins(self):
        play = load("dellwp/wp_scans_dellwp.yml")[0]
        self.assertEqual(play["vars_files"], ["../vars/wp.yml"]); self.assertIs(play["become"], True); self.assertEqual(play["hosts"], "dellwp")


VAULT_SHIM = """#!/bin/bash
case "$1" in
  view) tail -n +2 "$2" ;;
  encrypt) out=""; src=""; shift; while [ $# -gt 0 ]; do case "$1" in --output) out="$2"; shift 2 ;; *) src="$1"; shift ;; esac; done
           if [ -f "$SHIM_BREAK" ]; then echo garbage > "$out"; else { echo '$ANSIBLE_VAULT;1.1;AES256'; cat "$src"; } > "$out"; fi ;;
  *) exit 2 ;;
esac
"""
KEY = "wfk_abcdefghijklmnopqrstuvwxyz0123456789"


class SecretScript(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.repo = Path(self.d.name)
        (self.repo / "scripts").mkdir()
        (self.repo / "host_vars/dellwp").mkdir(parents=True)
        (self.repo / "bin").mkdir()
        (self.repo / "scripts/wp_provider_key.sh").write_text((ROOT / "scripts/wp_provider_key.sh").read_text())
        (self.repo / "bin/ansible-vault").write_text(VAULT_SHIM)
        (self.repo / "bin/ansible-vault").chmod(0o755)
        self.vault = self.repo / "host_vars/dellwp/vault_wp.yml"
        self.vault.write_text('$ANSIBLE_VAULT;1.1;AES256\ndellwp_wp_master_secret: "m"\n')
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, capture_output=True)
        self.env = dict(os.environ, PATH="%s:%s" % (self.repo / "bin", os.environ["PATH"]), SHIM_BREAK=str(self.repo / "break"))

    def tearDown(self):
        self.d.cleanup()

    def run_script(self, typed=(), tty=True, env=None, preset="wordfence"):
        """Run it on a terminal; each string in typed is sent once the script has asked (a prompt ends in ': ' or '] ')."""
        cmd = ["bash", str(self.repo / "scripts/wp_provider_key.sh")] + ([preset] if preset else [])
        if not tty:
            r = subprocess.run(cmd, capture_output=True, text=True, env=env or self.env, stdin=subprocess.DEVNULL, timeout=60)
            return r.returncode, r.stdout, r.stderr
        master, slave = pty.openpty()
        p = subprocess.Popen(cmd, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env or self.env, close_fds=True)
        os.close(slave)
        out, err, sent = b"", b"", 0
        deadline = time.time() + 30
        while p.poll() is None and time.time() < deadline:
            ready, _, _ = select.select([p.stderr, p.stdout], [], [], 0.2)
            for f in ready:
                chunk = os.read(f.fileno(), 4096)
                if f is p.stderr:
                    err += chunk
                else:
                    out += chunk
            asked = err.count(b"hidden): ") + err.count(b"[y/N] ")
            while sent < len(typed) and asked > sent:
                os.write(master, (typed[sent] + "\n").encode()); sent += 1
        p.wait(timeout=10)
        for f, acc in ((p.stderr, "err"), (p.stdout, "out")):
            rest = f.read()
            if acc == "err":
                err += rest
            else:
                out += rest
        os.close(master)
        p.stdout.close(); p.stderr.close()
        return p.returncode, out.decode(), err.decode()

    def values(self):
        return yaml.safe_load("\n".join(self.vault.read_text().splitlines()[1:]))

    def test_it_adds_the_key_and_shows_only_its_length(self):
        rc, out, err = self.run_script([KEY])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.values()["dellwp_wp_wordfence_key"], KEY)
        self.assertEqual(self.values()["dellwp_wp_master_secret"], "m", "what was there is kept")
        self.assertIn("is %d characters" % len(KEY), out)
        self.assertNotIn(KEY, out + err)
        self.assertTrue(self.vault.read_text().startswith("$ANSIBLE_VAULT"))

    def test_the_resend_preset_writes_its_own_name_and_leaves_the_other_key_alone(self):
        self.run_script([KEY])
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "x"], check=True, capture_output=True)
        mail = "re_mailkey_abcdefghijklmnopqrstuvwxyz"
        rc, out, err = self.run_script([mail], preset="resend")
        self.assertEqual(rc, 0, out + err)
        v = self.values()
        self.assertEqual((v["dellwp_wp_resend_key"], v["dellwp_wp_wordfence_key"], v["dellwp_wp_master_secret"]), (mail, KEY, "m"))
        self.assertIn("dellwp_wp_resend_key is %d characters" % len(mail), out); self.assertNotIn(mail, out + err)
        self.assertIn("Resend API key (hidden)", err)

    def test_an_unknown_or_missing_provider_changes_nothing(self):
        before = self.vault.read_bytes()
        for preset in (None, "", "stripe", "Wordfence", "wordfence resend"):
            rc, out, err = self.run_script([KEY], preset=preset)
            self.assertEqual(rc, 2, repr(preset)); self.assertIn("usage: bash scripts/wp_provider_key.sh wordfence|resend", err)
            self.assertEqual(self.vault.read_bytes(), before, repr(preset))

    def test_an_existing_key_is_replaced_only_after_a_yes(self):
        self.run_script([KEY])
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "x"], check=True, capture_output=True)
        new = "second_key_abcdefghijklmnopqrstuvwxyz"
        rc, out, err = self.run_script(["n"])
        self.assertEqual(rc, 0); self.assertEqual(self.values()["dellwp_wp_wordfence_key"], KEY); self.assertIn("left alone", out)
        rc, out, err = self.run_script(["y", new])
        self.assertEqual(rc, 0, out + err); self.assertEqual(self.values()["dellwp_wp_wordfence_key"], new)
        self.assertEqual(self.vault.read_text().count("dellwp_wp_wordfence_key"), 1, "replaced, not appended")
        self.assertNotIn(new, out + err)

    def test_a_key_that_does_not_look_like_one_changes_nothing(self):
        before = self.vault.read_bytes()
        for bad in ("short", "has spaces in it which is far too long to be a key ok", 'quote"in"it_abcdefghijklmnop', "a" * 600, ""):
            rc, out, err = self.run_script([bad])
            self.assertEqual(rc, 2, repr(bad)); self.assertIn("does not look like an API key", err)
            self.assertEqual(self.vault.read_bytes(), before, repr(bad))

    def test_it_will_not_start_without_a_terminal(self):
        rc, out, err = self.run_script(tty=False)
        self.assertEqual(rc, 2); self.assertIn("needs a terminal", err); self.assertEqual(self.vault.read_text().count("wordfence"), 0)

    def test_it_refuses_a_vault_with_uncommitted_changes_or_one_that_is_not_encrypted(self):
        self.vault.write_text(self.vault.read_text() + "x: 1\n")
        rc, out, err = self.run_script([KEY])
        self.assertEqual(rc, 2); self.assertIn("uncommitted changes", err)
        subprocess.run(["git", "-C", str(self.repo), "checkout", "--", str(self.vault)], check=True, capture_output=True)
        self.vault.write_text("dellwp_wp_master_secret: plain\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "plain"], check=True, capture_output=True)
        rc, out, err = self.run_script([KEY])
        self.assertEqual(rc, 2); self.assertIn("not an encrypted vault", err)

    def test_a_failed_encryption_leaves_the_vault_untouched(self):
        (self.repo / "break").write_text("1")
        before = self.vault.read_bytes()
        rc, out, err = self.run_script([KEY])
        self.assertEqual(rc, 2); self.assertIn("encryption failed", err)
        self.assertEqual(self.vault.read_bytes(), before)

    def test_the_key_is_never_an_argument_or_echoed(self):
        text = (ROOT / "scripts/wp_provider_key.sh").read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertIn("read -r -s", code)
        self.assertNotRegex(code, r"echo[^\n]*\$key|echo[^\n]*\$\{key"); self.assertNotRegex(code, r"ansible-vault[^\n]*\$key")
        self.assertIn("unset key", code)

    def test_nothing_is_left_in_tmp_after_a_run(self):
        before = set(os.listdir(tempfile.gettempdir()))
        self.run_script([KEY])
        left = [n for n in set(os.listdir(tempfile.gettempdir())) - before if not n.startswith("tmp") or True]
        self.assertEqual([n for n in left if n.startswith("tmp") and (Path(tempfile.gettempdir()) / n).is_file()], [])


if __name__ == "__main__":
    unittest.main()
