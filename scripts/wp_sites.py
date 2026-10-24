#!/usr/bin/env python3
"""Manage the WordPress sites on dellwp from one place: list them, create one, change one.

    python3 scripts/wp_sites.py                       a numbered menu
    python3 scripts/wp_sites.py list
    python3 scripts/wp_sites.py create [--name N] [--state hold|live] [--policy auto|notify|freeze] [--title T] [--user U] [--email E] [--set key=value ...] [--yes] [--no-run]
    python3 scripts/wp_sites.py modify <site> [--set key=value ...] [--yes] [--yes-public] [--no-run]

A site is declared in git (`wp_sites` in host_vars/dellwp/vars.yml) and built by the play, so every change here is an edit of that file followed by the play and the verifier. The script
edits only the lines it means to, re-reads the YAML to prove nothing else changed, checks the new list against the rules of the play itself (the assert in tasks/wp_sites_common.yml),
shows the diff and asks. (A new site gets no check run: the play creates its directory and the next task reads it, which check mode cannot do.) It refuses to start if the file has uncommitted changes, so the diff is only this change, and it leaves the commit to you: it prints the command.

A site's hostname is <name>.<wp_domain> and nothing else: no friend-owned domains (the recorded decision), so there is no Terraform step; the wildcard record already covers every name.
The name is the site's identity (directories, containers, quota project), so it cannot be changed; make a new site and import into it. Port and quota project id are chosen above every
value the file has ever held in git, because a quota id must never be reused for another site. The administrator's password is not asked here and cannot be given as an option: wp-site-install
asks for it at a hidden prompt on the VM (this script runs it with ssh -t), so it never passes through this script, a command line, a file or the shell history.

`create` ends with a site on `hold` unless you say otherwise: nginx answers only a request from the VM itself. Going live (public on the internet) asks one more question, whatever else
was answered: --yes does not answer it, --yes-public does. This is what the script cannot do: delete a site (the play only adds; the stopped and closed states are not built), reset a
password, rotate a site's secrets, or prove the public path. Run it from the repo root on the ThinkPad; it needs ssh access to dellwp and the vault password like any play.
"""
import argparse
import copy
import difflib
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
VARS = "host_vars/dellwp/vars.yml"
HOST = "dellwp"
PLAY = "dellwp/wp_sites_dellwp.yml"
NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,22}[a-z0-9])$")   # a valid hostname label (the play's own rule also lets a trailing hyphen through)
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STATES = ("hold", "live")
POLICIES = ("auto", "notify", "freeze")
# What `modify` and `create --set` may change: key -> (kind, minimum, maximum). Minimums are sanity floors, not laws: the play's own assert still decides whether the whole list fits.
EDITABLE = {
    "state": ("choice", STATES),
    "update_policy": ("choice", POLICIES),
    "quota_mb": ("int", 512, 100000),
    "backup_quota_mb": ("int", 512, 100000),
    "php_mem_mb": ("int", 384, 4096),
    "db_mem_mb": ("int", 192, 4096),
    "upload_mb": ("int", 1, 512),
    "allow_xmlrpc": ("bool",),
}
# What each change costs, so the question can say it: the compose file changes (the whole stack restarts, about ten seconds), or only the vhost or php.ini (a reload).
RESTARTS = {"php_mem_mb": "restarts the whole stack, about ten seconds offline", "db_mem_mb": "restarts the whole stack, about ten seconds offline",
            "upload_mb": "reloads nginx and php", "allow_xmlrpc": "reloads nginx"}


class Refuse(Exception):
    """A plain-language reason to stop; nothing has been written."""


# ---------------------------------------------------------------------------------------------------------------------------------------- the file
def render(value):
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def site_span(lines, site):
    """(start, end) of one site's block inside wp_sites: `  - name: x` up to the line after its last indented line (trailing blank lines are not part of it)."""
    top = next((i for i, l in enumerate(lines) if re.match(r"^wp_sites:\s*(#.*)?$", l)), None)
    if top is None:
        raise Refuse("no top-level wp_sites: in %s" % VARS)
    start = next((i for i in range(top + 1, len(lines)) if re.match(r"^  - name:\s*%s\s*(#.*)?$" % re.escape(site), lines[i])), None)
    if start is None:
        raise Refuse("no site '%s' in wp_sites" % site)
    return start, block_end(lines, start)


