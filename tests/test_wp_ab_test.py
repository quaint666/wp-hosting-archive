#!/usr/bin/env python3
"""Offline tests of scripts/wp_ab_test.py, the A/B test of php.ini settings on one live site: the file edit and its restore on EVERY way out, the phase order, the restarts, the verdict arithmetic, the checks of
what may be set, and that nothing but php.ini is written. The site, systemctl, nginx and the clock are stand-ins; a "setting" makes the fake site faster or slower so the verdict can be checked.
Run: python3 -I tests/test_wp_ab_test.py"""
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import wp_ab_test as ab  # noqa: E402

INI = "memory_limit = 256M\nopcache.memory_consumption = 128\nopcache.interned_strings_buffer = 16\nopcache.validate_timestamps = 1\n"
VHOST = "server {\n    listen 127.0.0.1:8080;\n    listen 127.0.0.1:8081;\n    server_name alpha.example.test;\n}\n"


class Args:
    def __init__(self, tmp, **kw):
        self.site, self.set, self.rounds, self.requests, self.ready_timeout_s = "alpha", ["opcache.interned_strings_buffer=8"], 2, 4, 20
        self.sites_dir, self.vhost_dir, self.cache_dir, self.lock_file = str(tmp / "sites"), str(tmp / "conf.d"), str(tmp / "cache"), ""
        self.__dict__.update(kw)


