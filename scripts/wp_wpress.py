#!/usr/bin/env python3
"""Read a .wpress archive (All-in-One WP Migration, as Local writes it) and judge whether dellwp can host it. Nothing is unpacked or written.

    python3 scripts/wp_wpress.py preflight <file.wpress>

A .wpress is a run of entries: a 4377-byte header (file name 255, size 14, mtime 12, directory 4096, all NUL-padded) and then the bytes; an all-zero header, or a last block with no name and no path, ends it. Content paths are
relative to wp-content (plugins/<slug>/..., themes/<slug>/...); package.json and database.sql sit at the top ("."). The preflight indexes the headers (seeking over the data, so
a 300 MB file takes a moment), reads only package.json and the version line of each active plugin and the theme, asks wordpress.org about each of them and compares the archive
with the WordPress and PHP the sites run (vars/wp.yml, wp_image_php: the one pin for every site). It prints a table and GO, GO with notes, or NO-GO.

NO-GO: an archive core newer than ours; a plugin or theme that needs a newer PHP than ours; an unsafe path (.., absolute, a drive letter or a backslash); a truncated or unreadable archive.
Notes (you decide): a plugin or theme that is not on wordpress.org; a PHP minor that differs from ours; an older core (update-db will run). Exit codes: 0 GO, 1 NO-GO, 2 not judged
(wordpress.org could not be reached, so nothing is claimed). Only the fields below are printed: package.json holds the archive owner's local paths and user name."""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WP_VARS = "vars/wp.yml"
HEADER = 4377
NAME, SIZE, PATH = slice(0, 255), slice(255, 269), slice(281, HEADER)
JSON_LIMIT = 4 * 1024 * 1024
HEAD_BYTES = 16 * 1024
MAX_ITEMS = 150          # plugins plus themes one archive may name; each is a lookup on wordpress.org
NAME_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
API = "https://api.wordpress.org/%s/info/1.2/?action=%s_information&request[slug]=%s&request[fields][versions]=1"


class ArchiveError(Exception):
    pass


def printable(text, keep_newline=False, limit=None):
    """Text from an archive can reach the operator's terminal and a notification. Controls, escapes (ESC starts a terminal sequence), the C1 range and the bidirectional overrides become '?'."""
    out = []
    for c in str(text):
        o = ord(c)
        bad = o < 32 or 127 <= o <= 159 or 0x200B <= o <= 0x200F or 0x202A <= o <= 0x202E or 0x2066 <= o <= 0x2069 or o in (0xFEFF, 0x2028, 0x2029)
        out.append("?" if bad and not (keep_newline and c == "\n") else c)
    text = "".join(out)
    return text if limit is None or len(text) <= limit else text[:limit] + "..."


def pin(image):
    """('7.1.3', '8.5') out of an image reference such as wordpress:7.1.3-php8.5-fpm."""
    m = re.fullmatch(r"wordpress:(\d+\.\d+(?:\.\d+)?)-php(\d+\.\d+)-fpm", str(image))
    if not m:
        raise ArchiveError("'%s' is not of the form wordpress:<version>-php<minor>-fpm" % image)
    return m.group(1), m.group(2)


def ours(repo=REPO):
    """The WordPress and PHP the sites run, out of the one pin (vars/wp.yml). A missing line fails: no default."""
    m = re.search(r'^wp_image_php:\s*"([^"]+)"', (Path(repo) / WP_VARS).read_text(), re.M)
    if not m:
        raise ArchiveError("%s has no wp_image_php line" % WP_VARS)
    return pin(m.group(1))


def vtuple(version):
    """'7.1.3' -> (7, 1, 3); a suffix such as '-beta1' or a missing part is ignored/zero, so '7.1' == '7.1.0'."""
    nums = [int(x) for x in re.findall(r"\d+", str(version).split("-")[0])]
    return tuple(nums + [0] * (3 - len(nums)))[:3]


def unsafe(path, name):
    full = path.replace("\\", "/") + "/" + name if path not in ("", ".") else name
    return ("\\" in path or "\\" in name or full.startswith("/") or re.match(r"^[A-Za-z]:", full) is not None or ".." in full.split("/")
            or printable(full) != full)


