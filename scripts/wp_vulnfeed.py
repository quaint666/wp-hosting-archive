#!/usr/bin/env python3
"""wp-vulnfeed: which of the plugins, themes and core a site runs are in the Wordfence Intelligence vulnerability feed. Standard library only; copied to the VM with the other tool modules and
run through the root-only wrapper templates/wp/wp_vulnfeed.py.j2 (tests/test_wp_vulnfeed.py runs it against stand-ins).

    wp-vulnfeed update                 download the production feed (a free API key, sent as a Bearer token) and reduce it to a compact index
    wp-vulnfeed check <site>           what the site runs against the index; the lines it prints are the answer
    wp-vulnfeed check-all              every site that is up and has WordPress; a NEW hit goes to the phone through ntfy-notify, a known one is not told again

Exit codes (the same convention as wp-clamscan, which wp-site-import reads): 0 nothing known, 1 something found, 3 NOT RUN (no key, no index yet, or an index too old to trust: a state, never a pass),
2 anything else (a feed that refuses, a site that cannot be read). The index is old when it is more than max_age_days days: a feed that has stopped updating must not say "none known".

The feed is about 120 MB of JSON. It is streamed to a private file, parsed once by the update (run under a memory limit by its unit) and reduced to what a check needs: per software, the version ranges,
the title, the CVE, the score and the versions that fix it. Checks read only the reduced index. Nothing here runs plugin code: versions are read with WP-CLI with plugins and themes skipped.

Record shape (v3, wordfence.com/help/wordfence-intelligence/v3-accessing-and-consuming-the-vulnerability-data-feed): each record has title, cve, cvss {score, rating}, and software[] with type
(plugin, theme, core), slug, affected_versions {"<range>": {from_version, from_inclusive, to_version, to_inclusive}} and patched_versions[]. A bound of "*" means unbounded."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SITE_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}\Z")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,512}\Z")
KINDS = ("plugin", "theme", "core")
CHUNK = 1 << 20
MAX_FEED_BYTES = 1 << 30
INDEX_VERSION = 1
TITLE_MAX = 160


class FeedError(Exception):
    """A failure that is the tool's answer (exit 2)."""


class NotRun(Exception):
    """Not set up, or too old to trust (exit 3). The message says why."""


def printable(text, limit=200):
    out = "".join("?" if ord(c) < 32 or 127 <= ord(c) <= 159 else c for c in str(text))
    return out if len(out) <= limit else out[:limit] + "..."


# ---- versions

def vkey(version):
    """A sortable key: the numbers of the version padded to six, and a flag that puts 1.0-beta below 1.0."""
    text = str(version).strip().lstrip("vV")
    nums = [int(x) for x in re.findall(r"\d+", re.split(r"[-+_ ]", text, maxsplit=1)[0])][:6]
    pre = bool(re.search(r"[A-Za-z]", re.sub(r"^[0-9.]+", "", text)))
    return tuple(nums + [0] * (6 - len(nums))), 0 if pre else 1


def in_range(version, rng):
    lo, hi = rng[0], rng[2]
    v = vkey(version)
    if lo not in (None, "*", "") and (v < vkey(lo) or (v == vkey(lo) and not rng[1])):
        return False
    if hi not in (None, "*", "") and (v > vkey(hi) or (v == vkey(hi) and not rng[3])):
        return False
    return True


# ---- the feed and its index

def read_key(path):
    try:
        text = Path(path).read_text().strip()
    except OSError:
        raise NotRun("there is no feed key (%s): run scripts/wp_provider_key.sh wordfence, then dellwp/wp_scans_dellwp.yml" % path)
    if not KEY_RE.match(text):
        raise NotRun("the feed key in %s does not look like a key: run scripts/wp_provider_key.sh wordfence again" % path)
    return text