class Fake:
    """systemctl, wp-site-provision and the site. `speed(ini_text)` gives the milliseconds a request takes under the php.ini that is in the file when it is asked."""

    def __init__(self, tmp, speed=lambda ini: 100.0, ready_after=0):
        self.tmp, self.speed, self.calls, self.restarts, self.http_log, self.ready_after, self.provision_line = tmp, speed, [], 0, [], ready_after, "same     /srv/wp/sites/alpha/php.ini"
        self.restart_rc, self.fail_after_requests, self.requests, self.not_ready = 0, None, 0, False
        self.ini_at_restart = []
        self.t = 0.0

    def run(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[0] == "systemctl":
            self.restarts += 1
            self.ini_at_restart.append((self.tmp / "sites/alpha/php.ini").read_text())
            return subprocess.CompletedProcess(cmd, self.restart_rc, "", "Failed" if self.restart_rc else "")
        if cmd[0].endswith("wp-site-provision"):
            return subprocess.CompletedProcess(cmd, 0, self.provision_line + "\nsame     /srv/wp/sites/alpha/php-fpm.conf\n", "")
        raise AssertionError(cmd)

    def client(self, method, path, headers=None, body=None):
        self.requests += 1
        self.http_log.append((method, path, (headers or {}).get("Cookie")))
        if self.fail_after_requests is not None and self.requests > self.fail_after_requests:
            self.fail_after_requests = None             # a Ctrl-C comes once
            raise KeyboardInterrupt
        if self.not_ready:
            return 503, 0.0, "-"
        ini = (self.tmp / "sites/alpha/php.ini").read_text()
        cache = "MISS" if path.startswith("/?ab=") else "BYPASS" if method == "POST" else "-"
        return 200, self.speed(ini), cache

    def sleep(self, s):
        self.t += s

    def clock(self):
        self.t += 0.5
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.tmp = Path(self.d.name)
        (self.tmp / "sites/alpha").mkdir(parents=True)
        (self.tmp / "sites/alpha/php.ini").write_text(INI)
        (self.tmp / "conf.d").mkdir()
        (self.tmp / "conf.d/wp_site_alpha.conf").write_text(VHOST)
        (self.tmp / "cache/a/bc").mkdir(parents=True)
        (self.tmp / "cache/a/bc/page").write_text("cached")
        self.out = []

    def site(self, fake, **kw):
        args = Args(self.tmp, **kw)
        return args, ab.Site("alpha", args, run=fake.run, sleep=fake.sleep, clock=fake.clock, client=fake.client)

    def go(self, fake, **kw):
        args, site = self.site(fake, **kw)
        rc = ab.run_test(args, out=self.out.append, site=site)
        return rc, "\n".join(self.out)

    def ini(self):
        return (self.tmp / "sites/alpha/php.ini").read_text()


class Edit(unittest.TestCase):
    def test_a_key_in_the_file_has_its_line_changed_and_nothing_else(self):
        new, changes = ab.apply_sets(INI, [("opcache.interned_strings_buffer", "8")])
        self.assertEqual(new, INI.replace("buffer = 16", "buffer = 8"))
        self.assertEqual(changes, [("opcache.interned_strings_buffer", "16", "8")])

    def test_a_key_that_is_not_there_is_added_at_the_end(self):
        new, changes = ab.apply_sets(INI, [("opcache.jit", "off")])
        self.assertEqual((new, changes), (INI + "opcache.jit = off\n", [("opcache.jit", None, "off")]))

    def test_a_key_is_matched_whole_not_as_a_prefix_or_inside_a_comment(self):
        text = "; opcache.memory = 1\nopcache.memory_consumption = 128\nopcache.memory = 64\n"
        new, _ = ab.apply_sets(text, [("opcache.memory", "32")])
        self.assertEqual(new, "; opcache.memory = 1\nopcache.memory_consumption = 128\nopcache.memory = 32\n")

    def test_the_same_value_is_refused_because_there_is_nothing_to_compare(self):
        with self.assertRaises(SystemExit) as cm:
            ab.apply_sets(INI, [("opcache.interned_strings_buffer", "16")])
        self.assertIn("nothing to compare", str(cm.exception))

    def test_odd_settings_are_refused(self):
        for bad in ("nokey", "=1", "UPPER=1", "a=", "a b=1", "opcache.x=1;2", "opcache.x=$(id)", "opcache.x=1\n2", "../x=1", "x=" + "a" * 61, "k=v w"):
            with self.assertRaises(SystemExit, msg=bad):
                ab.parse_sets([bad])
        for items in ([], ["a.b=1", "a.b=2"]):
            with self.assertRaises(SystemExit):
                ab.parse_sets(items)
        self.assertEqual(ab.parse_sets(["opcache.a=8", "memory_limit=256M", "x.y=a:b/c,d+e-f_g.h"]), [("opcache.a", "8"), ("memory_limit", "256M"), ("x.y", "a:b/c,d+e-f_g.h")])


class Arithmetic(unittest.TestCase):
    def test_percentiles(self):
        v = list(range(1, 101))
        self.assertEqual((ab.pct(v, 50), ab.pct(v, 95), ab.pct([7], 95)), (50, 95, 7))

    def test_a_clear_difference_is_called_real_and_the_direction_is_right(self):
        change, noise, text = ab.judge([100, 102], [80, 81])
        self.assertLess(change, -0.15); self.assertEqual(text, "B is 20 % faster")
        self.assertEqual(ab.judge([100, 101], [130, 131])[2], "B is 30 % slower")

    def test_a_difference_inside_the_noise_is_not(self):
        self.assertEqual(ab.judge([100, 130], [110, 140])[2], "no difference beyond the noise", "the same setting differed 26 % from itself")
        self.assertEqual(ab.judge([100, 100], [102, 102])[2], "no difference beyond the noise", "under 3 % is never real")

    def test_one_round_uses_the_three_percent_rule_alone(self):
        self.assertEqual(ab.judge([100], [90])[2], "B is 10 % faster")
        self.assertEqual(ab.judge([100], [98])[2], "no difference beyond the noise")

    def test_nothing_measured(self):
        self.assertEqual(ab.judge([0, 0], [5, 5])[2], "nothing measured")


class Phases(Base):
    def test_the_order_is_a_b_a_b_with_a_restart_each_and_a_restore_at_the_end(self):
        fake = Fake(self.tmp)
        rc, text = self.go(fake)
        self.assertEqual(rc, 0, text)
        self.assertEqual([("buffer = 16" in i, "buffer = 8" in i) for i in fake.ini_at_restart], [(True, False), (False, True), (True, False), (False, True), (True, False)], "A B A B, then A again")
        self.assertEqual(fake.restarts, 5)
        self.assertEqual(self.ini(), INI, "the file is exactly as it was")

    def test_the_file_is_written_in_place_so_the_containers_bind_mount_follows(self):
        before = os.stat(self.tmp / "sites/alpha/php.ini").st_ino
        self.go(Fake(self.tmp))
        self.assertEqual(os.stat(self.tmp / "sites/alpha/php.ini").st_ino, before)

    def test_every_phase_starts_with_an_empty_page_cache_and_measures_only_php_built_requests(self):
        fake = Fake(self.tmp)
        (self.tmp / "cache/a/bc/page").write_text("cached")
        self.go(fake)
        self.assertEqual(list((self.tmp / "cache").rglob("page")), [])
        paths = {(m, p.split("-")[0] if p.startswith("/?ab=") else p) for m, p, _ in fake.http_log}
        self.assertEqual(paths, {("GET", "/wp-login.php"), ("GET", "/wp-json/"), ("GET", "/?ab=A1"), ("GET", "/?ab=B1"), ("GET", "/?ab=A2"), ("GET", "/?ab=B2"), ("POST", "/?wc-ajax=get_refreshed_fragments")} | {("GET", "/?ab=A1")} - set())
        urls = [p for m, p, _ in fake.http_log if p.startswith("/?ab=")]
        self.assertEqual(len(urls), len(set(urls)), "every page address is new, so every one is a cache MISS")
        self.assertTrue(all(c and "woocommerce_items_in_cart" in c for m, p, c in fake.http_log if m == "POST"), "the mini-cart POST carries a cart cookie, so nginx does not answer it")

    def test_a_setting_that_makes_the_site_faster_is_reported_faster_and_one_that_changes_nothing_is_not(self):
        fake = Fake(self.tmp, speed=lambda ini: 80.0 if "buffer = 8" in ini else 100.0)
        rc, text = self.go(fake)
        self.assertIn("B is 20 % faster", text)
        self.assertNotIn("no difference", text.split("==== A")[1].split("page cache seen")[0])
        self.setUp()
        rc, text = self.go(Fake(self.tmp))
        self.assertIn("no difference beyond the noise", text); self.assertNotIn("faster", text.split("==== A")[1])

    def test_the_report_names_the_change_the_cache_tally_and_how_to_read_it(self):
        rc, text = self.go(Fake(self.tmp))
        self.assertIn("B changes opcache.interned_strings_buffer: 16 -> 8", text)
        self.assertIn("MISS", text); self.assertIn("BYPASS", text); self.assertIn("Read it:", text)
        self.assertIn("wp-site-provision on php.ini: same", text)

    def test_one_round_is_a_then_b_then_a_again(self):
        fake = Fake(self.tmp)
        self.go(fake, rounds=1)
        self.assertEqual(fake.restarts, 3)


class WayOut(Base):
    def test_an_error_in_the_middle_puts_the_file_back_and_says_so(self):
        fake = Fake(self.tmp)
        fake.fail_after_requests = 40
        rc, text = self.go(fake)
        self.assertEqual(rc, 1)
        self.assertEqual(self.ini(), INI)
        self.assertIn("stopped early", text); self.assertIn("restored php.ini", text)
        self.assertEqual(fake.ini_at_restart[-1], INI, "and the site was restarted on the original")

    def test_a_signal_is_a_way_out_too(self):
        fake = Fake(self.tmp)
        calls = {"n": 0}
        real = fake.client

        def client(*a, **k):
            calls["n"] += 1
            if calls["n"] == 30:
                os.kill(os.getpid(), signal.SIGHUP)             # the ssh dropped
            return real(*a, **k)
        fake.client = client
        rc, text = self.go(fake)
        self.assertEqual(rc, 1)
        self.assertEqual(self.ini(), INI); self.assertIn("signal %d" % signal.SIGHUP, text)

    def test_the_signal_handlers_are_put_back(self):
        before = {s: signal.getsignal(s) for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
        self.go(Fake(self.tmp))
        self.assertEqual({s: signal.getsignal(s) for s in before}, before)

    def test_a_restart_that_fails_still_restores_the_file_and_says_it_could_not_confirm(self):
        fake = Fake(self.tmp)
        fake.restart_rc = 1
        rc, text = self.go(fake)
        self.assertEqual(rc, 1)
        self.assertEqual(self.ini(), INI)
        self.assertIn("COULD NOT CONFIRM THE RESTORE", text); self.assertIn("wp-site-provision apply alpha", text)

    def test_a_site_that_never_comes_back_is_reported_not_hung(self):
        fake = Fake(self.tmp)
        fake.not_ready = True
        rc, text = self.go(fake)
        self.assertEqual(rc, 1)
        self.assertEqual(self.ini(), INI)
        self.assertIn("did not answer 200", text)

    def test_when_the_repo_says_php_ini_differs_it_is_said_loudly_and_the_exit_is_1(self):
        fake = Fake(self.tmp)
        fake.provision_line = "differs  /srv/wp/sites/alpha/php.ini"
        rc, text = self.go(fake)
        self.assertEqual(rc, 1); self.assertIn("PHP.INI IS NOT WHAT THE REPO RENDERS", text)

    def test_a_busy_site_is_refused_before_anything_changes(self):
        import fcntl
        lock = self.tmp / "alpha.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        fake = Fake(self.tmp)
        with self.assertRaises(SystemExit) as cm:
            self.go(fake, lock_file=str(lock))
        os.close(fd)
        self.assertIn("another change", str(cm.exception))
        self.assertEqual((fake.restarts, self.ini()), (0, INI))

    def test_an_unwanted_setting_changes_nothing(self):
        fake = Fake(self.tmp)
        with self.assertRaises(SystemExit):
            self.go(fake, set=["opcache.interned_strings_buffer=16"])
        self.assertEqual((fake.restarts, self.ini()), (0, INI))


class Command(unittest.TestCase):
    def test_only_php_ini_is_written_and_nothing_is_run_through_a_shell(self):
        import re
        src = (HERE.parent / "scripts/wp_ab_test.py").read_text()
        writes = [m for m in re.findall(r"open\(([^)]*)\)", src) if "r+" in m]
        self.assertEqual(writes, ['self.ini, "r+"'], "the one place a file is opened for writing is php.ini")
        self.assertNotIn("shell=True", src)
        self.assertNotIn("docker", src)

    def test_arguments_are_checked(self):
        for argv in (["Bad_Site", "--set", "a.b=1"], ["alpha", "--set", "a.b=1", "--rounds", "0"], ["alpha", "--set", "a.b=1", "--rounds", "5"], ["alpha", "--set", "a.b=1", "--requests", "2"], ["alpha"]):
            with self.assertRaises(SystemExit):
                ab.main(argv)


class Wrapper(unittest.TestCase):
    """The shell wrapper: one question, a fixed command line, and the paths the repo's variables name."""
    SH = HERE.parent / "scripts/wp_ab_test.sh"

    def run_sh(self, *argv, stdin=None):
        env = dict(os.environ, PATH=str(self.bin) + ":" + os.environ["PATH"], XDG_STATE_HOME=str(self.tmp / "state"))
        return subprocess.run(["bash", str(self.SH), *argv], capture_output=True, text=True, input=stdin, env=env, timeout=60)

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.tmp = Path(self.d.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "ssh").write_text('#!/bin/bash\nprintf "SSH:%s\\n" "$*" >> "$XDG_STATE_HOME/ssh.log"\ncat > "$XDG_STATE_HOME/program.py"\necho "remote output"\n')
        (self.bin / "ssh").chmod(0o755)
        (self.tmp / "state").mkdir()

    def test_with_yes_it_asks_nothing_and_sends_the_program_and_one_fixed_command(self):
        r = self.run_sh("alpha", "--set", "opcache.interned_strings_buffer=8", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = (self.tmp / "state/ssh.log").read_text()
        self.assertIn("BatchMode=yes", log); self.assertIn("sudo python3 -u - alpha --rounds 2 --requests 12 --lock-file /var/lib/wp-ctl/locks/alpha.lock --set opcache.interned_strings_buffer=8", log)
        self.assertEqual((self.tmp / "state/program.py").read_text(), (HERE.parent / "scripts/wp_ab_test.py").read_text())
        self.assertIn("remote output", r.stdout)
        saved = list((self.tmp / "state/wp-perf").glob("wp_ab_alpha_*.txt"))
        self.assertEqual(len(saved), 1); self.assertIn("remote output", saved[0].read_text())

    def test_without_a_terminal_and_without_yes_it_refuses_instead_of_hanging(self):
        r = self.run_sh("alpha", "--set", "a.b=1", stdin="alpha\n")
        self.assertEqual(r.returncode, 2); self.assertIn("--yes", r.stderr)
        self.assertFalse((self.tmp / "state/ssh.log").exists())

    def test_odd_arguments_are_refused_before_ssh_is_run(self):
        for argv in (["alpha"], ["Alpha", "--set", "a.b=1"], ["alpha", "--set", "a.b=1;id"], ["alpha", "--set", "A.b=1"], ["alpha", "--set", "a.b"], ["alpha", "--set", "a.b=1", "--rounds", "9"],
                     ["alpha", "--set", "a.b=1", "--requests", "2"], ["alpha", "beta", "--set", "a.b=1"], ["alpha", "--set", "a.b=1", "--host", "x;y"], ["alpha", "--set", "a.b=1", "--nope"], ["../x", "--set", "a.b=1"]):
            r = self.run_sh(*argv, "--yes")
            self.assertEqual(r.returncode, 2, argv)
        self.assertFalse((self.tmp / "state/ssh.log").exists())

    def test_the_paths_the_script_assumes_are_the_repos(self):
        from test_wp_sites_render import base_vars
        v = base_vars()
        py = (HERE.parent / "scripts/wp_ab_test.py").read_text(); sh = self.SH.read_text()
        self.assertIn('"--sites-dir", default="%s/sites"' % v["wp_data_mount"], py)
        self.assertIn('"--vhost-dir", default="%s"' % v["wp_vhost_dir"], py)
        self.assertIn('"--cache-dir", default="%s"' % v["wp_page_cache_path"], py)
        self.assertIn("%s/locks/$SITE.lock" % v["wp_ctl_state_dir"], sh)
        self.assertIn("/usr/local/sbin/wp-site-provision", py); self.assertEqual(v["wp_tools_bin"], "/usr/local/sbin")


if __name__ == "__main__":
    unittest.main()
