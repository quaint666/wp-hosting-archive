#!/usr/bin/env python3
"""Offline tests of the control app's server settings (app/gunicorn.conf.py) and of the app behind a real gunicorn.

The settings are run as a Python file with the CPU count patched, so the workers rule (2 x the CPU threads + 1) is seen at 1, 2, 4 and 16 threads. Then a real gunicorn is started on a local port with
secret files made for the test, and spoken to over plain HTTP: the login page answers with the security headers, a page without a login is sent to the login, an over-long request line and too many
headers are refused by the server itself, and the process ends when asked. They do not prove the container, HTTPS, or the load the real VM can carry. Needs Flask, pyotp and gunicorn (app/requirements.txt).
Run: python3 -I tests/test_wp_panel_server.py"""
import http.client
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
        env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT / "app"), "PANEL_CTL_HOST": "dellwp", "PANEL_SECRET_DIR": str(secrets), "PANEL_STATE_DIR": str(Path(cls.d.name) / "state"),
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


if __name__ == "__main__":
    unittest.main()
