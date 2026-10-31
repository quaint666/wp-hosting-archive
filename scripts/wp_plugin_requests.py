#!/usr/bin/env python3
"""wp-plugin-requests: the operator's side of "Install" in a site's wp-admin (templates/wp/mu/50-wp-hosting-plugin-requests.php). Standard library only; copied to the VM with the other tool modules and run
through the root-only wrapper templates/wp/wp_plugin_requests.py.j2 (tests/test_wp_plugin_requests.py runs it against stand-ins).

    wp-plugin-requests collect [--no-notify] [site]     read each site's waiting requests, remember the new ones, and send each a notification with Approve and Deny buttons
    wp-plugin-requests list                             the requests it remembers, as one JSON line (the control page reads this)
    wp-plugin-requests decide <site> <id> approve|deny [--accept-risk <slug>]
    wp-plugin-requests resolve <token>                  a button on the phone (the approval listener passes the token; nothing else reaches this)

A site asks by writing the option wph_plugin_requests; the answer goes into wph_plugin_request_results, which only this program writes. Both are read and written with WP-CLI as the web app's identity (wp-site-cli), so
root never opens a file the site controls. EVERYTHING in the site's option is the site's word: a request is accepted only if its id, slug, time and user are the shapes the plugin writes, a site may have at most
max_pending_per_site waiting, and nothing is trusted to be on wordpress.org until wordpress.org says so (wp-site-cli ... plugin search). A request that is not found there, that names a plugin version in the vulnerability
index, or whose check could not be run has NO Approve button on the phone: "not checked" is never a pass. At the control page it can still be approved on purpose, by typing the plugin's name (--accept-risk).

A request is for a plugin (the default) or a theme (`kind` "theme": the same rules, `theme search`, `wp-vulnfeed check-theme`, `wp-site-ext <site> theme install`). Approve installs the plugin or theme from wordpress.org by slug with wp-site-ext (checksum checked, removed again if it does not match), INACTIVE: activating runs the plugin's code, and that stays the administrator's or the
operator's own step. Then the page cache is cleared (wp_ctl.purge_page_cache) and the result is written back for the site's administrator to read. A token is single use and bound to one request.
Exit status: 0 done, 1 the install failed, 2 refused or usage, 3 nothing could be read at all."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import wp_approvals as ap
import wp_ctl
import wp_wpress as w

SITE_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}\Z")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}\Z")
ID_RE = re.compile(r"^[a-f0-9]{8,24}\Z")
USER_RE = re.compile(r"[^A-Za-z0-9_.@ -]")
VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.+_-]{0,39}\Z")
DAY = 86400
PENDING, INSTALLING, INSTALLED, FAILED, DENIED, EXPIRED = "pending", "installing", "installed", "failed", "denied", "expired"
KEEP_FINISHED_DAYS = 60
KINDS = ("plugin", "theme")
RESULTS_KEEP = 50


class Refuse(Exception):
    """A request that was not carried out; the text is for the operator or the control page."""


# ---- the store: one root-only JSON file, changed under a lock

@contextmanager
def locked(cfg):
    d = Path(cfg["state_dir"])
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(d / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load(cfg):
    p = Path(cfg["state_dir"]) / "requests.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return data.get("requests", {}) if isinstance(data, dict) and isinstance(data.get("requests"), dict) else {}


def save(cfg, requests):
    p = Path(cfg["state_dir"]) / "requests.json"
    tmp = p.with_name(p.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"v": 1, "requests": requests}, f, sort_keys=True)
    os.replace(tmp, p)


def key_of(site, rid):
    return "%s:%s" % (site, rid)


def identity_sha(site, slug, rid, kind="plugin"):
    """What a token is bound to. A plugin request keeps the form it always had (tokens in flight stay good); a theme request has its own, so neither kind's token fits the other."""
    return hashlib.sha256((("plugin-request:%s:%s:%s" if kind == "plugin" else kind + "-request:%s:%s:%s") % (site, slug, rid)).encode()).hexdigest()


