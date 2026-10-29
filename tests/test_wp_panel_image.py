#!/usr/bin/env python3
"""Offline tests of the control app's image recipe (app/Dockerfile, app/.dockerignore, app/smoke_in_container.sh) and of scripts/wp_panel_image_rehearsal.sh.

There is no Docker on the machine these run on, so the recipe is checked as text: one pinned base image with Renovate's anchor above it, no compiler and wheels only, a non-root user that matches the
smoke test and the rehearsal, no secret baked in, the command the gunicorn settings and the entry point say. The rehearsal script is run against a stand-in for ssh that records every call and keeps the
tar stream and the smoke script it is sent: they prove the order (preflight, build, smoke, removal), that the container is run read-only without capabilities, network or volumes, that the image is removed
when the smoke test fails, and that nothing runs Docker on the ThinkPad. They do NOT prove that the recipe builds or that the container starts: scripts/wp_panel_image_rehearsal.sh on a host with Docker
does. Run: python3 -I tests/test_wp_panel_image.py"""
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP = ROOT / "app"
DOCKERFILE = (APP / "Dockerfile").read_text()
SCRIPT = ROOT / "scripts/wp_panel_image_rehearsal.sh"


def instructions():
    """(INSTRUCTION, rest) for every Dockerfile instruction, continuation lines joined; comments are dropped."""
    out, cur = [], ""
    for line in DOCKERFILE.splitlines():
        if line.lstrip().startswith("#") and not cur:
            continue
        cur += line.rstrip("\\ ").rstrip() + " " if line.rstrip().endswith("\\") else line + "\n"
        if not line.rstrip().endswith("\\"):
            word, _, rest = cur.strip().partition(" ")
            out.append((word.upper(), " ".join(rest.split())))
            cur = ""
    return out


