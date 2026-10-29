#!/usr/bin/env python3
"""wp-site-provision: make a WordPress site on dellwp and keep it matching its record, without Ansible.

    wp-site-provision create <name> [--set key=value ...]   record a new site (always on hold, empty: no WordPress yet) and apply it
    wp-site-provision apply  <name>                         make the VM match the site's record; safe to run again, and the way to finish a create that stopped half-way
    wp-site-provision set    <name> key=value [...]         change editable fields of the record and apply them (label and note are only recorded)
    wp-site-provision state  <name> hold|live|stopped       move the site to that state (model.plan_state: the moves that exist); the final backup is taken before it stops
    wp-site-provision rows                                  one line for every site that is not closed, for the tools that watch or report on the sites (see rows(); read-only)
    wp-site-provision close  <name>                         leave the site's tombstone and remove its record (wp-site-delete does this before it removes the site's files; safe to repeat)
    wp-site-provision render <name> [--diff]                compare what the templates give today with the files on the VM (read-only; a secret file is compared by hash, never shown)

The steps of the Ansible tasks this replaced (tasks/wp_site_one_common.yml, gone; git has it), in their order, as argument lists (no shell): directory, quota project, backup directory and project, database directory, the core seeded from
the image once, the configuration files, the images, the systemd unit, start / restart / php reload as the play decides, the nginx vhost (validated; the previous one is put back on a failure), the
probe through nginx and php, and the three timers. The rules and the record are scripts/wp_site_model.py, the files come from scripts/wp_site_renderer.py. The record holds only what differs from
wp_site_defaults (only what differs is kept), plus the name, the port, the quota id, the state and when it was made. Design: docs/handoff_wp_control_temp.md.

templates/wp/wp_site_provision.py.j2 carries the repo's values and installs this as /usr/local/sbin/wp-site-provision (root only). Exit status: 0 done, 1 it ran and failed, 2 refused (nothing was
changed). Nothing it prints is a secret. Tests: tests/test_wp_site_provision.py runs it against stand-ins for docker, nginx, systemctl and the quota tools."""
import argparse
import difflib
import fcntl
import grp
import hashlib
import os
import pwd
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import wp_site_model as model
import wp_site_renderer as renderer

SEED_SCRIPT = ("cp -a /usr/src/wordpress/. /var/www/html/ && chown -R 0:0 /var/www/html && mkdir -p /var/www/html/wp-content/uploads && "
               "chown {php}:{php} /var/www/html/wp-content/uploads && touch /var/www/html/wp-config.php && chmod 0644 /var/www/html/wp-config.php")
TIMERS = (("cron", lambda state: state == "live"), ("backup", lambda state: state != "stopped"), ("update", lambda state: state != "stopped"))


class Failed(Exception):
    """The work was started and something went wrong; the message says what and what to do. Running `apply` again is the way on."""


def tail(text, n=600):
    text = (text or "").strip()
    return text[-n:]


