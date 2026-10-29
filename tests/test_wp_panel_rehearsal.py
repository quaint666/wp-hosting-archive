#!/usr/bin/env python3
"""Offline tests of the control app's end-to-end rehearsal: scripts/wp_panel_rehearsal_driver.py (the HTTPS half) and scripts/wp_panel_rehearsal_aopenpanel.sh (the whole).

The driver is run for real against the real app (app/wp_panel) served over TLS by Werkzeug on a local port, with a certificate made for the test, a stand-in for nginx's banner and HSTS header and a handshake
that refuses every name but the app's, the way nginx's default server does. They prove: a good server passes every check and a full login walks Sites, a site, Jobs, a job and Audit and logs out; each thing the
driver is there to catch is caught (a certificate about to expire or not trusted, another name accepted, a missing header, a banner with a version, a wrong password, a reused code); the password, the code and the
cookie are never printed; and the shell script runs its steps in order against a stand-in for ssh, counts the VM's failures, reads the password and the code hidden and passes them on standard input only. They do
NOT prove nginx, Docker, systemd, the VM's firewall or dellwp: the script on the real VM does. Needs Flask, pyotp and werkzeug (app/requirements.txt) and openssl. Run: python3 -I tests/test_wp_panel_rehearsal.py"""
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "app"))

import pyotp  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from test_wp_panel import FakeCtl, JOB  # noqa: E402
from wp_panel import config  # noqa: E402
from wp_panel.app import create_app  # noqa: E402

DRIVER = ROOT / "scripts/wp_panel_rehearsal_driver.py"
SCRIPT = ROOT / "scripts/wp_panel_rehearsal_aopenpanel.sh"
NAME = "wp-panel.lab.example.com"
PASSWORD = "a long passphrase for the rehearsal test"
TOTP = pyotp.random_base32()


def make_cert(d, days=30, name=NAME):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "key.pem"), "-out", str(d / "cert.pem"), "-days", str(days), "-subj", "/CN=" + name,
                    "-addext", "subjectAltName=DNS:" + name], check=True, capture_output=True)
    return d / "cert.pem", d / "key.pem"


class Server:
    """The real app over TLS on a local port, with the things nginx adds."""

    def __init__(self, d, days=30, strict_sni=True, hsts=True, banner="nginx", name=NAME, rewrite=None, no_logout_csrf=False):
        self.dir = d
        self.cert, self.key = make_cert(d, days, name)
        settings = config.Settings("dellwp", "wpctl", "/k", "/kh", str(d / "state"), 3 * 3600, b"k" * 40, generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000"), TOTP)
        self.ctl = FakeCtl()
        self.app = create_app(settings, self.ctl)
        if no_logout_csrf:                     # a bad app: logging out needs no token
            from flask import redirect, session, url_for
            self.app.view_functions["logout"] = lambda: (session.clear(), redirect(url_for("login"), 303))[1]
        inner = self.app.wsgi_app

        def nginx_like(environ, start):
            def sr(status, headers, exc=None):
                headers = [(k, v) for k, v in headers if k.lower() != "server"]
                if hsts:
                    headers.append(("Strict-Transport-Security", "max-age=31536000"))
                headers.append(("Server", banner))
                return start(status, headers, exc)
            if rewrite is None:
                return inner(environ, sr)
            captured = {}
            body = b"".join(inner(environ, lambda status, headers, exc=None: captured.update(status=status, headers=headers)))
            body = body.replace(rewrite[0].encode(), rewrite[1].encode())
            captured["headers"] = [(k, str(len(body)) if k.lower() == "content-length" else v) for k, v in captured["headers"]]
            sr(captured["status"], captured["headers"])
            return [body]
        self.app.wsgi_app = nginx_like
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(self.cert), str(self.key))
        if strict_sni:
            ctx.sni_callback = lambda sock, server_name, c: None if server_name == name else ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        self.server = make_server("127.0.0.1", 0, self.app, ssl_context=ctx, threaded=True)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(5)


def run_driver(server, *args, stdin="", cacert=True, name=NAME):
    cmd = [sys.executable, "-I", str(DRIVER), "--host", name, "--ip", "127.0.0.1", "--port", str(server.port)] + (["--cacert", str(server.cert)] if cacert else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=120)


def code():
    return pyotp.TOTP(TOTP).now()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.dir = Path(self.d.name)

    def server(self, **kw):
        s = Server(self.dir, **kw)
        self.addCleanup(s.close)
        return s


