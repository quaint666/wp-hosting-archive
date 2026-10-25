#!/usr/bin/env python3
"""wp-site-import: bring a .wpress archive into a WordPress site on dellwp, step by step. Runs on the VM as root (rendered wrapper: templates/wp/wp_site_import.py.j2; this module and
wp_wpress.py are copied to the tools library by tasks/wp_tools_common.yml).

    wp-site-import <site> <file.wpress> [--plan]     (the file must sit in the intake directory)
    wp-site-import reset <site>                      (asks for the site name; removes the unpacked copy)

BATCH A, what exists: every refusal, the plan, the unpack into a work directory, the ledger. Nothing is written into the site, its database or its containers. Not built yet (batch B):
copy-in, plugin reinstall, the SQL load, the WP-CLI steps, the scan, the report, the first backup, the approval.
Refuses, changing nothing: a site that is not in wp_sites or not on hold; a site with a database that is not empty; a file that is not a plain *.wpress directly inside the intake directory;
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
import urllib.error
from pathlib import Path

import wp_wpress as w

SITE_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.wpress$")
CHUNK = 1 << 20
MIB = 1 << 20
# Directories of wp-content that are not carried over, whatever they hold. The first two are in the practice file (upgrade/ held 36 MB of a half-finished update); ai1wm-backups is the
# migration plugin's own backup folder.
DROP_DIRS = {"upgrade": "upgrade/ (a half-finished update)", "cache": "cache directory", "litespeed": "LiteSpeed cache data", "ai1wm-backups": "the migration plugin's own backups"}
# Directories of wp-content that are copied as they are (mu-plugins is code: the report says so)
KEEP_DIRS = ("uploads", "mu-plugins", "languages")
DB_EMPTY = 'MYSQL_PWD="$MARIADB_PASSWORD" exec mariadb -u"$MARIADB_USER" -N -B -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()" "$MARIADB_DATABASE"'


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
    if top in KEEP_DIRS:
        return "keep", "wp-content/" + rel(name, path)
    return "skip", "unknown directory '%s': not copied" % top


def build_plan(a, cfg, get):
    """Everything decided from the archive and wordpress.org, before anything is written. Raises Refuse when the archive may not be imported."""
    our_wp, our_php = w.pin(cfg["image_pin"])
    rows, notes, nogo, facts = w.judge(a, our_wp, our_php, get)
    if nogo:
        raise Refuse("the archive may not be imported:\n  " + "\n  ".join(nogo))
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
    return {"facts": facts, "rows": rows, "notes": notes, "plugins": plugins, "themes": themes, "keep": keep, "keep_bytes": keep_bytes, "tally": tally}


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


def check_db_empty(site, run):
    db = "wp_%s_db" % site.replace("-", "_")
    p = run(["docker", "exec", db, "sh", "-c", DB_EMPTY], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = (p.stdout or "").strip()
    if p.returncode != 0 or not out.isdigit():
        raise Refuse("could not read the database of %s (%s): is its stack up? systemctl status wp-%s" % (site, (p.stderr or out or "no answer").strip()[:200], site))
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


def run_import(cfg, site, arg, plan_only, get, run, out):
    site_dir = check_site(cfg, site)
    path = locate(cfg, arg)
    work = Path(cfg["intake_dir"]) / ("work-" + site)
    if work.exists() or work.is_symlink():
        raise Refuse("%s exists: an earlier import of %s left it. Look at it, then run: wp-site-import reset %s" % (work, site, site))
    check_db_empty(site, run)
    with w.Archive(path) as a:
        plan = build_plan(a, cfg, get)
        need = plan["keep_bytes"]
        intake_free = int(cfg["intake_quota_mb"]) * MIB - tree_bytes(cfg["intake_dir"])
        site_free = int(cfg["sites"][site]["quota_mb"]) * MIB - tree_bytes(site_dir)
        if need > intake_free:
            raise Refuse("the unpacked copy needs %.1f MB and the intake quota has %.1f MB free (the archive itself counts): remove files from %s or raise wp_intake_quota_mb. Nothing was changed"
                         % (need / MIB, intake_free / MIB, cfg["intake_dir"]))
        if need > site_free:
            raise Refuse("the files to copy in need %.1f MB and %s has %.1f MB left of its quota: raise quota_mb (wp_sites.py modify %s --set quota_mb=...). Nothing was changed" % (need / MIB, site, site_free / MIB, site))
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
        record = {"site": site, "file": path.name, "sha256": digest, "size": size, "unpacked_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "facts": plan["facts"],
                  "plugins": plan["plugins"], "themes": plan["themes"], "rows": plan["rows"], "notes": plan["notes"], "tally": plan["tally"], "files": len(plan["keep"]), "bytes": need}
        fd = os.open(work / "PLAN.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=1, sort_keys=True)
    ledger(cfg, site, "unpacked", digest, size, path.name)
    show_plan(plan, site, path.name, digest, size, out)
    out("\nUNPACKED into %s. Batch A ends here: copy-in, plugin reinstall, the SQL load, the scan and the approval are not built yet. Undo: wp-site-import reset %s" % (work, site))
    return 0


def run_reset(cfg, site, ask, out):
    if not SITE_RE.match(site or ""):
        raise Refuse("'%s' is not a site name" % site)
    work = Path(cfg["intake_dir"]) / ("work-" + site)
    if not work.is_dir() or work.is_symlink():
        raise Refuse("there is nothing to reset: %s is not a directory" % work)
    if ask("Type the site name (%s) to remove %s: " % (site, work)).strip() != site:
        raise Refuse("the name did not match: nothing was removed")
    shutil.rmtree(work)
    ledger(cfg, site, "reset")
    out("removed %s. The file in the intake directory was left (you remove it)." % work)
    return 0


def tty_ask(prompt):
    with open(os.environ.get("WP_SITE_TTY", "/dev/tty")) as t:
        sys.stderr.write(prompt)
        return t.readline()


def main(cfg, argv, get=w.fetch, run=subprocess.run, ask=tty_ask, out=print):
    plan_only = "--plan" in argv
    args = [x for x in argv if x != "--plan"]
    lock = None
    try:
        lock = os.open(os.path.join(cfg["intake_dir"], ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refuse("another import is running")
        if args[:1] == ["reset"] and len(args) == 2:
            return run_reset(cfg, args[1], ask, out)
        if len(args) != 2 or args[0] == "reset":
            out("usage: wp-site-import <site> <file.wpress> [--plan]\n       wp-site-import reset <site>")
            return 2
        return run_import(cfg, args[0], args[1], plan_only, get, run, out)
    except Refuse as e:
        out("wp-site-import: %s" % e)
        return 2
    except w.ArchiveError as e:
        out("wp-site-import: the archive may not be imported: %s" % e)
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
