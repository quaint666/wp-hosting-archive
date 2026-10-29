#!/usr/bin/env python3
"""Offline tests of scripts/wp_site_provision.py (wp-site-provision): create, apply and render.

The filesystem is real (a temporary tree stands in for /srv/wp, /etc/nginx/conf.d, /etc/systemd/system and /run), and so are the locks and the atomic writes. docker, nginx, systemctl, lsattr,
chattr, repquota and setquota are one in-process stand-in (FakeHost) that KEEPS STATE (a quota project per path, a limit per project, which images exist, which units are enabled and active) and logs every call,
so "a second apply changes nothing" means no call that changes anything, not just a quiet exit; it also checks that each call is an argument list with the clean environment and no stdin. chown is recorded
(the tests do not run as root); the HTTP probe is a list of statuses.

They prove: the steps and their order (the ones tasks/wp_site_one_common.yml had before the tool replaced it; git has it); every command is the argument list the tool is meant to run (the quota project and limit, the seed container's equal memory and swap);
a repeat changes nothing; a changed record restarts the stack only when the compose file or the database settings changed and recreates only php otherwise; a vhost nginx rejects is taken back; a create
that stops half-way is finished by apply; a refusal leaves nothing behind; the lock; the stopped, hold and live states; render tells the truth and never shows a secret; no secret is in any output or argument.
They do not prove that docker, nginx, systemd or the quota tools behave as the stand-in does (scripts/wp_provision_rehearsal_dellwp.sh will, on the host), nor that chown works. Run: python3 -I tests/test_wp_site_provision.py"""
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import wp_site_model as model  # noqa: E402
import wp_site_provision as prov  # noqa: E402
import wp_site_renderer as renderer  # noqa: E402
import test_wp_sites_render as play  # noqa: E402
from test_wp_site_renderer import platform  # noqa: E402

MASTER = "tree-master-secret-value"
RESEND = "re_tree_resend_value"


class FakeHost:
    """docker, nginx, systemctl and the quota tools in one object that keeps their state (a project per path, a limit per project, which images exist, which units are enabled and active) and logs
    every call as one line. It is the `run` the tool is given: the real call (argument list, clean environment, no stdin, a timeout) is checked here."""

    def __init__(self, path):
        self.path, self.calls, self.timeouts = path, [], {}
        self.projects, self.quota, self.images, self.enabled, self.active, self.fails = {}, {}, set(), set(), set(), {}

    def failing(self, *keys):
        for k in keys:
            if k in self.fails:
                return self.fails[k]
        return None

    def __call__(self, argv, capture_output=None, text=None, stdin=None, timeout=None, env=None):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv), argv
        assert env == {"PATH": self.path, "LANG": "C"}, env
        assert stdin == subprocess.DEVNULL and capture_output and text and isinstance(timeout, int), (stdin, timeout)
        name, args = os.path.basename(argv[0]), argv[1:]
        self.calls.append(" ".join([name] + args))
        self.timeouts[self.calls[-1]] = timeout
        rc, out, err = 0, "", ""
        bad = {"setquota": self.failing("setquota"), "nginx": self.failing("nginx"), "wp-site-backup": self.failing("backup"), "docker": self.failing("docker." + args[0]) if args else None,
               "systemctl": self.failing("systemctl." + args[0], "systemctl.%s.%s" % (args[0], [a for a in args[1:] if not a.startswith("--")][0] if len(args) > 1 else "")) if args and name == "systemctl" else None}.get(name)
        if bad is not None:
            return subprocess.CompletedProcess(argv, 1, "", bad)
        if name == "lsattr":
            out = "%7d ---------------e------- %s\n" % (self.projects.get(args[-1], 0), args[-1])
        elif name == "chattr":
            self.projects[args[-1]] = int(args[args.index("-p") + 1])
        elif name == "repquota":
            out = "*** Report for project quotas on device /dev/vdb\nProject         used    soft    hard  grace    used  soft  hard  grace\n" + "-" * 70 + "\n#0         --       0       0       0       0     0     0\n"
            out += "".join("#%d      --       0       0 %8d       1     0     0\n" % (k, v) for k, v in sorted(self.quota.items()))
        elif name == "setquota":
            self.quota[int(args[1])] = int(args[3])
        elif name == "docker" and args[:2] == ["image", "inspect"]:
            rc = 0 if args[2] in self.images else 1
        elif name == "docker" and args[0] == "pull":
            self.images.add(args[1])
        elif name == "docker" and args[0] == "run":
            host = Path(args[args.index("-v") + 1].split(":")[0])
            (host / "wp-includes").mkdir(parents=True, exist_ok=True)
            (host / "wp-includes/version.php").write_text("<?php")
            (host / "wp-config-docker.php").write_text("<?php")
        elif name == "systemctl":
            verb, flags, units = args[0], [a for a in args[1:] if a.startswith("--")], [a for a in args[1:] if not a.startswith("--")]
            unit = units[0] if units else ""
            if verb == "is-enabled":
                rc = 0 if unit in self.enabled else 1
            elif verb == "is-active":
                rc = 0 if unit in self.active else 1
            elif verb == "enable":
                self.enabled.add(unit)
                if "--now" in flags:
                    self.active.add(unit)
            elif verb == "disable":
                self.enabled.discard(unit)
                if "--now" in flags:
                    self.active.discard(unit)
            elif verb in ("start", "restart"):
                self.active.add(unit)
            elif verb == "stop":
                self.active.discard(unit)
        return subprocess.CompletedProcess(argv, rc, out, err)

    def units(self):
        return {u + ".enabled" for u in self.enabled} | {u + ".active" for u in self.active}


