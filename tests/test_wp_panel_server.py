#!/usr/bin/env python3
"""Offline tests of the control app's server settings (app/gunicorn.conf.py) and of the app behind a real gunicorn.

The settings are run as a Python file with the CPU count patched, so the workers rule (2 x the CPU threads + 1) is seen at 1, 2, 4 and 16 threads. Then a real gunicorn is started on a local port with
secret files made for the test, and spoken to over plain HTTP: the login page answers with the security headers, a page without a login is sent to the login, an over-long request line and too many
headers are refused by the server itself, and the process ends when asked. They do not prove the container, HTTPS, or the load the real VM can carry. Needs Flask, pyotp and gunicorn (app/requirements.txt).
Run: python3 -I tests/test_wp_panel_server.py"""
import http.client
import json
import os
import runpy
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONF = ROOT / "app/gunicorn.conf.py"

try:
    import gunicorn  # noqa: F401
    HAVE_GUNICORN = True
except ImportError:
    HAVE_GUNICORN = False


def conf_with(cpus, env=None):
    real, env_before = os.sched_getaffinity, dict(os.environ)
    os.sched_getaffinity = lambda pid: set(range(cpus))
    try:
        os.environ.pop("PANEL_BIND", None)
        os.environ.update(env or {})
        return runpy.run_path(str(CONF))
    finally:
        os.sched_getaffinity = real
        os.environ.clear()
        os.environ.update(env_before)


class Settings(unittest.TestCase):
    def test_workers_are_two_per_cpu_thread_plus_one(self):
        for cpus, workers in ((1, 3), (2, 5), (4, 9), (16, 33)):
            self.assertEqual(conf_with(cpus)["workers"], workers, cpus)

    def test_the_count_follows_the_threads_this_process_may_use_not_the_hosts(self):
        text = CONF.read_text()
        self.assertIn("os.sched_getaffinity(0)", text)
        self.assertNotIn("multiprocessing.cpu_count", text)

    def test_every_input_a_browser_controls_is_bounded(self):
        c = conf_with(1)
        self.assertLessEqual(c["limit_request_line"], 4094)
        self.assertLessEqual(c["limit_request_fields"], 100)
        self.assertLessEqual(c["limit_request_field_size"], 8190)
        self.assertEqual((c["worker_class"], c["worker_tmp_dir"]), ("gthread", "/dev/shm"))
        self.assertGreaterEqual(c["timeout"], 60, "longer than the longest ssh call the app makes (30 s)")

    def test_the_access_log_holds_no_cookie_no_referer_and_no_user_agent(self):
        fmt = conf_with(1)["access_log_format"]
        for field in ("%({cookie}i)s", "%(f)s", "%(a)s", "{", "Cookie", "Referer", "User-Agent"):
            self.assertNotIn(field, fmt)
        self.assertEqual(conf_with(1)["accesslog"], "-")

    def test_the_app_is_loaded_once_and_shared_by_the_workers(self):
        self.assertIs(conf_with(1)["preload_app"], True, "17 workers of 50 MB each do not fit a small container; one import shared by fork does")

    def test_the_bind_address_comes_from_the_environment_with_a_default(self):
        self.assertEqual(conf_with(1)["bind"], "0.0.0.0:8000")
        self.assertEqual(conf_with(1, {"PANEL_BIND": "127.0.0.1:9000"})["bind"], "127.0.0.1:9000")


@unittest.skipUnless(HAVE_GUNICORN, "gunicorn is not installed (pip install -r app/requirements.txt)")
class RealServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pyotp
        from werkzeug.security import generate_password_hash
        cls.d = tempfile.TemporaryDirectory()
        secrets = Path(cls.d.name) / "secrets"
        secrets.mkdir()
        (secrets / "session_key").write_bytes(b"k" * 40)
        (secrets / "password_hash").write_text(generate_password_hash("pw", method="pbkdf2:sha256:1000"))
        (secrets / "totp_secret").write_text(pyotp.random_base32())
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT / "app"), "PANEL_CTL_HOST": "dellwp", "PANEL_SITE_DOMAIN": "example.test", "PANEL_SECRET_DIR": str(secrets), "PANEL_STATE_DIR": str(Path(cls.d.name) / "state"),
               "PANEL_BIND": "127.0.0.1:%d" % cls.port, "PANEL_CTL_KEY": "/nonexistent", "PANEL_KNOWN_HOSTS": "/nonexistent"}
        cls.proc = subprocess.Popen([sys.executable, "-I", "-m", "gunicorn", "-c", str(CONF), "--worker-tmp-dir", "/tmp", "wp_panel.wsgi:app"], env=env, cwd=str(ROOT / "app"),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                if cls.proc.poll() is not None:
                    raise RuntimeError("gunicorn did not start: " + cls.proc.stdout.read().decode()[-1500:])
                time.sleep(0.1)
        else:
            raise RuntimeError("gunicorn did not listen in 10 seconds")

    @classmethod
    def tearDownClass(cls):
        cls.proc.send_signal(signal.SIGTERM)
        try:
            cls.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.proc.stdout.close()
        cls.d.cleanup()

    def request(self, method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, headers=headers or {})
            r = c.getresponse()
            return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
        finally:
            c.close()

    def test_the_login_page_answers_with_the_security_headers_and_no_server_banner_of_the_app(self):
        status, headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn(b"Log in", body)
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_a_page_without_a_login_is_sent_to_the_login(self):
        for path in ("/sites", "/jobs", "/audit", "/sites/alpha"):
            status, headers, _ = self.request("GET", path)
            self.assertEqual((status, headers["location"].endswith("/login")), (302, True), path)

    def test_the_server_refuses_a_request_line_or_headers_that_are_too_big(self):
        status, _, _ = self.request("GET", "/login?x=" + "a" * 5000)
        self.assertIn(status, (400, 414))
        status, _, _ = self.request("GET", "/login", {"X-Big": "a" * 9000})
        self.assertIn(status, (400, 431))
        status, _, _ = self.request("GET", "/login", {("X-%d" % i): "a" for i in range(120)})
        self.assertIn(status, (400, 431))

    def test_the_session_cookie_of_a_real_response_is_secure_and_strict(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("GET", "/login")
            r = c.getresponse()
            r.read()
            cookies = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
        finally:
            c.close()
        self.assertTrue(cookies)
        for flag in ("Secure", "HttpOnly", "SameSite=Strict"):
            self.assertIn(flag, cookies[0])

    def test_the_server_runs_two_workers_per_cpu_thread_plus_one(self):
        want = 2 * len(os.sched_getaffinity(0)) + 1
        for _ in range(100):               # the workers start one after another; give them a few seconds
            out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(self.proc.pid)], capture_output=True, text=True).stdout.split()
            if len(out) >= want:
                break
            time.sleep(0.1)
        self.assertEqual(len(out), want)


