#!/usr/bin/env python3
"""wp-site-import: bring a .wpress archive into a WordPress site on dellwp, step by step. Runs on the VM as root (rendered wrapper: templates/wp/wp_site_import.py.j2; this module and
wp_wpress.py are copied to the tools library by tasks/wp_tools_common.yml).

    wp-site-import <site> <file.wpress> [--plan]     (the file must sit in the intake directory)
    wp-site-import reset <site>                      (asks for the site name; removes the unpacked copy)

    wp-site-import approve <site> [--reject a,b] [--accept-failures]
    wp-site-import report <site>                     (prints the report again)
    wp-site-import receive <file.wpress>             (the file on standard input: how the laptop sends it; the intake directory is root-only)
    wp-site-import discard <file.wpress> [--if-present]   (removes a file from the intake directory, unless an import was made from it; --if-present: silent when it is not there)
    wp-site-import resolve <token>                   (what the phone's Approve and Deny buttons lead to, through the listener: scripts/wp_approval_listener.py)

When an import has finished, a notification goes to the phone (the independent ntfy) with a Deny button and, only if the import is clean (no failed step, checksums ok, no ClamAV find, no
suspicious pattern), an Approve button. A token is signed, expires, is bound to the exact report and is used once; it can approve a clean import or deny, and nothing else: --accept-failures,
--reject and going live are not available to it. Without the key file the run says that phone approval is off and goes on.

After the unpack the run goes on by itself: load the SQL (the migration plugin's table prefix is swapped for the site's), copy the data (uploads, languages), reinstall every plugin and theme
wordpress.org offers (the archive's version if it is still offered, else the newest, and the report says so), run the WP-CLI steps (update-db, the URLs, Elementor, transients, rewrite rules),
scan, write the report, take the first backup and stop. The site stays on hold. CODE THAT IS NOT FROM WORDPRESS.ORG (plugins and themes it does not offer, mu-plugins) STAYS IN THE WORK
DIRECTORY until `approve` copies it in, so no step before your approval runs a line of it; `approve` also deletes the intake file and the work directory. `reset` undoes a run that is not
approved yet, by the manifest in PLAN.json and a snapshot of wp-content taken before the load: it drops the tables, removes what the run copied or installed, and sweeps whatever else appeared
under wp-content meanwhile (what the plugins made when they first ran, WordPress's upgrade directory), so the site's tree is what it was before the import.
Refuses (before anything is written): a site that is not in wp_sites or not on hold; a site with a database that is not empty; a file that is not a plain *.wpress directly inside the intake directory;
an archive the reader rejects (wp_wpress.judge: core newer than ours, a plugin that needs a newer PHP, an unsafe path, a multisite, a truncated file); a work directory left by an earlier run; not enough
room in the intake quota or in the site's quota. --plan does all of that and prints the plan, writing nothing.
The work directory is intake/work-<site>, inside the intake quota project: /srv/wp/scratch is shared with the restore drill, which sets and then removes its project limit around its own run.
What is unpacked: the database, package.json, uploads, mu-plugins, languages, and an active plugin or theme only if wordpress.org does not offer it (you decide on it; it is unpacked for the scan).
Everything else is counted and named in the plan and not written: plugins and themes wordpress.org offers (they are reinstalled), inactive ones, the migration plugin and LiteSpeed Cache,
caches, upgrade/, the migration plugin's own backups, and any directory not on the list."""
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

import wp_approvals as ap
import wp_wpress as w

SITE_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}\Z")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.wpress\Z")
CHUNK = 1 << 20
MIB = 1 << 20
# Directories of wp-content that are not carried over, whatever they hold. The first two are in the practice file (upgrade/ held 36 MB of a half-finished update); ai1wm-backups is the
# migration plugin's own backup folder.
DROP_DIRS = {"upgrade": "upgrade/ (a half-finished update)", "cache": "cache directory", "litespeed": "LiteSpeed cache data", "ai1wm-backups": "the migration plugin's own backups"}
# Directories of wp-content that are copied as they are (mu-plugins is code: the report says so)
KEEP_DIRS = ("uploads", "mu-plugins", "languages")
DB_SH = 'MYSQL_PWD="$MARIADB_PASSWORD" exec mariadb -u"$MARIADB_USER" -N -B -e "$SQL" "$MARIADB_DATABASE"'
LOAD_SH = 'MYSQL_PWD="$MARIADB_PASSWORD" exec mariadb -u"$MARIADB_USER" "$MARIADB_DATABASE"'
# What the load starts with: the dump is a MySQL dump (zero dates, no strict mode), and checks the load does not need are off. Nothing else of the session is touched.
PRELUDE = b"SET SESSION sql_mode='';\nSET SESSION foreign_key_checks=0;\nSET SESSION unique_checks=0;\nSET NAMES utf8mb4;\n"
# MySQL 8's default collation does not exist in MariaDB; the dump of the practice file has none of it, an archive from MySQL 8 may
COLLATION_FIXES = ((b"utf8mb4_0900_ai_ci", b"utf8mb4_unicode_520_ci"), (b"utf8mb4_0900_as_cs", b"utf8mb4_unicode_520_ci"))
SERVMASK = b"SERVMASK_PREFIX_"
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?\Z")
PREFIX_RE = re.compile(r"\$table_prefix\s*=\s*'([A-Za-z0-9_]+)'")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
STAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z\Z")
SNAPSHOT_LINE = re.compile(r"^OK: snapshot ([0-9]{8}T[0-9]{6}Z) of ([a-z][a-z0-9-]{1,23}) ", re.M)
# A plugin or theme folder name that comes out of the archive and goes into a path: a plain name, never "..", never a slash. (A wordpress.org slug is stricter: SLUG_RE.)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.+-]*\Z")
# A heuristic, not a verdict: patterns that legitimate plugins use too. A hit is a place to look, an absence proves nothing.
SCAN_PATTERNS = ((rb"\beval\s*\(", "eval()"), (rb"\bbase64_decode\s*\(", "base64_decode()"), (rb"\bgzinflate\s*\(|\bgzuncompress\s*\(|\bstr_rot13\s*\(", "gzinflate/str_rot13"),
                 (rb"\b(?:shell_exec|passthru|proc_open|popen|pcntl_exec)\s*\(", "shell execution"), (rb"\b(?:system|exec)\s*\(", "system()/exec()"), (rb"\bassert\s*\(\s*\$", "assert($var)"))
SCAN_EXT = (".php", ".phtml", ".phar", ".php3", ".php4", ".php5", ".php7", ".inc", ".pht")
SCAN_BYTES = 8 * MIB        # a file is read this far for the pattern scan, so one huge file cannot take the VM's memory
# A name that PHP or the web server would act on. Under uploads/ nothing of the kind is copied: the archive's PHP there would be code that can be included by a database option.
EXEC_NAME = re.compile(r"\.(php\d?|phtml|phar|pht|inc)(\.|$)", re.I)
UPLOADS_BLOCKED = "PHP or hidden file under uploads (code or server settings): not copied"
LANG_OK = (".mo", ".po", ".json")
LANG_BLOCKED = "languages file that is not a translation (.mo, .po, .json; a .l10n.php file is code): not copied"
HOST_MAX = 253
TREE_LIMIT = 100000       # entries of wp-content recorded before the load; a bigger tree is not swept on undo (the manifest still is)
# The archive's cached update checks. They describe the FRIEND's site (which plugins it had, which had an update waiting) and WordPress core reads them when it decides whether a plugin is
# "already installed": with Elementor in there as "update available", `wp plugin install elementor` says "Plugin already installed.", installs nothing and exits 0 (dellwp 2026-10-25).
UPDATE_STATE = ("_site_transient_update_plugins", "_site_transient_timeout_update_plugins", "_site_transient_update_themes", "_site_transient_timeout_update_themes",
                "_site_transient_update_core", "_site_transient_timeout_update_core")