def download(url, key, dest, opener=urllib.request.urlopen, timeout=180):
    """Stream the feed to dest (a new private file). Returns the number of bytes. The key goes only in the Authorization header."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key, "Accept": "application/json", "User-Agent": "wp-vulnfeed"})
    try:
        resp = opener(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        e.close()
        why = {401: "the key was refused", 403: "the key was refused", 429: "rate limited: try again later", 410: "this version of the feed has been switched off"}.get(e.code, "the feed answered")
        raise FeedError("%s (HTTP %d)" % (why, e.code))
    except (urllib.error.URLError, OSError) as e:
        raise FeedError("the feed could not be reached (%s)" % printable(getattr(e, "reason", None) or e, 100))
    total = 0
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f, resp:
            for chunk in iter(lambda: resp.read(CHUNK), b""):
                total += len(chunk)
                if total > MAX_FEED_BYTES:
                    raise FeedError("the feed is larger than %d MB: refusing it" % (MAX_FEED_BYTES >> 20))
                f.write(chunk)
    except OSError as e:
        raise FeedError("the feed download broke off (%s)" % printable(e, 100))
    return total


def build_index(feed):
    """The reduced index out of a parsed feed (an object of records, or a list of them). Records that are not what they should be are skipped; a feed with none usable is an error."""
    records = list(feed.values()) if isinstance(feed, dict) else feed
    if not isinstance(records, list):
        raise FeedError("the feed is not a list or an object of records")
    entries, used = {}, 0
    for rec in records:
        if not isinstance(rec, dict) or not isinstance(rec.get("software"), list):
            continue
        cvss = rec.get("cvss") if isinstance(rec.get("cvss"), dict) else {}
        score = cvss.get("score") if isinstance(cvss.get("score"), (int, float)) and not isinstance(cvss.get("score"), bool) else None
        for sw in rec["software"]:
            if not isinstance(sw, dict) or sw.get("type") not in KINDS or not isinstance(sw.get("slug"), str) or not SLUG_RE.match(sw["slug"]):
                continue
            av = sw.get("affected_versions")
            ranges = []
            for r in (av.values() if isinstance(av, dict) else []):
                if isinstance(r, dict):
                    ranges.append([r.get("from_version"), bool(r.get("from_inclusive", True)), r.get("to_version"), bool(r.get("to_inclusive", True))])
            if not ranges:
                continue
            patched = [str(x)[:40] for x in sw.get("patched_versions", []) if isinstance(x, (str, int, float))][:5] if isinstance(sw.get("patched_versions"), list) else []
            entries.setdefault("%s:%s" % (sw["type"], sw["slug"]), []).append(
                {"title": printable(rec.get("title", "?"), TITLE_MAX), "cve": rec.get("cve") if isinstance(rec.get("cve"), str) and len(rec["cve"]) < 40 else None,
                 "score": score, "ranges": ranges, "patched": patched})
            used += 1
    if used == 0:
        raise FeedError("the feed has no usable record: refusing to replace the index with an empty one")
    return {"v": INDEX_VERSION, "entries": entries, "records": used}


def write_index(path, index, clock=time.time):
    index = dict(index, built=int(clock()))
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".index-", suffix=".new")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(index, f, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_index(path, max_age_days, clock=time.time):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise NotRun("the vulnerability feed has not been downloaded yet (wp-vulnfeed update)")
    if not isinstance(data, dict) or data.get("v") != INDEX_VERSION or not isinstance(data.get("entries"), dict) or not isinstance(data.get("built"), int):
        raise NotRun("the vulnerability index is not in a form this tool reads: run wp-vulnfeed update")
    age_days = (clock() - data["built"]) / 86400
    if age_days > max_age_days:
        raise NotRun("the vulnerability index is %d days old (the limit is %d): the daily update is failing" % (age_days, max_age_days))
    return data


def matches(index, kind, slug, version):
    hits = [e for e in index["entries"].get("%s:%s" % (kind, slug), []) if any(in_range(version, r) for r in e["ranges"])]
    return sorted(hits, key=lambda e: -(e["score"] or 0))


def describe(kind, slug, version, e):
    bits = [x for x in (e["cve"], ("CVSS %s" % e["score"]) if e["score"] is not None else None, ("fixed in %s" % ", ".join(e["patched"])) if e["patched"] else "no fix known") if x]
    return "%s %s %s: %s (%s)" % (kind, slug, printable(version, 40), e["title"], ", ".join(bits))


# ---- what a site runs

def parse_json_list(text):
    """WP-CLI's JSON list out of its output (a line that starts with [ and is valid JSON). Raises FeedError when there is none."""
    for line in reversed(str(text).splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, list):
                return data
    raise FeedError("WP-CLI printed no list")


def software_of(site, cfg, run):
    """[(kind, slug, version)] of a site, or None when WordPress is not installed there yet. Plugins and themes are skipped when WP-CLI runs: only versions are read."""
    cli = os.path.join(cfg["tools_bin"], "wp-site-cli")
    flags = ["--skip-plugins", "--skip-themes"]

    def ask(*args):
        p = run([cli, site, *args, *flags], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=cfg["timeout_s"])
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    rc, _, _ = ask("core", "is-installed")
    if rc != 0:
        return None
    rc, out, err = ask("core", "version")
    version = out.splitlines()[-1].strip() if out else ""
    if rc != 0 or not re.match(r"^[0-9][0-9A-Za-z.+-]*$", version):
        raise FeedError("%s: WordPress did not tell its version (%s)" % (site, printable(err or out, 100)))
    found = [("core", "wordpress", version)]
    for kind, sub in (("plugin", "plugin"), ("theme", "theme")):
        rc, out, err = ask(sub, "list", "--fields=name,version", "--format=json")
        if rc != 0:
            raise FeedError("%s: the %s list could not be read (%s)" % (site, kind, printable(err or out, 100)))
        for row in parse_json_list(out):
            if isinstance(row, dict) and isinstance(row.get("name"), str) and SLUG_RE.match(row["name"]) and isinstance(row.get("version"), str):
                found.append((kind, row["name"], row["version"]))
    return found