def default_probe(url, host, timeout=10):
    """The HTTP status of a request to the loopback port with the site's host name, or 0 when nothing answered."""
    req = urllib.request.Request(url, headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return 0


class Provisioner:
    def __init__(self, cfg, run=subprocess.run, sleep=time.sleep, probe=default_probe, chown=os.chown, out=print, now=time.time):
        self.cfg, self.run, self.sleep, self.probe, self.chown, self.out, self.now = cfg, run, sleep, probe, chown, out, now
        self.defaults, self.platform = cfg["defaults"], cfg["platform"]
        self.floor = [{**self.defaults, **k, "state": "closed"} for k in cfg["closed_floor"]]            # the ports and quota ids of the sites of the old engine: reserved, every entry closed
        self.registry = model.Registry(cfg["data_dir"], cfg["tombstones_dir"], self.defaults)

    # ---------------------------------------------------------------------------------------------------------------------------------- plumbing
    def cmd(self, name, *args, check=True, timeout=None):
        argv = [self.cfg["tools"][name], *args]
        p = self.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout or self.cfg["command_timeout_s"], env={"PATH": self.cfg["path"], "LANG": "C"})
        if check and p.returncode != 0:
            raise Failed("%s %s failed (exit %d): %s" % (name, args[0] if args else "", p.returncode, tail(p.stderr or p.stdout)))
        return p

    @contextmanager
    def lock(self, name):
        d = Path(self.cfg["lock_dir"])
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        fh = open(d / (name + ".lock"), "w")
        try:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Failed("another wp-site-provision holds the lock for %s; wait for it to finish" % name)
            yield
        finally:
            fh.close()

    def ids(self, owner, group):
        uid = owner if isinstance(owner, int) else pwd.getpwnam(owner).pw_uid
        gid = group if isinstance(group, int) else grp.getgrnam(group).gr_gid
        return uid, gid

    def site_dir(self, name):
        return Path(self.cfg["data_dir"]) / name

    def secrets(self):
        try:
            master = renderer.read_secret_file(self.cfg["master_secret_file"])
            key_file = Path(self.cfg["resend_key_file"])
            resend = renderer.read_secret_file(key_file) if key_file.exists() or key_file.is_symlink() else None
        except renderer.SecretFileError as e:
            raise Failed(str(e))
        return master, resend

    def files(self, site):
        """The rendered files of a site (merged over the defaults), or Failed with the reason: no secret file, a value the platform lacks, a field a template reads that the site does not have."""
        master, resend = self.secrets()
        try:
            return renderer.Renderer(self.cfg["template_dir"], self.platform, master, resend).files(site)
        except renderer.RenderError as e:
            raise Failed(str(e))

    # ------------------------------------------------------------------------------------------------------------------------------- the registry
    def all_merged(self):
        """Every site the registry or the repo knows, each over the defaults: one entry for every site and for every time a name was closed (a name may have a site and tombstones of earlier sites).
        The same site seen in two places must agree (model.everything checks); a site record that has a tombstone with the same port and quota id is a close that was interrupted, and is broken."""
        model.everything(self.registry, self.floor)
        by = {(k["port"], k["quota_id"]): model.merged(self.defaults, k) for k in self.floor}
        records = {(r["port"], r["quota_id"]): r for r in self.registry.sites()}
        for t in self.registry.tombstones():
            key = (t["port"], t["quota_id"])
            if key in records:
                raise model.RegistryError("%s has a site record and a tombstone (a close that was interrupted: run wp-site-provision close %s)" % (t["name"], t["name"]))
            by[key] = model.merged(self.defaults, t)
        by.update({key: model.merged(self.defaults, r) for key, r in records.items()})
        return sorted(by.values(), key=lambda s: (s["name"], s["port"]))

    def check_fit(self, sites):
        broken = model.fit_report(sites, self.cfg["fit"])
        if broken:
            raise model.Refuse("the sites would not fit this VM: " + "; ".join(broken))

    # --------------------------------------------------------------------------------------------------------------------------------- create
    @staticmethod
    def parse_sets(sets):
        """{key: value} from key=value items, each key once and each value through the rule of its field."""
        values = {}
        for item in sets:
            key, eq, raw = item.partition("=")
            if not eq or key in values:
                raise model.Refuse("%s: give each key once, as key=value" % item)
            value, why = model.validate_value(key, raw)
            if why:
                raise model.Refuse("%s: %s" % (key, why))
            values[key] = value
        return values

    def create(self, name, sets=()):
        values = self.parse_sets(sets)
        with self.lock("registry"), self.lock("site-" + name):
            try:
                everything = model.everything(self.registry, self.floor)
                merged = self.all_merged()
            except model.RegistryError as e:
                raise Failed(str(e))
            why = model.check_name(name, self.cfg["fit"]["reserved_names"], {s["name"] for s in everything if s["state"] != "closed"})
            if why:
                raise model.Refuse("'%s': %s" % (name, why))
            d = self.site_dir(name)
            if d.exists() and (d.is_symlink() or not d.is_dir() or any(d.iterdir())):
                raise model.Refuse("%s exists and is not an empty directory; nothing was changed" % d)
            port, quota_id = model.next_numbers(everything)
            rec = {"schema": model.SCHEMA, "name": name, "port": port, "quota_id": quota_id, "state": "hold", "created": datetime.fromtimestamp(self.now(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **values}
            model.check_record(rec, self.defaults, "new site")
            self.check_fit(merged + [model.merged(self.defaults, rec)])
            self.preflight()
            self.files(model.merged(self.defaults, rec))     # the secrets and every value the templates need, before anything is made
            self.ensure_dir(d, "root", "root", 0o755)
            self.ensure_project(d, quota_id, model.merged(self.defaults, rec)["quota_mb"])
            self.registry.write_site(rec)
            self.out("recorded %s: port %d, quota project %d, on hold" % (name, port, quota_id))
            try:
                self.apply_locked(name)
            except Failed as e:
                raise Failed("%s is recorded but not fully built: %s. Fix that and run: wp-site-provision apply %s" % (name, e, name))
        return rec

    # ----------------------------------------------------------------------------------------------------------------------------- set and state
    def change(self, name, changes, check=None, before=None):
        """Put `changes` into the site's record and make the VM match: the one path of `set` and `state`. `check(rec)` runs first, on the record as it is, and may refuse. Everything else that can be
        refused is refused before anything is written (the new record is valid, the sites still fit, the secrets and templates render); `before(rec)` runs after those checks and before the write (the
        final backup: if it fails the record is untouched). If the apply then fails the record stays changed, as after a create that stops half-way, and the error says to run apply. Returns the
        steps `apply` took."""
        if not model.NAME_RE.fullmatch(name):
            raise model.Refuse("'%s' is not a site name" % name)
        with self.lock("registry"), self.lock("site-" + name):
            try:
                rec = self.registry.read_site(name)
                merged = self.all_merged()
            except model.RegistryError as e:
                raise Failed(str(e))
            if rec is None:
                raise model.Refuse("%s has no site record (create it first)" % name)
            if check:
                check(rec)
            new = {**rec, **changes}
            if new == rec:
                self.out("%s already has that; nothing changed" % name)
                return []
            model.check_record(new, self.defaults, "new record for %s" % name)
            self.check_fit([m for m in merged if m["name"] != name] + [model.merged(self.defaults, new)])
            registry_only = set(changes) <= set(model.TEXT_KEYS)
            if not registry_only:
                self.preflight()
                self.files(model.merged(self.defaults, new))
            if before:
                before(rec)
            self.registry.write_site(new)
            self.out("recorded %s for %s" % (", ".join("%s=%s" % (k, changes[k]) for k in sorted(changes)), name))
            if registry_only:
                return []
            try:
                return self.apply_locked(name)
            except Failed as e:
                raise Failed("%s is recorded but the VM does not match yet: %s. Fix that and run: wp-site-provision apply %s" % (name, e, name))

    def set(self, name, sets):
        values = self.parse_sets(sets)
        if not values:
            raise model.Refuse("give at least one key=value")
        return self.change(name, values)

    def state(self, name, target):
        """Move a site to hold, live or stopped (model.plan_state says which moves exist). Before it stops, the final backup is taken, and a failed backup stops everything: the record is not changed."""
        plan = []

        def check(rec):
            plan[:] = model.plan_state(rec["state"], target)

        def backup(rec):
            if "backup" in plan:
                self.out("taking the final backup of %s" % name)
                self.cmd("backup", name, "--tag=final", timeout=self.cfg["backup_timeout_s"])
        return self.change(name, {"state": target}, check=check, before=backup)

    ROW_FIELDS = "name state quota_id quota_mb backup_quota_id backup_quota_mb update_policy"

    def rows(self):
        """One line per site that is not closed, the facts the monitors and the status tool need, from the registry (and the repo's list while it still declares sites), each over the defaults:
        `name state quota_id quota_mb backup_quota_id backup_quota_mb update_policy`, separated by single spaces, sorted by name. The backup project is the site's plus the platform's offset, as in
        the play. Read-only. It raises Failed when the registry cannot be read (a record that is not valid, a site with both a record and a tombstone): a monitor that cannot read its input
        must say so, and a caller that treats no output as no sites would hide a broken registry."""
        try:
            sites = [s for s in self.all_merged() if s["state"] != "closed"]
        except model.RegistryError as e:
            raise Failed(str(e))
        offset = int(self.platform["wp_backup_quota_offset"])
        return ["%s %s %d %d %d %d %s" % (s["name"], s["state"], int(s["quota_id"]), int(s["quota_mb"]), int(s["quota_id"]) + offset, int(s["backup_quota_mb"]), s["update_policy"]) for s in sites]

    def close(self, name):
        """Leave the site's tombstone, <name>.<UTC stamp>.json, and remove its record, so its port and quota id are never given out again (its name may be). wp-site-delete calls this after its archive
        is verified and before it removes anything. The tombstone is written first (atomically) and the record removed second; if that is interrupted between the two, the registry reports the site
        as broken until this runs again, and running it again finishes it without a second tombstone. A name with no record and a tombstone is already closed."""
        if not model.NAME_RE.fullmatch(name):
            raise model.Refuse("'%s' is not a site name" % name)
        with self.lock("registry"), self.lock("site-" + name):
            try:
                rec = self.registry.read_site(name)
            except model.RegistryError as e:
                raise Failed(str(e))
            if rec is None:
                if self.registry.tombstone_files(name):
                    self.out("%s is already closed; nothing changed" % name)
                    return
                raise model.Refuse("%s has no site record and no tombstone" % name)
            try:
                same = [p for p in self.registry.tombstone_files(name) if self.registry.load(p).get("port") == rec["port"] and self.registry.load(p).get("quota_id") == rec["quota_id"]]
            except model.RegistryError as e:
                raise Failed(str(e))
            if same:
                tomb = same[-1]                      # an earlier close of this site was interrupted after the tombstone: no second one
            else:
                tomb = self.registry.write_tombstone(rec, datetime.fromtimestamp(self.now(), timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            self.registry.site_file(name).unlink()
            self.out("closed %s: its tombstone is %s" % (name, tomb))

    # ---------------------------------------------------------------------------------------------------------------------------------- apply
    def apply(self, name):
        if not model.NAME_RE.fullmatch(name):
            raise model.Refuse("'%s' is not a site name" % name)
        with self.lock("site-" + name):
            return self.apply_locked(name)

    def preflight(self):
        base = Path(self.platform["wp_vhost_dir"]) / "wp_base.conf"
        if not base.is_file():
            raise Failed("%s is missing: run dellwp/nginx_base_dellwp.yml first" % base)
        if not Path(self.platform["wp_mu_dir"]).is_dir():
            raise Failed("%s is missing: run dellwp/wp_sites_dellwp.yml first (it deploys the must-use plugins every site mounts)" % self.platform["wp_mu_dir"])

    def apply_locked(self, name):
        try:
            rec = self.registry.read_site(name)
            merged = self.all_merged()
        except model.RegistryError as e:
            raise Failed(str(e))
        if rec is None:
            raise model.Refuse("%s has no site record (create it first)" % name)
        site = model.merged(self.defaults, rec)
        self.check_fit(merged)
        self.preflight()
        files = self.files(site)
        by = {f["path"].rsplit("/", 1)[1]: f for f in files}
        p, d, state = self.platform, self.site_dir(name), site["state"]
        done = []

        def note(text):
            done.append(text)
            self.out(text)

        self.ensure_dir(d, "root", "root", 0o755)
        flag = d / "stopped.flag"
        if state == "stopped" and not flag.exists():
            self.put({"path": str(flag), "content": "", "owner": "root", "group": "root", "mode": 0o644})
        elif state != "stopped" and flag.exists():
            flag.unlink()
        if self.ensure_project(d, site["quota_id"], site["quota_mb"]):
            note("set the quota project of %s" % d)
        backups = Path(p["wp_backup_dir"]) / name
        self.ensure_dir(backups, "root", "root", 0o700)
        if self.ensure_project(backups, site["quota_id"] + int(p["wp_backup_quota_offset"]), site["backup_quota_mb"]):
            note("set the quota project of %s" % backups)
        self.ensure_dir(d / "db", p["wp_db_uid"], p["wp_db_uid"], 0o700)
        self.own_tree(d / "db", p["wp_db_uid"])
        if not (d / "html/wp-includes/version.php").exists():
            self.cmd("docker", "run", "--rm", "--memory", "256m", "--memory-swap", "256m", "--entrypoint", "sh", "-v", "%s/html:/var/www/html" % d, p["wp_image_php"], "-c",
                     SEED_SCRIPT.format(php=p["wp_php_uid"]), timeout=300)
            note("seeded WordPress core from the image")
        (d / "html/wp-config-docker.php").unlink(missing_ok=True)

        wrote = {fname: self.put(by[fname]) for fname in (".env", "wp-config.php", "php.ini", "fpm-pool.conf", "compose.yml")}
        for fname, changed in wrote.items():
            if changed:
                note("wrote %s" % fname)
        for image in (p["wp_image_php"], p["wp_image_db"]):
            if self.cmd("docker", "image", "inspect", image, check=False).returncode != 0:
                self.cmd("docker", "pull", image, timeout=900)
                note("pulled %s" % image)

        unit_name = "wp-%s" % name
        unit_changed = self.put(by["%s.service" % unit_name])
        if unit_changed:
            self.cmd("systemctl", "daemon-reload")
            note("wrote the unit and reloaded systemd")
        self.switch(unit_name, "enable" if state != "stopped" else "disable", note=note)
        restart = wrote[".env"] or wrote["compose.yml"]
        php_changed = wrote["wp-config.php"] or wrote["php.ini"] or wrote["fpm-pool.conf"]
        stack_timeout = int(p["wp_stack_start_timeout_s"]) + int(p["wp_stack_stop_timeout_s"]) + 60
        active = self.cmd("systemctl", "is-active", "--quiet", unit_name, check=False).returncode == 0
        if state == "stopped":
            if active:
                self.cmd("systemctl", "stop", unit_name, timeout=stack_timeout)
                note("stopped the stack")
            (d / "maintenance.flag").unlink(missing_ok=True)
        elif restart:
            self.cmd("systemctl", "restart", unit_name, timeout=stack_timeout)
            note("restarted the stack (its compose file or database settings changed)")
        elif not active:
            self.cmd("systemctl", "start", unit_name, timeout=stack_timeout)
            note("started the stack")
        elif php_changed:
            self.cmd("systemctl", "reload", unit_name, timeout=stack_timeout)
            note("recreated the php container (its mounted files changed)")

        self.vhost(name, by["wp_site_%s.conf" % name], note)
        if state != "stopped":
            self.probe_site(site, note)
        for kind, wanted in TIMERS:
            self.switch("wp-%s@%s.timer" % (kind, name), "enable" if wanted(state) else "disable", timer=True, note=note)
        if not done:
            self.out("%s already matches its record; nothing changed" % name)
        return done

    # ------------------------------------------------------------------------------------------------------------------------------ the steps
    def ensure_dir(self, path, owner, group, mode):
        path = Path(path)
        if path.is_symlink():
            raise Failed("%s is a link" % path)
        uid, gid = self.ids(owner, group)
        try:
            path.mkdir(exist_ok=True)
        except FileNotFoundError:
            raise Failed("%s cannot be made: %s does not exist (run dellwp/wp_data_disk_dellwp.yml)" % (path, path.parent))
        st = path.stat()
        if (st.st_mode & 0o7777) != mode:
            os.chmod(path, mode)
        if (st.st_uid, st.st_gid) != (uid, gid):
            self.chown(path, uid, gid)

    def own_tree(self, path, uid):
        """Everything below `path` to `uid` (the play recurses so a changed wp_db_uid reaches files MariaDB already made); the mode of the data files is left alone."""
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                q = os.path.join(root, n)
                st = os.lstat(q)
                if (st.st_uid, st.st_gid) != (uid, uid):
                    self.chown(q, uid, uid)

    def put(self, spec):
        """Write one file atomically with its owner, group and mode; True when the content changed. A file that is already right is left alone (its mode and owner are put right silently)."""
        path = Path(spec["path"])
        uid, gid = self.ids(spec["owner"], spec["group"])
        data = spec["content"].encode()
        if path.is_symlink():
            raise Failed("%s is a link" % path)
        if path.exists() and path.read_bytes() == data:
            st = path.stat()
            if (st.st_mode & 0o7777) != spec["mode"]:
                os.chmod(path, spec["mode"])
            if (st.st_uid, st.st_gid) != (uid, gid):
                self.chown(path, uid, gid)
            return False
        tmp = path.with_name(".%s.new-%d" % (path.name, os.getpid()))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
                os.fchmod(fh.fileno(), spec["mode"])
            self.chown(tmp, uid, gid)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True

    def ensure_project(self, path, project_id, mb):
        """Put `path` in ext4 project `project_id` with a hard limit of `mb`; True when something changed (tasks/wp_project_quota_common.yml)."""
        changed = False
        current = self.cmd("lsattr", "-dp", str(path)).stdout.split()
        if not current or int(current[0]) != int(project_id):
            self.cmd("chattr", "-R", "+P", "-p", str(project_id), str(path))
            changed = True
        want = int(mb) * 1024
        have = -1
        for line in self.cmd("repquota", "-P", "-n", self.platform["wp_data_mount"]).stdout.splitlines():
            if line.startswith("#%d " % int(project_id)) or line.startswith("#%d\t" % int(project_id)):
                tokens = line.split()
                have = int(tokens[4]) if len(tokens) > 4 and tokens[4].isdigit() else -1
        if have != want:
            self.cmd("setquota", "-P", str(project_id), "0", str(want), "0", "0", self.platform["wp_data_mount"])
            changed = True
        return changed

    def switch(self, unit, action, timer=False, note=None):
        """Enable or disable a unit (and for a timer start or stop it too) only when it is not already so."""
        enabled = self.cmd("systemctl", "is-enabled", "--quiet", unit, check=False).returncode == 0
        active = timer and self.cmd("systemctl", "is-active", "--quiet", unit, check=False).returncode == 0
        want = action == "enable"
        if want and (not enabled or (timer and not active)):
            self.cmd("systemctl", "enable", *(["--now"] if timer else []), unit)
        elif not want and (enabled or (timer and active)):
            self.cmd("systemctl", "disable", *(["--now"] if timer else []), unit)
        else:
            return
        if note:
            note("%sd %s" % (action, unit))

    def vhost(self, name, spec, note):
        """Write the vhost, test nginx, reload it. A configuration nginx rejects is taken back (the previous vhost restored, or a first one removed) and the apply stops."""
        path = Path(spec["path"])
        before = path.read_bytes() if path.exists() else None
        if not self.put(spec):
            return
        test = self.cmd("nginx", "-t", check=False)
        if test.returncode != 0:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(before)
            raise Failed("the new vhost for %s failed nginx -t; the previous one was put back and nginx was not reloaded: %s" % (name, tail(test.stderr or test.stdout)))
        self.cmd("systemctl", "reload", "nginx")
        note("wrote the vhost and reloaded nginx")

    def probe_site(self, site, note):
        url = "http://127.0.0.1:%s/wp-admin/install.php" % self.platform["wp_nginx_listen_port"]
        host = "%s.%s" % (site["name"], self.platform["wp_domain"])
        status = 0
        for attempt in range(int(self.cfg["probe_tries"])):
            status = self.probe(url, host)
            if status == 200:
                return
            if attempt + 1 < int(self.cfg["probe_tries"]):
                self.sleep(self.cfg["probe_delay_s"])
        raise Failed("%s did not answer 200 through nginx and php (last status %s): look at systemctl status wp-%s and docker logs" % (host, status, site["name"]))

    # ---------------------------------------------------------------------------------------------------------------------------------- render
    def render(self, name, show_diff=False):
        """[(path, 'same' | 'differs' | 'missing')] for the files of the site's record, and the text of the differences (never of a secret file)."""
        rec = self.registry.read_site(name)
        if rec is None:
            raise model.Refuse("%s has no site record" % name)
        rows, text = [], []
        for f in self.files(model.merged(self.defaults, rec)):
            path = Path(f["path"])
            if not path.exists():
                rows.append((f["path"], "missing"))
                continue
            now = path.read_bytes()
            want = f["content"].encode()
            st = path.stat()
            same = now == want and (st.st_mode & 0o7777) == f["mode"]
            rows.append((f["path"], "same" if same else "differs"))
            if not same and show_diff:
                if f["secret"]:
                    text.append("%s: content hash %s, wanted %s (secret file: no text shown)" % (f["path"], hashlib.sha256(now).hexdigest()[:12], hashlib.sha256(want).hexdigest()[:12]))
                elif now != want:
                    text.extend(difflib.unified_diff(now.decode(errors="replace").splitlines(), f["content"].splitlines(), "on the VM: " + f["path"], "rendered now", lineterm=""))
                else:
                    text.append("%s: mode %o, wanted %o" % (f["path"], st.st_mode & 0o7777, f["mode"]))
        return rows, text


def main(cfg, argv, out=print, err=None, **kw):
    err = err or (lambda s: print(s, file=sys.stderr))
    ap = argparse.ArgumentParser(prog="wp-site-provision", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("create")
    c.add_argument("name")
    c.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="one of: " + ", ".join(model.EDITABLE))
    a = sub.add_parser("apply")
    a.add_argument("name")
    st = sub.add_parser("set")
    st.add_argument("name")
    st.add_argument("pairs", nargs="+", metavar="KEY=VALUE", help="one of: " + ", ".join(model.EDITABLE))
    sw = sub.add_parser("state")
    sw.add_argument("name")
    sw.add_argument("target", choices=("hold", "live", "stopped"))
    sub.add_parser("rows")
    cl = sub.add_parser("close")
    cl.add_argument("name")
    r = sub.add_parser("render")
    r.add_argument("name")
    r.add_argument("--diff", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    prov = Provisioner(cfg, out=out, **kw)
    try:
        if args.verb == "create":
            prov.create(args.name, args.set)
            out("%s is built and on hold; WordPress is not installed in it" % args.name)
        elif args.verb == "apply":
            prov.apply(args.name)
        elif args.verb == "set":
            prov.set(args.name, args.pairs)
        elif args.verb == "state":
            prov.state(args.name, args.target)
        elif args.verb == "close":
            prov.close(args.name)
        elif args.verb == "rows":
            for line in prov.rows():
                out(line)
        else:
            rows, text = prov.render(args.name, args.diff)
            for path, status in rows:
                out("%-8s %s" % (status, path))
            for line in text:
                out(line)
            return 0 if all(s == "same" for _, s in rows) else 1
    except model.Refuse as e:
        err("wp-site-provision: refused: %s" % e)
        return 2
    except (Failed, model.RegistryError) as e:
        err("wp-site-provision: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit("run wp-site-provision (templates/wp/wp_site_provision.py.j2 carries the configuration)")