class Refuse(Exception):
    pass


def dropped_plugin(slug):
    """Never carried over, active or not: the migration plugin itself, and the cache plugin of a web server we do not run."""
    return slug == "litespeed-cache" or slug.startswith("all-in-one-wp-migration")


def rel(name, path):
    return name if path in (".", "") else path + "/" + name


def classify(name, path, plugins, themes):
    """('keep', destination under the work directory) or ('skip', reason). The reason is what the plan counts under."""
    if path in (".", ""):
        return ("keep", name) if name in ("package.json", "database.sql") else ("skip", "archive housekeeping file")
    parts = path.split("/")
    top = parts[0]
    if top in DROP_DIRS:
        return "skip", DROP_DIRS[top]
    if top in ("plugins", "themes"):
        kind, table = ("plugin", plugins) if top == "plugins" else ("theme", themes)
        slug = parts[1] if len(parts) > 1 else name.rsplit(".", 1)[0]
        decision = table.get(slug)
        if decision == "approve":
            return "keep", "wp-content/" + rel(name, path)
        if decision == "reinstall":
            return "skip", "%s %s: reinstalled from wordpress.org" % (kind, slug)
        if decision == "dropped":
            return "skip", "%s %s: not carried over" % (kind, slug)
        return "skip", "%s %s: inactive" % (kind, slug)
    if top == "uploads":
        if name.startswith(".") or any(x.startswith(".") for x in parts[1:]) or EXEC_NAME.search(name):
            return "skip", UPLOADS_BLOCKED
        return "keep", "wp-content/" + rel(name, path)
    if top == "languages":
        return ("keep", "wp-content/" + rel(name, path)) if name.lower().endswith(LANG_OK) and not name.startswith(".") else ("skip", LANG_BLOCKED)
    if top in KEEP_DIRS:
        return "keep", "wp-content/" + rel(name, path)
    return "skip", "unknown directory '%s': not copied" % w.printable(top, limit=40)


def build_plan(a, cfg, get):
    """Everything decided from the archive and wordpress.org, before anything is written. Raises Refuse when the archive may not be imported."""
    our_wp, our_php = w.pin(cfg["image_pin"])
    rows, notes, nogo, facts = w.judge(a, our_wp, our_php, get)
    if nogo:
        raise Refuse("the archive may not be imported:\n  " + "\n  ".join(nogo))
    odd = [r["slug"] for r in rows if not NAME_RE.match(r["slug"])]
    if odd:
        raise Refuse("the archive names a plugin or theme '%s', which is not a plain folder name: refusing" % odd[0][:60])
    plugins, themes = {}, {}
    for r in rows:
        table = plugins if r["kind"] == "plugin" else themes
        if r["kind"] == "plugin" and dropped_plugin(r["slug"]):
            table[r["slug"]] = "dropped"
        else:
            table[r["slug"]] = "reinstall" if r["onorg"] else "approve"
    keep, tally, keep_bytes = [], {}, 0
    for name, path, size, off in a.entries:
        action, what = classify(name, path, plugins, themes)
        if action == "keep":
            keep.append((name, path, size, off, what))
            keep_bytes += size
        else:
            t = tally.setdefault(what, [0, 0])
            t[0] += 1
            t[1] += size
    sql_bytes = sum(k[2] for k in keep if k[4] == "database.sql")
    return {"facts": facts, "rows": rows, "notes": notes, "plugins": plugins, "themes": themes, "keep": keep, "keep_bytes": keep_bytes, "tally": tally, "sql_bytes": sql_bytes}


def tree_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def check_site(cfg, site):
    if not SITE_RE.match(site or ""):
        raise Refuse("'%s' is not a site name" % site)
    s = cfg["sites"].get(site)
    if s is None:
        raise Refuse("'%s' is not in wp_sites (add it and run dellwp/wp_sites_dellwp.yml)" % site)
    if s["state"] != "hold":
        raise Refuse("%s is %s: an import goes into a site on hold (python3 scripts/wp_sites.py modify %s --set state=hold)" % (site, s["state"], site))
    d = Path(cfg["data_mount"]) / "sites" / site
    if not d.is_dir() or d.is_symlink():
        raise Refuse("%s is not a real directory: run dellwp/wp_sites_dellwp.yml so the site exists" % d)
    if (d / "stopped.flag").exists():
        raise Refuse("%s is stopped (it has no containers): start it first" % site)
    return d


def db_name(site):
    return "wp_%s_db" % site.replace("-", "_")