READ_ONLY = ("lsattr", "repquota", "docker image inspect", "systemctl is-active", "systemctl is-enabled")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wpprov-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        for sub in ("mount/sites", "mount/backups", "mount/mu", "conf.d", "units", "etc"):
            (t / sub).mkdir(parents=True)
        self.host = FakeHost("/usr/bin:/bin")
        (t / "conf.d/wp_base.conf").write_text("# base\n")
        self.secret = t / "etc/master_secret"
        self.secret.write_text(MASTER + "\n")
        self.secret.chmod(0o400)
        self.resend = t / "etc/resend_key"
        v = play.base_vars()
        self.v = v
        plat = platform()
        plat.update(wp_data_mount=str(t / "mount"), wp_unit_dir=str(t / "units"), wp_vhost_dir=str(t / "conf.d"), wp_backup_dir=str(t / "mount/backups"), wp_mu_dir=str(t / "mount/mu"))
        vm = play.dellwp_vm()
        self.cfg = {
            "data_dir": str(t / "mount/sites"), "tombstones_dir": str(t / "mount/tombstones"), "lock_dir": str(t / "lock"), "template_dir": str(HERE.parent / "templates"),
            "master_secret_file": str(self.secret), "resend_key_file": str(self.resend), "platform": plat, "defaults": v["wp_site_defaults"],
            "closed_floor": [{"name": "old", "port": 9101, "quota_id": 1001, "state": "closed", "update_policy": "auto"}],
            "fit": dict(reserved_names=v["wp_reserved_names"], vm_memory_mb=vm["memory"], data_disk_gb=vm["data_disk"], ram_reserve_mb=v["wp_vm_ram_reserve_mb"], data_reserve_mb=v["wp_data_reserve_mb"],
                        backup_quota_offset=v["wp_backup_quota_offset"], scratch_quota_id=v["wp_scratch_quota_id"], archive_quota_id=v["wp_archive_quota_id"], intake_quota_id=v["wp_intake_quota_id"]),
            "tools": {**{n: "/usr/bin/" + n for n in ("lsattr", "chattr", "repquota", "setquota", "docker", "nginx", "systemctl")}, "backup": "/usr/bin/wp-site-backup"},
            "path": "/usr/bin:/bin", "command_timeout_s": 30, "probe_tries": 3, "probe_delay_s": 5, "backup_timeout_s": 3600,
        }
        self.lines, self.chowns, self.statuses, self.sleeps, self.probes = [], [], [200], [], []
        self.clock = 1793000000.0

    def make(self):
        def probe(url, host):
            self.probes.append((url, host))
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return prov.Provisioner(self.cfg, run=self.host, sleep=self.sleeps.append, probe=probe, chown=lambda *a: self.chowns.append(a), out=self.lines.append, now=lambda: self.clock)

    def calls(self):
        return self.host.calls

    def changing(self, since=0):
        return [c for c in self.calls()[since:] if not c.startswith(READ_ONLY)]

    def inject(self, what, text="boom"):
        self.host.fails[what] = text

    def clear(self, what):
        del self.host.fails[what]

    def site_dir(self, name="alpha"):
        return Path(self.cfg["data_dir"]) / name

    def record(self, name="alpha"):
        return json.loads((self.site_dir(name) / "site.json").read_text())

    def edit_record(self, name="alpha", **kw):
        reg = model.Registry(self.cfg["data_dir"], self.cfg["tombstones_dir"], self.cfg["defaults"])
        reg.write_site({**reg.read_site(name), **kw})

    def tree(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if "lock" not in p.parts)