class Archive:
    def __init__(self, path):
        self.f = open(path, "rb")
        try:
            self._index()
        except BaseException:
            self.f.close()
            raise

    def _index(self):
        self.f.seek(0, 2)
        self.length = self.f.tell()
        self.f.seek(0)
        self.entries = []
        while True:
            at = self.f.tell()
            h = self.f.read(HEADER)
            if len(h) < HEADER:
                raise ArchiveError("truncated: no end marker after %d entries" % len(self.entries))
            if h == b"\0" * HEADER or (not h[NAME].split(b"\0")[0] and not h[PATH].split(b"\0")[0] and at + HEADER == self.length):
                break  # the end marker: all zeros, or (as Local writes it, seen 2026-10-25) the last block with no name and no path whose size field holds the archive's length
            try:
                name, path = h[NAME].split(b"\0")[0].decode(), h[PATH].split(b"\0")[0].decode()
                size = int(h[SIZE].split(b"\0")[0] or 0)
            except (UnicodeDecodeError, ValueError):
                raise ArchiveError("unreadable header at byte %d" % at)
            if size < 0:
                raise ArchiveError("unreadable header at byte %d (a negative size)" % at)
            if at + HEADER + size > self.length:
                raise ArchiveError("truncated inside %s/%s" % (path, name))
            self.entries.append((name, path, size, at + HEADER))
            self.f.seek(size, 1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()

    def find(self, path, name):
        return next((e for e in self.entries if e[1] == path and e[0] == name), None)

    def read(self, entry, limit):
        name, path, size, at = entry
        self.f.seek(at)
        return self.f.read(min(size, limit))

    def unsafe_paths(self):
        return [("%s/%s" % (p, n)).lstrip("./") for n, p, _, _ in self.entries if unsafe(p, n)]


def header_version(data):
    m = re.search(rb"^[ \t/*#@]*Version:[ \t]*([^\r\n]+)", data, re.M | re.I)
    return printable(m.group(1).decode(errors="replace").strip(), limit=40) if m else None


def header_field(data, field):
    m = re.search(rb"^[ \t/*#@]*%s:[ \t]*([^\r\n]+)" % field.encode(), data, re.M | re.I)
    return m.group(1).decode(errors="replace").strip() if m else None


def fetch(url):
    """(status, json or None). 404 is a real answer ("not on wordpress.org"); anything else that fails raises."""
    req = urllib.request.Request(url, headers={"User-Agent": "ansible-homelab-wpress-preflight"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, None
        raise


def lookup(kind, slug, get):
    """kind is 'plugins' or 'themes'. Returns the record, or None when wordpress.org says it does not know the slug."""
    status, body = get(API % (kind, kind[:-1], urllib.parse.quote(slug)))
    if status == 404 or not isinstance(body, dict) or body.get("error"):
        return None
    return body


def judge_item(kind, slug, header, get, our_php):
    """One plugin or theme: its archive version, what wordpress.org says, and a verdict (ok, note, nogo)."""
    row = {"kind": kind[:-1], "slug": slug, "have": header_version(header) if header else None, "latest": "-", "requires_php": "-", "verdict": "ok", "why": "", "onorg": False}
    if header is None:
        row.update(verdict="note", why="no readable header in the archive")
        return row
    rec = lookup(kind, slug, get)
    if rec is None:
        row.update(verdict="note", why="not on wordpress.org: you approve or reject it by hand")
        return row
    row["onorg"] = True
    row["latest"] = rec.get("version") or "?"
    row["requires_php"] = rec.get("requires_php") or "-"
    need = rec.get("requires_php")
    if need and vtuple(need) > vtuple(our_php):
        row.update(verdict="nogo", why="needs PHP %s, the sites run %s" % (need, our_php))
    elif row["have"] and isinstance(rec.get("versions"), dict) and rec["versions"] and row["have"] not in rec["versions"] and row["have"] != row["latest"]:
        row.update(verdict="note", why="version %s is no longer offered: the newest would be installed" % row["have"])
    elif row["have"] is None:
        row.update(verdict="note", why="the archive's version could not be read")
    return row


def preflight(path, repo=REPO, get=fetch):
    """Returns (rows, notes, nogo, facts). Raises ArchiveError for an archive that cannot be read; a network error propagates (exit 2)."""
    our_wp, our_php = ours(repo)
    with Archive(path) as a:
        return judge(a, our_wp, our_php, get)


def check_meta(meta):
    """package.json comes from the archive: prove its shape before anything reads a field. Returns (wp_version, php_version or None, plugin entries, themes)."""
    if not isinstance(meta, dict):
        raise ArchiveError("package.json is not a JSON object")
    wpd = meta.get("WordPress")
    if not isinstance(wpd, dict) or not isinstance(wpd.get("Version"), str) or not wpd["Version"]:
        raise ArchiveError("package.json names no WordPress version")
    phpd = meta.get("PHP")
    php = phpd.get("Version") if isinstance(phpd, dict) else None
    if php is not None and not isinstance(php, str):
        raise ArchiveError("package.json has an unreadable PHP version")
    plugins = meta.get("Plugins", [])
    if not isinstance(plugins, list) or not all(isinstance(x, str) for x in plugins):
        raise ArchiveError("package.json lists its plugins in a form this tool does not read")
    for entry in plugins:
        parts = entry.split("/")
        if len(parts) > 2 or not all(NAME_PART.match(x) for x in parts):
            raise ArchiveError("package.json names a plugin '%s', which is not a plain folder/file name" % printable(entry, limit=60))
    themes = []
    for key in ("Template", "Stylesheet"):
        v = meta.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or not NAME_PART.match(v):
            raise ArchiveError("package.json names a theme '%s', which is not a plain folder name" % printable(v, limit=60))
        if v not in themes:
            themes.append(v)
    if not themes:
        raise ArchiveError("package.json names no active theme")
    if len(plugins) + len(themes) > MAX_ITEMS:
        raise ArchiveError("package.json names %d plugins and themes; more than %d is not an archive this tool will look up one by one" % (len(plugins) + len(themes), MAX_ITEMS))
    return wpd["Version"], php, plugins, themes


def judge(a, our_wp, our_php, get):
    """The verdicts for an archive that is already open: (rows, notes, nogo, facts), as preflight returns them. Also used by wp-site-import on the VM."""
    pj = a.find(".", "package.json")
    if pj is None:
        raise ArchiveError("no package.json: this is not an All-in-One WP Migration archive")
    if pj[2] > JSON_LIMIT:
        raise ArchiveError("package.json is larger than %d MB" % (JSON_LIMIT >> 20))
    try:
        meta = json.loads(a.read(pj, JSON_LIMIT))
    except (ValueError, RecursionError):
        raise ArchiveError("package.json is not valid JSON")
    wp, php, plugin_entries, theme_names = check_meta(meta)
    if a.find(".", "database.sql") is None:
        raise ArchiveError("no database.sql in the archive")
    nogo, notes, rows = [], [], []
    for name in ("multisite.json", "blogs.json"):
        if a.find(".", name):
            nogo.append("this is a multisite network (%s): not supported" % name)
    for bad in a.unsafe_paths():
        nogo.append("unsafe path in the archive: %s" % bad)
    if vtuple(wp) > vtuple(our_wp):
        nogo.append("the archive's WordPress %s is newer than the sites' %s: wait for the image to catch up" % (wp, our_wp))
    elif vtuple(wp) < vtuple(our_wp):
        notes.append("the archive's WordPress %s is older than the sites' %s: the database will be upgraded (wp core update-db)" % (wp, our_wp))
    if php and vtuple(php)[:2] != vtuple(our_php)[:2]:
        notes.append("the archive ran PHP %s, the sites run %s: check the plugins on the first page loads" % (php, our_php))
    for entry in plugin_entries:
        slug, _, mainfile = str(entry).partition("/")
        path_, name = ("plugins/" + slug, mainfile) if mainfile else ("plugins", slug)
        e = a.find(path_, name)
        rows.append(judge_item("plugins", slug if mainfile else name.rsplit(".", 1)[0], a.read(e, HEAD_BYTES) if e else None, get, our_php))
    for theme in theme_names:
        e = a.find("themes/" + theme, "style.css")
        rows.append(judge_item("themes", theme, a.read(e, HEAD_BYTES) if e else None, get, our_php))
    for r in rows:
        if r["verdict"] == "nogo":
            nogo.append("%s %s: %s" % (r["kind"], r["slug"], r["why"]))
        elif r["verdict"] == "note":
            notes.append("%s %s: %s" % (r["kind"], r["slug"], r["why"]))
    active = {r["slug"] for r in rows if r["kind"] == "plugin"}
    on_disk = sorted({p.split("/")[1] for _, p, _, _ in a.entries if p.startswith("plugins/") and p.count("/") >= 1} - active)
    if on_disk:
        notes.append("%d installed but inactive plugin folder(s) (%s): not reinstalled unless you ask" % (len(on_disk), ", ".join(on_disk)))
    facts = {"archive_wp": wp, "our_wp": our_wp, "archive_php": php, "our_php": our_php, "entries": len(a.entries), "size_mb": round(a.length / 1048576)}
    return rows, notes, nogo, facts


def report(rows, notes, nogo, facts, out=print):
    out("archive: WordPress %(archive_wp)s, PHP %(archive_php)s, %(entries)d entries, %(size_mb)d MB.  sites: WordPress %(our_wp)s, PHP %(our_php)s" % facts)
    out("\n%-8s %-42s %-10s %-10s %-8s %s" % ("KIND", "SLUG", "ARCHIVE", "NEWEST", "PHP", "VERDICT"))
    for r in rows:
        out("%-8s %-42s %-10s %-10s %-8s %s%s" % (r["kind"], r["slug"][:42], r["have"] or "?", r["latest"], r["requires_php"], r["verdict"], ("  " + r["why"]) if r["why"] else ""))
    for n in notes:
        out("note: " + n)
    for n in nogo:
        out("NO-GO: " + n)
    out("\n" + ("NO-GO" if nogo else "GO with notes" if notes else "GO"))


def main(argv=None, repo=REPO, get=fetch, out=print):
    p = argparse.ArgumentParser(description="Judge a .wpress archive against the WordPress and PHP the sites run. Writes nothing.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="read package.json and the plugin and theme headers, ask wordpress.org, print GO / NO-GO").add_argument("file")
    args = p.parse_args(argv)
    try:
        rows, notes, nogo, facts = preflight(args.file, repo, get)
    except ArchiveError as e:
        out("NO-GO: %s" % e)
        return 1
    except (urllib.error.URLError, OSError) as e:
        out("NOT JUDGED: wordpress.org could not be asked (%s); nothing is claimed. Run it again." % e)
        return 2
    report(rows, notes, nogo, facts, out)
    return 1 if nogo else 0


if __name__ == "__main__":
    sys.exit(main())