def db_query(site, sql, run):
    """One statement as the site's own database user, inside its container. Returns the first line of the answer; raises Refuse when the container does not answer."""
    p = run(["docker", "exec", "-e", "SQL=" + sql, db_name(site), "sh", "-c", DB_SH], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        raise Refuse("could not read the database of %s (%s): is its stack up? systemctl status wp-%s" % (site, (p.stderr or out or "no answer").strip()[:200], site))
    return out.split("\n")[0].strip()


def db_query_all(site, sql, run):
    """Every line of the answer to one statement, as the site's database user. Raises Refuse when the container does not answer."""
    p = run(["docker", "exec", "-e", "SQL=" + sql, db_name(site), "sh", "-c", DB_SH], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise Refuse("could not read the database of %s (%s)" % (site, (p.stderr or p.stdout or "no answer").strip()[:160]))
    return [l for l in (p.stdout or "").splitlines() if l.strip()]


def check_db_empty(site, run):
    out = db_query(site, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()", run)
    if not out.isdigit():
        raise Refuse("could not read the database of %s (answer: '%s'): is its stack up? systemctl status wp-%s" % (site, out[:80], site))
    if int(out) != 0:
        raise Refuse("the database of %s is not empty (%s tables): an import needs an empty one. Nothing was changed" % (site, out))


def locate(cfg, arg):
    intake = Path(os.path.realpath(cfg["intake_dir"]))
    p = Path(arg) if os.path.isabs(arg) else intake / arg
    if not FILE_RE.match(p.name):
        raise Refuse("'%s' is not a plain .wpress file name" % p.name)
    if p.is_symlink() or not p.is_file():
        raise Refuse("%s is not a regular file" % p)
    if Path(os.path.realpath(p)).parent != intake:
        raise Refuse("%s is not directly inside %s: copy it there first" % (p, intake))
    return p


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def ledger(cfg, site, event, digest="-", size=0, fname="-", now=None):
    line = "\t".join([(now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"), site, event, digest, str(size), fname]) + "\n"
    fd = os.open(os.path.join(cfg["intake_dir"], "ledger.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line)


def extract(a, keep, work):
    """Write the planned files into a fresh work directory: root-only, never following a link, never overwriting. Any failure removes the directory this run made."""
    old = os.umask(0o077)
    try:
        work.mkdir(mode=0o700)
        try:
            for name, path, size, off, dest in keep:
                target = Path(os.path.normpath(work / dest))
                if os.path.commonpath([str(work), str(target)]) != str(work):
                    raise Refuse("refusing to write outside the work directory: %s" % dest)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    raise Refuse("the archive lists %s twice (or one name is a file and a directory): refusing" % dest)
                with os.fdopen(fd, "wb") as out:
                    a.f.seek(off)
                    left = size
                    while left:
                        chunk = a.f.read(min(CHUNK, left))
                        if not chunk:
                            raise Refuse("the archive ended inside %s" % dest)
                        out.write(chunk)
                        left -= len(chunk)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
    finally:
        os.umask(old)


def show_plan(plan, site, fname, digest, size, out):
    f = plan["facts"]
    out("site %s; archive %s (%d MB, sha256 %s)" % (site, fname, size // MIB, digest[:16] + "..." if digest else "not computed in a plan run"))
    out("archive: WordPress %(archive_wp)s, PHP %(archive_php)s, %(entries)d entries.  sites: WordPress %(our_wp)s, PHP %(our_php)s" % f)
    out("\n%-8s %-40s %-10s %s" % ("KIND", "SLUG", "ARCHIVE", "WHAT HAPPENS"))
    what = {"reinstall": "reinstalled from wordpress.org (batch B)", "approve": "NOT on wordpress.org: unpacked for the scan, you approve it", "dropped": "not carried over"}
    for r in plan["rows"]:
        table = plan["plugins"] if r["kind"] == "plugin" else plan["themes"]
        out("%-8s %-40s %-10s %s" % (r["kind"], r["slug"][:40], r["have"] or "?", what[table[r["slug"]]]))
    out("\nunpacked (%d files, %.1f MB): database.sql, package.json, uploads, mu-plugins, languages and the plugins and themes marked NOT on wordpress.org" % (len(plan["keep"]), plan["keep_bytes"] / MIB))
    out("not unpacked:")
    for reason, (count, nbytes) in sorted(plan["tally"].items()):
        out("  %6d files %8.1f MB  %s" % (count, nbytes / MIB, reason))
    for n in plan["notes"]:
        out("note: " + n)
    if any(k.startswith(("mu-plugins", "wp-content/mu-plugins")) for _, _, _, _, k in plan["keep"]):
        out("note: the archive carries mu-plugins: code that runs on every request. Read it before you approve")


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- the record of a run: PLAN.json in the work directory is what `reset` and `approve` work from

def read_record(work):
    try:
        return json.loads((work / "PLAN.json").read_text())
    except (OSError, ValueError):
        raise Refuse("%s has no readable PLAN.json: the unpack did not finish. Run: wp-site-import reset <site>" % work)


def save_record(work, record):
    tmp = work / ".PLAN.json.new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=1, sort_keys=True)
    os.replace(tmp, work / "PLAN.json")


class Tools:
    """The things the run asks of the host, in one place so that the tests can stand in for them."""
    def __init__(self, cfg, run, popen, chown, which, clock=time.time, post=None):
        self.cfg, self.popen, self.chown, self.which, self.clock, self.post = cfg, popen, chown, which, clock, post
        self.timeout = int(cfg["step_timeout_s"])
        self.run = lambda cmd, **kw: run(cmd, timeout=self.timeout, **kw)

    def bin(self, name):
        return os.path.join(self.cfg["tools_bin"], name)

    def call(self, *cmd):
        try:
            p = self.run(list(cmd), capture_output=True, text=True, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, "timed out after %d s" % self.timeout
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()

    def cli(self, site, *args, plugins=False):
        """WP-CLI as the web app, with no theme code loaded and, unless the step needs it, no plugin code: the database is the archive's, and it decides what WordPress would include."""
        return self.call(self.bin("wp-site-cli"), site, *args, "--skip-themes", *([] if plugins else ["--skip-plugins"]))

    def ext(self, site, *args):
        return self.call(self.bin("wp-site-ext"), site, *args)


def table_prefix(site_dir):
    try:
        m = PREFIX_RE.search((site_dir / "wp-config.php").read_text())
    except OSError:
        m = None
    if not m:
        raise Refuse("cannot read $table_prefix from %s/wp-config.php: run dellwp/wp_sites_dellwp.yml" % site_dir)
    return m.group(1)


# ---- stage: the SQL

def load_sql(site, work, prefix, tools):
    """Stream database.sql into the site's own database, swapping the migration plugin's placeholder for the site's table prefix. Raises Refuse with the end of MariaDB's complaint."""
    old = [(SERVMASK, prefix.encode())] + list(COLLATION_FIXES)
    with tempfile.TemporaryFile() as err:
        p = tools.popen(["docker", "exec", "-i", db_name(site), "sh", "-c", LOAD_SH], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        try:
            p.stdin.write(PRELUDE)
            with open(work / "database.sql", "rb") as f:
                for line in f:
                    for a, b in old:
                        line = line.replace(a, b)
                    p.stdin.write(line)
            p.stdin.close()
        except BrokenPipeError:
            pass
        try:
            rc = p.wait(timeout=tools.timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            raise Refuse("the load did not finish within %d s and was stopped" % tools.timeout)
        err.seek(0)
        tail = err.read()[-500:].decode(errors="replace").strip()
    if rc != 0:
        raise Refuse("MariaDB stopped the load (exit %s): %s" % (rc, tail or "no message"))
    tables = db_query(site, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()", tools.run)
    url = db_query(site, "SELECT option_value FROM %soptions WHERE option_name='siteurl'" % prefix, tools.run)
    if not tables.isdigit() or int(tables) == 0 or not url:
        raise Refuse("the load ended without an error but the database has %s tables and no siteurl: treat it as failed" % tables)
    return int(tables)


def drop_all_tables(site, tools):
    p = tools.run(["docker", "exec", "-e", "SQL=SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE()", db_name(site), "sh", "-c", DB_SH],
                  capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise Refuse("could not list the tables of %s: %s" % (site, (p.stderr or "")[:200]))
    names = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
    if any(not re.fullmatch(r"[A-Za-z0-9_$]+", n) for n in names):
        raise Refuse("a table of %s has a name this tool will not put in a statement: drop the tables by hand" % site)
    sql = "SET FOREIGN_KEY_CHECKS=0;\n" + "".join("DROP TABLE IF EXISTS `%s`;\n" % n for n in names)
    p = tools.run(["docker", "exec", "-i", db_name(site), "sh", "-c", LOAD_SH], input=sql, capture_output=True, text=True)
    if p.returncode != 0:
        raise Refuse("could not drop the tables of %s: %s" % (site, (p.stderr or "")[:300]))


# ---- stage: copy the data in (uploads, languages); code waits for approval

def copy_tree(src, dst, uid, mode_dir, mode_file, chown, manifest, root, save=lambda: None):
    """Copy a tree of plain files. Never overwrites, never follows a link; every path it made is added to the manifest (relative to root) the moment it exists, and the manifest is saved
    every 200 files, so an interrupted copy can still be undone."""
    for cur, dirs, files in os.walk(src):
        here = Path(dst) / Path(cur).relative_to(src)
        if not here.exists():
            here.mkdir(mode=mode_dir)
            manifest.append(str(here.relative_to(root)) + "/")      # recorded the moment it exists, before anything that can be interrupted
            os.chmod(here, mode_dir)
            chown(here, uid, uid)
        elif here.is_symlink() or not here.is_dir():
            raise Refuse("%s exists and is not a directory: refusing" % here)
        for name in sorted(files):
            target = here / name
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode_file)
            except FileExistsError:
                raise Refuse("%s already exists: refusing to overwrite it" % target)
            manifest.append(str(target.relative_to(root)))
            if len(manifest) % 200 == 0:
                save()
            with os.fdopen(fd, "wb") as out, open(Path(cur) / name, "rb") as f:
                shutil.copyfileobj(f, out, CHUNK)
            os.chmod(target, mode_file)
            chown(target, uid, uid)


def copy_data(cfg, site_dir, work, record, tools):
    html = site_dir / "html"
    uid = int(cfg["php_uid"])
    for sub, owner in (("uploads", uid), ("languages", 0)):
        src = work / "wp-content" / sub
        if src.is_dir():
            copy_tree(src, html / "wp-content" / sub, owner, 0o755, 0o644, tools.chown, record["copied"], html, lambda: save_record(work, record))
            save_record(work, record)


# ---- stage: plugins and themes from wordpress.org

def install_from_org(site, record, work, tools):
    steps = []
    for r in record["rows"]:
        kind, slug, have = r["kind"], r["slug"], r["have"]
        table = record["plugins"] if kind == "plugin" else record["themes"]
        if table.get(slug) != "reinstall":
            continue
        if not SLUG_RE.match(slug):
            steps.append({"name": "%s %s" % (kind, slug), "ok": False, "note": "not a wordpress.org slug: skipped"})
            continue
        present, _ = tools.cli(site, kind, "is-installed", slug)
        if present:
            steps.append({"name": "%s %s" % (kind, slug), "ok": True, "note": "already in the image: left as it is"})
            continue
        # The newest is installed instead of the archive's version ONLY when wordpress.org itself says that version is gone (the preflight read its list of versions: a row note). A versioned
        # install that fails for any other reason is a failed step with the command's own output: it used to fall back to the newest whatever the reason and call that "not offered any more",
        # which hid the real error (dellwp 2026-10-25: Elementor's checksum lookup) and installed a version the friend never had.
        gone = r.get("verdict") == "note" and "no longer offered" in str(r.get("why", ""))
        if have and VERSION_RE.match(have) and not gone:
            ok, out = tools.ext(site, kind, "install", slug, "--version=" + have)
            note = "installed version %s" % have
        else:
            ok, out = tools.ext(site, kind, "install", slug)
            note = ("version %s is no longer offered by wordpress.org: the newest was installed" % have) if have and gone else "the newest was installed (the archive's version could not be read)"
        if ok:
            record["installed"][kind].append(slug)
            save_record(work, record)
        steps.append({"name": "%s %s" % (kind, slug), "ok": ok, "note": note if ok else "FAILED: " + out[-200:], **({} if ok else {"detail": out[-2500:]})})
    return steps


# ---- stage: WP-CLI

def old_hosts(meta):
    """The addresses the archive says the site had, without scheme and trailing slashes. A value longer than a host name can be is ignored (it is the archive's text, not an address)."""
    hosts = []
    for key in ("SiteURL", "HomeURL", "InternalSiteURL", "InternalHomeURL"):
        v = meta.get(key)
        if not isinstance(v, str):
            continue
        for pre in ("https://", "http://", "//"):
            if v.startswith(pre):
                v = v[len(pre):].rstrip("/")
                if v and len(v) <= HOST_MAX and v not in hosts:
                    hosts.append(v)
                break
    return hosts


def sanitize_options(site, work, tools):
    """Right after the load, before any command that could load plugin or theme code: the database is the archive's and says which files WordPress includes (active_plugins, template and
    stylesheet are paths). Put back only what package.json says, which the reader has already checked to be plain names. Raises Refuse if one of them cannot be set."""
    meta = json.loads((work / "package.json").read_text())
    _, _, plugins, themes = w.check_meta(meta)
    keep = [e for e in plugins if not dropped_plugin(e.split("/")[0] if "/" in e else e.rsplit(".", 1)[0])]
    template, stylesheet = meta.get("Template") or themes[0], meta.get("Stylesheet") or themes[0]
    steps = []
    for name, value, fmt in (("active_plugins", json.dumps(keep), ["--format=json"]), ("template", template, []), ("stylesheet", stylesheet, [])):
        ok, out = tools.cli(site, "option", "update", name, value, *fmt)
        if not ok:
            raise Refuse("could not reset the %s option from package.json before running anything else (%s)" % (name, out[-200:]))
        steps.append({"name": "option %s reset to package.json" % name, "ok": True, "note": "%d plugin(s)" % len(keep) if name == "active_plugins" else value})
    return steps


def clear_update_state(site, tools):
    """Drop the archive's cached update checks before anything is installed. A row that is not there is fine; any other failure stops the run (a stale row would silently turn an install into a no-op)."""
    gone, steps = 0, []
    for name in UPDATE_STATE:
        ok, out = tools.cli(site, "option", "delete", name)
        if ok:
            gone += 1
        elif "does it exist" not in out.lower():
            raise Refuse("could not drop %s before the installs (%s)" % (name, out[-200:]))
    steps.append({"name": "archive's update checks dropped", "ok": True, "note": "%d of %d rows were there; plugin installs now decide from the disk, not from the friend's site" % (gone, len(UPDATE_STATE))})
    return steps


def snapshot_tree(root, limit=None):
    """Every path under root as a relative string, directories with a trailing slash; None when there are more than `limit` (then nothing is swept later). Links are listed, never followed."""
    limit = TREE_LIMIT if limit is None else limit
    out = []
    for cur, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(cur, n)
            out.append(os.path.relpath(p, root) + ("/" if os.path.isdir(p) and not os.path.islink(p) else ""))
            if len(out) > limit:
                return None
    return sorted(out)


def sweep_new(root, before):
    """Remove what appeared under root since the snapshot: files and links (a link is removed, never followed) and directories once they are empty. Returns how many paths went."""
    known, removed = set(before), 0
    for cur, dirs, files in os.walk(root, topdown=False):
        for n in files + dirs:
            p = os.path.join(cur, n)
            is_dir = os.path.isdir(p) and not os.path.islink(p)
            if os.path.relpath(p, root) + ("/" if is_dir else "") in known:
                continue
            try:
                os.rmdir(p) if is_dir else os.unlink(p)
                removed += 1
            except OSError:
                pass
    return removed


def wpcli_steps(cfg, site, work, record, tools):
    steps = []
    new_host = "%s.%s" % (site, cfg["domain"])
    new_url = "https://" + new_host

    def step(name, *args, plugins=False):
        ok, out = tools.cli(site, *args, plugins=plugins)
        steps.append({"name": name, "ok": ok, "note": (out.strip().splitlines() or [""])[-1][:160] if ok else "FAILED: " + out[-300:], **({} if ok else {"detail": out[-1500:]})})
        return ok, out

    step("core update-db", "core", "update-db")
    meta = json.loads((work / "package.json").read_text())
    hosts = [h for h in old_hosts(meta) if h != new_host]
    for h in hosts:
        if not HOST_RE.match(h):
            steps.append({"name": "search-replace %s" % h[:60], "ok": False, "note": "the old address in package.json has characters this tool will not pass on: skipped; replace it by hand"})
            continue
        for a, b in (("http://" + h, new_url), ("https://" + h, new_url), ("//" + h, "//" + new_host),
                     ("http:\\/\\/" + h, "https:\\/\\/" + new_host), ("https:\\/\\/" + h, "https:\\/\\/" + new_host), ("\\/\\/" + h, "\\/\\/" + new_host)):
            step("search-replace %s" % a, "search-replace", a, b, "--all-tables-with-prefix", "--report-changed-only", "--precise")
    # wp-config.php defines WP_HOME and WP_SITEURL, which shadow the stored options: `wp option update siteurl` then says "unchanged" and writes nothing (seen on dellwp 2026-10-25). What the
    # search-replace passes did to the stored rows is read from the database itself.
    try:
        rows = db_query_all(site, "SELECT option_name, option_value FROM %soptions WHERE option_name IN ('siteurl','home') ORDER BY option_name" % record["prefix"], tools.run)
        got = dict(r.split("\t", 1) for r in rows if "\t" in r)
        bad = {k: v for k, v in got.items() if v.rstrip("/") != new_url}
        steps.append({"name": "stored siteurl and home", "ok": set(got) == {"siteurl", "home"} and not bad,
                      "note": "both are %s in the database" % new_url if set(got) == {"siteurl", "home"} and not bad else "FAILED: the database has %s" % (w.printable(json.dumps(got), limit=160) or "no siteurl/home row")})
    except Refuse as e:
        steps.append({"name": "stored siteurl and home", "ok": False, "note": "FAILED: " + str(e)[:200]})
    active, _ = tools.cli(site, "plugin", "is-active", "elementor")
    if active:
        ok, help_text = tools.cli(site, "elementor", plugins=True)
        names = set(re.findall(r"\b(replace[-_]urls|flush[-_]css)\b", help_text))
        rep = next((n for n in ("replace-urls", "replace_urls") if n in names), None)
        if rep:
            for h in hosts:
                if HOST_RE.match(h):
                    for scheme in ("http://", "https://"):
                        step("elementor %s %s" % (rep, scheme + h), "elementor", rep, scheme + h, new_url, plugins=True)
        else:
            steps.append({"name": "elementor replace-urls", "ok": False, "note": "Elementor is active but its help lists no replace command: URLs inside Elementor data may still be the old ones"})
        flush = next((n for n in ("flush-css", "flush_css") if n in names), None)
        if flush:
            step("elementor %s" % flush, "elementor", flush, plugins=True)
    step("transient delete", "transient", "delete", "--all")
    # Not `rewrite flush`: with themes skipped (on purpose: no theme code runs before approval) WP-CLI warns "Some rewrite rules may be missing" and stores the incomplete set (seen on dellwp
    # 2026-10-25). Deleting the stored rules makes WordPress build them again on the first request, with the theme and every plugin loaded [from memory].
    ok, out = tools.cli(site, "option", "delete", "rewrite_rules")
    none_stored = (not ok) and "does it exist" in out.lower()
    steps.append({"name": "rewrite rules", "ok": ok or none_stored, "note": "none were stored: WordPress builds them on the first request" if none_stored
                  else ("deleted: WordPress builds them again on the first request" if ok else "FAILED: " + out[-300:])})
    step("option blog_public", "option", "update", "blog_public", "0")
    step("cache flush", "cache", "flush")
    return steps


# ---- stage: scan

def pattern_scan(roots, limit=40):
    """[(path relative to its root, [what matched])] for PHP-like files under the roots."""
    hits = []
    for root in roots:
        root = Path(root)
        for cur, _, files in os.walk(root):
            for name in files:
                if not name.lower().endswith(SCAN_EXT):
                    continue
                path = Path(cur) / name
                try:
                    with open(path, "rb") as fh:
                        data = fh.read(SCAN_BYTES)
                except OSError:
                    continue
                found = [label for rx, label in SCAN_PATTERNS if re.search(rx, data)]
                if found:
                    hits.append((str(path.relative_to(root.parent)), found))
    return sorted(hits)[:limit], len(hits)


def php_in_uploads(uploads):
    out = []
    for cur, _, files in os.walk(uploads):
        out += [str((Path(cur) / n).relative_to(uploads)) for n in files if n.lower().endswith(SCAN_EXT)]
    return sorted(out)


def scan(site, site_dir, work, record, tools):
    res = {}
    held = [work / "wp-content" / d for d in ("plugins", "themes", "mu-plugins") if (work / "wp-content" / d).is_dir()]
    res["held_hits"], res["held_total"] = pattern_scan(held)
    res["uploads_php"] = php_in_uploads(site_dir / "html/wp-content/uploads")
    uploads_hits, uploads_total = pattern_scan([site_dir / "html/wp-content/uploads"])
    res["uploads_hits"], res["uploads_hits_total"] = uploads_hits, uploads_total
    ok, out = tools.cli(site, "core", "verify-checksums")
    res["core"] = (ok, (out.strip().splitlines() or [""])[-1][:200])
    ok, out = tools.cli(site, "plugin", "verify-checksums", "--all")
    res["plugins"] = (ok, (out.strip().splitlines() or [""])[-1][:200])
    clam = tools.which("clamscan")
    if clam:
        targets = [str(work / "wp-content"), str(site_dir / "html/wp-content/uploads")]
        ok, out = tools.call(clam, "-r", "--infected", "--no-summary", *[t for t in targets if os.path.isdir(t)])
        res["clam"] = "clean" if ok else ("FOUND: " + out[-400:] if out else "FAILED to run")
    else:
        res["clam"] = "NOT RUN: ClamAV is not installed on this VM"
    return res


# ---- the report

def held_items(record, work):
    items = []
    for kind, table in (("plugin", record["plugins"]), ("theme", record["themes"])):
        for slug, d in sorted(table.items()):
            if d == "approve":
                base = work / "wp-content" / ("plugins" if kind == "plugin" else "themes") / slug
                items.append((kind, slug, sum(1 for _ in base.rglob("*") if _.is_file()) if base.is_dir() else 0))
    mu = work / "wp-content" / "mu-plugins"
    if mu.is_dir():
        items.append(("mu-plugins", "mu-plugins", sum(1 for _ in mu.rglob("*") if _.is_file())))
    return items


def build_report(site, record, steps, res, work):
    f = record["facts"]
    lines = ["IMPORT REPORT for %s  (%s, sha256 %s...)" % (site, record["file"], record["sha256"][:16]),
             "archive WordPress %s, PHP %s; sites WordPress %s, PHP %s" % (f["archive_wp"], f["archive_php"], f["our_wp"], f["our_php"]), "",
             "STEPS (%d, %d failed)" % (len(steps), sum(1 for s in steps if not s["ok"]))]
    lines += ["  %-6s %-52s %s" % ("ok" if s["ok"] else "FAILED", s["name"][:52], " ".join(str(s["note"]).split())[:240]) for s in steps]
    lines += ["", "CODE HELD BACK until you approve it (nothing of it has run):"]
    items = held_items(record, work)
    lines += ["  %-10s %-36s %d files" % i for i in items] or ["  none"]
    lines += ["", "SCAN (a heuristic: a hit is a place to look, a clean result proves nothing)",
              "  core checksums:   %s %s" % ("ok" if res["core"][0] else "FAILED", res["core"][1]),
              "  plugin checksums: %s %s" % ("ok" if res["plugins"][0] else "FAILED", res["plugins"][1]),
              "  ClamAV:           %s" % res["clam"],
              "  PHP files under uploads/: %s" % (", ".join(res["uploads_php"][:10]) + (" ..." if len(res["uploads_php"]) > 10 else "") if res["uploads_php"] else "none"),
              "  suspicious patterns in the held code: %d file(s)" % res["held_total"]]
    lines += ["    %s: %s" % (p, ", ".join(l)) for p, l in res["held_hits"]]
    lines += ["  suspicious patterns in uploads/: %d file(s)" % res["uploads_hits_total"]]
    lines += ["    %s: %s" % (p, ", ".join(l)) for p, l in res["uploads_hits"]]
    details = [s for s in steps if not s["ok"] and s.get("detail")]
    if details:
        lines += ["", "DETAILS OF THE FAILED STEPS (the end of what each command printed)"]
        for s_ in details:
            lines += ["  %s:" % s_["name"]] + ["    " + l for l in str(s_["detail"]).strip().splitlines()[-40:]]
    blocked = [(k, v) for k, v in record["tally"].items() if k in (UPLOADS_BLOCKED, LANG_BLOCKED)]
    if blocked:
        lines += ["", "NOT COPIED because PHP or the web server would act on it:"] + ["  %d file(s)  %s" % (v[0], k) for k, v in blocked]
    if record["notes"]:
        lines += ["", "NOTES"] + ["  " + n for n in record["notes"]]
    failed = [s for s in steps if not s["ok"]] + ([{"name": "core checksums"}] if not res["core"][0] else [])
    lines += ["", "NEXT  wp-site-import approve %s [--reject slug,...]    (copies the held code in, deletes the intake file)" % site,
              "      wp-site-import reset %s                         (undoes this run)" % site]
    if failed:
        lines.append("      approve refuses while a step failed; add --accept-failures once you have looked at them")
    return w.printable("\n".join(lines), keep_newline=True)


# ---- approvals by phone (the tokens are in wp_approvals.py; what a valid one may do is decided here)

def unclean_reasons(record):
    """Why an import may not be approved from a phone. Empty means clean. A missing result counts against it: silence is not a pass."""
    why = []
    failed = [s["name"] for s in record.get("steps", []) if not s["ok"]]
    if failed:
        why.append("%d step(s) failed (%s)" % (len(failed), failed[0]))
    sc = record.get("scan")
    if not isinstance(sc, dict):
        return why + ["no scan result"]
    for key, label in (("core", "core checksums"), ("plugins", "plugin checksums")):
        if not (isinstance(sc.get(key), (list, tuple)) and sc[key] and sc[key][0] is True):   # a tuple in memory, a list once it has been through JSON
            why.append("%s failed" % label)
    clam = str(sc.get("clam", "FAILED: no result"))
    if clam.startswith(("FOUND", "FAILED")):
        why.append("ClamAV: " + clam[:60])
    for key, label in (("held_total", "held-back file(s)"), ("uploads_hits_total", "file(s) under uploads")):
        if sc.get(key, 1) != 0:
            why.append("%s %s match suspicious patterns" % (sc.get(key, "some"), label))
    return why


def notify_report(cfg, site, record, work, tools):
    """Send the phone notification for a finished import: Deny always, Approve only when the import is clean. Never raises: the answer is one line the run prints."""
    a = cfg.get("approvals")
    if not a:
        return "phone approval is not configured"
    try:
        key = ap.read_key(a["key_file"])
        url, ask_topic = ap.read_conf(a["ntfy_conf"])
        sender = ap.sender_auth(a["netrc"], url)
        button = ap.approver_auth(a["approver_file"])
        sha = hashlib.sha256((work / "REPORT.txt").read_bytes()).hexdigest()
        why = unclean_reasons(record)
        now = tools.clock()
        tokens, issued = {}, {}
        for action in (("approve",) if not why else ()) + ("deny",):
            tokens[action], nonce = ap.mint(key, site, action, sha, a["ttl_s"], now)
            issued[nonce] = action
        record["tokens"] = issued
        save_record(work, record)
        held = held_items(record, work)
        body = "\n".join(["%s: %d steps, %d failed." % (site, len(record["steps"]), sum(1 for s in record["steps"] if not s["ok"])),
                          ("Held back for you: " + ", ".join("%s %s (%d files)" % i for i in held)) if held else "Nothing held back.",
                          "ClamAV: " + str(record["scan"].get("clam", "?"))[:60]]
                         + (["NOT CLEAN: " + "; ".join(why) + ". Decide at the control page: wp_sites.py approve %s [--reject ...] [--accept-failures]." % site] if why
                            else ["Clean. Approve copies the held-back code in; the site stays on hold."]))
        (tools.post or ap.post_json)(url, sender, ap.build_message(url, ask_topic, a["reply_topic"], button, site, body, tokens))
        return "phone notification sent (%s)" % ("Approve and Deny" if not why else "Deny only: " + why[0])
    except ap.ApprovalError as e:
        return "phone approval is off or failed: %s" % e
    except Exception as e:        # a notification must never fail the import it reports on
        return "the phone notification failed: %s" % w.printable(e, limit=120)


def run_resolve(cfg, token, tools, out):
    """What a valid token may do: approve a CLEAN import (no override of any kind) or deny (reset) the import it was issued for. Consumed before it acts."""
    a = cfg.get("approvals")
    if not a:
        raise Refuse("phone approval is not configured")
    try:
        d = ap.verify(ap.read_key(a["key_file"]), token, tools.clock())
    except ap.ApprovalError as e:
        raise Refuse("token refused: %s" % e)
    site = d["s"]
    check_site(cfg, site)
    work = Path(cfg["intake_dir"]) / ("work-" + site)
    if not work.is_dir() or work.is_symlink():
        raise Refuse("token refused: no import of %s is waiting" % site)
    record = read_record(work)
    if record.get("stage") != "reported":
        raise Refuse("token refused: the import of %s is at stage '%s', not 'reported'" % (site, record.get("stage")))
    issued = record.get("tokens")
    if not isinstance(issued, dict) or issued.get(d["n"]) != d["a"]:
        raise Refuse("token refused: it was not issued for the import now waiting, or it was used")
    try:
        current = hashlib.sha256((work / "REPORT.txt").read_bytes()).hexdigest()
    except OSError:
        raise Refuse("token refused: the report cannot be read")
    if current != d["r"]:
        raise Refuse("token refused: the report is not the one the token was issued for")
    record["tokens"] = {}          # one use: consumed before anything runs, and a failure does not leave a second try
    save_record(work, record)
    ledger(cfg, site, "token-" + d["a"], d["n"])
    if d["a"] == "approve":
        why = unclean_reasons(record)
        if why:
            raise Refuse("not approved: %s. Decide at the control page" % "; ".join(why))
        return run_approve(cfg, site, [], False, tools, out)
    return run_reset(cfg, site, None, tools, out, confirmed=True)


# ---- the run

def run_import(cfg, site, arg, plan_only, get, tools, out):
    site_dir = check_site(cfg, site)
    path = locate(cfg, arg)
    work = Path(cfg["intake_dir"]) / ("work-" + site)
    if work.exists() or work.is_symlink():
        raise Refuse("%s exists: an earlier import of %s left it. Look at it, then run: wp-site-import reset %s" % (work, site, site))
    check_db_empty(site, tools.run)
    prefix = table_prefix(site_dir)
    with w.Archive(path) as a:
        plan = build_plan(a, cfg, get)
        need = plan["keep_bytes"]
        intake_free = int(cfg["intake_quota_mb"]) * MIB - tree_bytes(cfg["intake_dir"])
        site_free = int(cfg["sites"][site]["quota_mb"]) * MIB - tree_bytes(site_dir)
        site_need = need + 2 * plan["sql_bytes"]   # the database on disk is about three times its dump (a guess, so the check is a floor, not a promise)
        if need > intake_free:
            raise Refuse("the unpacked copy needs %.1f MB and the intake quota has %.1f MB free (the archive itself counts): remove files from %s or raise wp_intake_quota_mb. Nothing was changed"
                         % (need / MIB, intake_free / MIB, cfg["intake_dir"]))
        if site_need > site_free:
            raise Refuse("the files to copy in and the database need about %.1f MB and %s has %.1f MB left of its quota: raise quota_mb (wp_sites.py modify %s --set quota_mb=...). Nothing was changed" % (site_need / MIB, site, site_free / MIB, site))
        if plan_only:
            show_plan(plan, site, path.name, "", path.stat().st_size, out)
            out("\nPLAN ONLY: nothing was written.")
            return 0
        digest = sha256(path)
        size = path.stat().st_size
        extract(a, plan["keep"], work)
        written = tree_bytes(work)
        if written != need:
            shutil.rmtree(work, ignore_errors=True)
            raise Refuse("internal check failed: wrote %d bytes, planned %d; the work directory was removed" % (written, need))
        record = {"site": site, "file": path.name, "sha256": digest, "size": size, "unpacked_at": now_utc(), "facts": plan["facts"], "plugins": plan["plugins"], "themes": plan["themes"],
                  "rows": plan["rows"], "notes": plan["notes"], "tally": plan["tally"], "files": len(plan["keep"]), "bytes": need, "stage": "unpacked", "prefix": prefix,
                  "db_loaded": False, "copied": [], "installed": {"plugin": [], "theme": []}, "steps": []}
        save_record(work, record)
    ledger(cfg, site, "unpacked", digest, size, path.name)
    show_plan(plan, site, path.name, digest, size, out)
    out("\nUNPACKED into %s. Loading the database ..." % work)
    try:
        record["db_loaded"] = True      # set BEFORE the load: a load that is interrupted has still put tables in, and reset must drop them
        wc = site_dir / "html" / "wp-content"
        record["tree_before"] = snapshot_tree(wc) if wc.is_dir() and not wc.is_symlink() else None      # what reset returns wp-content to
        save_record(work, record)
        tables = load_sql(site, work, prefix, tools)
        ledger(cfg, site, "loaded", digest, size, path.name)
        out("loaded %d tables. Resetting the options that name code, copying data, installing from wordpress.org, running WP-CLI ..." % tables)
        steps = sanitize_options(site, work, tools)
        steps += clear_update_state(site, tools)
        copy_data(cfg, site_dir, work, record, tools)
        steps += install_from_org(site, record, work, tools)
        steps += wpcli_steps(cfg, site, work, record, tools)
        res = scan(site, site_dir, work, record, tools)
    except (Refuse, KeyboardInterrupt) as e:
        save_record(work, record)
        raise Refuse("%s\nThe run stopped part way. Look at the site, then undo it with: wp-site-import reset %s" % (str(e) or "interrupted", site))
    ok, bout = tools.call(tools.bin("wp-site-backup"), site)
    steps.append({"name": "first backup", "ok": ok, "note": "taken" if ok else "FAILED: " + bout[-200:]})
    m = SNAPSHOT_LINE.search(bout) if ok else None
    if m and m.group(2) == site:
        record["snapshots"] = [m.group(1)]       # the import's own snapshot: reset and Deny take it away again
        save_record(work, record)
    record["steps"], record["stage"], record["scan"] = steps, "reported", {k: v for k, v in res.items() if k not in ("held_hits", "uploads_hits")}
    report = build_report(site, record, steps, res, work)
    fd = os.open(work / "REPORT.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(report + "\n")
    save_record(work, record)
    ledger(cfg, site, "reported", digest, size, path.name)
    out("\n" + report)
    out(notify_report(cfg, site, record, work, tools))
    return 1 if any(not s["ok"] for s in steps) else 0


def run_approve(cfg, site, reject, accept_failures, tools, out):
    check_site(cfg, site)
    work = Path(cfg["intake_dir"]) / ("work-" + site)
    if not work.is_dir() or work.is_symlink():
        raise Refuse("there is no unapproved import of %s (%s is not a directory)" % (site, work))
    record = read_record(work)
    if record.get("stage") != "reported":
        raise Refuse("the import of %s is at stage '%s', not 'reported': let it finish, or reset it" % (site, record.get("stage")))
    failed = [s["name"] for s in record["steps"] if not s["ok"]]
    if failed and not accept_failures:
        raise Refuse("%d step(s) failed (%s ...): look at the report, then use --accept-failures, or reset" % (len(failed), failed[0]))
    items = held_items(record, work)
    odd = [i[1] for i in items if i[0] != "mu-plugins" and not NAME_RE.match(i[1])]
    if odd:
        raise Refuse("the record names '%s', which is not a plain folder name: refusing to copy anything" % odd[0][:60])
    unknown = [r for r in reject if r not in {i[1] for i in items}]
    if unknown:
        raise Refuse("--reject names %s, which is not held back (held: %s)" % (", ".join(unknown), ", ".join(i[1] for i in items) or "nothing"))
    html = Path(cfg["data_mount"]) / "sites" / site / "html"
    take = [i for i in items if i[1] not in reject]
    where = {"plugin": "plugins", "theme": "themes", "mu-plugins": "mu-plugins"}
    for kind, slug, _ in take:
        dst = html / "wp-content" / where[kind] / ("" if kind == "mu-plugins" else slug)
        if dst.is_symlink() or (dst.is_dir() and any(dst.iterdir())) or (dst.exists() and not dst.is_dir()):
            raise Refuse("%s already exists: refusing to overwrite it. Nothing was copied" % dst)
    for kind, slug, _ in take:
        sub, name = where[kind], ("" if kind == "mu-plugins" else slug)
        try:
            copy_tree(work / "wp-content" / sub / name, html / "wp-content" / sub / name, 0, 0o755, 0o644, tools.chown, record["copied"], html, lambda: save_record(work, record))
        finally:
            save_record(work, record)
    f = Path(cfg["intake_dir"]) / record["file"]
    if FILE_RE.match(str(record["file"])) and f.is_file() and not f.is_symlink():
        f.unlink()
    shutil.rmtree(work)
    ledger(cfg, site, "approved", record["sha256"], record["size"], record["file"])
    ok, bout = tools.call(tools.bin("wp-site-backup"), site)
    out("approved: copied in %s; rejected %s. The intake file and the work directory are gone (the ledger line stays). The site is still on hold; going live is its own step. Backup after the copy: %s"
        % (", ".join(i[1] for i in take) or "nothing", ", ".join(reject) or "nothing", "taken" if ok else "FAILED (" + bout[-120:] + ")"))
    return 0


def drop_own_snapshots(cfg, site, record, out):
    """Remove the snapshot(s) this import took itself, so a turned-down import leaves the site's backups as they were. Only a stamp the record names, in this site's own backup directory, as a real
    directory (never a link), whose MANIFEST says it is this site's and not a final snapshot. .last-success is moved back to the newest snapshot that is left, or removed."""
    stamps = record.get("snapshots", [])
    root = Path(cfg["backup_dir"]) / site
    gone = 0
    for stamp in stamps if isinstance(stamps, list) else []:
        d = root / stamp if isinstance(stamp, str) and STAMP_RE.match(stamp) else None
        if d is None or d.is_symlink() or not d.is_dir():
            continue
        try:
            lines = (d / "MANIFEST").read_text().splitlines()
        except OSError:
            continue
        if "site=" + site not in lines or any(l.startswith("tag=") for l in lines):
            continue
        shutil.rmtree(d)
        gone += 1
    if gone:
        marker = root / ".last-success"
        left = sorted(x.name for x in root.iterdir() if STAMP_RE.match(x.name) and x.is_dir() and not x.is_symlink())
        try:
            if marker.is_file() and not marker.is_symlink() and not (root / marker.read_text().strip()).is_dir():
                if left:
                    marker.write_text(left[-1] + "\n")
                else:
                    marker.unlink()
        except OSError:
            pass
        out("removed the %d snapshot(s) this import took" % gone)


def undo(cfg, site, work, tools, out):
    """Put the site back as it was before the run: the tables, the copied files, the installed plugins and themes. Only what the record names."""
    record = read_record(work)
    html = Path(cfg["data_mount"]) / "sites" / site / "html"
    if record.get("db_loaded"):
        drop_all_tables(site, tools)
        out("dropped the tables of %s" % site)
    files = [m for m in record.get("copied", []) if not m.endswith("/")]
    dirs = sorted((m for m in record.get("copied", []) if m.endswith("/")), key=len, reverse=True)
    for m in files:
        t = html / m
        if t.is_file() and not t.is_symlink():
            t.unlink()
    for m in dirs:
        t = html / m
        if t.is_dir() and not t.is_symlink():
            try:
                t.rmdir()
            except OSError:
                pass
    for kind, sub in (("plugin", "plugins"), ("theme", "themes")):
        for slug in record.get("installed", {}).get(kind, []):
            t = html / "wp-content" / sub / slug
            if SLUG_RE.match(slug) and t.is_dir() and not t.is_symlink():
                shutil.rmtree(t)
    out("removed %d copied files and %d installed plugins/themes" % (len(files), sum(len(v) for v in record.get("installed", {}).values())))
    before, wc = record.get("tree_before"), html / "wp-content"
    if isinstance(before, list) and all(isinstance(x, str) for x in before) and wc.is_dir() and not wc.is_symlink():
        n = sweep_new(wc, before)
        out("swept %d more path(s) that appeared under wp-content during the import (files the plugins made when they first ran, upgrade directories)" % n)
    elif before is None and "tree_before" in record:
        out("wp-content was too big to snapshot: only what this run copied or installed was removed")
    drop_own_snapshots(cfg, site, record, out)


def run_reset(cfg, site, ask, tools, out, confirmed=False):
    if not SITE_RE.match(site or ""):
        raise Refuse("'%s' is not a site name" % site)
    work = Path(cfg["intake_dir"]) / ("work-" + site)
    if not work.is_dir() or work.is_symlink():
        raise Refuse("there is nothing to reset: %s is not a directory" % work)
    if not confirmed and ask("Type the site name (%s) to undo the import and remove %s: " % (site, work)).strip() != site:
        raise Refuse("the name did not match: nothing was removed")
    if (work / "PLAN.json").exists():
        undo(cfg, site, work, tools, out)
    shutil.rmtree(work)
    ledger(cfg, site, "reset")
    out("removed %s. The file in the intake directory was left (you remove it)." % work)
    return 0


def run_receive(cfg, name, stdin, out):
    """Take a file from standard input into the intake directory (the laptop sends it with ssh, because the directory is root-only). Writes a hidden partial file and renames it into place
    only when the stream ended, never past the free space of the intake quota, never over a file that is there. Prints one line: received <sha256> <bytes> <name>, which the sender compares."""
    if not FILE_RE.match(name or ""):
        raise Refuse("'%s' is not a plain .wpress file name (letters, digits, . _ - only)" % w.printable(name, limit=60))
    intake = Path(cfg["intake_dir"])
    final, part = intake / name, intake / (".recv-" + name)
    if final.exists() or final.is_symlink():
        raise Refuse("%s is already in the intake directory: import it, or remove it first with: wp-site-import discard %s" % (name, name))
    free = int(cfg["intake_quota_mb"]) * MIB - tree_bytes(intake)
    digest, total = hashlib.sha256(), 0
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in iter(lambda: stdin.read(CHUNK), b""):
                total += len(chunk)
                if total > free:
                    raise Refuse("the file is larger than the %.1f MB free in the intake quota: nothing was kept" % (max(free, 0) / MIB))
                digest.update(chunk)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        if total == 0:
            raise Refuse("nothing was received")
        os.rename(part, final)
    except BaseException:
        try:
            os.unlink(part)
        except FileNotFoundError:
            pass
        raise
    ledger(cfg, "-", "received", digest.hexdigest(), total, name)
    out("received %s %d %s" % (digest.hexdigest(), total, name))
    return 0


def run_discard(cfg, name, out, if_present=False):
    """Remove a file from the intake directory, unless an import in progress was made from it. With if_present, a file that is not there is not an error and nothing is printed."""
    if not FILE_RE.match(name or ""):
        raise Refuse("'%s' is not a plain .wpress file name" % w.printable(name, limit=60))
    intake = Path(cfg["intake_dir"])
    f = intake / name
    if not f.is_file() or f.is_symlink():
        if if_present and not f.exists() and not f.is_symlink():
            return 0
        raise Refuse("%s is not a file in the intake directory" % name)
    for work in sorted(intake.glob("work-*")):
        try:
            if read_record(work).get("file") == name:
                raise Refuse("%s is the file of the import in %s: approve or reset that first" % (name, work.name))
        except Refuse as e:
            if "is the file of" in str(e):
                raise
    f.unlink()
    ledger(cfg, "-", "discarded", "-", 0, name)
    out("removed %s from the intake directory" % name)
    return 0


def run_report(cfg, site, out):
    if not SITE_RE.match(site or ""):
        raise Refuse("'%s' is not a site name" % site)
    p = Path(cfg["intake_dir"]) / ("work-" + site) / "REPORT.txt"
    if not p.is_file() or p.is_symlink():
        raise Refuse("there is no report for %s: no import is waiting for approval" % site)
    out(p.read_text().rstrip("\n"))
    return 0


def tty_ask(prompt):
    with open(os.environ.get("WP_SITE_TTY", "/dev/tty")) as t:
        sys.stderr.write(prompt)
        sys.stderr.flush()       # stderr is line-buffered and a prompt has no newline: without this the question is never shown and the tool waits for an answer nobody can see (dellwp 2026-10-25)
        return t.readline()


USAGE = ("usage: wp-site-import <site> <file.wpress> [--plan]\n       wp-site-import approve <site> [--reject a,b] [--accept-failures]\n       wp-site-import reset <site>\n"
         "       wp-site-import report <site>\n       wp-site-import receive <file.wpress>      (the file on standard input)\n       wp-site-import discard <file.wpress> [--if-present]\n"
         "       wp-site-import resolve <token>            (a signed token from the phone: approve a clean import, or deny)")


def main(cfg, argv, get=w.fetch, run=subprocess.run, ask=tty_ask, out=print, popen=subprocess.Popen, chown=os.chown, which=shutil.which, stdin=None, clock=time.time, post=None):
    flags = {"--plan", "--accept-failures", "--if-present"}
    args = [x for x in argv if x not in flags]
    reject = []
    if "--reject" in args:
        i = args.index("--reject")
        reject = [x for x in (args[i + 1] if i + 1 < len(args) else "").split(",") if x]
        args = args[:i] + args[i + 2:]
    plan_only, accept, if_present = "--plan" in argv, "--accept-failures" in argv, "--if-present" in argv
    tools = Tools(cfg, run, popen, chown, which, clock, post)
    raw_out = out

    def out(text):          # everything printed passes the filter: names and versions come from the archive
        raw_out(w.printable(text, keep_newline=True))
    lock = None
    try:
        lock = os.open(os.path.join(cfg["intake_dir"], ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refuse("another import is running")
        command = args[0] if args[:1] and args[0] in ("reset", "approve", "receive", "discard", "report", "resolve") else "import"
        if (plan_only and command != "import") or ((reject or accept) and command != "approve") or (if_present and command != "discard"):
            out(USAGE)           # a flag belongs to one command; one that is given to another is not quietly dropped
            return 2
        if args[:1] == ["reset"] and len(args) == 2:
            return run_reset(cfg, args[1], ask, tools, out)
        if args[:1] == ["resolve"] and len(args) == 2:
            return run_resolve(cfg, args[1], tools, out)
        if args[:1] == ["receive"] and len(args) == 2:
            return run_receive(cfg, args[1], stdin or sys.stdin.buffer, out)
        if args[:1] == ["discard"] and len(args) == 2:
            return run_discard(cfg, args[1], out, if_present)
        if args[:1] == ["report"] and len(args) == 2:
            return run_report(cfg, args[1], out)
        if args[:1] == ["approve"] and len(args) == 2:
            return run_approve(cfg, args[1], reject, accept, tools, out)
        if len(args) != 2 or args[0] in ("reset", "approve", "receive", "discard", "report", "resolve") or reject or accept:
            out(USAGE)
            return 2
        return run_import(cfg, args[0], args[1], plan_only, get, tools, out)
    except Refuse as e:
        out("wp-site-import: %s" % e)
        return 2
    except w.ArchiveError as e:
        out("wp-site-import: the archive may not be imported: %s" % e)
        return 2
    except KeyboardInterrupt:
        out("wp-site-import: interrupted. If files or tables were already written, wp-site-import reset <site> undoes them.")
        return 130
    except subprocess.TimeoutExpired as e:
        out("wp-site-import: a command did not answer within %d s and was stopped: %s" % (tools.timeout, " ".join(map(str, e.cmd))[:120]))
        return 2
    except urllib.error.URLError as e:
        out("wp-site-import: NOT JUDGED (wordpress.org could not be asked: %s); nothing was changed. Run it again." % e)
        return 2
    except OSError as e:
        out("wp-site-import: %s" % e)
        return 2
    finally:
        if lock is not None:
            os.close(lock)