class Recipe(unittest.TestCase):
    def test_one_base_image_pinned_to_an_exact_tag_with_renovates_anchor_directly_above(self):
        froms = [i for i, (w, _) in enumerate(instructions()) if w == "FROM"]
        self.assertEqual(len(froms), 1)
        lines = DOCKERFILE.splitlines()
        n = next(i for i, l in enumerate(lines) if l.startswith("FROM "))
        self.assertRegex(lines[n], r"^FROM python:3\.\d+\.\d+-slim-[a-z]+$", "an exact patch version, never :latest or a moving minor")
        self.assertEqual(lines[n - 1], "# renovate: depName=python")
        self.assertNotIn("latest", [r for w, r in instructions() if w == "FROM"][0])

    def test_the_user_is_not_root_and_is_the_one_the_smoke_test_and_the_rehearsal_expect(self):
        steps = instructions()
        users = [r for w, r in steps if w == "USER"]
        self.assertEqual(users, ["10001:10001"])
        self.assertGreater([w for w, _ in steps].index("USER"), [w for w, _ in steps].index("RUN"), "the user is set after the installs, which need root")
        run = " ".join(r for w, r in steps if w == "RUN")
        self.assertIn("useradd --system --uid 10001", run)
        self.assertIn("--shell /usr/sbin/nologin", run)
        self.assertIn("10001", (APP / "smoke_in_container.sh").read_text())
        self.assertIn("uid=10001,gid=10001", SCRIPT.read_text())
        self.assertIn("10001:10001", SCRIPT.read_text())

    def test_nothing_needs_a_compiler_and_the_packages_come_from_the_pinned_list(self):
        run = " ".join(r for w, r in instructions() if w == "RUN")
        self.assertIn("pip install --only-binary=:all: -r requirements.txt", run)
        self.assertNotRegex(run, r"build-essential|gcc|g\+\+|python3-dev|pip install (?!--only-binary)")
        self.assertIn("COPY requirements.txt .", DOCKERFILE)
        self.assertLess(DOCKERFILE.index("COPY requirements.txt"), DOCKERFILE.index("COPY wp_panel"), "the packages are a layer of their own: a code change does not reinstall them")
        reqs = [l for l in (APP / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
        self.assertTrue(reqs and all(re.fullmatch(r"[A-Za-z0-9._-]+==[0-9][0-9A-Za-z.]*", l) for l in reqs), reqs)

    def test_apt_installs_only_ssh_without_recommends_and_leaves_no_lists(self):
        run = " ".join(r for w, r in instructions() if w == "RUN")
        self.assertIn("apt-get install -y --no-install-recommends openssh-client", run)
        self.assertIn("rm -rf /var/lib/apt/lists/*", run)
        self.assertEqual(len(re.findall(r"apt-get install", run)), 1)

    def test_every_file_copied_exists_and_the_command_is_the_one_the_server_settings_describe(self):
        for w, r in instructions():
            if w == "COPY":
                src = r.split()[0]
                self.assertTrue((APP / src).exists(), src)
        cmd = [r for w, r in instructions() if w == "CMD"][0]
        self.assertEqual(cmd, '["gunicorn", "-c", "/app/gunicorn.conf.py", "wp_panel.wsgi:app"]')
        self.assertTrue((APP / "wp_panel/wsgi.py").is_file() and (APP / "gunicorn.conf.py").is_file())
        self.assertIn("app = create_app(", (APP / "wp_panel/wsgi.py").read_text())

    def test_the_health_check_asks_the_login_page_on_the_port_the_server_listens_on(self):
        hc = [r for w, r in instructions() if w == "HEALTHCHECK"]
        self.assertEqual(len(hc), 1)
        self.assertIn("http://127.0.0.1:8000/login", hc[0])
        self.assertIn('"0.0.0.0:8000"', (APP / "gunicorn.conf.py").read_text())
        self.assertIn("EXPOSE 8000", DOCKERFILE)

    def test_no_secret_no_download_to_a_shell_and_no_root_default(self):
        for bad in ("curl ", "wget ", "ADD http", "| sh", "| bash", "PASSWORD", "SECRET", "TOKEN", "PRIVATE KEY", "ssh-keygen", "chmod 777", "--privileged"):
            self.assertNotIn(bad, DOCKERFILE, bad)
        env = " ".join(r for w, r in instructions() if w == "ENV")
        self.assertNotRegex(env, r"(?i)pass|secret|key|token")
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", env)

    def test_the_ignore_file_keeps_caches_and_build_files_out_of_the_image(self):
        ignore = (APP / ".dockerignore").read_text().split()
        for needed in ("**/__pycache__", "**/*.pyc", "Dockerfile", ".dockerignore", "smoke_in_container.sh"):
            self.assertIn(needed, ignore)


class Smoke(unittest.TestCase):
    def test_the_script_parses_and_checks_what_the_real_container_must_have(self):
        path = APP / "smoke_in_container.sh"
        self.assertEqual(subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True).returncode, 0)
        text = path.read_text()
        for needle in ("trap diagnose EXIT", "memory.events", "Booting worker", "2 * threads + 1", "memory.peak", "id -u", "ssh -V", "import flask, pyotp, gunicorn, werkzeug", "/app/should-not-be-writable", "/run/panel", "PANEL_BIND=127.0.0.1:8000", "wp_panel.wsgi:app", "smoke ok", "default-src 'none'"):
            self.assertIn(needle, text)

    def test_it_uses_no_mounted_file_and_no_network_beyond_the_loopback(self):
        text = (APP / "smoke_in_container.sh").read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for bad in ("curl", "wget", "apt", "pip install", "http://", "https://", "/mnt", "/host"):
            self.assertNotIn(bad, code, bad)
        self.assertEqual(set(re.findall(r"127\.0\.0\.1", code)), {"127.0.0.1"})


class Harness(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.dir = Path(self.d.name)
        self.repo = self.dir / "repo"
        shutil.copytree(APP, self.repo / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.repo / "scripts").mkdir()
        shutil.copy(SCRIPT, self.repo / "scripts/")
        (self.repo / "app/wp_panel/__pycache__").mkdir()
        (self.repo / "app/wp_panel/__pycache__/x.pyc").write_text("x")
        self.log = self.dir / "calls.log"
        (self.dir / "bin").mkdir()
        ssh = self.dir / "bin/ssh"
        ssh.write_text(r'''#!/bin/bash
echo "ssh $*" >> "%(log)s"
case "$*" in
  *"docker build"*) cat > "%(dir)s/context.tgz"; echo "sha256:abc"; exit ${SSH_BUILD_RC:-0} ;;
  *"docker run"*) cat > "%(dir)s/smoke.sh"; if [ -n "${SSH_RUN_SLEEP:-}" ]; then touch "%(dir)s/started"; sleep "$SSH_RUN_SLEEP"; fi; echo "smoke ok"; exit ${SSH_RUN_RC:-0} ;;
  *"docker version"*) echo 29.1.0; exit ${SSH_DOCKER_RC:-0} ;;
  *"docker image ls"*) [ -z "${SSH_EXISTING:-}" ] || echo "${SSH_EXISTING}"; exit 0 ;;
  *"Config.User"*) echo "${SSH_USER:-10001:10001}"; exit 0 ;;
  *"Healthcheck"*) echo "${SSH_HEALTH:-yes}"; exit 0 ;;
  *".Size"*) echo 150000000; exit 0 ;;
  *"docker rmi"*) exit ${SSH_RMI_RC:-0} ;;
  *"sudo -n true"*) exit ${SSH_SUDO_RC:-0} ;;
esac
exit ${SSH_RC:-0}
''' % {"log": self.log, "dir": self.dir})
        docker = self.dir / "bin/docker"
        docker.write_text('#!/bin/bash\necho "LOCAL docker $*" >> "%s"\nexit 0\n' % self.log)
        for p in (ssh, docker):
            p.chmod(0o755)
        self.env = dict(os.environ, PATH="%s:%s" % (self.dir / "bin", os.environ["PATH"]))

    def run_script(self, *args, env=None, stdin=""):
        return subprocess.run(["bash", str(self.repo / "scripts/wp_panel_image_rehearsal.sh"), *args], capture_output=True, text=True, env=env or self.env, input=stdin, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def go(self, **env):
        return self.run_script("--host", "builder", "--yes", env=dict(self.env, **env))


class Arguments(Harness):
    def test_a_host_is_required_and_must_be_a_plain_name(self):
        for args in ((), ("--host",), ("--host", ""), ("--host", "a b"), ("--host", "-oProxyCommand=x"), ("--host", "a;id"), ("--host", "$(id)"), ("--bogus",)):
            r = self.run_script(*args, "--yes")
            self.assertEqual(r.returncode, 2, args)
        self.assertEqual(self.calls(), [])

    def test_not_confirming_does_nothing(self):
        r = self.run_script("--host", "builder", stdin="n\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was done.", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_the_help_prints_the_header(self):
        r = self.run_script("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--host NAME", r.stdout)


class Flow(Harness):
    def test_the_steps_come_in_this_order_and_the_image_is_removed_at_the_end(self):
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])
        self.assertIn("All checks passed.", r.stdout)
        kinds = []
        for c in self.calls():
            for key in ("docker version", "docker image ls", "docker build", "Config.User", "Healthcheck", ".Size", "docker run", "docker rmi", "sudo -n true"):
                if key in c:
                    kinds.append(key)
                    break
            else:
                kinds.append("ssh true")
        self.assertEqual(kinds, ["ssh true", "sudo -n true", "docker version", "docker image ls", "docker build", "Config.User", "Healthcheck", ".Size", "docker run", "docker rmi"])

    def test_the_build_context_is_the_app_directory_without_caches_and_the_smoke_script_is_the_repos(self):
        self.go()
        with tarfile.open(self.dir / "context.tgz") as t:
            names = {n[2:] if n.startswith("./") else n for n in t.getnames()}
        for need in ("Dockerfile", "requirements.txt", "gunicorn.conf.py", ".dockerignore", "wp_panel/app.py", "wp_panel/templates/login.html", "wp_panel/static/panel.css"):
            self.assertIn(need, names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])
        self.assertEqual((self.dir / "smoke.sh").read_bytes(), (APP / "smoke_in_container.sh").read_bytes())

    def test_the_container_runs_the_way_the_real_one_will_with_no_volume_port_network_or_privilege(self):
        self.go()
        run = [c for c in self.calls() if "docker run" in c][0]
        for need in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--cpuset-cpus 0", "--memory 384m --memory-swap 384m", "--network none", "--tmpfs /run/panel:uid=10001,gid=10001,mode=700", "--tmpfs /tmp", "--tmpfs /dev/shm", "--rm"):
            self.assertIn(need, run)
        for bad in (" -v ", "--volume", "--mount", " -p ", "--publish", "--privileged", "--network host", "--pid", "--device", "docker.sock"):
            self.assertNotIn(bad, run)

    def test_nothing_runs_docker_on_the_thinkpad_and_nothing_is_left_in_the_repo(self):
        before = sorted(p.name for p in (self.repo).rglob("*") if p.is_file() and "__pycache__" not in str(p))
        self.go()
        self.assertFalse([c for c in self.calls() if c.startswith("LOCAL")])
        self.assertEqual(sorted(p.name for p in (self.repo).rglob("*") if p.is_file() and "__pycache__" not in str(p)), before)

    def test_a_failing_smoke_test_fails_the_run_and_still_removes_the_image(self):
        r = self.go(SSH_RUN_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("check(s) failed", r.stdout)
        self.assertEqual(len([c for c in self.calls() if "docker rmi" in c]), 1, "removed once, not twice")

    def test_an_interrupt_in_the_middle_of_the_smoke_test_still_removes_the_image(self):
        import signal
        import time
        p = subprocess.Popen(["bash", str(self.repo / "scripts/wp_panel_image_rehearsal.sh"), "--host", "builder", "--yes"], env=dict(self.env, SSH_RUN_SLEEP="30"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)
        try:
            for _ in range(100):
                if (self.dir / "started").exists():
                    break
                time.sleep(0.1)
            self.assertTrue((self.dir / "started").exists(), "the smoke test never started")
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=15)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
            p.stdout.close()
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(len([c for c in self.calls() if "docker rmi" in c]), 1)

    def test_a_failing_build_stops_the_run_before_the_smoke_test(self):
        r = self.go(SSH_BUILD_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the build failed", r.stdout)
        self.assertFalse([c for c in self.calls() if "docker run" in c])

    def test_an_image_that_is_there_already_is_not_overwritten(self):
        r = self.go(SSH_EXISTING="abc123")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in self.calls() if "docker build" in c or "docker rmi" in c])

    def test_a_root_user_or_a_missing_health_check_is_a_failure(self):
        for env in ({"SSH_USER": "root"}, {"SSH_USER": "0:0"}, {"SSH_USER": "10001"}, {"SSH_HEALTH": "no"}):
            self.setUp()
            r = self.go(**env)
            self.assertNotEqual(r.returncode, 0, env)

    def test_no_ssh_no_sudo_or_no_docker_are_their_own_stops(self):
        for env, needle in (({"SSH_RC": "255"}, "cannot ssh"), ({"SSH_SUDO_RC": "1"}, "sudo -n does not work"), ({"SSH_DOCKER_RC": "1"}, "Docker does not answer")):
            self.setUp()
            r = self.go(**env)
            self.assertIn(needle, r.stdout, env)
            self.assertFalse([c for c in self.calls() if "docker build" in c], env)

    def test_the_script_never_commits_edits_the_repo_or_reads_a_secret(self):
        code = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))
        for word in ("git commit", "git add", "sed -i", "ansible", "vault", "master_secret", "ssh-keygen", "cat ~", "/.ssh"):
            self.assertNotIn(word, code, word)


if __name__ == "__main__":
    unittest.main()