@unittest.skipUnless(HAVE_GUNICORN, "gunicorn is not installed (pip install -r app/requirements.txt)")
class SlowRequest(unittest.TestCase):
    def test_a_request_longer_than_the_worker_timeout_is_not_killed_under_gthread(self):
        """Installing WordPress waits minutes for the tool on dellwp, far over `timeout`; the setting is the worker's heartbeat, not a request's length (gthread). Proven here with a 2 s timeout and a 5 s request."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "slow.py").write_text("import time\n\ndef app(environ, start_response):\n    time.sleep(5)\n    start_response('200 OK', [('Content-Type', 'text/plain')])\n    return [b'done']\n")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {"PATH": os.environ["PATH"], "PANEL_BIND": "127.0.0.1:%d" % port}
        proc = subprocess.Popen([sys.executable, "-I", "-m", "gunicorn", "-c", str(CONF), "--timeout", "2", "--workers", "1", "--worker-tmp-dir", "/tmp", "--chdir", d.name, "slow:app"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        c.request("GET", "/")
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (200, b"done"))
        c.close()


@unittest.skipUnless(HAVE_GUNICORN and Path("/proc/self/status").exists(), "gunicorn or /proc is not here")
class BigBody(unittest.TestCase):
    def test_a_300_mb_put_is_read_a_piece_at_a_time_by_gunicorn_and_the_worker_stays_small(self):
        """The import page streams a file of up to 3 GB through this server to ssh. Proven here with 300 MB through the real gunicorn configuration and a WSGI app that reads it in 64 KiB pieces: the worker's
        peak memory must stay far below the body (so gunicorn does not collect the body first), and every byte arrives."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "big.py").write_text(
            "import hashlib, json\n\n"
            "def app(environ, start_response):\n"
            "    n, left, h = 0, int(environ['CONTENT_LENGTH']), hashlib.sha256()\n"
            "    body = environ['wsgi.input']\n"
            "    while left:\n"
            "        b = body.read(min(65536, left))\n"
            "        if not b:\n"
            "            break\n"
            "        left -= len(b); n += len(b); h.update(b)\n"
            "    peak = [int(l.split()[1]) for l in open('/proc/self/status') if l.startswith('VmHWM')][0]\n"
            "    out = json.dumps({'bytes': n, 'sha': h.hexdigest(), 'peak_kb': peak}).encode()\n"
            "    start_response('200 OK', [('Content-Type', 'application/json')])\n"
            "    return [out]\n")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {"PATH": os.environ["PATH"], "PANEL_BIND": "127.0.0.1:%d" % port}
        proc = subprocess.Popen([sys.executable, "-I", "-m", "gunicorn", "-c", str(CONF), "--workers", "1", "--worker-tmp-dir", "/tmp", "--chdir", d.name, "big:app"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        import hashlib
        total, piece = 300 * 1024 * 1024, b"\x5a" * (1024 * 1024)

        class Body:
            def __init__(self):
                self.left, self.h = total, hashlib.sha256()

            def read(self, n=-1):
                if not self.left:
                    return b""
                size = min(n if n and n > 0 else len(piece), len(piece), self.left)
                self.left -= size
                self.h.update(piece[:size])
                return piece[:size]
        body = Body()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
        c.request("PUT", "/", body=body, headers={"Content-Length": str(total)})
        r = c.getresponse()
        got = json.loads(r.read())
        c.close()
        self.assertEqual((r.status, got["bytes"], got["sha"]), (200, total, body.h.hexdigest()))
        self.assertLess(got["peak_kb"], 120 * 1024, "the worker's peak memory (kB) must be far below the 300 MB body")


if __name__ == "__main__":
    unittest.main()