def check_site(site, cfg, run, index):
    """(software count or None, [(kind, slug, version, entry)])."""
    software = software_of(site, cfg, run)
    if software is None:
        return None, []
    return len(software), [(k, s, v, e) for k, s, v in software for e in matches(index, k, s, v)]


# ---- commands

def cmd_update(cfg, out, opener, clock):
    key = read_key(cfg["key_file"])
    state = Path(cfg["index_file"]).parent
    fd, tmp = tempfile.mkstemp(dir=str(state), prefix=".feed-", suffix=".json")
    os.close(fd)
    os.unlink(tmp)
    try:
        size = download(cfg["feed_url"], key, tmp, opener)
        try:
            with open(tmp, "rb") as f:
                feed = json.load(f)
        except (ValueError, MemoryError):
            raise FeedError("the feed is not valid JSON (%d bytes)" % size)
        index = build_index(feed)
        del feed
        write_index(cfg["index_file"], index, clock)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    out("feed updated: %d MB downloaded, %d vulnerabilities for %d plugins, themes and core versions" % (size >> 20, index["records"], len(index["entries"])))
    return 0


def cmd_check(cfg, site, out, run, clock):
    if not SITE_RE.match(site or "") or site not in cfg["sites"]:
        raise FeedError("'%s' is not a site this host knows" % printable(site, 40))
    index = load_index(cfg["index_file"], cfg["max_age_days"], clock)
    count, hits = check_site(site, cfg, run, index)
    if count is None:
        out("%s has no WordPress yet: nothing to check" % site)
        return 0
    for kind, slug, version, e in hits:
        out(describe(kind, slug, version, e))
    if not hits:
        out("%s: %d plugins, themes and core checked against %d records; none known" % (site, count, index.get("records", 0)))
    return 1 if hits else 0


def cmd_check_all(cfg, out, run, clock, notify):
    index = load_index(cfg["index_file"], cfg["max_age_days"], clock)
    state = Path(cfg["state_dir"])
    rc = 0
    for site in sorted(cfg["sites"]):
        if os.path.exists(os.path.join(cfg["data_dir"], site, "stopped.flag")):
            continue
        marker = state / ("seen-%s.json" % site)
        try:
            count, hits = check_site(site, cfg, run, index)
        except FeedError as e:
            out("%s: %s" % (site, e))
            rc = 2
            continue
        if count is None:
            continue
        ids = sorted({"%s:%s:%s:%s" % (k, s, v, e["title"][:60]) for k, s, v, e in hits})
        try:
            before = set(json.loads(marker.read_text())) if marker.is_file() and not marker.is_symlink() else set()
        except (OSError, ValueError):
            before = set()
        fresh = [h for h in hits if "%s:%s:%s:%s" % (h[0], h[1], h[2], h[3]["title"][:60]) not in before]
        for k, s, v, e in hits:
            out("%s: %s" % (site, describe(k, s, v, e)))
        if fresh:
            notify("%s: %d new known vulnerabilit%s: %s" % (site, len(fresh), "y" if len(fresh) == 1 else "ies", "; ".join(describe(*h[:3], h[3]) for h in fresh[:3]) + (" ..." if len(fresh) > 3 else "")))
        marker.write_text(json.dumps(ids))      # what is there now: a vulnerability that goes away and comes back is told again
    return rc


USAGE = "usage: wp-vulnfeed update | check <site> | check-all"


def main(cfg, argv, out=print, run=subprocess.run, opener=urllib.request.urlopen, clock=time.time, notify=None):
    if notify is None:
        def notify(text):
            try:
                run([cfg["notify_cmd"], cfg["ntfy_topic"], "%s %s" % (cfg["host_name"], text)], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.SubprocessError) as e:
                out("could not notify: %s" % printable(e, 80))
    try:
        if argv == ["update"]:
            return cmd_update(cfg, out, opener, clock)
        if len(argv) == 2 and argv[0] == "check":
            return cmd_check(cfg, argv[1], out, run, clock)
        if argv == ["check-all"]:
            return cmd_check_all(cfg, out, run, clock, notify)
        out(USAGE)
        return 2
    except NotRun as e:
        out("NOT RUN: %s" % e)
        return 3
    except FeedError as e:
        out("wp-vulnfeed: %s" % e)
        return 2
    except subprocess.TimeoutExpired as e:
        out("wp-vulnfeed: a command did not answer in time: %s" % printable(" ".join(map(str, e.cmd)), 100))
        return 2
    except OSError as e:
        out("wp-vulnfeed: %s" % printable(e, 160))
        return 2