def block_end(lines, start):
    end = start + 1
    while end < len(lines) and (lines[end].startswith("    ") or lines[end].strip() == "") and not lines[end].startswith("  - "):
        end += 1
    while end > start + 1 and lines[end - 1].strip() == "":
        end -= 1
    return end


def set_site_field(text, site, key, value):
    """The text with one key of one site set; only that line changes (or one line is added to that site's block). Raises Refuse if anything else would differ."""
    lines = text.split("\n")
    start, end = site_span(lines, site)
    for i in range(start + 1, end):
        m = re.match(r"^(    %s:\s*)([^\s#]+)(.*)$" % re.escape(key), lines[i])
        if m:
            lines[i] = m.group(1) + render(value) + m.group(3)
            break
    else:
        last = max(i for i in range(start, end) if lines[i].strip() and not lines[i].lstrip().startswith("#"))
        lines.insert(last + 1, "    %s: %s" % (key, render(value)))
    new = "\n".join(lines)
    expect = copy.deepcopy(yaml.safe_load(text))
    next(s for s in expect["wp_sites"] if s["name"] == site)[key] = value
    if yaml.safe_load(new) != expect:
        raise Refuse("the edit would change more than %s of '%s'; nothing was written" % (key, site))
    return new


def add_site(text, entry):
    """The text with a new site appended to wp_sites, after the last one's block."""
    lines = text.split("\n")
    top = next((i for i, l in enumerate(lines) if re.match(r"^wp_sites:\s*(#.*)?$", l)), None)
    if top is None:
        raise Refuse("no top-level wp_sites: in %s" % VARS)
    starts = [i for i in range(top + 1, len(lines)) if re.match(r"^  - name:", lines[i])]
    if not starts:
        raise Refuse("wp_sites is empty; add the first site by hand")
    at = block_end(lines, starts[-1])
    block = ["  - name: %s" % entry["name"]] + ["    %s: %s" % (k, render(v)) for k, v in entry.items() if k != "name"]
    new = "\n".join(lines[:at] + block + lines[at:])
    expect = copy.deepcopy(yaml.safe_load(text))
    expect["wp_sites"].append(dict(entry))
    if yaml.safe_load(new) != expect:
        raise Refuse("the edit would change more than the new site; nothing was written")
    return new


def ever_used(repo, text, key):
    """Every integer the file has ever held for `key` (in this text and in the git history of all branches), so a new id is above all of them."""
    seen = {int(s[key]) for s in yaml.safe_load(text)["wp_sites"] if key in s}
    log = subprocess.run(["git", "-C", str(repo), "log", "--all", "-p", "--format=", "--", VARS], capture_output=True, text=True)
    for m in re.finditer(r"^[+-]\s+(?:- )?%s:\s*(\d+)" % re.escape(key), log.stdout, re.M):
        seen.add(int(m.group(1)))
    return seen


def validate_value(key, raw):
    """(value, None) or (None, why) for a value typed or given with --set."""
    spec = EDITABLE[key]
    raw = str(raw).strip()
    if spec[0] == "choice":
        return (raw, None) if raw in spec[1] else (None, "%s must be one of %s" % (key, ", ".join(spec[1])))
    if spec[0] == "bool":
        return (raw == "true", None) if raw in ("true", "false") else (None, "%s must be true or false" % key)
    if not re.fullmatch(r"[0-9]{1,6}", raw):
        return None, "%s must be a whole number" % key
    n = int(raw)
    return (n, None) if spec[1] <= n <= spec[2] else (None, "%s must be between %d and %d" % (key, spec[1], spec[2]))


