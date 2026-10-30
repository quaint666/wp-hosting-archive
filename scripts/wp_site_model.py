#!/usr/bin/env python3
"""The rules and the registry of the WordPress sites on dellwp: what a site may be called, which fields may be set and to what, where a site's record lives, which port and quota id a new site gets,
and whether the whole list of sites still fits the VM. Standard library only: the provisioning tool (wp-site-provision, a later batch) runs it on dellwp and tests/test_wp_site_model.py runs it offline.
Design: docs/wordpress_hosting_explained.md section 11.

A site's record is {data_dir}/<name>/site.json (JSON, written atomically, only by the provisioning tool). A closed site leaves a tombstone, {tombstones_dir}/<name>.<UTC stamp>.json (a file from before
the stamps, <name>.json, still counts), outside the site's directory so that deleting the directory never touches it. A NAME may be given again once its site is closed, and may then have several
tombstones; the port and quota id of a closed site never are: every number ever recorded stays reserved, so a new site always gets numbers above all of them. Until the sites move out of
host_vars/dellwp/vars.yml the allocator and the fit check also see that list (`known`): a record in the registry and an entry in the repo are one set of ports and ids.

The fit rules (RULES, broken_rules) are the one copy of what a list of sites must satisfy; the play used to carry the same rules as an assert and no longer does. tests/test_wp_site_model.py runs them."""
import json
import os
import re
import tempfile
from pathlib import Path

SCHEMA = 1
NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,22}[a-z0-9])$")                  # a hostname label (the same rule as scripts/wp_ctl.py)
FIT_NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,23}")                             # the play's own, looser rule (fullmatch: the play's `match` test also lets a trailing newline through)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
STATES = ("hold", "live", "stopped", "closed")
POLICIES = ("auto", "notify", "freeze")
MAIL_MODES = ("log", "send")
CART_MODES = ("normal", "cache")                                              # cart_fragments: see templates/wp/site_nginx.conf.j2
PORT_BASE, QUOTA_BASE = 9100, 1000                                            # a first site gets 9101 and 1001: above the base
# What `set` may change: key -> (kind, minimum, maximum). `state` is not here: it has its own verb. `name`, `port` and `quota_id` are
# never editable (the name is in the container, database, unit, vhost, directories and quota projects; the port and the id are assigned from the registry).
EDITABLE = {
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
    "cart_fragments": ("choice", CART_MODES),
    "label": ("text", 100),
    "note": ("text", 500),
}
# What the registry keeps beyond the editable keys: identity, assigned numbers, state and the keys the page never shows (they stay at the defaults; the registry file is the CLI fallback).
FIXED_KEYS = ("schema", "name", "port", "quota_id", "state", "secret_rev", "created")
HIDDEN_KEYS = ("cpus", "pids_limit", "php_memory_limit", "fpm_max_children", "opcache_mb", "db_buffer_pool_mb", "php_allow_functions")
RECORD_KEYS = frozenset(FIXED_KEYS) | frozenset(EDITABLE) | frozenset(HIDDEN_KEYS)
TEXT_KEYS = ("label", "note")                                                 # registry-only information: never applied to the site, so absent from wp_site_defaults


class Refuse(Exception):
    """A plain-language reason to stop; nothing has been written."""


class RegistryError(Exception):
    """A record on disk that cannot be trusted (not JSON, wrong name, unknown key, wrong type). The tool stops rather than guess."""


# ----------------------------------------------------------------------------------------------------------------------------------------- the rules
def validate_value(key, raw):
    """(value, None) or (None, why) for a value given to `set` or `create`. Text arrives typed or as a string; a number or a switch also arrives as a string (a form field, a command line)."""
    if key not in EDITABLE:
        return None, "%s is not a field that can be set" % key
    spec = EDITABLE[key]
    if spec[0] == "text":
        if not isinstance(raw, str):
            return None, "%s must be text" % key
        if len(raw) > spec[1]:
            return None, "%s is at most %d characters" % (key, spec[1])
        if CONTROL_RE.search(raw):
            return None, "%s may not contain control characters" % key
        return raw, None
    if spec[0] == "bool":
        if isinstance(raw, bool):
            return raw, None
        raw = str(raw).strip()
        return (raw == "true", None) if raw in ("true", "false") else (None, "%s must be true or false" % key)
    if isinstance(raw, bool):
        return None, "%s must be a whole number" % key
    raw = str(raw).strip()
    if spec[0] == "choice":
        return (raw, None) if raw in spec[1] else (None, "%s must be one of %s" % (key, ", ".join(spec[1])))
    if not re.fullmatch(r"[0-9]{1,6}", raw):
        return None, "%s must be a whole number" % key
    n = int(raw)
    return (n, None) if spec[1] <= n <= spec[2] else (None, "%s must be between %d and %d" % (key, spec[1], spec[2]))