def audit(cfg, run, text):
    """One line in the journal for every decision (the control page's own audit records the click; this records what the program did, including a phone's)."""
    try:
        run([cfg["logger"], "-t", "wp-plugin-requests", "--", text], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


# ---- WP-CLI, as the web app's identity

def cli(cfg, run, site, *args, timeout=None):
    """(returncode, output) of wp-site-cli for a site. Never raises for a slow or missing tool."""
    cmd = [os.path.join(cfg["tools_bin"], "wp-site-cli"), site] + list(args)
    try:
        p = run(cmd, capture_output=True, text=True, timeout=timeout or cfg["cli_timeout_s"], stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "the command did not answer in time"
    except OSError as e:
        return 127, "the command could not be started (%s)" % (e.strerror or e)


def json_line(text, opener="["):
    """The last line of WP-CLI's output that is JSON of the wanted kind (plugins print notices before it)."""
    for line in reversed(str(text).splitlines()):
        line = line.strip()
        if line.startswith(opener):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def clean_entry(raw):
    """A request as the plugin writes it, or None. The id, the slug, the time and the user name must be the shapes the plugin makes; anything else is dropped without a word."""
    if not isinstance(raw, dict):
        return None
    rid, slug, t, user, kind = raw.get("id"), raw.get("slug"), raw.get("t"), raw.get("user", ""), raw.get("kind", "plugin")
    if kind not in KINDS:
        return None
    if not (isinstance(rid, str) and ID_RE.fullmatch(rid) and isinstance(slug, str) and SLUG_RE.fullmatch(slug) and isinstance(t, int) and not isinstance(t, bool) and t > 0):
        return None
    return {"id": rid, "slug": slug, "t": t, "kind": kind, "user": USER_RE.sub("", user)[:60] if isinstance(user, str) else ""}


def sites_to_ask(cfg, only=None):
    """Sites with a registry record that are live or on hold (a stopped site has no containers to ask)."""
    found = []
    try:
        names = sorted(os.listdir(cfg["data_dir"]))
    except OSError:
        return found
    for name in names:
        if only and name != only:
            continue
        rec = Path(cfg["data_dir"]) / name / "site.json"
        if not SITE_RE.fullmatch(name) or not rec.is_file() or rec.is_symlink():
            continue
        try:
            state = json.loads(rec.read_text()).get("state")
        except (OSError, ValueError, AttributeError):
            continue
        if state in ("live", "hold"):
            found.append(name)
    return found


def read_site(cfg, run, site):
    """(entries, problem) of one site's waiting requests."""
    rc, text = cli(cfg, run, site, "option", "get", "wph_plugin_requests", "--format=json", "--skip-plugins", "--skip-themes")
    if rc != 0:
        if rc == 1 and "Could not get" in text:
            return [], None
        return [], "could not be read (status %d): %s" % (rc, w.printable(" ".join(text.split())[-120:]))
    data = json_line(text)
    if data is None and text.strip() in ("", "[]"):
        return [], None
    if not isinstance(data, list):
        return [], "has an option that is not a list"
    return [e for e in (clean_entry(x) for x in data) if e], None


def write_result(cfg, run, site, rid, status, note):
    """Tell the site what became of a request: merge one answer into wph_plugin_request_results. A failure is reported, never raised: the decision stands."""
    rc, text = cli(cfg, run, site, "option", "get", "wph_plugin_request_results", "--format=json", "--skip-plugins", "--skip-themes")
    current = json_line(text, "{") if rc == 0 else None
    current = current if isinstance(current, dict) else {}
    current[rid] = {"status": status, "note": w.printable(note, limit=120)}
    current = dict(list(current.items())[-RESULTS_KEEP:])
    rc, text = cli(cfg, run, site, "option", "update", "wph_plugin_request_results", json.dumps(current, sort_keys=True), "--format=json", "--skip-plugins", "--skip-themes")
    return "" if rc == 0 else "the answer could not be written to the site (status %d)" % rc


# ---- looking a plugin up before anyone is asked to approve it

def lookup(cfg, run, site, slug, kind="plugin"):
    """{name, version, installs, rating, updated} of the plugin or theme wordpress.org has under exactly this slug, or None when it has none; raises Refuse when wordpress.org could not be asked.
    A theme has no install count or update date in the answer: those two stay empty."""
    fields = "slug,name,version,rating,active_installs,last_updated" if kind == "plugin" else "slug,name,version,rating"
    rc, text = cli(cfg, run, site, kind, "search", slug, "--per-page=10", "--fields=" + fields, "--format=json", "--skip-plugins", "--skip-themes")
    rows = json_line(text)
    if rc != 0 or not isinstance(rows, list):
        raise Refuse("wordpress.org could not be asked about %s (status %d)" % (slug, rc))
    for r in rows:
        if isinstance(r, dict) and r.get("slug") == slug:
            version = r.get("version") if isinstance(r.get("version"), str) and VERSION_RE.fullmatch(r.get("version")) else ""
            return {"name": w.printable(r.get("name", slug), limit=80), "version": version, "installs": r.get("active_installs") if isinstance(r.get("active_installs"), int) else None,
                    "rating": r.get("rating") if isinstance(r.get("rating"), (int, float)) and not isinstance(r.get("rating"), bool) else None, "updated": w.printable(str(r.get("last_updated", "")), limit=30)}
    return None


def vulnerabilities(cfg, run, slug, version, kind="plugin"):
    """('clean'|'found'|'unchecked', one line). Exit codes are wp-vulnfeed's: 0 nothing known, 1 found, 3 not run; a version it cannot name is unchecked."""
    if not version:
        return "unchecked", "no version to check"
    try:
        p = run([os.path.join(cfg["tools_bin"], "wp-vulnfeed"), "check-" + kind, slug, version], capture_output=True, text=True, timeout=cfg["cli_timeout_s"], stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return "unchecked", "the vulnerability check could not run (%s)" % w.printable(e, limit=60)
    lines = [l for l in ((p.stdout or "") + "\n" + (p.stderr or "")).splitlines() if l.strip()]
    first = w.printable(lines[0], limit=200) if lines else "no output"
    return ("clean" if p.returncode == 0 else "found" if p.returncode == 1 else "unchecked"), first


def named(kind, slug):
    """How a message names what was asked for: a plugin by its slug, as it always was, a theme as 'theme <slug>'."""
    return slug if kind == "plugin" else "%s %s" % (kind, slug)


def can_approve_by_phone(entry):
    return bool(entry.get("found")) and entry.get("vuln") == "clean"


# ---- the phone

def message_body(entry):
    bits = []
    if entry.get("found"):
        bits.append("%s %s%s%s%s" % (entry.get("name", entry["slug"]), entry.get("version") or "?", (", %s+ installs" % entry["installs"]) if entry.get("installs") is not None else "",
                                     (", rating %s" % entry["rating"]) if entry.get("rating") is not None else "", (", updated %s" % entry["updated"]) if entry.get("updated") else ""))
    else:
        bits.append("NOT FOUND on wordpress.org under that name")
    bits.append({"clean": "Vulnerabilities: none known.", "found": "Vulnerabilities: " + entry.get("vuln_line", "?"), "unchecked": "Vulnerabilities: NOT CHECKED (%s)." % entry.get("vuln_line", "?")}.get(entry.get("vuln"), "Vulnerabilities: ?"))
    bits.append("Approve installs it inactive; they activate it." if can_approve_by_phone(entry) else "No Approve button: decide at the control page, where it can still be approved on purpose.")
    return "%s asks to install %s on %s.\n%s" % (entry.get("user") or "someone", named(entry.get("kind", "plugin"), entry["slug"]), entry["site"], "\n".join(bits))


def notify(cfg, entry, now, post):
    """Send the notification. Returns '' when sent, otherwise why not. The tokens are stored only after the server took the message."""
    a = cfg.get("approvals")
    if not a:
        return "phone approval is not configured"
    try:
        key = ap.read_key(a["key_file"])
        url, ask_topic = ap.read_conf(a["ntfy_conf"])
        sender = ap.sender_auth(a["netrc"], url)
        button = ap.approver_auth(a["approver_file"])
        sha = identity_sha(entry["site"], entry["slug"], entry["id"], entry.get("kind", "plugin"))
        tokens, issued = {}, {}
        for action in (("approve",) if can_approve_by_phone(entry) else ()) + ("deny",):
            tokens[action], nonce = ap.mint_plugin(key, entry["site"], action, sha, a["ttl_s"], now)
            issued[nonce] = action
        (post or ap.post_json)(url, sender, ap.build_message(url, ask_topic, a["reply_topic"], button, entry["site"], message_body(entry), tokens,
                                                              title="%s request: %s" % (entry.get("kind", "plugin").capitalize(), entry["site"]), tags=["jigsaw"]))
        entry["tokens"] = issued
        return ""
    except ap.ApprovalError as e:
        return "phone approval is off or failed: %s" % e
    except Exception as e:        # a notification must never stop the collection
        return "the phone notification failed: %s" % w.printable(e, limit=120)


# ---- collect

def cmd_collect(cfg, only, notify_phone, out, run, clock, post):
    now = int(clock())
    sites = sites_to_ask(cfg, only)
    if only and not sites:
        raise Refuse("%s is not a live or held site" % w.printable(only, limit=40))
    read_any = False
    with locked(cfg):
        store = load(cfg)
        for key, e in list(store.items()):                 # old business first: expire what waited too long, forget what finished long ago
            if e.get("state") == PENDING and now - e.get("first_seen", now) > cfg["expire_days"] * DAY:
                e["state"], e["tokens"] = EXPIRED, {}
            if e.get("state") in (INSTALLED, FAILED, DENIED, EXPIRED) and now - e.get("first_seen", now) > KEEP_FINISHED_DAYS * DAY:
                del store[key]
        for site in sites:
            entries, problem = read_site(cfg, run, site)
            if problem:
                out("%s: %s" % (site, problem))
                continue
            read_any = True
            waiting = sum(1 for e in store.values() if e["site"] == site and e["state"] in (PENDING, INSTALLING))
            new = 0
            for raw in entries:
                key = key_of(site, raw["id"])
                if key in store or now - raw["t"] > cfg["expire_days"] * DAY or raw["t"] > now + DAY:
                    continue
                if any(e["site"] == site and e["slug"] == raw["slug"] and e.get("kind", "plugin") == raw["kind"] and e["state"] in (PENDING, INSTALLING) for e in store.values()) or waiting >= cfg["max_pending_per_site"]:
                    continue
                store[key] = dict(raw, site=site, first_seen=now, state=PENDING, note="", tokens={}, notified=False)
                waiting += 1
                new += 1
            out("%s: %d request%s read, %d new" % (site, len(entries), "" if len(entries) == 1 else "s", new))
        for key, e in sorted(store.items()):
            if e["state"] != PENDING or "found" in e:
                continue
            try:                                           # who it is and whether it is known to be bad, once, when it first appears
                info = lookup(cfg, run, e["site"], e["slug"], e.get("kind", "plugin"))
            except Refuse as err:
                e["note"] = str(err)
                continue
            e["found"] = info is not None
            if info:
                e.update(info)
                e["vuln"], e["vuln_line"] = vulnerabilities(cfg, run, e["slug"], info["version"], e.get("kind", "plugin"))
            else:
                e["vuln"], e["vuln_line"] = "unchecked", "not on wordpress.org"
        if notify_phone:
            for key, e in sorted(store.items()):
                if e["state"] == PENDING and "found" in e and not e["notified"]:
                    why = notify(cfg, e, now, post)
                    e["notified"] = not why
                    if why:
                        out("%s %s: %s" % (e["site"], e["slug"], why))
                    else:
                        out("%s %s: notification sent (%s)" % (e["site"], e["slug"], "Approve and Deny" if can_approve_by_phone(e) else "Deny only"))
        save(cfg, store)
    pending = sum(1 for e in store.values() if e["state"] == PENDING)
    out("%d request%s waiting" % (pending, "" if pending == 1 else "s"))
    return 0 if read_any or not sites else 3


# ---- list

def cmd_list(cfg, out):
    with locked(cfg):
        store = load(cfg)
    rows = []
    for e in store.values():
        rows.append({k: e.get(k) for k in ("site", "id", "slug", "user", "t", "first_seen", "state", "note", "name", "version", "installs", "rating", "updated", "found", "vuln", "vuln_line", "notified")})
        rows[-1]["kind"] = e.get("kind", "plugin")
    rows.sort(key=lambda r: (r["state"] != PENDING, -(r["first_seen"] or 0)))
    out(json.dumps({"ok": True, "requests": rows[:200]}, sort_keys=True))
    return 0


# ---- deciding

def decide(cfg, site, rid, decision, by, accept_slug, out, run, clock):
    """Approve or deny one waiting request. `by` is 'panel' or 'phone' (for the journal). Returns the exit status."""
    if not SITE_RE.fullmatch(site or "") or not ID_RE.fullmatch(rid or "") or decision not in ("approve", "deny"):
        raise Refuse("usage: decide <site> <id> approve|deny")
    with locked(cfg):
        store = load(cfg)
        e = store.get(key_of(site, rid))
        if e is None:
            raise Refuse("there is no such request")
        if e["state"] != PENDING:
            raise Refuse("this request is %s, not waiting" % e["state"])
        slug, kind = e["slug"], e.get("kind", "plugin")
        if decision == "deny":
            e["state"], e["tokens"], e["note"] = DENIED, {}, ""
            save(cfg, store)
            audit(cfg, run, "site=%s request=%s kind=%s slug=%s decision=deny by=%s" % (site, rid, kind, slug, by))
            problem = write_result(cfg, run, site, rid, DENIED, "")
            out("denied %s for %s" % (named(kind, slug), site) + ((" (%s)" % problem) if problem else ""))
            return 0
        if not e.get("found"):
            raise Refuse("%s is not on wordpress.org under that name: nothing to install" % slug)
        if not can_approve_by_phone(e):
            if by != "panel" or accept_slug != slug:
                raise Refuse("%s has %s (%s): approve it only on purpose, from the control page, by typing its name" % (slug, "a known vulnerability" if e.get("vuln") == "found" else "no vulnerability check", e.get("vuln_line", "?")))
        e["state"], e["tokens"] = INSTALLING, {}
        save(cfg, store)                                   # saved before the install starts: a second approval finds it taken
        audit(cfg, run, "site=%s request=%s kind=%s slug=%s decision=approve by=%s%s" % (site, rid, kind, slug, by, " accept-risk" if not can_approve_by_phone(e) else ""))
        try:
            p = run([os.path.join(cfg["tools_bin"], "wp-site-ext"), site, kind, "install", slug], capture_output=True, text=True, timeout=cfg["install_timeout_s"], stdin=subprocess.DEVNULL)
            rc, text = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            rc, text = 124, "the install did not finish in time"
        except OSError as err:
            rc, text = 127, "the install could not be started (%s)" % (err.strerror or err)
        lines = [l for l in text.splitlines() if l.strip()]
        last = w.printable(lines[-1], limit=120) if lines else "no output"
        e["state"], e["note"] = (INSTALLED, "") if rc == 0 else (FAILED, last)
        cleared = wp_ctl.purge_page_cache(cfg) if rc == 0 else None
        save(cfg, store)
        problem = write_result(cfg, run, site, rid, e["state"], e["note"])
        out(("installed %s on %s, inactive" % (named(kind, slug), site) + (("; page cache: %d files cleared" % cleared) if cleared is not None else "")) if rc == 0 else "the install of %s on %s failed: %s" % (named(kind, slug), site, last))
        if problem:
            out(problem)
        return 0 if rc == 0 else 1


def cmd_resolve(cfg, token, out, run, clock):
    """What a valid token may do: approve a request that is clean (no override of any kind) or deny it. Consumed before anything runs."""
    a = cfg.get("approvals")
    if not a:
        raise Refuse("phone approval is not configured")
    try:
        d = ap.verify_plugin(ap.read_key(a["key_file"]), token, clock())
    except ap.ApprovalError as e:
        raise Refuse("token refused: %s" % e)
    with locked(cfg):
        store = load(cfg)
        mine = [e for e in store.values() if e["site"] == d["s"] and identity_sha(e["site"], e["slug"], e["id"], e.get("kind", "plugin")) == d["r"]]
        if not mine:
            raise Refuse("token refused: it was not issued for a request this host remembers")
        e = mine[0]
        if e["state"] != PENDING:
            raise Refuse("token refused: the request is %s, not waiting" % e["state"])
        if not isinstance(e.get("tokens"), dict) or e["tokens"].get(d["n"]) != d["a"]:
            raise Refuse("token refused: it was not issued for this request, or it was used")
        if d["a"] == "approve" and not can_approve_by_phone(e):
            raise Refuse("not approved: this request is not clean; decide at the control page")
        e["tokens"] = {}                                   # one use: consumed before anything runs
        save(cfg, store)
        site, rid, action = e["site"], e["id"], d["a"]
    return decide(cfg, site, rid, action, "phone", None, out, run, clock)


# ---- the command line

USAGE = "usage: wp-plugin-requests collect [--no-notify] [site] | list | decide <site> <id> approve|deny [--accept-risk <slug>] | resolve <token>"


def main(cfg, argv, out=print, run=subprocess.run, clock=time.time, post=None):
    try:
        if argv[:1] == ["collect"]:
            rest = argv[1:]
            quiet = "--no-notify" in rest
            rest = [x for x in rest if x != "--no-notify"]
            if len(rest) > 1 or (rest and not SITE_RE.fullmatch(rest[0])):
                raise Refuse(USAGE)
            return cmd_collect(cfg, rest[0] if rest else None, not quiet, out, run, clock, post)
        if argv == ["list"]:
            return cmd_list(cfg, out)
        if argv[:1] == ["decide"] and len(argv) in (4, 6):
            slug = None
            if len(argv) == 6:
                if argv[4] != "--accept-risk":
                    raise Refuse(USAGE)
                slug = argv[5]
            return decide(cfg, argv[1], argv[2], argv[3], "panel", slug, out, run, clock)
        if argv[:1] == ["resolve"] and len(argv) == 2:
            return cmd_resolve(cfg, argv[1], out, run, clock)
        raise Refuse(USAGE)
    except Refuse as e:
        out("wp-plugin-requests: %s" % e)
        return 2
    except OSError as e:
        out("wp-plugin-requests: %s" % w.printable(e, limit=160))
        return 2
