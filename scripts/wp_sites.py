#!/usr/bin/env python3
"""Manage the WordPress sites on dellwp from one place: list them, create one, change one.

    python3 scripts/wp_sites.py                       a numbered menu
    python3 scripts/wp_sites.py list
    python3 scripts/wp_sites.py create [--name N] [--state hold|live] [--policy auto|notify|freeze] [--title T] [--user U] [--email E] [--set key=value ...] [--yes] [--no-run] [--commit]
    python3 scripts/wp_sites.py modify <site> [--set key=value ...] [--yes] [--yes-public] [--no-run] [--commit]
    python3 scripts/wp_sites.py password <site> [--user U] [--yes]
    python3 scripts/wp_sites.py stop <site> [--yes] [--no-run] [--commit]
    python3 scripts/wp_sites.py start <site> [--yes] [--no-run] [--commit]
    python3 scripts/wp_sites.py delete <site> [--yes] [--commit]
    python3 scripts/wp_sites.py import <site> <file.wpress> [--yes]          (the site must be on hold and have no WordPress yet: create it with --no-install)
    python3 scripts/wp_sites.py report <site>
    python3 scripts/wp_sites.py approve <site> [--reject a,b] [--accept-failures] [--yes]
    python3 scripts/wp_sites.py reset <site>

A site is declared in git (`wp_sites` in host_vars/dellwp/vars.yml) and built by the play, so every change here is an edit of that file followed by the play and the verifier. The script
edits only the lines it means to, re-reads the YAML to prove nothing else changed, checks the new list against the rules of the play itself (the assert in tasks/wp_sites_common.yml),
shows the diff and asks. With --commit it also makes the commit of that change when it is safe (below). It refuses to start if the file has uncommitted changes, so the diff is only this change. By default it leaves the commit to you and prints the command; with --commit it commits
`vars.yml` itself, and only that file (anything else staged is left alone), only when the play succeeded and the verifier ended with 0 failed checks. Otherwise it says why
it did not and prints the command.

`create --no-install` makes an empty site on hold (no WordPress, no administrator, no password) to import a .wpress into. `import` checks the archive here first (what the sites run against what the
archive needs, asking wordpress.org; a NO-GO stops it before anything is copied), sends the file to the VM's intake directory over ssh and compares its hash on both sides, then runs wp-site-import
on the VM, which loads, copies, installs from wordpress.org, runs WP-CLI, scans, takes the first backup and prints a report. Nothing is public and nothing of the friend's code that is not from
wordpress.org has run. `approve` shows that report again, asks, and copies the held-back code in (--reject leaves some out); `reset` undoes the import. None of them edits a file in this repo.

A site's hostname is <name>.<wp_domain> and nothing else: no friend-owned domains (the recorded decision), so there is no Terraform step; the wildcard record already covers every name.
The name is the site's identity (directories, containers, quota project), so it cannot be changed; make a new site and import into it. Port and quota project id are chosen above every
value the file has ever held in git, because a quota id must never be reused for another site. The administrator's password is not asked here and cannot be given as an option: wp-site-install
asks for it at a hidden prompt on the VM (this script runs it with ssh -t), so it never passes through this script, a command line, a file or the shell history.

`delete` removes a STOPPED site for good: it runs wp-site-delete on the VM (which archives the final backup for 14 days and asks you to type the site's name), and only then marks the
site `closed` in wp_sites (a tombstone: its name, port and project id are never reused), runs the play and the verifier. `stop` pauses a site: a final backup first, then its containers go down, its timers stop, backups and updates skip it and visitors get a "This site is paused." page; its data stays. `start`
brings it back on `hold` (going live is a separate, explicit step). `create` ends with a site on `hold` unless you say otherwise: nginx answers only a request from the VM itself. Going live (public on the internet) asks one more question, whatever else
was answered: --yes does not answer it, --yes-public does. This is what the script cannot do: rename a site, rotate a site's
secrets, or prove the public path. `password` changes an existing user's password through wp-site-password on the VM; it edits no file. Run it from the repo root on the ThinkPad; it needs ssh access to dellwp and the vault password like any play.
"""
import argparse
import copy
import difflib
import hashlib
import re
import shlex
import subprocess
import sys
import urllib.error
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))      # the sibling module also when run with python3 -I, which leaves the script's directory off the path
import wp_wpress  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
VARS = "host_vars/dellwp/vars.yml"
HOST = "dellwp"
PLAY = "dellwp/wp_sites_dellwp.yml"
NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,22}[a-z0-9])$")   # a valid hostname label (the play's own rule also lets a trailing hyphen through)
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")                       # a new administrator (wp-site-install)
USER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,60}$")                 # an existing user (wp-site-password, wp-site-login-check)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
IMPORT_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.wpress")              # fullmatch: what wp-site-import accepts in the intake directory
REJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(,[A-Za-z0-9][A-Za-z0-9._-]*)*")
STATES = ("hold", "live")
POLICIES = ("auto", "notify", "freeze")
MAIL_MODES = ("log", "send")
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
    "allow_app_passwords": ("bool",),
    "allow_user_enum": ("bool",),
    "mail_mode": ("choice", MAIL_MODES),
}
# What each change costs, so the question can say it: the compose file changes (the whole stack restarts, about ten seconds), or only the vhost or php.ini (a reload).
RESTARTS = {"php_mem_mb": "restarts the whole stack, about ten seconds offline", "db_mem_mb": "restarts the whole stack, about ten seconds offline",
            "upload_mb": "reloads nginx and php", "allow_xmlrpc": "reloads nginx",
            "allow_app_passwords": "rewrites wp-config.php, picked up on the next request", "allow_user_enum": "rewrites wp-config.php, picked up on the next request",
            "mail_mode": "rewrites wp-config.php, picked up on the next request; send needs dellwp_wp_resend_key in the vault"}


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
    def __init__(self, repo=REPO, ask=input, run=None, out=print, judge=None):
        self.repo, self.path = Path(repo), Path(repo) / VARS
        self._ask, self.out = ask, out
        self.run = run or self.run_command
        self.judge = judge or wp_wpress.preflight

    # --- plumbing
    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.repo), *a], capture_output=True, text=True)

    def run_command(self, cmd, tee=False, stdin=None):
        """stdin is the path of a file to send to the command (a .wpress to ssh), never a string."""
        self.out("$ " + " ".join(shlex.quote(c) for c in cmd) + (" < " + shlex.quote(stdin) if stdin else ""))
        fh = open(stdin, "rb") if stdin else None
        try:
            if not tee:
                return subprocess.run(cmd, cwd=str(self.repo), stdin=fh).returncode, ""
            p = subprocess.Popen(cmd, cwd=str(self.repo), stdin=fh, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lines = []
            for line in p.stdout:
                print(line, end="")
                lines.append(line)
            return p.wait(), "".join(lines)
        finally:
            if fh:
                fh.close()

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

    def apply_and_run(self, old, new, site, no_run, yes, pre=None):
        """Show the diff, ask ONCE, write, run the play. No check run and no second run (they were dropped on request: the play runs for real anyway, and the verifier follows in finish())."""
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/" + VARS, "b/" + VARS))
        self.out(diff)
        if not yes and not self.confirm("Write this change to %s%s?" % (VARS, "" if no_run else " and run the play")):
            self.out("Nothing was written.")
            return 1
        self.path.write_text(new)
        if no_run:
            self.out("Edited only (--no-run). Run %s, then scripts/wp_site_verify_dellwp.sh %s." % (PLAY, site))
            return 0
        play = ["ansible-playbook", "-i", "inventory.yml", PLAY]
        if pre is not None:   # runs immediately before the play, after every answer: the final backup of a stop must not wait for a person
            rc = pre()
            if rc != 0:
                self.path.write_text(old)
                self.out("%s is back as it was; nothing on the host was changed by the play." % VARS)
                return rc
        rc, _ = self.run(play)
        if rc != 0:
            self.out("The play failed (exit %d). The file stays edited and the host may be half-way; run it again once the cause is fixed, or `git checkout -- %s` and run it to undo." % (rc, VARS))
            return rc
        return 0

    def finish(self, site, what, commit=False):
        rc, _ = self.run(["bash", "scripts/wp_site_verify_dellwp.sh", site])
        if rc != 0:
            self.out("The verifier reported %d failed check(s) for %s." % (rc, site))
        self.run(["python3", "scripts/wp_sites_status.py"])
        if self.git("diff", "--quiet", "HEAD", "--", VARS).returncode == 0:
            self.out("\nThe file is back to what is committed; there is nothing to commit.")
        elif commit and rc == 0:
            # only vars.yml (`git commit -- path` leaves anything else that is staged alone), only after the play succeeded and the verifier ended clean
            msg = "ops(wp): %s" % what
            added = self.git("add", "--", VARS)
            done = self.git("commit", "-q", "-m", msg, "--", VARS) if added.returncode == 0 else added
            if done.returncode == 0:
                self.out("\nCommitted %s: %s" % (self.git("rev-parse", "--short", "HEAD").stdout.strip(), msg))
            else:
                self.out("\n--commit could not commit (%s). Commit it yourself:\n  git add %s && git commit -m \"%s\"" % ((done.stderr or done.stdout).strip()[:200], VARS, msg))
        else:
            if commit:
                self.out("\n--commit did not commit: the verifier reported failures.")
            self.out("\nWhen you are satisfied, commit it:\n  git add %s && git commit -m \"ops(wp): %s\"" % (VARS, what))
        return rc

    def need_run_for_commit(self, commit, no_run):
        if commit and no_run:
            raise Refuse("--commit needs the run (the play and the verifier decide whether it is safe to commit); drop --no-run or --commit")

    # --- list
    def list(self):
        return self.run(["python3", "scripts/wp_sites_status.py"])[0]

    # --- create
    def create(self, name=None, state=None, policy=None, title=None, user=None, email=None, sets=(), yes=False, yes_public=False, no_run=False, commit=False, install=True):
        self.need_run_for_commit(commit, no_run)
        if not install and (state not in (None, "hold") or title or user or email):
            raise Refuse("--no-install makes an empty site on hold to import into: it takes no --state live, --title, --user or --email (the administrator comes with the archive)")
        self.preflight()
        text = self.path.read_text()
        d = self.data(text)
        domain, taken = d["wp_domain"], {s["name"] for s in d["wp_sites"]}
        closed_names = {s["name"] for s in self.merged(d) if s["state"] == "closed"}

        def name_check(v):
            if not NAME_RE.fullmatch(v) or "--" in v:
                return "letters, digits and single hyphens inside, 2 to 24 characters, starting with a letter"
            if v in d["wp_reserved_names"]:
                return "'%s' is reserved" % v
            if v in closed_names:
                return "'%s' is the name of a closed site; a name is never reused" % v
            if v in taken:
                return "'%s' already exists" % v
            return None
        name = name or self.ask("Site name (the hostname will be <name>.%s)" % domain, check=name_check)
        why = name_check(name)
        if why:
            raise Refuse("'%s': %s" % (name, why))
        host = "%s.%s" % (name, domain)
        self.out("  hostname: %s" % host)
        state = state or ("hold" if not install else self.ask("Starting state (hold = only the VM itself can reach it; live = public)", default="hold", check=lambda v: None if v in STATES else "hold or live"))
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
        if install:
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
        if install:
            self.out("\nNew site: %s -> %s, state %s, update policy %s, port %d, quota project %d. Administrator %s <%s>, title '%s'." % (name, host, state, policy, entry["port"], entry["quota_id"], user, email, title))
            self.out("The password is asked later, on the VM, at a hidden prompt (14 or more characters).\n")
        else:
            self.out("\nNew EMPTY site: %s -> %s, state hold, update policy %s, port %d, quota project %d. No WordPress is installed: import a .wpress into it." % (name, host, policy, entry["port"], entry["quota_id"]))
        rc = self.apply_and_run(text, new, name, no_run, yes)
        if rc != 0 or no_run:
            return rc
        if not install:
            self.out("\n%s is up and empty, on hold. Import into it with: python3 scripts/wp_sites.py import %s <file.wpress>" % (name, name))
            return self.finish(name, "create empty site %s" % name, commit)
        remote = "sudo wp-site-install " + " ".join(shlex.quote(x) for x in (name, "--title=" + title, "--user=" + user, "--email=" + email))
        rc, _ = self.run(["ssh", "-t", HOST, remote])
        if rc != 0:
            self.out("wp-site-install failed or was refused (exit %d). The site exists but has no WordPress yet; run it again by hand: ssh -t %s sudo wp-site-install %s" % (rc, HOST, name))
            return rc
        self.out("\n%s is installed. %s" % (host, "It is PUBLIC: https://%s" % host if state == "live" else "It is on hold: only a request from the VM itself gets in. Go live with: python3 scripts/wp_sites.py modify %s --set state=live" % name))
        return self.finish(name, "create site %s" % name, commit)

    def public_question(self, host, yes_public):
        if yes_public:
            return
        if not self.confirm("Make https://%s PUBLIC on the internet?" % host):
            raise Refuse("not confirmed; nothing was written")

    # --- modify
    def modify(self, site, sets=(), yes=False, yes_public=False, no_run=False, commit=False):
        self.need_run_for_commit(commit, no_run)
        self.preflight()
        text = self.path.read_text()
        d = self.data(text)
        cur = next((s for s in self.merged(d) if s["name"] == site), None)
        if cur is None:
            raise Refuse("no site '%s' in wp_sites" % site)
        host = "%s.%s" % (site, d["wp_domain"])
        if cur["state"] == "closed":
            raise Refuse("%s is closed (a tombstone); nothing of it is on the VM. Make a new site instead; a closed site's name is never reused" % site)
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
        if cur["state"] == "stopped" and "state" in changes:
            raise Refuse("%s is stopped; bring it back with: python3 scripts/wp_sites.py start %s (it returns on hold; going live is a second step)" % (site, site))
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
        return self.finish(site, "modify site %s: %s" % (site, ", ".join("%s=%s" % (k, render(v)) for k, v in changes.items())), commit)

    # --- stop and start
    def site_state(self, text, site):
        d = self.data(text)
        cur = next((s for s in self.merged(d) if s["name"] == site), None)
        if cur is None:
            raise Refuse("no site '%s' in wp_sites" % site)
        if cur["state"] == "closed":
            raise Refuse("%s is closed (a tombstone: its name, port and project id are reserved, and nothing of it is on the VM)" % site)
        return d, cur, "%s.%s" % (site, d["wp_domain"])

    def stop(self, site, yes=False, no_run=False, commit=False):
        """Pause a site: final backup, then state: stopped and the play (containers down, timers off, the 'paused' page). Reversible with start."""
        self.need_run_for_commit(commit, no_run)
        self.preflight()
        text = self.path.read_text()
        d, cur, host = self.site_state(text, site)
        if cur["state"] == "stopped":
            raise Refuse("%s is already stopped" % site)
        new = set_site_field(text, site, "state", "stopped")
        self.check_rules(new)
        self.out("Stopping %s: a final backup (the site is put in maintenance while it runs and stays so until it is stopped), then its containers go down and its timers stop; backups and updates skip it; "
                 "visitors of https://%s get a 'This site is paused.' page. Its data stays."
                 % (site, host))
        if cur["state"] == "live":
            self.out("It is LIVE: https://%s goes offline for its visitors." % host)

        def final_backup():
            rc, _ = self.run(["ssh", HOST, "sudo -n wp-site-backup %s --force --tag=final" % shlex.quote(site)])
            if rc != 0:
                self.out("The final backup failed (exit %d): nothing was stopped. If the site was put in maintenance, it was brought back." % rc)
            return rc
        rc = self.apply_and_run(text, new, site, no_run, yes, pre=final_backup)
        if rc != 0 or no_run:
            return rc
        self.out("\n%s is stopped. Bring it back with: python3 scripts/wp_sites.py start %s" % (site, site))
        return self.finish(site, "stop site %s" % site, commit)

    def start(self, site, yes=False, no_run=False, commit=False):
        """Bring a stopped site back, on hold. Going live is a separate question."""
        self.need_run_for_commit(commit, no_run)
        self.preflight()
        text = self.path.read_text()
        d, cur, host = self.site_state(text, site)
        if cur["state"] != "stopped":
            raise Refuse("%s is not stopped (it is %s)" % (site, cur["state"]))
        new = set_site_field(text, site, "state", "hold")
        self.check_rules(new)   # a stopped site does not count against the VM's RAM: starting it must still fit
        self.out("Starting %s: its containers come up and its timers start, on hold (only a request from the VM itself gets in)." % site)
        rc = self.apply_and_run(text, new, site, no_run, yes)
        if rc != 0 or no_run:
            return rc
        self.out("\n%s is up on hold. Go live with: python3 scripts/wp_sites.py modify %s --set state=live" % (site, site))
        return self.finish(site, "start site %s" % site, commit)

    # --- delete
    def delete(self, site, yes=False, commit=False):
        """Remove a stopped site for good. The VM tool comes FIRST (it is the only thing that deletes data, and it asks for the name to be typed); the repo says closed only after it succeeded."""
        self.preflight()
        text = self.path.read_text()
        d = self.data(text)
        cur = next((s for s in self.merged(d) if s["name"] == site), None)
        if cur is None:
            raise Refuse("no site '%s' in wp_sites" % site)
        if cur["state"] == "closed":
            raise Refuse("%s is already closed (a tombstone); nothing of it is on the VM" % site)
        if cur["state"] != "stopped":
            raise Refuse("%s is %s. A site is deleted only after it is stopped, because the stop takes the final backup that gets archived: python3 scripts/wp_sites.py stop %s" % (site, cur["state"], site))
        host = "%s.%s" % (site, d["wp_domain"])
        new = set_site_field(text, site, "state", "closed")
        self.check_rules(new)
        self.out("Deleting %s (%s) FOR GOOD." % (site, host))
        self.out("  - on the VM: its final backup is archived and kept for %s days, then purged; its containers, unit, timers, vhost, directory and backups are removed (wp-site-delete)." % d["wp_archive_keep_days"])
        self.out("  - in the repo: %s stays in wp_sites as state: closed, a tombstone: its name, port and project id are never given to another site." % site)
        self.out("  - the nightly Proxmox backups of the whole VM may still hold the data for months and cannot be purged for one site.")
        # Confirmation 1 of 2, and --yes does not answer it: the name typed here. Confirmation 2 is the name typed on the VM, which the tool asks for itself.
        typed = self.ask("Confirmation 1 of 2: type the site's name (%s) to go on; the tool on the VM will ask again" % site)
        if typed != site:
            self.out("Not confirmed; nothing was changed.")
            return 1
        rc, _ = self.run(["ssh", "-t", HOST, "sudo wp-site-delete " + shlex.quote(site)])
        if rc != 0:
            self.out("wp-site-delete did not finish (exit %d); the repo was not changed. Read its message above. If it stopped part way, run this again: the tool resumes." % rc)
            return rc
        rc = self.apply_and_run(text, new, site, False, yes)
        if rc != 0:
            now = self.data(self.path.read_text())["wp_sites"]
            closed_now = next((x for x in now if x["name"] == site), {}).get("state") == "closed"
            if closed_now:
                self.out("The site is gone from the VM and vars.yml now says closed, but the play did not finish. Run it again, then the verifier:\n  ansible-playbook -i inventory.yml %s\n  bash scripts/wp_site_verify_dellwp.sh %s" % (PLAY, site))
            else:
                self.out("The site is already gone from the VM, but vars.yml still says stopped. Run this again: python3 scripts/wp_sites.py delete %s (the tool sees the archive and finishes the clean-up without asking again)." % site)
            return rc
        self.out("\n%s is deleted. Its archive is on the VM until the purge removes it; the name is reserved for good." % site)
        return self.finish(site, "delete site %s" % site, commit)

    # --- password
    def password(self, site, user=None, yes=False):
        """Change a user's password on a site: ssh -t into wp-site-password, which asks at a hidden prompt. This script never sees the password and edits no file."""
        d = self.data(self.path.read_text())
        if site not in [x["name"] for x in d["wp_sites"]]:
            raise Refuse("no site '%s' in wp_sites" % site)
        if site in [x["name"] for x in self.merged(d) if x["state"] == "closed"]:
            raise Refuse("%s is closed (a tombstone); there is no site to change a password on" % site)
        host = "%s.%s" % (site, d["wp_domain"])
        user_check = lambda v: None if USER_NAME_RE.fullmatch(v) else "1 to 60 characters of letters, digits and . _ @ -"
        user = user or self.ask("User name on %s" % host, check=user_check)
        why = user_check(user)
        if why:
            raise Refuse("the user '%s': %s" % (user, why))
        if not yes and not self.confirm("Change the password of '%s' on %s? (their other sessions are ended)" % (user, host)):
            self.out("Nothing was changed.")
            return 1
        self.out("Type the new password twice at the hidden prompt on the VM (14 or more characters); this script never sees it.")
        rc, _ = self.run(["ssh", "-t", HOST, "sudo wp-site-password " + " ".join(shlex.quote(x) for x in (site, user))])
        if rc != 0:
            self.out("wp-site-password reported a problem (exit %d); look at its output above. If the new password does not work, run this again." % rc)
        return rc

    # --- import a .wpress
    def live_site(self, site):
        """The site's entry, or a Refuse: it must be declared and not a tombstone."""
        d = self.data(self.path.read_text())
        cur = next((s for s in self.merged(d) if s["name"] == site), None)
        if cur is None:
            raise Refuse("no site '%s' in wp_sites" % site)
        if cur["state"] == "closed":
            raise Refuse("%s is closed (a tombstone); there is nothing to import into" % site)
        return cur

    def import_site(self, site, file, yes=False, replace=False):
        """Check the archive here, send it to the VM's intake directory (over ssh, hash compared), then run wp-site-import there. Edits no file; nothing is public."""
        cur = self.live_site(site)
        if cur["state"] != "hold":
            raise Refuse("%s is %s: an import goes into a site on hold (python3 scripts/wp_sites.py %s)" % (site, cur["state"], "start " + site if cur["state"] == "stopped" else "modify %s --set state=hold" % site))
        f = Path(file).expanduser()
        if not f.is_file() or f.is_symlink():
            raise Refuse("%s is not a regular file" % file)
        if not IMPORT_FILE_RE.fullmatch(f.name):
            raise Refuse("the file name '%s' is not accepted: rename it to letters, digits, . _ - only, ending in .wpress" % f.name)
        self.out("Checking %s against what the sites run (reads package.json and the plugin headers, asks wordpress.org; nothing is copied yet) ..." % f.name)
        try:
            rows, notes, nogo, facts = self.judge(str(f), self.repo)
        except wp_wpress.ArchiveError as e:
            raise Refuse("NO-GO: %s" % e)
        except (urllib.error.URLError, OSError) as e:
            raise Refuse("NOT JUDGED: wordpress.org could not be asked (%s); nothing was copied. Run it again." % e)
        wp_wpress.report(rows, notes, nogo, facts, self.out)
        if nogo:
            raise Refuse("the preflight says NO-GO; nothing was copied")
        size = f.stat().st_size
        question = ("Copy %s (%d MB) to the VM and import it into %s? The site stays on hold; nothing becomes public." % (f.name, size // (1 << 20), site) if not replace else
                    "Copy %s (%d MB) to the VM and use it to REPLACE everything of %s (the old site is snapshotted first, and a deny puts it back)? The VM asks you to type the name before it wipes anything."
                    % (f.name, size // (1 << 20), site))
        if not yes and not self.confirm(question):
            self.out("Nothing was copied.")
            return 1
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
        # a copy of this file left by an earlier try (a reset or a Deny keeps the file) is replaced; one that an import still waiting on the VM was made from is not touched
        _, gone = self.run(["ssh", HOST, "sudo -n wp-site-import discard --if-present " + shlex.quote(f.name)], tee=True)
        if "is the file of the import" in (gone or ""):
            raise Refuse("%s is the file of an import that is still waiting on the VM: approve it or undo it first (python3 scripts/wp_sites.py approve|reset <site>)" % f.name)
        rc, text = self.run(["ssh", HOST, "sudo -n wp-site-import receive " + shlex.quote(f.name)], tee=True, stdin=str(f))
        if rc != 0:
            self.out("The copy to the VM failed (exit %d); read its message above. Nothing was imported." % rc)
            return rc
        m = re.search(r"^received ([0-9a-f]{64}) (\d+) (\S+)$", text or "", re.M)
        if not m or m.group(1) != digest or int(m.group(2)) != size or m.group(3) != f.name:
            self.run(["ssh", HOST, "sudo -n wp-site-import discard " + shlex.quote(f.name)])
            raise Refuse("the copy on the VM does not match the file here (here %s, %d bytes; the VM said %s). It was removed from the intake directory; run this again."
                         % (digest[:16], size, (m.group(0)[:90] if m else "nothing I could read")))
        self.out("Copied and verified (sha256 %s..., %d bytes). Importing on the VM ..." % (digest[:16], size))
        rc, _ = self.run(["ssh", "-t", HOST, "sudo wp-site-import " + " ".join(shlex.quote(x) for x in (site, f.name) + (("--replace",) if replace else ()))])
        if rc == 0:
            self.out("\nThe report is above. If you accept it: python3 scripts/wp_sites.py approve %s     to undo it: python3 scripts/wp_sites.py reset %s" % (site, site))
        elif rc == 1:
            self.out("\nThe import finished but a step failed (the report is above). Look at it. approve refuses until you pass --accept-failures; reset undoes the whole import.")
        else:
            self.out("\nwp-site-import stopped (exit %d); its message is above. If it says to reset, run: python3 scripts/wp_sites.py reset %s" % (rc, site))
        return rc

    def report(self, site):
        self.live_site(site)
        return self.run(["ssh", HOST, "sudo -n wp-site-import report " + shlex.quote(site)])[0]

    def approve(self, site, reject=(), accept_failures=False, yes=False):
        """Show the report, ask, then let the VM copy the held-back code in. Edits no file; the site stays on hold."""
        cur = self.live_site(site)
        if cur["state"] != "hold":
            raise Refuse("%s is %s: an import is approved while the site is on hold" % (site, cur["state"]))
        reject = [x for x in (reject or []) if x]
        if reject and not REJECT_RE.fullmatch(",".join(reject)):
            raise Refuse("--reject takes plugin or theme folder names separated by commas (letters, digits, . _ -)")
        self.out("The report of the import of %s:\n" % site)
        rc = self.report(site)
        if rc != 0:
            return rc
        if not yes and not self.confirm("\nApprove this import of %s? The code listed under 'held back'%s is copied in, the intake file is deleted, and the site stays on hold" % (site, " (except %s)" % ", ".join(reject) if reject else "")):
            self.out("Not approved; nothing was changed.")
            return 1
        remote = "sudo wp-site-import approve " + shlex.quote(site) + (" --reject " + shlex.quote(",".join(reject)) if reject else "") + (" --accept-failures" if accept_failures else "")
        rc, _ = self.run(["ssh", "-t", HOST, remote])
        if rc != 0:
            self.out("wp-site-import did not approve (exit %d); read its message above. Nothing is deleted until it succeeds." % rc)
            return rc
        self.run(["bash", "scripts/wp_site_verify_dellwp.sh", site])
        self.run(["python3", "scripts/wp_sites_status.py"])
        self.out("\n%s now holds the imported site and is still on hold. Look at it from the VM (a local request), then go live when you are ready: python3 scripts/wp_sites.py modify %s --set state=live" % (site, site))
        return 0

    def reset(self, site):
        """Undo an import that is not approved yet. The VM asks for the site's name to be typed."""
        self.live_site(site)
        rc, _ = self.run(["ssh", "-t", HOST, "sudo wp-site-import reset " + shlex.quote(site)])
        if rc != 0:
            self.out("wp-site-import reset did not finish (exit %d); read its message above." % rc)
            return rc
        self.run(["python3", "scripts/wp_sites_status.py"])
        self.out("\nThe import of %s is undone; the file is still in the VM's intake directory (python3 scripts/wp_sites.py import %s <file> replaces that copy and imports again)." % (site, site))
        return 0

    # --- menu
    def menu(self):
        while True:
            self.out("\nWordPress sites on %s\n  1) list   2) create a site   3) change a site   4) quit   5) change a user's password   6) stop a site   7) start a site   8) delete a stopped site   9) import a .wpress   10) approve an import   11) undo an import   12) show a report   (python3 scripts/wp_sites.py --help lists every option)" % HOST)
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
                    names = [x["name"] for x in self.merged(d) if x["state"] != "closed"]
                    self.modify(self.ask("Site (%s)" % ", ".join(names), check=lambda v: None if v in names else "one of the sites above"))
                elif choice == "5":
                    d = self.data(self.path.read_text())
                    names = [x["name"] for x in self.merged(d) if x["state"] != "closed"]
                    self.password(self.ask("Site (%s)" % ", ".join(names), check=lambda v: None if v in names else "one of the sites above"))
                elif choice == "8":
                    d = self.data(self.path.read_text())
                    names = [x["name"] for x in self.merged(d) if x["state"] == "stopped"]
                    if not names:
                        self.out("No site is stopped; a site is deleted only after it is stopped.")
                        continue
                    self.delete(self.ask("Site to DELETE (%s)" % ", ".join(names), check=lambda v: None if v in names else "one of the sites above"))
                elif choice in ("6", "7"):
                    d = self.data(self.path.read_text())
                    names = [x["name"] for x in self.merged(d) if (x["state"] == "stopped" if choice == "7" else x["state"] in ("hold", "live"))]
                    if not names:
                        self.out("No site is %s." % ("stopped" if choice == "7" else "running"))
                        continue
                    site = self.ask("Site (%s)" % ", ".join(names), check=lambda v: None if v in names else "one of the sites above")
                    (self.start if choice == "7" else self.stop)(site)
                elif choice in ("9", "10", "11", "12"):
                    d = self.data(self.path.read_text())
                    names = [x["name"] for x in self.merged(d) if x["state"] == "hold"]
                    if not names:
                        self.out("No site is on hold; an import goes into a site on hold (create one with --no-install).")
                        continue
                    site = self.ask("Site (%s)" % ", ".join(names), check=lambda v: None if v in names else "one of the sites above")
                    if choice == "9":
                        self.import_site(site, self.ask("Path of the .wpress file"))
                    elif choice == "10":
                        raw = self.ask("Leave out (plugin or theme folder names, comma separated; empty = none)", default="", check=lambda v: None if not v or REJECT_RE.fullmatch(v) else "folder names separated by commas")
                        self.approve(site, [x for x in raw.split(",") if x])
                    elif choice == "11":
                        self.reset(site)
                    else:
                        self.report(site)
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


COMMIT_HELP = "commit the change to vars.yml when it is safe: only that file, only if the play succeeded and the verifier ends with 0 failed; otherwise it prints the commit command"

TOOLS_HELP = """\
On the VM (run as: ssh -t dellwp sudo <tool>; every tool prints its usage when run with no arguments):
  wp-site-update <site> [--check|--apply]      what is available / apply it, with a snapshot first and a rollback
  wp-site-recover <site> [stamp|latest]        put a site back to a snapshot (database, wp-content, core)
  wp-site-backup <site> [--force]              back a site up now
  wp-site-restore <site> [stamp|latest]        prove a backup restores (a drill into scratch; never touches the site)
  wp-site-delete <site>                        delete a STOPPED site for good: archive its final backup, then remove it (asks for the name to be typed)
  wp-site-install <site> [--title --user --email]   install WordPress into a site that has none (asks the password, hidden)
  wp-site-password <site> <user>               change a user's password (hidden prompt, other sessions ended)
  wp-site-ext <site> plugin|theme install|update|delete <slug>   plugins and themes, wordpress.org slugs only
  wp-site-cli <site> <wp arguments>            WP-CLI against a site as the web app
  wp-site-import <site> <file.wpress> [--plan]  import a .wpress into an empty site on hold; also approve, reset, report, receive, discard (see its usage)
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
  python3 scripts/wp_sites.py stop shop --commit       and commit the change to vars.yml if the verifier ends clean
  python3 scripts/wp_sites.py delete shop              delete a stopped site for good (asks you to type its name on the VM)
  python3 scripts/wp_sites.py password shop --user owner   change a user's password (hidden prompt on the VM)
  python3 scripts/wp_sites.py stop shop                pause it (final backup first); start shop brings it back on hold
  python3 scripts/wp_sites.py create --name friend --no-install        an empty site on hold, to import into
  python3 scripts/wp_sites.py import friend ~/Downloads/friend.wpress  check, send, load, report; nothing is public
  python3 scripts/wp_sites.py approve friend --reject some-plugin      accept the report (and leave a held-back plugin out); reset friend undoes the import

notes:
  The administrator password is never an option and is not asked by this script: wp-site-install asks for it on the VM at a hidden prompt.
  Going live (public on the internet) always asks its own question; --yes does not answer it, --yes-public does.
  There is no check run and no second run: the change is written, the play runs, the verifier checks the result.
  The file must be committed first. The script prints the commit command, or with --commit makes the commit itself (only vars.yml, and only when the play succeeded and the verifier ends
  with 0 failed).
  A destructive change asks twice: delete makes you TYPE the site's name here, and the tool on the VM makes you type it again. --yes skips neither.
  Not here yet: rename.

""" + TOOLS_HELP


def build_parser():
    fmt = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(prog="wp_sites.py", formatter_class=fmt, description="Manage the WordPress sites on dellwp: list them, create one, change one. With no command it shows a menu.", epilog=EXAMPLES)
    sub = ap.add_subparsers(dest="cmd", title="commands", metavar="<command>")
    sub.add_parser("list", formatter_class=fmt, help="the one-table view of every site (wp_sites_status.py)", description="Show every site: state, stack, WordPress version, plugins, backup age, quota use, cron.")
    c = sub.add_parser("create", formatter_class=fmt, help="create a site: edit wp_sites, run the play, install WordPress, verify",
                       description="Create a site at <name>.%s. Every value not given as an option is asked. Steps: validate against the play's own rules, show the diff, write, run the play "
                                   "(one run), run wp-site-install on the VM (the password is typed there, hidden), run the verifier.\n\n"
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
                                   "The new site list is checked against the play's own rules before anything is written; the play then runs once and the verifier checks the result.\n\n"
                                   "fields (--set KEY=VALUE):\n" + field_help() + "\n\nNot editable here: name, port, quota_id, secret_rev.")
    m.add_argument("site", help="the site's name, as in wp_sites")
    pw = sub.add_parser("password", formatter_class=fmt, help="change a user's password on a site (asked at a hidden prompt on the VM)",
                        description="Change an existing user's password on a site, through wp-site-password on the VM (ssh -t). The new password is typed twice at a hidden prompt there (14 or more "
                                    "characters); this script never sees it, it is never an option, and WP-CLI's echo of it is redacted. The user's other sessions are ended and the new password is "
                                    "proved to log in. Edits no file.")
    pw.add_argument("site", help="the site's name, as in wp_sites")
    pw.add_argument("--user", help="the user name on that site (asked if not given)")
    pw.add_argument("--yes", action="store_true", help="do not ask for confirmation first")
    dl = sub.add_parser("delete", formatter_class=fmt, help="delete a STOPPED site for good (archived for 14 days; the name stays reserved)",
                        description="Delete a stopped site for good. Runs wp-site-delete on the VM first: it archives the final backup (kept for the archive period, then purged), asks you to TYPE the site's "
                                    "name, and removes the site's containers, unit, timers, vhost, directory and backups. Only after it succeeded, the site is set to state: closed in wp_sites "
                                    "(a tombstone: its name, port and project id are never reused), and the play and the verifier run. The Proxmox backups of the whole VM may keep the data for "
                                    "months. It asks TWICE: you type the site's name here and again on the VM; --yes skips neither (it only skips the question before the play). A site is deleted only after python3 scripts/wp_sites.py stop.")
    dl.add_argument("site", help="the site's name, as in wp_sites; it must be stopped")
    dl.add_argument("--yes", action="store_true", help="skip the question before the play; it does NOT skip the name you type here or the name you type on the VM")
    dl.add_argument("--commit", action="store_true", help=COMMIT_HELP)
    for name, what in (("stop", "pause a site: final backup, containers down, timers off, a 'paused' page for visitors; its data stays"),
                       ("start", "bring a stopped site back, on hold")):
        sp = sub.add_parser(name, formatter_class=fmt, help=what, description=what[0].upper() + what[1:] + ". Edits state in wp_sites, runs the play and the verifier."
                            + (" A final backup is taken first (wp-site-backup --force); if it fails nothing is changed." if name == "stop" else " Going live is a separate step (modify --set state=live), and it asks its own question."))
        sp.add_argument("site", help="the site's name, as in wp_sites")
        sp.add_argument("--yes", action="store_true", help="do not ask before writing and running")
        sp.add_argument("--no-run", action="store_true", help="edit the file and show the diff, run nothing (stop also skips the backup)")
        sp.add_argument("--commit", action="store_true", help=COMMIT_HELP)
    c.add_argument("--no-install", action="store_true", help="make an EMPTY site on hold, with no WordPress, no administrator and no password, to import a .wpress into (no --state live, --title, --user, --email)")
    im = sub.add_parser("import", formatter_class=fmt, help="import a .wpress into an empty site on hold: check, send, load, report (nothing public)",
                        description="Import a .wpress (All-in-One WP Migration, as Local writes it) into a site on hold that has no WordPress yet (create it with: create --no-install). Steps: the archive is "
                                    "checked here against what the sites run, asking wordpress.org (a NO-GO stops before anything is copied); the file is sent to the VM's intake directory over ssh and its "
                                    "hash compared on both sides; wp-site-import then loads the database, copies uploads, installs the plugins and themes wordpress.org offers, runs WP-CLI, scans, takes the first "
                                    "backup and prints a report. Code that is not from wordpress.org is held back until you approve it. Edits no file here.")
    im.add_argument("site", help="the site's name, as in wp_sites; it must be on hold")
    im.add_argument("file", help="the .wpress file on this machine (letters, digits, . _ - in its name)")
    im.add_argument("--yes", action="store_true", help="do not ask before copying the file to the VM")
    im.add_argument("--replace", action="store_true", help="the site already has data (an earlier import, approved): snapshot it, wipe it and import this archive instead. The VM asks you to type the site name; "
                                                            "reset or a Deny puts the old site back from the snapshot; approve keeps the snapshot as a pre-update one")
    rp = sub.add_parser("report", formatter_class=fmt, help="show the report of an import that is waiting for approval", description="Print the report of the import waiting for approval on a site.")
    rp.add_argument("site", help="the site's name, as in wp_sites")
    ap2 = sub.add_parser("approve", formatter_class=fmt, help="approve an import: copy the held-back code in and delete the intake file",
                         description="Show the report, ask, then copy the code held back from the archive (plugins and themes not from wordpress.org, mu-plugins) into the site, delete the intake file and the "
                                     "work directory, and verify. The site stays on hold: going live is a separate step. approve refuses while a step of the import failed, unless --accept-failures.")
    ap2.add_argument("site", help="the site's name, as in wp_sites")
    ap2.add_argument("--reject", help="plugin or theme folder names, comma separated, to leave out (they are not copied)")
    ap2.add_argument("--accept-failures", action="store_true", help="approve although a step of the import failed (read the report first)")
    ap2.add_argument("--yes", action="store_true", help="do not ask before approving")
    rs = sub.add_parser("reset", formatter_class=fmt, help="undo an import that is not approved yet (the VM asks for the site's name)",
                        description="Undo an import that is not approved yet: drops the tables it loaded and removes the files it copied and the plugins and themes it installed, nothing else. The VM asks "
                                    "you to type the site's name. The .wpress stays in the VM's intake directory.")
    rs.add_argument("site", help="the site's name, as in wp_sites")
    for p in (c, m):
        p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="set a field; can be repeated (the fields are listed above)")
        p.add_argument("--yes", action="store_true", help="do not ask before writing and running (it does not answer the question about going public)")
        p.add_argument("--yes-public", action="store_true", help="answer yes to 'make it public?'")
        p.add_argument("--no-run", action="store_true", help="edit the file and show the diff, run nothing")
        p.add_argument("--commit", action="store_true", help=COMMIT_HELP)
    return ap


def main(argv=None, repo=REPO, ask=input, run=None, out=print):
    ap = build_parser()
    a = ap.parse_args(argv)
    s = Sites(repo, ask, run, out)
    try:
        if a.cmd == "list":
            return s.list()
        if a.cmd == "create":
            return s.create(a.name, a.state, a.policy, a.title, a.user, a.email, a.set, a.yes, a.yes_public, a.no_run, a.commit, install=not a.no_install)
        if a.cmd == "import":
            return s.import_site(a.site, a.file, a.yes, a.replace)
        if a.cmd == "report":
            return s.report(a.site)
        if a.cmd == "approve":
            return s.approve(a.site, (a.reject or "").split(","), a.accept_failures, a.yes)
        if a.cmd == "reset":
            return s.reset(a.site)
        if a.cmd == "modify":
            return s.modify(a.site, a.set, a.yes, a.yes_public, a.no_run, a.commit)
        if a.cmd == "password":
            return s.password(a.site, a.user, a.yes)
        if a.cmd == "delete":
            return s.delete(a.site, a.yes, a.commit)
        if a.cmd == "stop":
            return s.stop(a.site, a.yes, a.no_run, a.commit)
        if a.cmd == "start":
            return s.start(a.site, a.yes, a.no_run, a.commit)
        return s.menu()
    except Refuse as e:
        out("wp_sites: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