def check_name(name, reserved, taken):
    """None when `name` may be given to a new site, else the reason. `taken` = names of existing (not closed) sites. A closed site's name may be used again: its numbers are not."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or "--" in name:
        return "letters, digits and single hyphens inside, 2 to 24 characters, starting with a letter"
    if name in reserved:
        return "'%s' is reserved" % name
    if name in taken:
        return "'%s' already exists" % name
    return None


TRANSITIONS = {"hold": ("live", "stopped"), "live": ("hold", "stopped"), "stopped": ("hold",), "closed": ()}   # a stopped site comes back on hold, never straight to live; a site is closed only by `delete`


def plan_state(current, target):
    """The steps that take a site from `current` to `target`, in order: [] when it is there already, else 'backup' (the final backup, before a site stops), 'record' and 'apply'. Raises Refuse
    with the reason for a move that is not allowed. Pure: it reads nothing and changes nothing, so every pair can be tested."""
    if target not in ("hold", "live", "stopped"):
        raise Refuse("'%s' is not a state a site can be put in (hold, live or stopped; a site is closed by deleting it)" % target)
    if current not in TRANSITIONS:
        raise Refuse("'%s' is not a state a site can be in" % current)
    if current == "closed":
        raise Refuse("the site is closed (a tombstone: nothing of it is on the VM); there is nothing to move")
    if current == target:
        return []
    if target not in TRANSITIONS[current]:
        raise Refuse("a %s site cannot go straight to %s: put it on %s first" % (current, target, " or ".join(TRANSITIONS[current])))
    return (["backup"] if target == "stopped" else []) + ["record", "apply"]


# -------------------------------------------------------------------------------------------------------------------------------------- the registry
def check_record(rec, defaults, where="record"):
    """Raise RegistryError unless `rec` is a site record this tool could have written. Every key is known, every editable value passes its rule, the assigned numbers are integers."""
    if not isinstance(rec, dict):
        raise RegistryError("%s: not a JSON object" % where)
    if rec.get("schema") != SCHEMA:
        raise RegistryError("%s: schema %r (this tool reads %d)" % (where, rec.get("schema"), SCHEMA))
    unknown = sorted(set(rec) - RECORD_KEYS)
    if unknown:
        raise RegistryError("%s: unknown key %s" % (where, ", ".join(unknown)))
    for key in ("name", "port", "quota_id", "state"):
        if key not in rec:
            raise RegistryError("%s: no %s" % (where, key))
    if not isinstance(rec["name"], str) or not NAME_RE.fullmatch(rec["name"]):
        raise RegistryError("%s: the name %r is not a site name" % (where, rec["name"]))
    for key in ("port", "quota_id"):
        if not isinstance(rec[key], int) or isinstance(rec[key], bool) or rec[key] <= 0:
            raise RegistryError("%s: %s must be a whole number above zero" % (where, key))
    if rec["state"] not in STATES:
        raise RegistryError("%s: state %r (one of %s)" % (where, rec["state"], ", ".join(STATES)))
    for key in EDITABLE:
        if key in rec:
            _, why = validate_value(key, rec[key])
            if why:
                raise RegistryError("%s: %s" % (where, why))
    for key in HIDDEN_KEYS + ("secret_rev",):
        if key in rec and type(rec[key]) is not type(defaults[key]):
            raise RegistryError("%s: %s has the wrong type" % (where, key))
    return rec


STAMP_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z")
TOMB_RE = re.compile(r"(?P<name>[a-z][a-z0-9-]{1,23})(?:\.(?P<stamp>[0-9]{8}T[0-9]{6}Z))?\.json")      # <name>.<stamp>.json; <name>.json is a tombstone from before the stamps


class Registry:
    """The records on disk. `data_dir` holds <name>/site.json, `tombstones_dir` holds <name>.<stamp>.json (one for every time a name was closed)."""

    def __init__(self, data_dir, tombstones_dir, defaults):
        self.data_dir, self.tombstones_dir, self.defaults = Path(data_dir), Path(tombstones_dir), defaults

    def site_file(self, name):
        return self.data_dir / name / "site.json"

    def tombstone_file(self, name, stamp):
        if not STAMP_RE.fullmatch(stamp):
            raise ValueError("not a tombstone stamp: %r" % stamp)
        return self.tombstones_dir / ("%s.%s.json" % (name, stamp))

    def tombstone_files(self, name):
        """The real tombstone files of `name` (a link does not count), oldest first; the file from before the stamps, <name>.json, sorts first."""
        if not self.tombstones_dir.is_dir():
            return []
        out = []
        for path in self.tombstones_dir.iterdir():
            m = TOMB_RE.fullmatch(path.name)
            if m and m["name"] == name and path.is_file() and not path.is_symlink():
                out.append(path)
        return sorted(out, key=lambda p: p.name.replace(".json", ".0"))

    @staticmethod
    def load(path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise RegistryError("%s: %s" % (path, e))

    def read_site(self, name):
        """The record of one site, or None when it has none (a missing file or a link, which is not a record)."""
        path = self.site_file(name)
        if path.is_symlink() or not path.is_file():
            return None
        rec = check_record(self.load(path), self.defaults, str(path))
        if rec["name"] != name:
            raise RegistryError("%s: the record is for %r" % (path, rec["name"]))
        return rec

    def sites(self):
        """Every record, by name. A directory without a record is not a site of the registry (it is what the repo still declares, or debris)."""
        if not self.data_dir.is_dir():
            return []
        return [rec for rec in (self.read_site(p.name) for p in sorted(self.data_dir.iterdir()) if p.is_dir() and not p.is_symlink()) if rec]

    def tombstones(self):
        if not self.tombstones_dir.is_dir():
            return []
        out = []
        for path in sorted(self.tombstones_dir.glob("*.json")):
            m = TOMB_RE.fullmatch(path.name)
            if not m:
                raise RegistryError("%s: a tombstone is called <name>.<UTC stamp>.json" % path)
            rec = check_record(self.load(path), self.defaults, str(path))
            if rec["state"] != "closed" or rec["name"] != m["name"]:
                raise RegistryError("%s: a tombstone is a closed site named like its file" % path)
            out.append(rec)
        return out

    def write_site(self, rec):
        """Write the record atomically (a temp file beside it, flushed, then renamed): a crash leaves the old record or the new one, never half. The site's directory must exist."""
        check_record(rec, self.defaults, "new record for %s" % rec.get("name"))
        if rec["state"] == "closed":
            raise Refuse("a closed site has a tombstone, not a site record")
        self.atomic_write(self.site_file(rec["name"]), rec)

    def write_tombstone(self, rec, stamp):
        rec = {**rec, "state": "closed"}
        check_record(rec, self.defaults, "tombstone for %s" % rec.get("name"))
        path = self.tombstone_file(rec["name"], stamp)
        if path.exists() or path.is_symlink():
            raise RegistryError("%s exists already" % path)
        self.tombstones_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.tombstones_dir, 0o755)
        self.atomic_write(path, rec)
        return path

    @staticmethod
    def atomic_write(path, rec):
        text = json.dumps(rec, indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".site-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


def merged(defaults, rec):
    """A record over the defaults: the dict the templates and the fit check read. `label` and `note` are registry-only and have no default."""
    return {**defaults, **rec}


def everything(registry, known=()):
    """Every site the registry or the repo knows, one per port and quota id (a site, or one time a name was closed), as plain dicts of name, port, quota_id, state: records, tombstones and `known`
    (the closed floor of wp_closed_floor in host_vars/dellwp/vars.yml). The same site seen in two places is one entry. A port or a quota id used by two different sites is an error: numbers are never shared."""
    seen = {}
    for rec in list(registry.sites()) + list(registry.tombstones()) + [dict(k) for k in known]:
        rec = {k: rec[k] for k in ("name", "port", "quota_id", "state") if k in rec}
        key = (rec["port"], rec["quota_id"])
        for (port, qid), other in seen.items():
            if (port, qid) == key and other["name"] != rec["name"]:
                raise RegistryError("%s and %s have the same port and quota id (%s, %s): numbers are never shared" % (other["name"], rec["name"], port, qid))
            if (port, qid) != key and (port == key[0] or qid == key[1]):
                raise RegistryError("%s and %s share a port or a quota id (%s, %s and %s, %s): numbers are never shared" % (other["name"], rec["name"], port, qid, key[0], key[1]))
        old = seen.get(key)
        if old and old.get("state") == "closed":
            rec["state"] = "closed"                                           # once closed, closed: a stale copy elsewhere cannot reopen the site
        seen[key] = {**(old or {}), **rec}
    return sorted(seen.values(), key=lambda s: (s["name"], s["port"]))


def next_numbers(sites):
    """(port, quota_id) for a new site: above every one ever recorded, closed sites included, so none is ever given twice."""
    return (max([s["port"] for s in sites] + [PORT_BASE]) + 1, max([s["quota_id"] for s in sites] + [QUOTA_BASE]) + 1)


# -------------------------------------------------------------------------------------------------------------------------------------------- the fit
def to_int(value):
    """Ansible's `| int`: a number or a string of digits, else 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# The rules, in this order. broken_rules() returns the positions of those that do not hold.
RULES = (
    "there is at least one site",
    "every site has a name, a port and a quota id",
    "names are 2 to 24 characters of a-z, 0-9 and -",
    "no name is reserved",
    "the names of the sites that exist are unique (a closed site's name may be used again)",
    "ports are unique",
    "quota ids are unique",
    "ports are above 1024",
    "quota ids are from 1000",
    "states are hold, live, stopped or closed",
    "update policies are auto, notify or freeze",
    "the memory of the sites that run fits the VM",
    "the quotas of the sites that exist fit the data disk",
    "the backup id offset is larger than the spread of the quota ids",
    "the highest backup id is below the scratch project",
    "the archive and scratch projects differ",
    "the highest backup id is below the archive project",
    "the intake and scratch projects differ",
    "the intake and archive projects differ",
    "the highest backup id is below the intake project",
)


def broken_rules(sites, limits):
    """Indexes into RULES that do not hold for `sites` (each merged over the defaults, tombstones included). `limits`: reserved_names, vm_memory_mb, data_disk_gb, ram_reserve_mb, data_reserve_mb,
    backup_quota_offset, scratch_quota_id, archive_quota_id, intake_quota_id."""
    n = len(sites)
    complete = all(all(k in s for k in ("name", "port", "quota_id")) for s in sites)
    if not complete:
        return [1]                                                            # the rest cannot be judged without them (the play's expressions raise on a missing key, which counts as failing)
    ids = [to_int(s["quota_id"]) for s in sites]
    ports = [to_int(s["port"]) for s in sites]
    names = [s["name"] for s in sites]
    live = [s for s in sites if s["state"] not in ("stopped", "closed")]
    exist = [s for s in sites if s["state"] != "closed"]
    top = (max(ids) if ids else 0) + to_int(limits["backup_quota_offset"])
    offset, scratch, archive, intake = (to_int(limits[k]) for k in ("backup_quota_offset", "scratch_quota_id", "archive_quota_id", "intake_quota_id"))
    holds = (
        n > 0,
        True,
        sum(1 for x in names if isinstance(x, str) and FIT_NAME_RE.fullmatch(x)) == n,
        not set(names) & set(limits["reserved_names"]),
        len({s["name"] for s in exist}) == len(exist),
        len(set(ports)) == n,
        len(set(ids)) == n,
        all(p > 1024 for p in ports),
        all(i >= 1000 for i in ids),
        all(s["state"] in STATES for s in sites),
        all(s["update_policy"] in POLICIES for s in sites),
        sum(to_int(s["php_mem_mb"]) for s in live) + sum(to_int(s["db_mem_mb"]) for s in live) <= to_int(limits["vm_memory_mb"]) - to_int(limits["ram_reserve_mb"]),
        sum(to_int(s["quota_mb"]) for s in exist) + sum(to_int(s["backup_quota_mb"]) for s in exist) <= to_int(limits["data_disk_gb"]) * 1024 - to_int(limits["data_reserve_mb"]),
        offset > (max(ids) - min(ids) if ids else 0),
        top < scratch,
        archive != scratch,
        top < archive,
        intake != scratch,
        intake != archive,
        top < intake,
    )
    return [i for i, ok in enumerate(holds) if not ok]


def fit_report(sites, limits):
    """The plain-language list of the rules a site list breaks (empty = fits)."""
    return [RULES[i] for i in broken_rules(sites, limits)]
