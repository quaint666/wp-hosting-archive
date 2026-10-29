#!/usr/bin/env python3
"""One table of every WordPress site: what its registry record says, what it is really doing, and a list of what needs attention.

    python3 scripts/wp_sites_status.py            asks dellwp over ssh (sudo wp-sites-status, the tool the sites play installs)
    python3 scripts/wp_sites_status.py --file F   reads the host's tab-separated report from F ("-" for stdin) instead: for tests, or to look at one you saved

The exit code is the number of things that need attention (0 = nothing), so it can sit in a script. Read-only: it changes nothing anywhere. The host's report is the registry (wp-site-provision rows),
the filesystem, systemctl, docker ps and repquota, no WP-CLI container, so it works when a stack is down. The sites are the registry's: nothing is compared with the repo, which declares none.
A line is flagged for: the vhost not doing what the record says (the host drifted, or nginx was not reloaded), a stack that is not fully up, no recent backup (not while frozen: a freeze pauses
backups on purpose), a quota near its limit, a cron timer that does not match the state, a missing nightly backup timer. The names the repo keeps as closed (the floor of names never reused) are
listed after the table; the tombstones the registry holds are on the VM. A STOPPED site is expected to have no containers, no running unit and no timers, and is flagged if it has
any, and it is not judged on its backups (they are paused). States that exist today are hold, live, stopped and closed; inbox comes with the import.
"""
import argparse
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
VARS = "host_vars/dellwp/vars.yml"
HOST = "dellwp"
COLUMNS = ["site", "deployed", "actual", "unit", "containers", "core", "plugins", "backup_age_h", "backup_stamp", "quota_pct", "backup_quota_pct", "cron_timer", "backup_timer", "frozen"]


def load_thresholds(repo):
    """The stale-backup hours, the quota warning percentage and the tools directory the host uses, from the repo's own variables."""
    v = yaml.safe_load((Path(repo) / VARS).read_text())
    return int(v["wp_backup_stale_hours"]), int(v["wp_disk_warn_pct"]), v["wp_tools_bin"]


def closed_sites(repo):
    """The names of the closed sites (tombstones: in wp_sites so that their name, port and project id are never reused, nothing on the VM)."""
    v = yaml.safe_load((Path(repo) / VARS).read_text())
    return sorted(s["name"] for s in v["wp_sites"] if {**v["wp_site_defaults"], **s}["state"] == "closed")


def parse(tsv):
    lines = [l for l in tsv.splitlines() if l.strip()]
    if not lines or lines[0].split("\t") != COLUMNS:
        raise ValueError("the host's report does not start with the expected header: %r" % (lines[:1],))
    rows = []
    for l in lines[1:]:
        f = l.split("\t")
        if len(f) != len(COLUMNS):
            raise ValueError("a report line has %d fields, want %d: %r" % (len(f), len(COLUMNS), l))
        rows.append(dict(zip(COLUMNS, f)))
    return rows


def attention(rows, stale_h, warn_pct):
    """A list of (site, what is wrong)."""
    out = []
    for r in rows:
        s = r["site"]
        if r["deployed"] != r["actual"]:
            out.append((s, "the vhost on the host is %s but the record says %s: the host drifted, or nginx was not reloaded" % (r["actual"], r["deployed"])))
        if r["deployed"] == "stopped":
            if r["unit"] == "active":
                out.append((s, "the site is stopped but its unit wp-%s is active" % s))
            if r["containers"] != "0/2":
                out.append((s, "the site is stopped but %s containers are up" % r["containers"]))
            if r["backup_timer"] == "active":
                out.append((s, "the site is stopped but its nightly backup timer is running"))
        else:
            if r["unit"] != "active":
                out.append((s, "the stack's unit wp-%s is %s" % (s, r["unit"])))
            elif r["containers"] != "2/2":
                out.append((s, "only %s containers are up and healthy" % r["containers"]))
            if r["frozen"] != "yes":
                if r["backup_stamp"] == "never":
                    out.append((s, "no backup has run yet"))
                elif int(r["backup_age_h"]) >= stale_h:
                    out.append((s, "the last good backup is %s hours old (limit %d)" % (r["backup_age_h"], stale_h)))
            if r["backup_timer"] != "active":
                out.append((s, "the nightly backup timer is %s" % r["backup_timer"]))
        if r["deployed"] == "live" and r["cron_timer"] != "active":
            out.append((s, "the site is live but its cron timer is %s" % r["cron_timer"]))
        if r["deployed"] == "hold" and r["cron_timer"] == "active":
            out.append((s, "the site is on hold but its cron timer is running"))
        if r["deployed"] == "stopped" and r["cron_timer"] == "active":
            out.append((s, "the site is stopped but its cron timer is running"))
        for label, key in (("site quota", "quota_pct"), ("backup quota", "backup_quota_pct")):
            if r[key] == "?":
                out.append((s, "the %s could not be read" % label))
            elif int(r[key]) >= warn_pct:
                out.append((s, "the %s is %s%% used (warning at %d%%)" % (label, r[key], warn_pct)))
    return out


def render(rows):
    head = ["SITE", "STATE", "STACK", "WP", "PLUGINS", "BACKUP", "QUOTA", "BACKUPS", "CRON"]
    body = []
    for r in sorted(rows, key=lambda x: x["site"]):
        state = r["actual"] + ("*" if r["deployed"] != r["actual"] else "") + (" (frozen)" if r["frozen"] == "yes" else "")
        if r["deployed"] == "stopped":
            stack = "stopped" if r["unit"] != "active" and r["containers"] == "0/2" else "NOT STOPPED " + r["containers"]
        else:
            stack = ("up " if r["unit"] == "active" and r["containers"] == "2/2" else "DOWN ") + r["containers"]
        backup = "never" if r["backup_stamp"] == "never" else "%s h" % r["backup_age_h"]
        pct = lambda x: x if x == "?" else x + "%"
        body.append([r["site"], state, stack, r["core"], r["plugins"], backup, pct(r["quota_pct"]), pct(r["backup_quota_pct"]), "on" if r["cron_timer"] == "active" else "off"])
    widths = [max(len(row[i]) for row in [head] + body) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in [head] + body)


def fetch(tools_bin, runner=subprocess.run):
    r = runner(["ssh", HOST, "sudo -n %s/wp-sites-status" % tools_bin], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("ssh %s failed (exit %d): %s" % (HOST, r.returncode, (r.stderr or "").strip()[:300]))
    return r.stdout


def main(argv=None, repo=REPO, runner=subprocess.run, stdin=sys.stdin):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="read the host's report from this file ('-' = stdin) instead of asking dellwp")
    a = ap.parse_args(argv)
    stale_h, warn_pct, tools_bin = load_thresholds(repo)
    try:
        text = (stdin.read() if a.file == "-" else Path(a.file).read_text()) if a.file else fetch(tools_bin, runner)
        rows = parse(text)
    except (RuntimeError, ValueError, OSError) as e:
        print("wp_sites_status: %s" % e, file=sys.stderr)
        return 99
    print(render(rows))
    closed = closed_sites(repo)
    if closed:
        print("\nClosed in the repo (names never reused; the registry's own tombstones are on the VM): %s" % ", ".join(closed))
    items = attention(rows, stale_h, warn_pct)
    print()
    if items:
        print("Needs attention:")
        for s, msg in items:
            print("  %s: %s" % (s, msg))
    else:
        print("Nothing needs attention.")
    return len(items)


if __name__ == "__main__":
    sys.exit(main())