def break_rules(sites, defaults_from_text):
    """The expressions of the play's own assert that the site list would break (empty list = fine). Uses the offline test helper, so the rules exist in one place."""
    sys.path.insert(0, str(REPO / "tests"))
    try:
        import test_wp_sites_render as t
    except ImportError as e:
        raise Refuse("cannot load tests/test_wp_sites_render.py (%s); the rules of the play are checked with it" % e)
    v = t.base_vars()
    v["wp_site_defaults"] = defaults_from_text["wp_site_defaults"]
    v["wp_reserved_names"] = defaults_from_text["wp_reserved_names"]
    return t.failing(t.assertions(), v, sites)


# --------------------------------------------------------------------------------------------------------------------------------------- the tool
class Sites:
    def __init__(self, repo=REPO, ask=input, run=None, out=print):
        self.repo, self.path = Path(repo), Path(repo) / VARS
        self._ask, self.out = ask, out
        self.run = run or self.run_command

    # --- plumbing
    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.repo), *a], capture_output=True, text=True)

    def run_command(self, cmd, tee=False):
        self.out("$ " + " ".join(shlex.quote(c) for c in cmd))
        if not tee:
            return subprocess.run(cmd, cwd=str(self.repo)).returncode, ""
        p = subprocess.Popen(cmd, cwd=str(self.repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = []
        for line in p.stdout:
            print(line, end="")
            lines.append(line)
        return p.wait(), "".join(lines)

    def ask(self, prompt, default=None, check=None):
        """Ask until the answer passes `check` (a function returning an error text or None). No terminal and no answer is an error, never a guess."""
        while True:
            try:
                raw = self._ask("%s%s: " % (prompt, " [%s]" % default if default not in (None, "") else "")).strip()
            except EOFError:
                raise Refuse("no answer to '%s' and no terminal to ask on; give it as an option" % prompt)
            raw = raw or (default or "")
            why = check(raw) if check else None
            if not why:
                return raw
            self.out("  " + why)

    def confirm(self, prompt):
        try:
            return self._ask("%s [y/N] " % prompt).strip().lower() == "y"
        except EOFError:
            return False

    def preflight(self):
        if self.git("diff", "--quiet", "HEAD", "--", VARS).returncode != 0:
            raise Refuse("%s has uncommitted changes; commit or revert them first, so the diff you are asked to approve is only this change.\n%s\n(`git checkout -- %s` throws them away.)"
                         % (VARS, self.git("diff", "HEAD", "--", VARS).stdout, VARS))

    def data(self, text):
        return yaml.safe_load(text)

    def merged(self, d):
        return [{**d["wp_site_defaults"], **s} for s in d["wp_sites"]]

    def check_rules(self, text):
        d = self.data(text)
        bad = break_rules(d["wp_sites"], d)
        if bad:
            raise Refuse("the play would refuse this site list. Rule(s) broken:\n  " + "\n  ".join(bad))

    def apply_and_run(self, old, new, site, no_run, yes, check=True):
        """Show the diff, ask, write, then the play (check run, real run, second run; no check run for a new site, which check mode cannot handle) and the verifier."""
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/" + VARS, "b/" + VARS))
        self.out(diff)
        if not yes and not self.confirm("Write this change to %s?" % VARS):
            self.out("Nothing was written.")
            return 1
        self.path.write_text(new)
        if no_run:
            self.out("Edited only (--no-run). Run %s, then scripts/wp_site_verify_dellwp.sh %s." % (PLAY, site))
            return 0
        play = ["ansible-playbook", "-i", "inventory.yml", PLAY]
        if not check:
            self.out("No check run: check mode cannot get past a site that does not exist yet (the play creates its directory, and the next task reads it; seen on dellwp 2026-10-24),"
                     "\nso the diff above and the rules of the play, checked offline, are the preview, and the first run is the real one.")
        if not yes and check:
            rc, _ = self.run(play + ["--check", "--diff"])
            if rc != 0:
                self.out("The check run failed (exit %d). The file stays edited; `git checkout -- %s` undoes it. Nothing on the host changed." % (rc, VARS))
                return rc
        if not yes:
            if not self.confirm("Run the play for real now?%s" % ("" if check else " (it builds the new site)")):
                self.out("Not run. The file is edited; `git checkout -- %s` undoes it." % VARS)
                return 1
        rc, _ = self.run(play)
        if rc != 0:
            self.out("The play failed (exit %d). The file stays edited and the host may be half-way; run it again once the cause is fixed, or `git checkout -- %s` and run it to undo." % (rc, VARS))
            return rc
        rc, text = self.run(play, tee=True)
        m = re.search(r"^\s*%s\s*:.*?changed=(\d+)" % HOST, text, re.M)
        if rc != 0 or not m or m.group(1) != "0":
            self.out("WARNING: the second run should end changed=0 and did not (%s). Look at what it changed before you trust this." % ("exit %d" % rc if rc else "changed=%s" % (m.group(1) if m else "not found")))
        return 0

    def finish(self, site, what):
        rc, _ = self.run(["bash", "scripts/wp_site_verify_dellwp.sh", site])
        if rc != 0:
            self.out("The verifier reported %d failed check(s) for %s." % (rc, site))
        self.run(["python3", "scripts/wp_sites_status.py"])
        if self.git("diff", "--quiet", "HEAD", "--", VARS).returncode == 0:
            self.out("\nThe file is back to what is committed; there is nothing to commit.")
        else:
            self.out("\nWhen you are satisfied, commit it:\n  git add %s && git commit -m \"ops(wp): %s\"" % (VARS, what))
        return rc

    # --- list
    def list(self):
        return self.run(["python3", "scripts/wp_sites_status.py"])[0]

    # --- create
    def create(self, name=None, state=None, policy=None, title=None, user=None, email=None, sets=(), yes=False, yes_public=False, no_run=False):
        self.preflight()
        text = self.path.read_text()
        d = self.data(text)
        domain, taken = d["wp_domain"], {s["name"] for s in d["wp_sites"]}

        def name_check(v):
            if not NAME_RE.fullmatch(v) or "--" in v:
                return "letters, digits and single hyphens inside, 2 to 24 characters, starting with a letter"
            if v in d["wp_reserved_names"]:
                return "'%s' is reserved" % v
            if v in taken:
                return "'%s' already exists" % v
            return None
        name = name or self.ask("Site name (the hostname will be <name>.%s)" % domain, check=name_check)
        why = name_check(name)
        if why:
            raise Refuse("'%s': %s" % (name, why))
        host = "%s.%s" % (name, domain)
        self.out("  hostname: %s" % host)
        state = state or self.ask("Starting state (hold = only the VM itself can reach it; live = public)", default="hold", check=lambda v: None if v in STATES else "hold or live")
        policy = policy or self.ask("Update policy (auto applies updates nightly, notify only tells you, freeze never)", default=d["wp_site_defaults"]["update_policy"],
                                    check=lambda v: None if v in POLICIES else "one of " + ", ".join(POLICIES))
        entry = {"name": name, "port": max(ever_used(self.repo, text, "port") | {9100}) + 1, "quota_id": max(ever_used(self.repo, text, "quota_id") | {1000}) + 1, "state": state, "update_policy": policy}
        for item in sets:
            key, _, raw = item.partition("=")
            if key not in EDITABLE or key in ("state", "update_policy"):
                raise Refuse("--set %s: not a field create can set (%s)" % (item, ", ".join(k for k in EDITABLE if k not in ("state", "update_policy"))))
            value, why = validate_value(key, raw)
            if why:
                raise Refuse("--set %s: %s" % (item, why))
            entry[key] = value
        if not sets and not yes and self.confirm("Change the limits (disk quota, memory, upload size)? The defaults are %s" % ", ".join("%s %s" % (k, d["wp_site_defaults"].get(k)) for k in ("quota_mb", "php_mem_mb", "db_mem_mb", "upload_mb"))):
            for key in ("quota_mb", "php_mem_mb", "db_mem_mb", "upload_mb"):
                raw = self.ask("  %s" % key, default=str(d["wp_site_defaults"][key]), check=lambda v, k=key: validate_value(k, v)[1])
                value, _ = validate_value(key, raw)
                if value != d["wp_site_defaults"][key]:
                    entry[key] = value
        title = title or self.ask("WordPress site title", default=name, check=lambda v: None if v and len(v) <= 100 and not re.search(r"[\x00-\x1f\x7f]", v) else "1 to 100 characters, no control characters")
        user = user or self.ask("Administrator user name", check=lambda v: None if USER_RE.fullmatch(v) else "3 to 30 characters of letters, digits and . _ -")
        email = email or self.ask("Administrator email", check=lambda v: None if EMAIL_RE.fullmatch(v) else "that does not look like an email address")
        for label, v, rx in (("title", title, None), ("user", user, USER_RE), ("email", email, EMAIL_RE)):
            if (rx and not rx.fullmatch(v)) or (label == "title" and (not v or len(v) > 100 or re.search(r"[\x00-\x1f\x7f]", v))):
                raise Refuse("the %s '%s' is not acceptable" % (label, v))
        if state == "live":
            self.public_question(host, yes_public)
        new = add_site(text, entry)
        self.check_rules(new)
        self.out("\nNew site: %s -> %s, state %s, update policy %s, port %d, quota project %d. Administrator %s <%s>, title '%s'." % (name, host, state, policy, entry["port"], entry["quota_id"], user, email, title))
        self.out("The password is asked later, on the VM, at a hidden prompt (14 or more characters).\n")
        rc = self.apply_and_run(text, new, name, no_run, yes, check=False)
        if rc != 0 or no_run:
            return rc
        remote = "sudo wp-site-install " + " ".join(shlex.quote(x) for x in (name, "--title=" + title, "--user=" + user, "--email=" + email))
        rc, _ = self.run(["ssh", "-t", HOST, remote])
        if rc != 0:
            self.out("wp-site-install failed or was refused (exit %d). The site exists but has no WordPress yet; run it again by hand: ssh -t %s sudo wp-site-install %s" % (rc, HOST, name))
            return rc
        self.out("\n%s is installed. %s" % (host, "It is PUBLIC: https://%s" % host if state == "live" else "It is on hold: only a request from the VM itself gets in. Go live with: python3 scripts/wp_sites.py modify %s --set state=live" % name))
        return self.finish(name, "create site %s" % name)

    def public_question(self, host, yes_public):
        if yes_public:
            return
        if not self.confirm("Make https://%s PUBLIC on the internet?" % host):
            raise Refuse("not confirmed; nothing was written")

    # --- modify
    def modify(self, site, sets=(), yes=False, yes_public=False, no_run=False):
        self.preflight()
        text = self.path.read_text()
        d = self.data(text)
        cur = next((s for s in self.merged(d) if s["name"] == site), None)
        if cur is None:
            raise Refuse("no site '%s' in wp_sites" % site)
        host = "%s.%s" % (site, d["wp_domain"])
        changes = {}
        for item in sets:
            key, _, raw = item.partition("=")
            if key not in EDITABLE:
                raise Refuse("--set %s: not a field modify can change (%s)" % (item, ", ".join(EDITABLE)))
            value, why = validate_value(key, raw)
            if why:
                raise Refuse("--set %s: %s" % (item, why))
            changes[key] = value
        if not sets:
            self.out("%s (%s): %s" % (site, host, ", ".join("%s=%s" % (k, render(cur[k])) for k in EDITABLE)))
            while True:
                key = self.ask("Field to change (%s), empty to finish" % ", ".join(EDITABLE), default="", check=lambda v: None if v == "" or v in EDITABLE else "one of the fields above")
                if not key:
                    break
                raw = self.ask("  new value for %s" % key, default=render(changes.get(key, cur[key])), check=lambda v, k=key: validate_value(k, v)[1])
                changes[key] = validate_value(key, raw)[0]
        changes = {k: v for k, v in changes.items() if v != cur[k]}
        if not changes:
            self.out("Nothing to change.")
            return 0
        new = text
        for key, value in changes.items():
            new = set_site_field(new, site, key, value)
        self.check_rules(new)
        for key in changes:
            if key in RESTARTS:
                self.out("note: %s %s." % (key, RESTARTS[key]))
        if changes.get("state") == "live":
            self.public_question(host, yes_public)
        elif changes.get("state") == "hold":
            self.out("note: %s stops being public; only a request from the VM itself gets in." % host)
        rc = self.apply_and_run(text, new, site, no_run, yes)
        if rc != 0 or no_run:
            return rc
        return self.finish(site, "modify site %s: %s" % (site, ", ".join("%s=%s" % (k, render(v)) for k, v in changes.items())))

    # --- menu
    def menu(self):
        while True:
            self.out("\nWordPress sites on %s\n  1) list   2) create a site   3) change a site   4) quit   (python3 scripts/wp_sites.py --help lists every option)" % HOST)
            try:
                choice = self._ask("> ").strip()
            except EOFError:
                return 0
            try:
                if choice == "1":
                    self.list()
                elif choice == "2":
                    self.create()
                elif choice == "3":
                    d = self.data(self.path.read_text())
                    names = [s["name"] for s in d["wp_sites"]]
                    self.modify(self.ask("Site (%s)" % ", ".join(names), check=lambda v: None if v in names else "one of the sites above"))
                elif choice in ("4", "q", ""):
                    return 0
            except Refuse as e:
                self.out("wp_sites: %s" % e)


def field_help(keys=None):
    """One line per editable field: what it takes (generated from EDITABLE, the table the script validates against, so the help cannot drift from the checks)."""
    lines = []
    for key, spec in EDITABLE.items():
        if keys is not None and key not in keys:
            continue
        what = "|".join(spec[1]) if spec[0] == "choice" else ("true|false" if spec[0] == "bool" else "%d..%d" % (spec[1], spec[2]))
        lines.append("  %-16s %-20s%s" % (key, what, "[%s]" % RESTARTS[key] if key in RESTARTS else ""))
    return "\n".join(lines)


TOOLS_HELP = """\
On the VM (run as: ssh -t dellwp sudo <tool>; every tool prints its usage when run with no arguments):
  wp-site-update <site> [--check|--apply]      what is available / apply it, with a snapshot first and a rollback
  wp-site-recover <site> [stamp|latest]        put a site back to a snapshot (database, wp-content, core)
  wp-site-backup <site> [--force]              back a site up now
  wp-site-restore <site> [stamp|latest]        prove a backup restores (a drill into scratch; never touches the site)
  wp-site-install <site> [--title --user --email]   install WordPress into a site that has none (asks the password, hidden)
  wp-site-ext <site> plugin|theme install|update|delete <slug>   plugins and themes, wordpress.org slugs only
  wp-site-cli <site> <wp arguments>            WP-CLI against a site as the web app
Other scripts here: wp_sites_status.py (the table), wp_site_state.py <site> hold|live, wp_site_verify_dellwp.sh <site>, and the rehearsals wp_*_rehearsal_dellwp.sh."""

EXAMPLES = """\
examples:
  python3 scripts/wp_sites.py                          a numbered menu (list, create, change)
  python3 scripts/wp_sites.py list
  python3 scripts/wp_sites.py create                   asks for everything it needs
  python3 scripts/wp_sites.py create --name shop --state hold --policy notify --title "Shop" --user owner --email owner@example.org
  python3 scripts/wp_sites.py modify shop --set update_policy=auto
  python3 scripts/wp_sites.py modify shop --set state=live --set upload_mb=128
  python3 scripts/wp_sites.py modify shop --no-run     edit the file and show the diff, run nothing

notes:
  The administrator password is never an option and is not asked by this script: wp-site-install asks for it on the VM at a hidden prompt.
  Going live (public on the internet) always asks its own question; --yes does not answer it, --yes-public does.
  A new site gets no check run (check mode cannot handle a site that does not exist yet); a change to an existing site does.
  The file must be committed first, and the script leaves the commit to you: it prints the command.
  Not here yet: delete, rename, password reset.

""" + TOOLS_HELP


def build_parser():
    fmt = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(prog="wp_sites.py", formatter_class=fmt, description="Manage the WordPress sites on dellwp: list them, create one, change one. With no command it shows a menu.", epilog=EXAMPLES)
    sub = ap.add_subparsers(dest="cmd", title="commands", metavar="<command>")
    sub.add_parser("list", formatter_class=fmt, help="the one-table view of every site (wp_sites_status.py)", description="Show every site: state, stack, WordPress version, plugins, backup age, quota use, cron.")
    c = sub.add_parser("create", formatter_class=fmt, help="create a site: edit wp_sites, run the play, install WordPress, verify",
                       description="Create a site at <name>.%s. Every value not given as an option is asked. Steps: validate against the play's own rules, show the diff, write, run the play "
                                   "(real run and a second run that must end changed=0), run wp-site-install on the VM (the password is typed there, hidden), run the verifier.\n\n"
                                   "limits you may set with --set (the defaults apply when not set):\n" % "example.com" + field_help(("quota_mb", "backup_quota_mb", "php_mem_mb", "db_mem_mb", "upload_mb", "allow_xmlrpc")),
                       )
    c.add_argument("--name", help="the site name: letters, digits, single hyphens inside; 2 to 24 characters; the hostname is <name>.<domain>")
    c.add_argument("--state", choices=STATES, help="hold (only the VM itself can reach it; the default) or live (public)")
    c.add_argument("--policy", choices=POLICIES, help="update policy: auto applies updates nightly with a snapshot first and a rollback if the site breaks (core: minor and security releases only, a new X.Y is only reported; "
                                                         "plugins and themes: minor updates, while a major version is held for you with one ntfy per set; wp-site-update --apply applies everything); "
                                                         "notify only tells you, once per set (the default); freeze does nothing")
    c.add_argument("--title", help="the WordPress site title (1 to 100 characters)")
    c.add_argument("--user", help="the administrator's user name (3 to 30 characters of letters, digits, . _ -)")
    c.add_argument("--email", help="the administrator's email address")
    m = sub.add_parser("modify", formatter_class=fmt, help="change a site's state, update policy, quotas, memory, upload size",
                       description="Change one site. With no --set it shows the current values and asks which field to change, until you give an empty answer. "
                                   "The new site list is checked against the play's own rules before anything is written; a change to an existing site gets a check run first.\n\n"
                                   "fields (--set KEY=VALUE):\n" + field_help() + "\n\nNot editable here: name, port, quota_id, secret_rev.")
    m.add_argument("site", help="the site's name, as in wp_sites")
    for p in (c, m):
        p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="set a field; can be repeated (the fields are listed above)")
        p.add_argument("--yes", action="store_true", help="do not ask before writing and running (it does not answer the question about going public)")
        p.add_argument("--yes-public", action="store_true", help="answer yes to 'make it public?'")
        p.add_argument("--no-run", action="store_true", help="edit the file and show the diff, run nothing")
    return ap


def main(argv=None, repo=REPO, ask=input, run=None, out=print):
    ap = build_parser()
    a = ap.parse_args(argv)
    s = Sites(repo, ask, run, out)
    try:
        if a.cmd == "list":
            return s.list()
        if a.cmd == "create":
            return s.create(a.name, a.state, a.policy, a.title, a.user, a.email, a.set, a.yes, a.yes_public, a.no_run)
        if a.cmd == "modify":
            return s.modify(a.site, a.set, a.yes, a.yes_public, a.no_run)
        return s.menu()
    except Refuse as e:
        out("wp_sites: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