class Create(Base):
    def test_the_happy_path(self):
        rec = self.make().create("alpha")
        self.assertEqual((rec["name"], rec["port"], rec["quota_id"], rec["state"]), ("alpha", 9102, 1002, "hold"), "above the repo's closed site")
        self.assertEqual(self.record(), {"schema": 1, "name": "alpha", "port": 9102, "quota_id": 1002, "state": "hold", "created": "2026-10-26T07:33:20Z"})
        d = self.site_dir()
        for rel in (".env", "wp-config.php", "php.ini", "fpm-pool.conf", "compose.yml", "html/wp-includes/version.php", "db", "site.json"):
            self.assertTrue((d / rel).exists(), rel)
        self.assertFalse((d / "html/wp-config-docker.php").exists(), "the image's config template is removed")
        self.assertFalse((d / "stopped.flag").exists())
        self.assertTrue((self.tmp / "units/wp-alpha.service").exists())
        self.assertTrue((self.tmp / "conf.d/wp_site_alpha.conf").exists())
        self.assertTrue((self.tmp / "mount/backups/alpha").is_dir())
        self.assertEqual(self.probes, [("http://127.0.0.1:%s/wp-admin/install.php" % self.cfg["platform"]["wp_nginx_listen_port"], "alpha." + self.cfg["platform"]["wp_domain"])])
        units = self.host.units()
        self.assertIn("wp-alpha.active", units)
        self.assertIn("wp-backup@alpha.timer.active", units)
        self.assertIn("wp-update@alpha.timer.active", units)
        self.assertNotIn("wp-cron@alpha.timer.enabled", units, "cron runs only while live")

    def test_the_order_is_the_plays(self):
        self.make().create("alpha")
        c = self.calls()
        site, back = str(self.site_dir()), str(self.tmp / "mount/backups/alpha")
        order = [next(i for i, x in enumerate(c) if x.startswith(prefix)) for prefix in (
            "lsattr -dp " + site, "lsattr -dp " + back, "docker run", "docker image inspect", "systemctl daemon-reload", "systemctl enable wp-alpha", "systemctl restart wp-alpha",
            "nginx -t", "systemctl reload nginx", "systemctl enable --now wp-backup@alpha.timer")]
        self.assertEqual(order, sorted(order), c)
        self.assertEqual(c.count("systemctl restart wp-alpha"), 1)
        self.assertNotIn("systemctl reload wp-alpha", c, "a first start uses the new files already")

    def test_the_commands_are_the_plays_argument_lists(self):
        self.make().create("alpha")
        c, d, back, mount = self.calls(), str(self.site_dir()), str(self.tmp / "mount/backups/alpha"), str(self.tmp / "mount")
        self.assertIn("chattr -R +P -p 1002 " + d, c)
        self.assertIn("setquota -P 1002 0 %d 0 0 %s" % (3072 * 1024, mount), c)
        self.assertIn("chattr -R +P -p %d %s" % (1002 + self.v["wp_backup_quota_offset"], back), c)
        self.assertIn("setquota -P %d 0 %d 0 0 %s" % (1002 + self.v["wp_backup_quota_offset"], 3072 * 1024, mount), c)
        run = next(x for x in c if x.startswith("docker run")).split(" ")
        self.assertEqual(run[run.index("--memory") + 1], run[run.index("--memory-swap") + 1], "--memory-swap equals --memory")
        self.assertEqual(run[run.index("-v") + 1], d + "/html:/var/www/html")
        self.assertIn(self.v["wp_image_php"], run)
        self.assertNotIn("latest", " ".join(c))
        self.assertIn("docker pull " + self.v["wp_image_php"], c)
        self.assertIn("docker pull " + self.v["wp_image_db"], c)

    def test_the_files_have_the_plays_modes_and_owners(self):
        self.make().create("alpha")
        d, p = self.site_dir(), self.cfg["platform"]
        mode = lambda path: (path.stat().st_mode & 0o7777)  # noqa: E731
        self.assertEqual(mode(d / ".env"), 0o600)
        self.assertEqual(mode(d / "wp-config.php"), 0o640)
        self.assertEqual(mode(d / "compose.yml"), 0o644)
        self.assertEqual(mode(d), 0o755)
        self.assertEqual(mode(d / "db"), 0o700)
        self.assertEqual(mode(self.tmp / "mount/backups/alpha"), 0o700)
        owners = {str(a[0]): a[1:] for a in self.chowns}
        self.assertEqual(owners[str(d / "db")], (p["wp_db_uid"], p["wp_db_uid"]))
        wpconfig = [v for k, v in owners.items() if k.endswith("wp-config.php.new-%d" % os.getpid())]
        self.assertEqual(wpconfig, [(0, p["wp_php_uid"])], "wp-config.php: root, group php")

    def test_set_values_are_recorded_only_when_given(self):
        rec = self.make().create("alpha", ["php_mem_mb=640", "label=Alpha shop", "update_policy=auto", "allow_xmlrpc=true"])
        self.assertEqual({k: v for k, v in rec.items() if k not in ("schema", "name", "port", "quota_id", "state", "created")},
                         {"php_mem_mb": 640, "label": "Alpha shop", "update_policy": "auto", "allow_xmlrpc": True})
        self.assertIn("mem_limit: 640m", (self.site_dir() / "compose.yml").read_text())

    def test_the_listed_platform_values_are_all_it_needs(self):
        required, _ = renderer.needed_platform_vars(self.cfg["template_dir"])
        self.cfg["platform"] = {k: self.cfg["platform"][k] for k in required}
        self.make().create("alpha")                                           # a value the tool reads and the list lacks would be a KeyError here
        self.make().apply("alpha")

    def test_every_entry_of_the_floor_is_closed_whatever_it_says_so_its_numbers_are_held_and_its_name_is_free(self):
        self.cfg["closed_floor"] = [{"name": "declared", "port": 9101, "quota_id": 1001}, {"name": "other", "port": 9102, "quota_id": 1002, "state": "live"}]
        rec = self.make().create("alpha")
        self.assertEqual((rec["port"], rec["quota_id"]), (9103, 1003), "above every number of the floor")
        again = self.make().create("declared")
        self.assertGreater(again["port"], rec["port"], "the name is free; it gets new numbers")
        self.assertEqual(self.make().rows() and sorted(r.split()[0] for r in self.make().rows()), ["alpha", "declared"], "a floor entry is never a site")

    def test_the_second_site_gets_the_next_numbers(self):
        self.make().create("alpha")
        rec = self.make().create("bravo")
        self.assertEqual((rec["port"], rec["quota_id"]), (9103, 1003))

    def test_a_name_the_repo_or_the_registry_closed_is_free_and_gets_numbers_above_every_one_ever_recorded(self):
        self.make().create("alpha")
        reg = model.Registry(self.cfg["data_dir"], self.cfg["tombstones_dir"], self.cfg["defaults"])
        reg.write_tombstone({"schema": 1, "name": "gone", "port": 9150, "quota_id": 1050, "state": "closed"}, "20261001T000000Z")
        for name in ("gone", "old"):
            rec = self.make().create(name)
            self.assertGreater(rec["port"], 9150, name)
            self.assertGreater(rec["quota_id"], 1050, name)

    def test_refusals_leave_nothing_behind(self):
        self.make().create("alpha")
        before, tree = len(self.calls()), self.tree()
        reg = model.Registry(self.cfg["data_dir"], self.cfg["tombstones_dir"], self.cfg["defaults"])
        reg.write_tombstone({"schema": 1, "name": "gone", "port": 9150, "quota_id": 1050, "state": "closed"}, "20261001T000000Z")
        tree = self.tree()
        stray = self.site_dir("stray")
        stray.mkdir()
        (stray / "file").write_text("x")
        tree = self.tree()
        cases = [("hub", []), ("vps-hub", []), ("Bad", []), ("a", []), ("alpha", []), ("stray", []), ("bravo", ["state=live"]), ("bravo", ["port=9200"]),
                 ("bravo", ["name=x"]), ("bravo", ["php_mem_mb=10"]), ("bravo", ["php_mem_mb=640", "php_mem_mb=700"]), ("bravo", ["nokey"]), ("bravo", ["quota_mb=100000", "backup_quota_mb=100000"]),
                 ("bravo", ["php_mem_mb=4096", "db_mem_mb=4096"]), ("bravo", ["label=" + "x" * 101])]
        for name, sets in cases:
            with self.assertRaises(model.Refuse, msg=(name, sets)):
                self.make().create(name, sets)
            self.assertEqual(self.tree(), tree, (name, sets))
            self.assertEqual(len(self.calls()), before, "no command ran for a refusal: %s %s" % (name, sets))

    def test_a_refusal_for_the_fit_names_the_rule(self):
        with self.assertRaises(model.Refuse) as cm:
            self.make().create("alpha", ["php_mem_mb=4096", "db_mem_mb=4096"])
        self.assertIn("memory", str(cm.exception))

    def test_nothing_is_made_when_the_secret_file_is_open_to_others(self):
        self.secret.chmod(0o644)
        with self.assertRaises(prov.Failed) as cm:
            self.make().create("alpha")
        self.assertIn("readable by group or others", str(cm.exception))
        self.assertFalse(self.site_dir().exists())
        self.assertEqual(self.calls(), [])

    def test_a_missing_nginx_base_stops_before_anything_is_made(self):
        (self.tmp / "conf.d/wp_base.conf").unlink()
        with self.assertRaises(prov.Failed) as cm:
            self.make().create("alpha")
        self.assertIn("nginx_base_dellwp", str(cm.exception))
        self.assertFalse(self.site_dir().exists())

    def test_a_create_that_stops_half_way_is_finished_by_apply(self):
        self.inject("docker.run", "image pull timed out")
        with self.assertRaises(prov.Failed) as cm:
            self.make().create("alpha")
        self.assertIn("recorded but not fully built", str(cm.exception))
        self.assertIn("wp-site-provision apply alpha", str(cm.exception))
        self.assertEqual(self.record()["name"], "alpha", "the record is kept")
        with self.assertRaises(model.Refuse):
            self.make().create("alpha")                                       # the name is taken: finishing it is apply's job
        self.clear("docker.run")
        self.make().apply("alpha")
        self.assertTrue((self.site_dir() / "html/wp-includes/version.php").exists())
        self.assertEqual(len([c for c in self.calls() if c.startswith("docker run")]), 2, "seeded once the second time, tried once before")
        self.assertIn("wp-alpha", self.host.active)

    def test_the_lock_refuses_a_second_run(self):
        Path(self.cfg["lock_dir"]).mkdir()
        fh = open(Path(self.cfg["lock_dir"]) / "registry.lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            with self.assertRaises(prov.Failed) as cm:
                self.make().create("alpha")
            self.assertIn("lock", str(cm.exception))
        finally:
            fh.close()
        self.assertFalse(self.site_dir().exists())


class Apply(Base):
    def setUp(self):
        super().setUp()
        self.make().create("alpha")
        self.mark = len(self.calls())
        self.lines.clear()

    def test_a_repeat_changes_nothing(self):
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), [])
        self.assertEqual(self.lines, ["alpha already matches its record; nothing changed"])
        self.assertEqual(len(self.probes), 2, "the probe still runs: it is the proof, not a change")

    def test_a_compose_change_restarts_the_stack(self):
        self.edit_record(php_mem_mb=640)
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), ["systemctl restart wp-alpha"])

    def test_a_new_secret_revision_changes_the_database_password_and_restarts_the_stack(self):
        before = (self.site_dir() / ".env").read_text()
        self.edit_record(secret_rev=2)
        self.make().apply("alpha")
        self.assertNotEqual((self.site_dir() / ".env").read_text(), before)
        self.assertEqual(self.changing(self.mark), ["systemctl restart wp-alpha"], "the database takes its password from .env: the whole stack restarts")

    def test_the_data_is_flushed_before_a_file_replaces_the_old_one(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        os.fsync = lambda fd: (order.append("fsync"), real_fsync(fd))[1]
        os.replace = lambda a, b: (order.append("replace"), real_replace(a, b))[1]
        try:
            self.edit_record(allow_xmlrpc=True)
            order.clear()
            self.make().apply("alpha")
        finally:
            os.fsync, os.replace = real_fsync, real_replace
        self.assertGreaterEqual(len(order), 4)
        for i in range(0, len(order), 2):
            self.assertEqual(order[i:i + 2], ["fsync", "replace"], order)

    def test_apply_refuses_when_the_sites_no_longer_fit(self):
        self.cfg["fit"]["vm_memory_mb"] = 1000
        with self.assertRaises(model.Refuse) as cm:
            self.make().apply("alpha")
        self.assertIn("memory", str(cm.exception))
        self.assertEqual(self.changing(self.mark), [])

    def test_a_timer_that_is_enabled_but_not_running_is_started(self):
        self.host.active.discard("wp-backup@alpha.timer")
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), ["systemctl enable --now wp-backup@alpha.timer"])

    def test_a_timer_that_runs_when_it_should_not_is_stopped(self):
        self.host.enabled.add("wp-cron@alpha.timer")
        self.host.active.add("wp-cron@alpha.timer")
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), ["systemctl disable --now wp-cron@alpha.timer"], "cron runs only while live")

    def test_a_database_setting_change_restarts_the_stack(self):
        self.edit_record(db_mem_mb=300)
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), ["systemctl restart wp-alpha"])

    def test_a_change_to_a_mounted_file_recreates_only_php(self):
        self.edit_record(allow_app_passwords=True)
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), ["systemctl reload wp-alpha"])

    def test_the_upload_limit_reloads_php_and_nginx(self):
        self.edit_record(upload_mb=128)
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), ["systemctl reload wp-alpha", "nginx -t", "systemctl reload nginx"])

    def test_a_quota_change_is_applied_to_the_project(self):
        self.edit_record(quota_mb=4096)
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), ["setquota -P 1002 0 %d 0 0 %s" % (4096 * 1024, self.tmp / "mount")])

    def test_a_project_that_drifted_is_put_back(self):
        self.host.projects[str(self.site_dir())] = 77
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), ["chattr -R +P -p 1002 " + str(self.site_dir())])

    def test_a_stack_that_is_down_is_started(self):
        self.host.active.discard("wp-alpha")
        self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), ["systemctl start wp-alpha"])

    def test_a_vhost_nginx_rejects_is_taken_back(self):
        old = (self.tmp / "conf.d/wp_site_alpha.conf").read_text()
        self.edit_record(upload_mb=128)
        self.inject("nginx", "emerald syntax error")
        with self.assertRaises(prov.Failed) as cm:
            self.make().apply("alpha")
        self.assertIn("previous one was put back", str(cm.exception))
        self.assertIn("emerald syntax error", str(cm.exception))
        self.assertEqual((self.tmp / "conf.d/wp_site_alpha.conf").read_text(), old)
        self.assertNotIn("systemctl reload nginx", self.calls()[self.mark:])
        self.clear("nginx")
        self.make().apply("alpha")                                            # the next run writes the new vhost again
        self.assertIn("client_max_body_size 128m", (self.tmp / "conf.d/wp_site_alpha.conf").read_text())

    def test_a_first_vhost_nginx_rejects_is_removed(self):
        (self.tmp / "conf.d/wp_site_alpha.conf").unlink()
        self.inject("nginx")
        with self.assertRaises(prov.Failed):
            self.make().apply("alpha")
        self.assertFalse((self.tmp / "conf.d/wp_site_alpha.conf").exists())
        self.assertNotIn("systemctl reload nginx", self.calls()[self.mark:])

    def test_a_probe_that_never_answers_fails_with_the_reason(self):
        self.statuses[:] = [502]
        self.edit_record(upload_mb=128)
        with self.assertRaises(prov.Failed) as cm:
            self.make().apply("alpha")
        self.assertIn("last status 502", str(cm.exception))
        self.assertEqual(self.sleeps, [5, 5], "three tries, a pause between them")

    def test_a_probe_that_answers_late_passes(self):
        self.statuses[:] = [0, 503, 200]
        self.make().apply("alpha")

    def test_stopped(self):
        self.edit_record(state="stopped")
        (self.site_dir() / "maintenance.flag").write_text("")
        self.probes.clear()
        self.make().apply("alpha")
        self.assertTrue((self.site_dir() / "stopped.flag").exists())
        self.assertFalse((self.site_dir() / "maintenance.flag").exists(), "the backup's maintenance flag goes once the stack is down")
        self.assertEqual(self.probes, [], "no probe for a stack that is not running")
        units = self.host.units()
        for gone in ("wp-alpha.active", "wp-alpha.enabled", "wp-backup@alpha.timer.active", "wp-update@alpha.timer.enabled"):
            self.assertNotIn(gone, units)
        self.edit_record(state="hold")
        self.make().apply("alpha")
        self.assertFalse((self.site_dir() / "stopped.flag").exists())
        self.assertIn("wp-alpha", self.host.active, "starting again is only a state change")

    def test_live_starts_the_cron_timer_and_opens_the_vhost(self):
        hold = (self.tmp / "conf.d/wp_site_alpha.conf").read_text()
        self.edit_record(state="live")
        self.make().apply("alpha")
        self.assertIn("wp-cron@alpha.timer", self.host.active)
        self.assertNotEqual((self.tmp / "conf.d/wp_site_alpha.conf").read_text(), hold)
        self.edit_record(state="hold")
        self.make().apply("alpha")
        self.assertNotIn("wp-cron@alpha.timer", self.host.active)
        self.assertEqual((self.tmp / "conf.d/wp_site_alpha.conf").read_text(), hold)

    def test_a_site_without_a_record_is_refused(self):
        with self.assertRaises(model.Refuse):
            self.make().apply("bravo")
        for bad in ("../x", "A", "a b", "", "x;id"):
            with self.assertRaises(model.Refuse, msg=bad):
                self.make().apply(bad)

    def test_a_corrupt_record_stops_apply(self):
        (self.site_dir() / "site.json").write_text("{nope")
        with self.assertRaises(prov.Failed):
            self.make().apply("alpha")
        self.assertEqual(self.changing(self.mark), [])

    def test_a_link_where_a_file_goes_is_refused(self):
        (self.site_dir() / "php.ini").unlink()
        os.symlink("/etc/passwd", self.site_dir() / "php.ini")
        with self.assertRaises(prov.Failed) as cm:
            self.make().apply("alpha")
        self.assertIn("is a link", str(cm.exception))

    def test_mail_send_needs_the_provider_key_file(self):
        self.edit_record(mail_mode="send")
        with self.assertRaises(prov.Failed) as cm:
            self.make().apply("alpha")
        self.assertIn("dellwp_wp_resend_key", str(cm.exception))
        self.resend.write_text(RESEND + "\n")
        self.resend.chmod(0o400)
        self.make().apply("alpha")
        self.assertIn(RESEND, (self.site_dir() / "wp-config.php").read_text())

    def test_no_secret_in_any_output_or_argument(self):
        self.edit_record(php_mem_mb=700)
        self.make().apply("alpha")
        pw = renderer.db_password(renderer.secret_base(MASTER, "alpha", 1))
        blob = "\n".join(self.lines + self.calls())
        for secret in (MASTER, pw):
            self.assertNotIn(secret, blob)

    def test_a_failed_command_reports_what_failed(self):
        self.edit_record(php_mem_mb=700)
        self.inject("systemctl.restart", "Job for wp-alpha.service failed")
        with self.assertRaises(prov.Failed) as cm:
            self.make().apply("alpha")
        self.assertIn("systemctl restart failed", str(cm.exception))
        self.assertIn("Job for wp-alpha.service failed", str(cm.exception))

    def test_a_write_that_fails_leaves_the_old_file_and_no_temp(self):
        self.edit_record(php_mem_mb=700)
        old = (self.site_dir() / "compose.yml").read_text()
        real = os.replace
        os.replace = lambda *a: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with self.assertRaises(OSError):
                self.make().apply("alpha")
        finally:
            os.replace = real
        self.assertEqual((self.site_dir() / "compose.yml").read_text(), old)
        self.assertEqual([p.name for p in self.site_dir().iterdir() if ".new-" in p.name], [])

    def test_the_lock_refuses_a_second_apply(self):
        Path(self.cfg["lock_dir"]).mkdir(exist_ok=True)
        fh = open(Path(self.cfg["lock_dir"]) / "site-alpha.lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            with self.assertRaises(prov.Failed):
                self.make().apply("alpha")
        finally:
            fh.close()


class SetAndState(Base):
    def setUp(self):
        super().setUp()
        self.make().create("alpha")
        self.mark = len(self.calls())
        self.lines.clear()

    def unchanged(self, what):
        self.assertEqual((self.record(), self.changing(self.mark)), (self.before, []), what)

    def test_set_records_the_value_and_applies_it(self):
        self.make().set("alpha", ["php_mem_mb=640", "upload_mb=64"])
        self.assertEqual((self.record()["php_mem_mb"], self.record()["upload_mb"]), (640, 64))
        self.assertIn("systemctl restart wp-alpha", self.changing(self.mark), "the memory is in the compose file")
        self.assertIn("recorded php_mem_mb=640, upload_mb=64 for alpha", self.lines)
        self.assertEqual(self.make().render("alpha")[0] and {st for _, st in self.make().render("alpha")[0]}, {"same"}, "the VM matches the record afterwards")

    def test_the_same_value_again_changes_nothing(self):
        self.make().set("alpha", ["php_mem_mb=640"])
        mark = len(self.calls())
        self.make().set("alpha", ["php_mem_mb=640"])
        self.assertEqual(self.changing(mark), [])
        self.assertEqual(self.lines[-1], "alpha already has that; nothing changed")

    def test_label_and_note_are_only_recorded(self):
        self.make().set("alpha", ["label=Aunt May's shop", "note=her bakery"])
        self.assertEqual((self.record()["label"], self.record()["note"]), ("Aunt May's shop", "her bakery"))
        self.assertEqual(self.calls()[self.mark:], [], "not even a read: nothing of a label is on the VM")
        self.make().set("alpha", ["note="])
        self.assertEqual(self.record()["note"], "", "a note can be cleared")

    def test_refusals_change_nothing(self):
        self.before = self.record()
        for label, sets in (("a field that is not editable", ["port=9999"]), ("the state", ["state=live"]), ("the name", ["name=other"]), ("the quota id", ["quota_id=5"]),
                            ("a value out of range", ["php_mem_mb=99999"]), ("not a number", ["php_mem_mb=lots"]), ("a key twice", ["php_mem_mb=500", "php_mem_mb=600"]),
                            ("no equals sign", ["php_mem_mb"]), ("nothing", []), ("a bad policy", ["update_policy=sometimes"]), ("a good one and a bad one", ["upload_mb=64", "mail_mode=maybe"])):
            with self.subTest(label), self.assertRaises(model.Refuse):
                self.make().set("alpha", sets)
        self.unchanged("a refused set")

    def test_a_set_that_no_longer_fits_the_vm_is_refused_before_anything_is_written(self):
        self.before = self.record()
        with self.assertRaises(model.Refuse) as e:
            self.make().set("alpha", ["php_mem_mb=4096", "db_mem_mb=4096"])
        self.assertIn("would not fit", str(e.exception))
        self.unchanged("the fit check")

    def test_a_site_without_a_record_or_a_bad_name_is_refused(self):
        for name in ("bravo", "old", "../x", "A", ""):
            with self.subTest(name), self.assertRaises(model.Refuse):
                self.make().set(name, ["upload_mb=64"])
            with self.assertRaises(model.Refuse):
                self.make().state(name, "live")

    def test_an_apply_that_fails_leaves_the_record_and_says_how_to_finish(self):
        self.inject("systemctl.restart")
        with self.assertRaises(prov.Failed) as e:
            self.make().set("alpha", ["php_mem_mb=640"])
        self.assertIn("wp-site-provision apply alpha", str(e.exception))
        self.assertEqual(self.record()["php_mem_mb"], 640, "what was asked for is recorded; apply converges to it")
        self.clear("systemctl.restart")
        self.make().apply("alpha")
        self.assertEqual({st for _, st in self.make().render("alpha")[0]}, {"same"})

    def test_a_secret_file_that_cannot_be_read_stops_a_set_before_the_write(self):
        self.before = self.record()
        self.secret.chmod(0o444)
        with self.assertRaises(prov.Failed):
            self.make().set("alpha", ["upload_mb=64"])
        self.unchanged("an unreadable master secret")

    def test_hold_to_live_and_back(self):
        hold = (self.tmp / "conf.d/wp_site_alpha.conf").read_text()
        self.make().state("alpha", "live")
        self.assertEqual(self.record()["state"], "live")
        self.assertIn("wp-cron@alpha.timer", self.host.active)
        self.assertNotEqual((self.tmp / "conf.d/wp_site_alpha.conf").read_text(), hold)
        self.assertFalse([c for c in self.calls() if c.startswith("wp-site-backup")], "no backup for a move that does not stop the site")
        self.make().state("alpha", "hold")
        self.assertEqual(self.record()["state"], "hold")
        self.assertNotIn("wp-cron@alpha.timer", self.host.active)
        self.assertEqual((self.tmp / "conf.d/wp_site_alpha.conf").read_text(), hold)

    def test_stopping_takes_the_final_backup_first_then_records_then_stops(self):
        self.make().state("alpha", "stopped")
        c = self.calls()
        backup = next(i for i, x in enumerate(c) if x.startswith("wp-site-backup"))
        self.assertEqual(c[backup], "wp-site-backup alpha --tag=final")
        self.assertLess(backup, c.index("systemctl stop wp-alpha"))
        self.assertEqual(self.record()["state"], "stopped")
        self.assertTrue((self.site_dir() / "stopped.flag").exists())
        self.assertNotIn("wp-alpha", self.host.active)
        self.assertIn("taking the final backup of alpha", self.lines)
        self.assertEqual(self.host.timeouts["wp-site-backup alpha --tag=final"], 3600, "the backup verb's own limit, not the 30 seconds of a quick command")

    def test_a_live_site_can_be_stopped_with_the_same_backup(self):
        self.make().state("alpha", "live")
        mark = len(self.calls())
        self.make().state("alpha", "stopped")
        self.assertIn("wp-site-backup alpha --tag=final", self.calls()[mark:])
        self.assertEqual(self.record()["state"], "stopped")
        self.assertNotIn("wp-cron@alpha.timer", self.host.active)

    def test_a_failed_final_backup_stops_nothing_and_changes_no_record(self):
        self.before = self.record()
        self.inject("backup", "dump failed")
        with self.assertRaises(prov.Failed) as e:
            self.make().state("alpha", "stopped")
        self.assertIn("dump failed", str(e.exception))
        self.assertEqual(self.record(), self.before)
        self.assertIn("wp-alpha", self.host.active, "the site is still running")
        self.assertEqual([c for c in self.changing(self.mark) if not c.startswith("wp-site-backup")], [])

    def test_a_stopped_site_comes_back_on_hold_without_a_backup_and_never_straight_to_live(self):
        self.make().state("alpha", "stopped")
        mark = len(self.calls())
        self.before = self.record()
        with self.assertRaises(model.Refuse) as e:
            self.make().state("alpha", "live")
        self.assertIn("put it on hold first", str(e.exception))
        self.assertEqual((self.record(), self.calls()[mark:]), (self.before, []), "a refused move reads the record and nothing else")
        self.make().state("alpha", "hold")
        self.assertEqual(self.record()["state"], "hold")
        self.assertIn("wp-alpha", self.host.active)
        self.assertFalse([c for c in self.calls()[mark:] if c.startswith("wp-site-backup")])

    def test_moving_to_where_it_is_changes_nothing(self):
        mark = len(self.calls())
        self.make().state("alpha", "hold")
        self.assertEqual(self.changing(mark), [])
        self.assertEqual(self.lines[-1], "alpha already has that; nothing changed")
        self.assertEqual([c for c in self.calls()[mark:] if c.startswith("wp-site-backup")], [])

    def test_closed_and_unknown_targets_are_refused(self):
        self.before = self.record()
        for target in ("closed", "paused", "", "LIVE"):
            with self.subTest(target), self.assertRaises(model.Refuse):
                self.make().state("alpha", target)
        self.unchanged("a refused target")

    def test_a_stopped_site_that_would_not_fit_when_running_is_refused_on_its_way_back(self):
        self.make().state("alpha", "stopped")
        self.edit_record(php_mem_mb=4096, db_mem_mb=4096)       # the record of a stopped site may be larger than the VM: it runs nothing
        with self.assertRaises(model.Refuse) as e:
            self.make().state("alpha", "hold")
        self.assertIn("would not fit", str(e.exception))
        self.assertEqual(self.record()["state"], "stopped")

    def test_the_lock_refuses_a_second_state_change(self):
        fh = open(Path(self.cfg["lock_dir"]) / "site-alpha.lock", "w")
        self.addCleanup(fh.close)
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(prov.Failed) as e:
            self.make().state("alpha", "live")
        self.assertIn("lock", str(e.exception))
        self.assertEqual(self.record()["state"], "hold")

    def test_no_secret_in_any_output_or_argument(self):
        self.make().set("alpha", ["php_mem_mb=640"])
        self.make().state("alpha", "stopped")
        seen = "\n".join(self.lines + self.calls())
        for secret in (MASTER, RESEND):
            self.assertNotIn(secret, seen)


class Close(Base):
    def setUp(self):
        super().setUp()
        self.make().create("alpha")
        self.mark = len(self.calls())
        self.lines.clear()
        self.reg = model.Registry(self.cfg["data_dir"], self.cfg["tombstones_dir"], self.cfg["defaults"])

    def tombs(self):
        d = Path(self.cfg["tombstones_dir"])
        return sorted(d.iterdir()) if d.is_dir() else []

    def stamp(self):
        return datetime.fromtimestamp(self.clock, timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def test_close_leaves_a_tombstone_with_the_numbers_and_removes_the_record(self):
        before = self.record()
        self.make().close("alpha")
        path = Path(self.cfg["tombstones_dir"]) / ("alpha.%s.json" % self.stamp())
        self.assertEqual(self.tombs(), [path], "one tombstone, named with the time of the close")
        tomb = json.loads(path.read_text())
        self.assertEqual({k: tomb[k] for k in ("name", "port", "quota_id", "created")}, {k: before[k] for k in ("name", "port", "quota_id", "created")})
        self.assertEqual(tomb["state"], "closed")
        self.assertFalse((self.site_dir() / "site.json").exists())
        self.assertTrue(self.site_dir().is_dir(), "files are the delete tool's business, not the registry's")
        self.assertEqual(self.calls()[self.mark:], [], "close touches no container, unit or nginx")
        self.assertEqual(self.lines, ["closed alpha: its tombstone is %s" % path])

    def test_a_closed_name_may_be_used_again_but_its_port_and_quota_id_never_are(self):
        before = self.record()
        self.make().close("alpha")
        with self.assertRaises(model.Refuse) as e:
            self.make().create("alpha")
        self.assertIn("is not an empty directory", str(e.exception), "the old files are still there until the delete tool has removed them: that is what stops the name, not a rule")
        with self.assertRaises(model.Refuse):
            self.make().apply("alpha")
        shutil.rmtree(self.site_dir())                              # what wp-site-delete does after close
        rec = self.make().create("alpha")
        self.assertEqual(rec["name"], "alpha")
        self.assertGreater(rec["port"], before["port"])
        self.assertGreater(rec["quota_id"], before["quota_id"])
        self.assertEqual(len(self.tombs()), 1, "the old tombstone is kept as history")
        self.assertEqual(self.record()["port"], rec["port"], "the record is the new site's")
        other = self.make().create("bravo")
        self.assertGreater(other["port"], rec["port"])
        names = [r.split()[0] for r in self.make().rows()]
        self.assertEqual(sorted(names), ["alpha", "bravo"], "rows lists the new site once and not the closed one")

    def test_a_second_close_of_the_same_name_leaves_a_second_tombstone(self):
        self.make().close("alpha")
        shutil.rmtree(self.site_dir())
        self.make().create("alpha")
        self.clock += 60
        self.make().close("alpha")
        self.assertEqual(len(self.tombs()), 2)
        ports = sorted(json.loads(p.read_text())["port"] for p in self.tombs())
        self.assertEqual(len(set(ports)), 2, "the two tombstones are two different sites")

    def test_closing_twice_changes_nothing(self):
        self.make().close("alpha")
        before = {p: p.read_text() for p in self.tombs()}
        self.lines.clear()
        self.clock += 60
        self.make().close("alpha")
        self.assertEqual(({p: p.read_text() for p in self.tombs()}, self.lines), (before, ["alpha is already closed; nothing changed"]))

    def test_a_close_interrupted_between_the_tombstone_and_the_removal_is_finished_by_running_it_again(self):
        self.reg.write_tombstone(self.reg.read_site("alpha"), "20261001T000000Z")       # the first half only
        with self.assertRaises(prov.Failed) as e:
            self.make().apply("alpha")
        self.assertIn("a site record and a tombstone", str(e.exception), "the registry says it is broken rather than guessing")
        self.assertIn("wp-site-provision close alpha", str(e.exception))
        self.make().close("alpha")
        self.assertIsNone(self.reg.read_site("alpha"))
        self.assertEqual(len(self.tombs()), 1, "finishing it writes no second tombstone for the same site")
        self.assertEqual(self.make().close("alpha"), None)

    def test_a_site_with_no_record_and_no_tombstone_or_a_bad_name_is_refused(self):
        for name in ("bravo", "old", "../x", "A", ""):
            with self.subTest(name), self.assertRaises(model.Refuse):
                self.make().close(name)
        self.assertEqual(self.record()["name"], "alpha")

    def test_a_corrupt_record_is_not_turned_into_a_tombstone(self):
        (self.site_dir() / "site.json").write_text("{not json")
        with self.assertRaises(prov.Failed):
            self.make().close("alpha")
        self.assertEqual(self.tombs(), [])

    def test_a_record_that_is_a_link_is_not_a_record(self):
        real = self.site_dir() / "site.json"
        moved = self.tmp / "elsewhere.json"
        real.rename(moved)
        real.symlink_to(moved)
        with self.assertRaises(model.Refuse):
            self.make().close("alpha")
        self.assertTrue(moved.exists() and self.tombs() == [])

    def test_the_lock_refuses_a_second_close(self):
        fh = open(Path(self.cfg["lock_dir"]) / "site-alpha.lock", "w")
        self.addCleanup(fh.close)
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(prov.Failed):
            self.make().close("alpha")
        self.assertEqual(self.record()["name"], "alpha")


class Rows(Base):
    def want(self, name, state="hold", **over):
        d = {**self.cfg["defaults"], **over}
        rec = self.record(name)
        return "%s %s %d %d %d %d %s" % (name, state, rec["quota_id"], d["quota_mb"], rec["quota_id"] + self.cfg["platform"]["wp_backup_quota_offset"], d["backup_quota_mb"], d["update_policy"])

    def test_no_sites_is_no_lines_and_the_closed_site_of_the_repo_is_not_one(self):
        self.assertEqual(self.make().rows(), [])

    def test_a_site_is_one_line_of_its_record_over_the_defaults(self):
        self.make().create("alpha")
        self.assertEqual(self.make().rows(), [self.want("alpha")])
        self.make().set("alpha", ["quota_mb=777", "backup_quota_mb=888", "update_policy=auto"])
        self.assertEqual(self.make().rows(), [self.want("alpha", quota_mb=777, backup_quota_mb=888, update_policy="auto")])

    def test_sites_are_sorted_and_a_stopped_site_is_listed_with_its_state(self):
        self.make().create("bravo")
        self.make().create("alpha")
        self.make().state("bravo", "stopped")
        self.assertEqual(self.make().rows(), [self.want("alpha"), self.want("bravo", "stopped")])

    def test_a_closed_site_is_gone_from_the_rows(self):
        self.make().create("alpha")
        self.make().create("bravo")
        self.make().close("alpha")
        self.assertEqual(self.make().rows(), [self.want("bravo")])

    def test_an_entry_of_the_floor_is_not_a_row_even_without_a_state(self):
        self.cfg["closed_floor"] = self.cfg["closed_floor"] + [{"name": "legacy", "port": 9150, "quota_id": 1050, "update_policy": "auto"}]
        self.assertEqual(self.make().rows(), [])

    def test_rows_runs_nothing_and_needs_no_secret(self):
        self.make().create("alpha")
        mark = len(self.calls())
        self.secret.unlink()
        self.assertEqual(self.make().rows(), [self.want("alpha")])
        self.assertEqual(self.calls()[mark:], [], "a read: no docker, systemctl or quota tool")

    def test_a_registry_that_cannot_be_read_is_an_error_and_not_an_empty_list(self):
        self.make().create("alpha")
        reg = model.Registry(self.cfg["data_dir"], self.cfg["tombstones_dir"], self.cfg["defaults"])
        reg.write_tombstone(reg.read_site("alpha"), "20261001T000000Z")                 # a record AND a tombstone (a close that was interrupted)
        with self.assertRaises(prov.Failed):
            self.make().rows()
        reg.site_file("alpha").unlink()
        self.make().create("bravo")
        (self.site_dir("bravo") / "site.json").write_text("{not json")
        with self.assertRaises(prov.Failed):
            self.make().rows()

    def test_the_command_prints_the_lines_and_a_broken_registry_is_exit_1(self):
        out, err = [], []
        run = lambda *a: prov.main(self.cfg, list(a), out=out.append, err=err.append, run=self.host, sleep=self.sleeps.append, probe=lambda u, h: 200, chown=lambda *x: None, now=lambda: self.clock)
        self.assertEqual(run("create", "alpha"), 0)
        out.clear()
        self.assertEqual(run("rows"), 0)
        self.assertEqual(out, [self.want("alpha")])
        (self.site_dir() / "site.json").write_text("{not json")
        out.clear()
        self.assertEqual((run("rows"), out), (1, []))
        self.assertTrue(err)
        self.assertEqual(run("rows", "extra"), 2)


class Render(Base):
    def setUp(self):
        super().setUp()
        self.make().create("alpha")

    def test_everything_matches_after_an_apply(self):
        rows, text = self.make().render("alpha", True)
        self.assertEqual({s for _, s in rows}, {"same"})
        self.assertEqual(len(rows), 7)
        self.assertEqual(text, [])

    def test_a_changed_file_shows_its_difference(self):
        ini = self.site_dir() / "php.ini"
        ini.write_text(ini.read_text().replace("memory_limit", "memory_limitX"))
        rows, text = self.make().render("alpha", True)
        self.assertEqual([p.rsplit("/", 1)[1] for p, s in rows if s == "differs"], ["php.ini"])
        self.assertTrue(any(line.startswith("+") and "memory_limit" in line for line in text), text)
        self.assertEqual(self.make().render("alpha", False)[1], [], "no text without --diff")

    def test_a_secret_file_is_compared_by_hash_and_never_shown(self):
        env = self.site_dir() / ".env"
        env.write_text("MARIADB_PASSWORD=totally-different\n")
        rows, text = self.make().render("alpha", True)
        self.assertEqual([p.rsplit("/", 1)[1] for p, s in rows if s == "differs"], [".env"])
        blob = "\n".join(text)
        self.assertIn("secret file: no text shown", blob)
        self.assertNotIn("totally-different", blob)
        self.assertNotIn(renderer.db_password(renderer.secret_base(MASTER, "alpha", 1)), blob)

    def test_a_missing_file_and_a_drifted_mode(self):
        (self.tmp / "conf.d/wp_site_alpha.conf").unlink()
        os.chmod(self.site_dir() / "compose.yml", 0o666)
        rows, text = self.make().render("alpha", True)
        status = {p.rsplit("/", 1)[1]: s for p, s in rows}
        self.assertEqual((status["wp_site_alpha.conf"], status["compose.yml"]), ("missing", "differs"))
        self.assertTrue(any("mode 666" in line for line in text), text)

    def test_render_changes_nothing(self):
        mark, tree = len(self.calls()), self.tree()
        self.make().render("alpha", True)
        self.assertEqual(self.calls()[mark:], [])
        self.assertEqual(self.tree(), tree)

    def test_no_record_is_refused(self):
        with self.assertRaises(model.Refuse):
            self.make().render("bravo")


class Cli(Base):
    def invoke(self, *argv):
        out, err = [], []
        rc = prov.main(self.cfg, list(argv), out=out.append, err=err.append, run=self.host, sleep=self.sleeps.append, probe=lambda url, host: 200, chown=lambda *a: self.chowns.append(a), now=lambda: self.clock)
        return rc, out, err

    def test_exit_codes(self):
        rc, out, err = self.invoke("create", "alpha", "--set", "php_mem_mb=640")
        self.assertEqual((rc, err), (0, []))
        self.assertIn("alpha is built and on hold; WordPress is not installed in it", out)
        self.assertEqual(self.invoke("apply", "alpha")[0], 0)
        self.assertEqual(self.invoke("render", "alpha")[0], 0)
        (self.site_dir() / "php.ini").write_text("x")
        self.assertEqual(self.invoke("render", "alpha", "--diff")[0], 1, "a difference is a failure, for a script")
        rc, out, err = self.invoke("create", "hub")
        self.assertEqual(rc, 2)
        self.assertIn("refused", err[0])
        self.assertEqual(self.invoke("apply", "bravo")[0], 2)
        self.inject("systemctl.restart")
        self.edit_record(php_mem_mb=700)
        self.assertEqual(self.invoke("apply", "alpha")[0], 1)

    def test_set_and_state_exit_codes(self):
        self.assertEqual(self.invoke("create", "alpha")[0], 0)
        rc, out, err = self.invoke("set", "alpha", "upload_mb=64", "note=hello")
        self.assertEqual((rc, err), (0, []))
        self.assertEqual(self.invoke("state", "alpha", "live")[0], 0)
        self.assertEqual(self.invoke("state", "alpha", "stopped")[0], 0)
        rc, out, err = self.invoke("state", "alpha", "live")
        self.assertEqual(rc, 2)
        self.assertIn("refused", err[0])
        self.assertEqual(self.invoke("set", "alpha", "port=1")[0], 2)
        self.assertEqual(self.invoke("set", "bravo", "upload_mb=64")[0], 2)
        self.inject("systemctl.start")
        rc, out, err = self.invoke("state", "alpha", "hold")
        self.assertEqual(rc, 1, "a failed start is a failure, not a refusal")
        self.assertIn("wp-site-provision apply alpha", err[0])

    def test_close_exit_codes(self):
        self.assertEqual(self.invoke("create", "alpha")[0], 0)
        self.assertEqual(self.invoke("close", "alpha")[0], 0)
        rc, out, err = self.invoke("close", "alpha")
        self.assertEqual((rc, err), (0, []), "closing again is not an error")
        self.assertEqual(self.invoke("close", "bravo")[0], 2)
        self.assertEqual(self.invoke("create", "alpha")[0], 2, "and the name is gone for good")

    def test_bad_usage_is_a_refusal(self):
        for argv in ([], ["nosuchverb"], ["create"], ["create", "a", "extra"], ["apply"], ["set"], ["set", "alpha"], ["state", "alpha"], ["state", "alpha", "closed"], ["state", "alpha", "live", "x"], ["close"], ["close", "a", "b"]):
            with io.StringIO() as buf, unittest.mock.patch("sys.stderr", buf):
                rc, _, _ = self.invoke(*argv)
            self.assertEqual(rc, 2, argv)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
