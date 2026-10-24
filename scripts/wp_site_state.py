#!/usr/bin/env python3
"""Put one WordPress site on hold or live: edit its `state:` in host_vars/dellwp/vars.yml, show the diff, run the play and the verifier.

    python3 scripts/wp_site_state.py <site> <hold|live> [--no-run | --yes]

hold = nginx answers only a local request; live = public through the tunnel. The state is declared in git, so this edits the file (and only that site's `state:` line, keeping
every comment), re-reads the YAML to prove nothing else changed, and leaves the commit to you: it prints the command. It refuses if the file has uncommitted changes, so the diff
you are asked to approve is the flip and nothing else; the one exception is a file whose only uncommitted differences are other `state:` values (earlier flips that are waiting for their commit), so a site can go live and back on hold without a commit between. Without a flag it asks once before running `ansible-playbook -i inventory.yml dellwp/wp_sites_dellwp.yml` and then
scripts/wp_site_verify_dellwp.sh; --yes skips the question, --no-run only edits. For an urgent take-down that cannot wait for a play, touch
/srv/wp/sites/<site>/maintenance.flag on the VM instead (nginx answers 503 at once); remove it to restore.
"""
import argparse
import copy
import difflib
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
VARS = "host_vars/dellwp/vars.yml"
STATES = ("hold", "live")


def set_state(text, site, state):
    """Return text with the site's state set; only the line of that site changes. Raises ValueError with a plain message otherwise."""
    if state not in STATES:
        raise ValueError("state must be one of %s" % ", ".join(STATES))
    lines = text.split("\n")
    try:
        top = next(i for i, l in enumerate(lines) if re.match(r"^wp_sites:\s*(#.*)?$", l))
    except StopIteration:
        raise ValueError("no top-level wp_sites: in the file")
    start = next((i for i in range(top + 1, len(lines)) if re.match(r"^  - name:\s*%s\s*(#.*)?$" % re.escape(site), lines[i])), None)
    if start is None:
        raise ValueError("no site '%s' in wp_sites" % site)
    end = start + 1
    while end < len(lines) and (lines[end].startswith("    ") or lines[end].strip() == "") and not lines[end].startswith("  - "):
        end += 1
    # trailing blank lines belong to the gap after the block, not to it
    while end > start + 1 and lines[end - 1].strip() == "":
        end -= 1
    for i in range(start + 1, end):
        m = re.match(r"^(    state:\s*)(\S+)(.*)$", lines[i])
        if m:
            lines[i] = m.group(1) + state + m.group(3)
            break
    else:
        last = max(i for i in range(start, end) if lines[i].strip() and not lines[i].lstrip().startswith("#"))
        lines.insert(last + 1, "    state: " + state)
    new = "\n".join(lines)
    before, after = yaml.safe_load(text), yaml.safe_load(new)
    expect = copy.deepcopy(before)
    next(s for s in expect["wp_sites"] if s["name"] == site)["state"] = state
    if after != expect:
        raise ValueError("the edit changed more than the state of '%s'; nothing was written" % site)
    return new


def only_states_differ(old_text, new_text):
    """True if the two files are the same text once every site's `state:` line is left out: the only change is flips that have not been committed yet.
    Text, not parsed data: a changed comment is a change too. The defaults block's `state:` sits at two spaces and is not a site's."""
    def bare(text):
        return "\n".join(l for l in text.split("\n") if not re.match(r"^    state:\s*\S", l))
    return bare(old_text) == bare(new_text)


def current_state(text, site):
    data = yaml.safe_load(text)
    s = next((x for x in data["wp_sites"] if x["name"] == site), None)
    if s is None:
        raise ValueError("no site '%s' in wp_sites" % site)
    return s.get("state", data["wp_site_defaults"]["state"])


def main(argv=None, repo=REPO):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("site")
    ap.add_argument("state", choices=STATES)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--no-run", action="store_true", help="edit the file and show the diff, run nothing")
    g.add_argument("--yes", action="store_true", help="do not ask before running the play and the verifier")
    a = ap.parse_args(argv)
    path = Path(repo) / VARS
    git = lambda *x: subprocess.run(["git", "-C", str(repo), *x], capture_output=True, text=True)
    text = path.read_text()
    if git("diff", "--quiet", "HEAD", "--", VARS).returncode != 0:
        head = git("show", "HEAD:" + VARS)
        if head.returncode != 0 or not only_states_differ(head.stdout, text):
            print("%s has uncommitted changes other than site states; commit or revert them first, so the diff below is only the flip. They are:" % VARS, file=sys.stderr)
            print(git("diff", "HEAD", "--", VARS).stdout, file=sys.stderr)
            print("(`git checkout -- %s` throws them away.)" % VARS, file=sys.stderr)
            return 2
        print("note: the file already has uncommitted state changes (earlier flips); only the edit below is new.")
    try:
        now = current_state(text, a.site)
        if now == "closed":
            raise ValueError("%s is closed (a tombstone: nothing of it is on the VM); there is nothing to flip" % a.site)
        if now == a.state:
            print("%s is already %s; nothing to do." % (a.site, a.state))
            return 0
        new = set_state(text, a.site, a.state)
        path.write_text(new)
    except ValueError as e:
        print("wp_site_state: %s" % e, file=sys.stderr)
        return 2
    print("".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), "a/" + VARS, "b/" + VARS)))
    if a.no_run:
        print("Edited only (--no-run). Run dellwp/wp_sites_dellwp.yml, then scripts/wp_site_verify_dellwp.sh %s." % a.site)
    else:
        if not a.yes:
            if input("Run the play and the verifier now? [y/N] ").strip().lower() != "y":
                print("Not run. The file is edited; `git checkout -- %s` undoes it." % VARS)
                return 1
        for cmd in (["ansible-playbook", "-i", "inventory.yml", "dellwp/wp_sites_dellwp.yml"], ["bash", "scripts/wp_site_verify_dellwp.sh", a.site]):
            print("$ " + " ".join(cmd))
            rc = subprocess.run(cmd, cwd=str(repo)).returncode
            if rc != 0:
                print("`%s` failed (exit %d). The file stays edited; the live state may not match it." % (" ".join(cmd[:3]), rc), file=sys.stderr)
                return rc
    if git("diff", "--quiet", "HEAD", "--", VARS).returncode == 0:
        print("\nThe file is back to what is committed; there is nothing to commit.")
    else:
        print("\nWhen you are satisfied, commit it:\n  git add %s && git commit -m \"ops(wp): %s %s\"" % (VARS, a.site, a.state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