class Driver(Base):
    def test_a_good_server_passes_every_check_without_a_login(self):
        r = run_driver(self.server())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for needle in ("TLS handshake for", "the protocol is TLS 1.2 or newer", "days left", "a handshake for another name is refused", "GET /login answers 200", "content security policy",
                       "Strict-Transport-Security is set", "the server banner names no version", "/sites without a login is sent to /login", "the stylesheet is served", "without its token is refused"):
            self.assertIn(needle, r.stdout)
        self.assertNotIn("FAIL", r.stdout)

    def test_a_full_login_walks_the_pages_and_logs_out_and_prints_no_secret(self):
        s = self.server()
        c = code()
        r = run_driver(s, "--login", stdin="%s\n%s\n" % (PASSWORD, c))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for needle in ("a wrong login is refused with the one plain message", "the right password and code log in", "Sites opens", "a site's page opens", "Jobs opens", "a job's page opens with its log",
                       "Audit opens", "logging out needs the token", "logging out works", "after logging out /sites is sent to /login", "site(s) listed"):
            self.assertIn(needle, r.stdout)
        for secret in (PASSWORD, c, "panel_session"):
            self.assertNotIn(secret, r.stdout + r.stderr)
        self.assertIn(("show", {"site": "alpha"}), s.ctl.calls)
        self.assertIn(("job-log", {"job": JOB, "offset": 0}), s.ctl.calls)
        self.assertTrue({v for v, _ in s.ctl.calls} <= {"list", "show", "jobs", "job-status", "job-log", "audit-tail"}, "the rehearsal asked for read verbs only")

    def test_login_only_skips_the_checks_it_has_already_made(self):
        r = run_driver(self.server(), "--login-only", stdin="%s\n%s\n" % (PASSWORD, code()))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("the right password and code log in", r.stdout)
        self.assertNotIn("GET /login answers 200", r.stdout)

    def test_a_wrong_password_and_a_used_code_are_failures_with_a_hint_and_no_secret(self):
        s = self.server()
        c = code()
        r = run_driver(s, "--login-only", stdin="wrong password\n%s\n" % c)
        self.assertGreater(r.returncode, 0)
        self.assertIn("FAIL: the right password and code log in", r.stdout)
        self.assertIn("password or code not accepted", r.stdout)
        self.assertNotIn("wrong password", r.stdout)
        ok_run = run_driver(s, "--login-only", stdin="%s\n%s\n" % (PASSWORD, c))
        self.assertEqual(ok_run.returncode, 0, "the wrong try did not use the code up (it was never accepted)")
        again = run_driver(s, "--login-only", stdin="%s\n%s\n" % (PASSWORD, c))
        self.assertGreater(again.returncode, 0, "a code works once")

    def test_five_wrong_logins_lock_it_and_the_driver_says_so(self):
        s = self.server()
        for _ in range(3):
            run_driver(s, "--login-only", stdin="wrong\n000000\n")          # two failures each (the deliberate one and the real try): six in all
        r = run_driver(s, "--login-only", stdin="%s\n%s\n" % (PASSWORD, code()))
        self.assertIn("locked for a while", r.stdout)
        self.assertGreater(r.returncode, 0)

    def test_a_server_that_says_which_half_of_a_login_was_wrong_is_a_failure(self):
        r = run_driver(self.server(rewrite=("That login was not accepted.", "Wrong password.")), "--login-only", stdin="%s\n%s\n" % (PASSWORD, code()))
        self.assertIn("FAIL: a wrong login is refused with the one plain message", r.stdout)

    def test_a_logout_that_needs_no_token_is_a_failure(self):
        r = run_driver(self.server(no_logout_csrf=True), "--login-only", stdin="%s\n%s\n" % (PASSWORD, code()))
        self.assertIn("FAIL: logging out needs the token", r.stdout)

    def test_a_certificate_about_to_expire_is_a_failure(self):
        r = run_driver(self.server(days=5))
        self.assertGreater(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAIL: the certificate has \d+ days left")

    def test_a_certificate_the_machine_does_not_trust_is_a_failure_and_stops_the_run(self):
        r = run_driver(self.server(), cacert=False)
        self.assertGreater(r.returncode, 0)
        self.assertIn("FAIL: the TLS handshake", r.stdout)
        self.assertNotIn("GET /login", r.stdout)

    def test_a_certificate_for_another_name_is_a_failure(self):
        r = run_driver(self.server(name="somewhere-else.example"), name="somewhere-else.example")
        self.assertEqual(r.returncode, 0, "the same driver against the right name passes")
        s = self.server(name="somewhere-else.example")
        r = run_driver(s, name=NAME)
        self.assertIn("FAIL: the TLS handshake", r.stdout)

    def test_a_server_that_answers_any_name_is_a_failure(self):
        r = run_driver(self.server(strict_sni=False))
        self.assertGreater(r.returncode, 0)
        self.assertIn("FAIL: a handshake for another name was accepted", r.stdout)

    def test_a_missing_hsts_header_and_a_banner_with_a_version_are_failures(self):
        r = run_driver(self.server(hsts=False))
        self.assertIn("FAIL: Strict-Transport-Security is set", r.stdout)
        self.setUp()
        r = run_driver(self.server(banner="nginx/1.26.3"))
        self.assertIn("FAIL: the server banner names no version", r.stdout)

    def test_nothing_is_ever_written_or_printed_that_the_driver_was_not_asked_for(self):
        text = DRIVER.read_text()
        code_only = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for bad in ("subprocess", "os.system", "open(", "os.environ", "getenv", "argv[1:]) and", "print(password", "print(code", "print(s.cookie"):
            self.assertNotIn(bad, code_only, bad)
        imports = {l.split()[1].split(".")[0] for l in text.splitlines() if l.startswith(("import ", "from "))}
        self.assertLessEqual(imports, {"argparse", "http", "re", "socket", "ssl", "sys", "time", "urllib"})


class Script(Base):
    def setUp(self):
        super().setUp()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.log = self.dir / "calls.log"
        (self.bin / "ssh").write_text('''#!/bin/bash
echo "ssh $*" >> "%(log)s"
case "$*" in
  *"bash -s"*) cat > "%(dir)s/snippet.sh"; echo "ok: stand-in for the VM"; exit ${SSH_BASH_RC:-0} ;;
  *"sudo -n true"*) exit ${SSH_SUDO_RC:-0} ;;
esac
exit ${SSH_RC:-0}
''' % {"log": self.log, "dir": self.dir})
        (self.bin / "getent").write_text('#!/bin/bash\necho "${GETENT_ANSWER:-127.0.0.1} $2"\n')
        for p in self.bin.iterdir():
            p.chmod(0o755)
        self.env = dict(os.environ, PATH="%s:%s" % (self.bin, os.environ["PATH"]))

    def free_ports(self, n=2):
        socks, ports = [], []
        for _ in range(n):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
            socks.append(s)
        for s in socks:
            s.close()
        return ports

    def run_script(self, server, *args, env=None, stdin=""):
        a, b = self.free_ports()
        cmd = ["bash", str(SCRIPT), "--yes", "--ip", "127.0.0.1", "--port", str(server.port), "--cacert", str(server.cert), "--closed-ports", "%d %d" % (a, b)] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, env=env or self.env, input=stdin, timeout=180)

    def test_the_script_parses_and_prints_its_usage(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True).returncode, 0)
        r = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--no-login", r.stdout)
        self.assertIn("Paste the whole output", r.stdout)
        self.assertNotIn("set -u", r.stdout)

    def test_bad_options_are_refused_before_anything_runs(self):
        for args in (("--bogus",), ("--port", "x"), ("--ip", "not-an-ip"), ("--ip", "1.2.3.4;id"), ("--cacert", "/nonexistent"), ("--closed-ports", "80;id")):
            r = subprocess.run(["bash", str(SCRIPT), "--yes", *args], capture_output=True, text=True, env=self.env)
            self.assertEqual(r.returncode, 2, args)
        self.assertFalse(self.log.exists())

    def test_not_confirming_does_nothing(self):
        r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=self.env, input="n\n")
        self.assertEqual((r.returncode, "Nothing was done." in r.stdout), (1, True))
        self.assertFalse(self.log.exists())

    def test_the_snippet_sent_to_the_vm_parses_and_uses_only_variables_the_ssh_line_passes(self):
        text = SCRIPT.read_text()
        lib = re.search(r"<<'LIB'\n(.*?)\nLIB\n", text, re.S).group(1)
        env_line = re.search(r'sudo -n env ([^"]*) bash -s', text).group(1)
        passed = set(re.findall(r"(\w+)=", env_line))
        for i, m in enumerate(re.finditer(r"vmrun <<'EOF'\n(.*?)\nEOF\n", text, re.S)):
            body = m.group(1)
            r = subprocess.run(["bash", "-n"], input=lib + "\n" + body + "\n", capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            used = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", body))
            local = {"FAIL", "PATH", "PYCODE", "C", "PANEL_CTL_HOST", "PANEL_CTL_USER"}
            self.assertEqual(sorted(used - passed - local), [], "snippet %d" % i)
            self.assertTrue(body.rstrip().endswith("finish"))

    def test_the_whole_run_passes_against_a_good_server_and_runs_the_steps_in_order(self):
        s = self.server()
        r = self.run_script(s, "--no-login")
        self.assertEqual(r.returncode, 0, r.stdout[-2500:])
        self.assertIn("All checks passed.", r.stdout)
        self.assertLess(r.stdout.index("== 0. preflight"), r.stdout.index("== 1. the VM"))
        self.assertLess(r.stdout.index("== 1. the VM"), r.stdout.index("== 2. the name, from here"))
        self.assertIn("(3. the login: skipped with --no-login)", r.stdout)
        calls = self.log.read_text().splitlines()
        self.assertEqual(len(calls), 3, calls)
        self.assertIn("admin@10.8.0.68", calls[0])
        self.assertIn("BatchMode=yes", calls[0])
        self.assertEqual(sum("bash -s" in c for c in calls), 1)
        self.assertIn("C=wp-panel", calls[2])
        self.assertIn("MEMB=536870912", calls[2])
        self.assertIn("PUID=10001", calls[2])

    def test_the_vms_failures_are_counted_into_the_exit_status(self):
        r = self.run_script(self.server(), "--no-login", env=dict(self.env, SSH_BASH_RC="3"))
        self.assertEqual(r.returncode, 3, r.stdout[-1500:])
        self.assertIn("3 check(s) failed.", r.stdout)

    def test_no_ssh_and_no_sudo_are_their_own_stops(self):
        r = self.run_script(self.server(), "--no-login", env=dict(self.env, SSH_RC="255"))
        self.assertIn("cannot ssh to admin@10.8.0.68", r.stdout)
        self.assertNotIn("== 1. the VM", r.stdout)
        r = self.run_script(self.server(), "--no-login", env=dict(self.env, SSH_SUDO_RC="1"))
        self.assertIn("sudo -n does not work on the VM", r.stdout)
        self.assertNotIn("== 1. the VM", r.stdout)

    def test_a_name_that_resolves_elsewhere_and_a_port_that_should_be_closed_but_answers_are_failures(self):
        s = self.server()
        r = self.run_script(s, "--no-login", env=dict(self.env, GETENT_ANSWER="10.9.9.9"))
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r"FAIL: wp-panel\.lab\.example\.com resolves to 127\.0\.0\.1 \(it says: 10\.9\.9\.9\)")
        cmd = ["bash", str(SCRIPT), "--yes", "--no-login", "--ip", "127.0.0.1", "--port", str(s.port), "--cacert", str(s.cert), "--closed-ports", str(s.port)]
        r = subprocess.run(cmd, capture_output=True, text=True, env=self.env, timeout=120)
        self.assertIn("FAIL: port %d must not answer from here, and it does" % s.port, r.stdout)

    def test_the_login_reads_the_password_and_the_code_hidden_and_passes_them_on_standard_input_only(self):
        s = self.server()
        c = code()
        r = self.run_script(s, stdin="%s\n%s\n" % (PASSWORD, c))
        self.assertEqual(r.returncode, 0, r.stdout[-2500:])
        self.assertIn("the right password and code log in", r.stdout)
        self.assertIn("logging out works", r.stdout)
        for secret in (PASSWORD, c):
            self.assertNotIn(secret, r.stdout + r.stderr)
            self.assertNotIn(secret, self.log.read_text(), "not in any ssh command either")
        text = SCRIPT.read_text()
        self.assertIn('read -r -s -p "   Password: " PW', text, "the password is typed hidden")
        self.assertIn('read -r -s -p "   Authenticator code: " CODE', text, "and so is the code")
        self.assertIn("printf '%s\\n%s\\n' \"$PW\" \"$CODE\" | \"${DRV[@]}\" --login-only", text)
        code_only = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for line in code_only.splitlines():
            if "$PW" in line or "$CODE" in line:
                self.assertRegex(line, r"read -r -s|printf '%s\\n%s\\n'|PW=\"\"|-z \"\$PW\"", "the password and the code may only be read, piped and cleared: " + line)

    def test_a_wrong_password_in_the_login_step_fails_the_run(self):
        s = self.server()
        r = self.run_script(s, stdin="not the password\n%s\n" % code())
        self.assertGreater(r.returncode, 0)
        self.assertIn("FAIL: the right password and code log in", r.stdout)

    def test_the_script_changes_nothing_in_the_repo_and_commits_nothing(self):
        code_only = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))
        for bad in ("git commit", "git add", "sed -i", "ansible-playbook", "docker rm", "docker stop", "docker restart", "systemctl restart", "systemctl stop", "rm -", "tee ", "> /", "vault"):
            self.assertNotIn(bad, code_only, bad)

    def test_docker_top_is_called_without_ps_options(self):
        # `docker top C -eo args` fails on the VM ("Couldn't find PID field in ps output"): the worker count must use the default columns
        code_only = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))
        calls = re.findall(r"docker top [^|)]*", code_only)
        self.assertEqual(len(calls), 1)
        self.assertNotRegex(calls[0], r"\s-")


if __name__ == "__main__":
    unittest.main()
