#!/usr/bin/env python3
"""wp-ctl, the control dispatcher of dellwp: the one program the control app's ssh key may run, and the operator's own   ssh dellwp sudo wp-ctl <verb> [key=value ...]

The logic is here (tests/test_wp_ctl.py runs it); templates/wp/wp_ctl.py.j2 only carries the repo's values and is installed as /usr/local/sbin/wp-ctl. Design: docs/handoff_wp_control_temp.md.

A request is ONE JSON line on standard input, {"verb": "...", "req": "optional id", <the verb's arguments>}, and the reply is ONE JSON line on standard output. A verb that carries bytes
reads them raw after that line: `password` one line (the new password), `import-receive` the whole archive. The first line is read a byte at a time so nothing after it is consumed
by this process: the child tool inherits the rest. On the command line the same request is   wp-ctl <verb> key=value ...   (the same checks, values coerced from text).

Every verb is a handler, handler(call) -> reply dict, in the table VERBS: the arguments it takes and how they are checked, whether it only reads, runs a quick tool, carries bytes or starts a job.
Everything around the handler is the same for all of them and is done here, not in the handlers: unknown verbs and unknown keys are refused, so are wrong types, odd characters and a site that does
not exist; every change writes an audit line BEFORE it runs and one after (for a job: "started" now and "result" when the unit has finished), and if the first cannot be written nothing runs; a
refusal is written too. The file is append-only and each line also goes to the journal (Alloy ships it to Loki), so what the caller did can be read where the caller cannot edit it. The tools run
as an argument LIST (no shell), with a clean environment. A password never appears in an argument, in the audit or in a job log, and reaches its tool through a file in a tmpfs. A slow verb runs
as a job (systemd-run, this program again with --run-job): its log and exit code survive the caller. A job request that carries a `req` is idempotent: the same request again returns the job
it started (a double click, a retry); the same `req` with another verb, site or arguments is refused. Each verb has a time limit, and each job a memory and process limit for the tool's own
processes (the containers a tool starts keep the limits of their own compose files): the table `limits` in the config, checked at every start so a typo fails loudly.

The caller shown in the audit is the sudo user (informational, not authentication: the key being forced to this program is what restricts it). Exit status: 0 done, 1 the tool ran and failed,
2 refused. Run: python3 -I tests/test_wp_ctl.py"""
import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,22}[a-z0-9])$")                  # a hostname label (the same rule as scripts/wp_site_model.py)
USER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,60}$")                        # an existing WordPress user (wp-site-password)
ADMIN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")                        # the administrator wp-site-install creates (the tool's own rule)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")                         # what wp-site-install accepts as an email address
LABEL_RE = re.compile(r"^[^\s].{0,98}[^\s]$|^[^\s]$")                          # a site's label: 1 to 100 characters, no leading or trailing space (the registry's rule; control characters are refused before this)
IMPORT_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.wpress")           # what wp-site-import accepts in the intake directory
REJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(,[A-Za-z0-9][A-Za-z0-9._-]*)*")
JOB_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
REQ_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
BACKUP_TAGS = ("pre-update", "final", "pre-import")
SET_KEYS = ("update_policy", "quota_mb", "backup_quota_mb", "php_mem_mb", "db_mem_mb", "upload_mb", "allow_xmlrpc", "allow_app_passwords", "allow_user_enum", "mail_mode", "label", "note")   # = wp_site_model.EDITABLE (a test compares them)
STATES = ("hold", "live", "stopped")
ANY_RE = re.compile(r".*", re.S)                                              # `set` takes any text here: the tool holds the rule of each field, and a refusal of it is a refusal of the job
SET_VALUE_MAX = 500                                                           # the longest value of any field (a site's note)
LIMIT_KEYS = ("timeout_s", "memory_mb", "tasks")
MIN_PASSWORD = 14                                                             # wp-site-password enforces it again
MAX_TEXT = 200
READ_CHUNK = 65536                                                            # the most of a job log or the audit one reply carries
OUTPUT_KEEP = 32768                                                           # the tail of a quick tool's output that goes back to the caller
BUSY = 75                                                                     # a job that found its site locked (EX_TEMPFAIL)
UNIT_GRACE_S = 300                                                            # systemd stops a job's unit this long after the time limit if the runner itself hung
LOST_GRACE_S = UNIT_GRACE_S + 120                                             # past this after the limit a job without an exit code is shown as lost


class Refuse(Exception):
    """A request that was not carried out: a short code for the caller and a plain-language detail. Nothing has been changed."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


class Arg:
    """One argument of a verb. kind: site (an existing site), text (matches rx), enum (one of values), int (lo..hi), flag (a boolean, false when absent), name (a proposed site name).
    `same` names another argument this one must equal (the typed confirmation); with `needs` it is only required while that flag is true, and with `when` = (key, value) only while that argument
    has that value. `limit` is the longest a text may be (MAX_TEXT when absent)."""

    def __init__(self, kind, required=True, values=(), rx=None, lo=0, hi=0, same=None, needs=None, default=None, when=None, limit=None):
        self.kind, self.required, self.values, self.rx, self.lo, self.hi, self.same, self.needs, self.default, self.when, self.limit = kind, required, values, rx, lo, hi, same, needs, default, when, limit

    def clean(self, ctl, key, value, coerce):
        if self.kind == "flag":
            if coerce and isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
                return value.lower() in ("true", "1", "yes")
            if not isinstance(value, bool):
                raise Refuse("bad_type", "%s must be true or false" % key)
            return value
        if self.kind == "int":
            if coerce and isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int):
                raise Refuse("bad_type", "%s must be a whole number" % key)
            if not self.lo <= value <= self.hi:
                raise Refuse("out_of_range", "%s must be between %d and %d" % (key, self.lo, self.hi))
            return value
        if not isinstance(value, str):
            raise Refuse("bad_type", "%s must be text" % key)
        if len(value) > (self.limit or MAX_TEXT) or CONTROL_RE.search(value):
            raise Refuse("bad_value", "%s is too long or holds a control character" % key)
        if self.kind == "enum":
            if value not in self.values:
                raise Refuse("bad_value", "%s must be one of %s" % (key, ", ".join(self.values)))
        elif self.kind == "text":
            if not self.rx.fullmatch(value):
                raise Refuse("bad_value", "%s is not acceptable" % key)
        elif self.kind == "name":
            if not NAME_RE.fullmatch(value):
                raise Refuse("invalid_name", "a site name is 2 to 24 characters: a lower-case letter, then lower-case letters, digits and hyphens, not ending in a hyphen")
        elif self.kind == "site":
            if not NAME_RE.fullmatch(value) or value not in known_sites(ctl.cfg):
                raise Refuse("unknown_site", "there is no such site")
        return value


class Verb:
    """kind: read (answers from this program, audited only when refused), inline (a quick tool, waited for), secret (inline, with a password line after the request), stream (inline, the bytes after
    the request go to the tool), job (a slow tool, started as a unit). lock: hold the site's lock while the handler runs (a job takes it in its own unit). handler(call) -> reply."""

    def __init__(self, kind, args, handler, lock=False):
        self.kind, self.args, self.handler, self.lock = kind, args, handler, lock


class Ctl:
    def __init__(self, cfg, env, now, run):
        self.cfg, self.env, self.now, self.run = cfg, env, now, run

    @property
    def state(self):
        return Path(self.cfg["state_dir"])

    def tool(self, name):
        return os.path.join(self.cfg["tools_bin"], name)

    def child_env(self, extra=None):
        e = {"PATH": self.cfg["path"], "LANG": "C.UTF-8"}
        e.update({k: self.env[k] for k in self.cfg.get("env_pass", ()) if k in self.env})
        e.update(extra or {})
        return e

    def stamp(self):
        return datetime.fromtimestamp(self.now(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def limit(self, verb, key):
        """A verb's own limit, else the default's."""
        return self.cfg["limits"].get(verb, {}).get(key, self.cfg["limits"]["default"][key])


class Call:
    """What a handler is given: the context, the verb's name, its checked arguments, the audit record so far, and the descriptor the bytes after the request come from."""

    def __init__(self, ctl, name, args, rec, fd):
        self.ctl, self.name, self.args, self.rec, self.fd = ctl, name, args, rec, fd

    def run(self, argv, tty=None, stdin=subprocess.DEVNULL, scrub=()):
        """(returncode, output tail) of a tool, within this verb's time limit. Never raises for a tool that is missing or slow: those are results."""
        return run_tool(self.ctl, argv, self.ctl.limit(self.name, "timeout_s"), tty=tty, stdin=stdin, scrub=scrub)

    def reply(self, argv, **kw):
        rc, text = self.run(argv, **kw)
        return {"ok": rc == 0, "rc": rc, "ran": True, "out": text}

    def job(self, argv, tty=None):
        return start_job(self.ctl, self.name, self.args, (argv, tty), self.rec)

    def chain(self, steps):
        """A job of several tools one after another, in one unit under one lock; the first that fails ends it and its status is the job's."""
        return start_job(self.ctl, self.name, self.args, (steps, None), self.rec)


# ------------------------------------------------------------------------------------------------------------------------------------- the config
def check_limits(cfg, verbs):
    """The limits table must name only verbs that exist, only the three keys, only positive whole numbers, give the default all three, and give every job and every stream its own timeout:
    a typo in the vars is an error at every start, not a limit that quietly does not apply."""
    lim = cfg.get("limits")
    if not isinstance(lim, dict) or set(lim.get("default", {})) != set(LIMIT_KEYS):
        raise Refuse("bad_config", "limits needs a default with %s" % ", ".join(LIMIT_KEYS))
    for name, own in lim.items():
        if name != "default" and name not in verbs:
            raise Refuse("bad_config", "limits names a verb that does not exist: %s" % name)
        if not isinstance(own, dict) or not set(own) <= set(LIMIT_KEYS):
            raise Refuse("bad_config", "limits of %s: only %s are allowed" % (name, ", ".join(LIMIT_KEYS)))
        for key, value in own.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise Refuse("bad_config", "limits of %s: %s must be a whole number of at least 1" % (name, key))
    for name, verb in verbs.items():
        if verb.kind in ("job", "stream") and "timeout_s" not in lim.get(name, {}):
            raise Refuse("bad_config", "limits needs a timeout_s of its own for %s (a default would not fit a slow verb)" % name)


# ------------------------------------------------------------------------------------------------------------------------------------- sites and names
def known_sites(cfg):
    """Sites that exist: those with a registry record, <data_dir>/<site>/site.json (a real file; the directory is a real directory)."""
    found = set()
    try:
        with os.scandir(cfg["data_dir"]) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False) and NAME_RE.fullmatch(e.name) and os.path.isfile(os.path.join(e.path, "site.json")):
                    found.add(e.name)
    except OSError:
        pass
    return found


def target(args):
    """The site a request is about: the existing site it names, or for a verb that makes one the name it proposes."""
    return args.get("site") or args.get("name")


def check_new_name(cfg, name):
    """Raise Refuse unless `name` may be given to a new site: a valid label, not reserved, not a site. A closed site's name may be used again (its numbers are not)."""
    if not NAME_RE.fullmatch(name):
        raise Refuse("invalid_name", "a site name is 2 to 24 characters: a lower-case letter, then lower-case letters, digits and hyphens, not ending in a hyphen")
    if name in cfg["reserved_names"]:
        raise Refuse("reserved_name", "%s is already used under the domain by something else" % name)
    if name in known_sites(cfg):
        raise Refuse("name_taken", "there is a site called %s" % name)


# ------------------------------------------------------------------------------------------------------------------------------------- request and reply
def read_line(fd, limit):
    """One line from a file descriptor, a byte at a time: a buffered reader would swallow the bytes after it that the tool is to read."""
    buf = bytearray()
    while True:
        b = os.read(fd, 1)
        if not b or b == b"\n":
            return bytes(buf), bool(b)
        buf += b
        if len(buf) > limit:
            raise Refuse("request_too_large", "the line is longer than %d bytes" % limit)


def no_duplicates(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise Refuse("bad_json", "the key %s appears twice" % json.dumps(k))
        d[k] = v
    return d


def read_request(ctl, argv, fd):
    """(verb, raw arguments, request id, coerce) from the command line or the first line of standard input."""
    if argv:
        raw = {}
        for item in argv[1:]:
            key, eq, val = item.partition("=")
            if not eq or not key or key in raw:
                raise Refuse("bad_usage", "give each argument once, as key=value")
            raw[key] = val
        return argv[0], raw, raw.pop("req", None), True
    line, _ = read_line(fd, ctl.cfg["max_request_bytes"])
    if not line.strip():
        raise Refuse("empty_request", "send one JSON line, or the verb and key=value arguments on the command line")
    try:
        req = json.loads(line.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (ValueError, UnicodeDecodeError):
        raise Refuse("bad_json", "the request is not one JSON object")
    if not isinstance(req, dict):
        raise Refuse("bad_json", "the request is not one JSON object")
    verb = req.pop("verb", None)
    if not isinstance(verb, str):
        raise Refuse("unknown_verb", "no verb")
    return verb, req, req.pop("req", None), False


def parse_args(ctl, verb, raw, coerce):
    unknown = sorted(set(raw) - set(verb.args))
    if unknown:
        raise Refuse("unknown_argument", "not an argument of this verb: " + ", ".join(json.dumps(k)[:40] for k in unknown))
    out = {}
    for key, arg in verb.args.items():
        if key in raw:
            out[key] = arg.clean(ctl, key, raw[key], coerce)
        elif arg.kind == "flag":
            out[key] = False
        elif arg.default is not None:
            out[key] = arg.default
    for key, arg in verb.args.items():
        wanted = arg.required and (not arg.needs or out.get(arg.needs)) and (not arg.when or out.get(arg.when[0]) == arg.when[1])
        if key not in out and wanted:
            raise Refuse("missing_argument", "%s is required" % key)
        if arg.same and key in out and out[key] != out.get(arg.same):
            raise Refuse("confirm_mismatch", "%s must be exactly the %s" % (key, arg.same))
    return out


# ------------------------------------------------------------------------------------------------------------------------------------- audit
def audit(ctl, phase, rec, quiet=False):
    """Append one JSON line to the audit file and the journal. Returns whether the FILE took it; with quiet=False a failure raises, so the caller does not go on without a record."""
    line = json.dumps({"ts": ctl.stamp(), "phase": phase, **{k: v for k, v in rec.items() if v is not None}}, sort_keys=True, separators=(",", ":"))
    try:
        fd = os.open(ctl.cfg["audit_file"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
        ok = True
    except OSError:
        ok = False
    try:
        ctl.run([ctl.cfg["logger"], "-t", "wp-ctl-audit", "--", line], stdin=subprocess.DEVNULL, capture_output=True, timeout=10, env=ctl.child_env())
    except (OSError, subprocess.SubprocessError):
        pass
    if not ok and not quiet:
        raise Refuse("audit_unavailable", "the audit file cannot be written, so nothing was done")
    return ok


def caller(ctl):
    user = ctl.env.get("SUDO_USER", "")
    return "panel" if user and user == ctl.cfg["panel_user"] else "cli:" + (user or "root")


def millis(since):
    return int((time.monotonic() - since) * 1000)


# ------------------------------------------------------------------------------------------------------------------------------------- running a tool
@contextmanager
def tty_file(ctl, lines):
    """What the tool will read from its terminal (WP_SITE_TTY): a typed confirmation, or a password twice. A file only root can read, in run_dir (a tmpfs, so a password is never written to the disk), removed afterwards."""
    if not lines:
        yield None
        return
    d = Path(ctl.cfg["run_dir"])
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = d / ("tty-" + secrets.token_hex(8))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, "".join(l + "\n" for l in lines).encode())
        os.close(fd)
        yield str(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def run_tool(ctl, argv, timeout, tty=None, stdin=subprocess.DEVNULL, scrub=()):
    """(returncode, output tail). Never raises for a tool that is missing or slow: those are results."""
    with tty_file(ctl, tty) as path:
        try:
            p = ctl.run(argv, stdin=stdin, capture_output=True, timeout=timeout, env=ctl.child_env({"WP_SITE_TTY": path} if path else None))
            rc, text = p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            rc, text = 124, "wp-ctl: the tool was stopped after %d s\n" % timeout
        except OSError as e:
            rc, text = 127, "wp-ctl: the tool could not be started (%s)\n" % e.strerror
    for s in scrub:
        text = text.replace(s, "REDACTED")
    return rc, text[-OUTPUT_KEEP:]


def take_lock(ctl, site, wait=False):
    """An exclusive lock on one site (or on the job table, named with a leading underscore no site can have), or None while another holds it. It is released when the descriptor is closed or the
    process ends. With wait=True it blocks until it has it."""
    d = ctl.state / "locks"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(d / (site + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def find_job(ctl, req):
    """(job id, its spec) of the job started for this request id, or (None, None)."""
    try:
        names = sorted(e.name for e in os.scandir(ctl.state / "jobs") if JOB_RE.fullmatch(e.name))
    except OSError:
        return None, None
    for n in names:
        try:
            spec = json.loads((ctl.state / "jobs" / n / "spec.json").read_text())
        except (OSError, ValueError):
            continue
        if spec.get("req") == req:
            return n, spec
    return None, None


def start_job(ctl, verb_name, args, plan, rec):
    """Write the job's request, take the site's lock once to see that it is free, and start the unit that runs this program again with --run-job. With a request id the whole step is one:
    the same request again returns the job it started; the same id for anything else is refused."""
    site = target(args)
    table = take_lock(ctl, "_jobs", wait=True)
    try:
        if rec.get("req"):
            old, spec = find_job(ctl, rec["req"])
            if old:
                if (spec.get("verb"), spec.get("site"), spec.get("args")) != (verb_name, site, rec.get("args")):
                    raise Refuse("req_reused", "the request id %s started another job (%s)" % (rec["req"], old))
                return {"ok": True, "job": old, "duplicate": True}
        held = take_lock(ctl, site)
        if held is None:
            raise Refuse("busy", "another verb is running on %s" % site)
        os.close(held)
        job = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ctl.now())) + "-" + secrets.token_hex(4)
        jd = ctl.state / "jobs" / job
        jd.mkdir(mode=0o700, parents=True)
        timeout = ctl.limit(verb_name, "timeout_s")
        spec = {"id": job, "verb": verb_name, "site": site, "argv": plan[0], "caller": rec["caller"], "req": rec.get("req"), "args": rec.get("args"), "started": ctl.now(), "timeout_s": timeout}
        (jd / "spec.json").write_text(json.dumps(spec, sort_keys=True))
        os.chmod(jd / "spec.json", 0o600)
        if plan[1]:
            fd = os.open(jd / "tty", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, "".join(l + "\n" for l in plan[1]).encode())
            os.close(fd)
        unit = [ctl.cfg["systemd_run"], "--unit=wpjob-" + job, "--collect", "--quiet", "--description=wp-ctl job %s (%s %s)" % (job, verb_name, site),
                "--property=MemoryMax=%dM" % ctl.limit(verb_name, "memory_mb"), "--property=MemorySwapMax=0", "--property=TasksMax=%d" % ctl.limit(verb_name, "tasks"),
                "--property=RuntimeMaxSec=%d" % (timeout + UNIT_GRACE_S), ctl.tool("wp-ctl"), "--run-job", job]
        try:
            p = ctl.run(unit, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, env=ctl.child_env())
            started = p.returncode == 0
        except (OSError, subprocess.SubprocessError):
            started = False
        if not started:
            shutil.rmtree(jd, ignore_errors=True)
            raise Refuse("job_not_started", "systemd could not start the job; nothing was done")
        return {"ok": True, "job": job}
    finally:
        os.close(table)


def run_job(ctl, argv):
    """--run-job <id>: the unit's side. Takes the site's lock, runs the tool with its output in the job's log, writes the exit code, then the audit line."""
    if len(argv) != 1 or not JOB_RE.fullmatch(argv[0]):
        return 2
    jd = ctl.state / "jobs" / argv[0]
    try:
        spec = json.loads((jd / "spec.json").read_text())
    except (OSError, ValueError):
        return 2
    if (jd / "exit").exists():
        return 0                                                  # a job runs once, whatever restarts the unit
    rec = {"req": spec.get("req"), "verb": spec["verb"], "site": spec["site"], "caller": spec["caller"], "job": spec["id"]}
    lock = take_lock(ctl, spec["site"])
    log = open(jd / "log", "ab")
    tty = jd / "tty"
    t0 = time.monotonic()
    try:
        if lock is None:
            rc = BUSY
            log.write(b"wp-ctl: another verb holds this site; nothing was run\n")
        else:
            steps = spec["argv"] if spec["argv"] and isinstance(spec["argv"][0], list) else [spec["argv"]]          # one tool, or several in a row (`Call.chain`)
            t_end = time.monotonic() + spec["timeout_s"]
            for number, argv in enumerate(steps, 1):
                if len(steps) > 1:
                    log.write(("== step %d of %d: %s\n" % (number, len(steps), " ".join([os.path.basename(argv[0])] + argv[1:]))).encode())
                    log.flush()
                try:
                    rc = ctl.run(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=max(1.0, t_end - time.monotonic()),
                                 env=ctl.child_env({"WP_SITE_TTY": str(tty)} if tty.exists() else None)).returncode
                except subprocess.TimeoutExpired:
                    rc = 124
                    log.write(("\nwp-ctl: the tool was stopped after %d s\n" % spec["timeout_s"]).encode())
                except OSError as e:
                    rc = 127
                    log.write(("wp-ctl: the tool could not be started (%s)\n" % e.strerror).encode())
                if rc != 0:
                    if len(steps) > 1 and number < len(steps):
                        log.write(("wp-ctl: step %d of %d ended with status %d; the next step was not run\n" % (number, len(steps), rc)).encode())
                    break
    finally:
        log.close()
        if lock is not None:
            os.close(lock)
        try:
            tty.unlink()
        except OSError:
            pass
    tmp = jd / "exit.tmp"
    tmp.write_text("%d\n" % rc)
    os.replace(tmp, jd / "exit")
    audit(ctl, "result", dict(rec, ok=rc == 0, rc=rc, duration_ms=millis(t0)), quiet=True)
    return 0


def prune_jobs(ctl):
    """Remove job directories older than the retention. Best effort; a failure here never stops a request."""
    cutoff = ctl.now() - ctl.cfg["job_keep_days"] * 86400
    try:
        with os.scandir(ctl.state / "jobs") as it:
            for e in it:
                if JOB_RE.fullmatch(e.name) and e.is_dir(follow_symlinks=False) and e.stat(follow_symlinks=False).st_mtime < cutoff:
                    shutil.rmtree(e.path, ignore_errors=True)
    except OSError:
        pass


# ------------------------------------------------------------------------------------------------------------------------------------- the handlers
def status_rows(call):
    """wp-sites-status prints tab-separated values: a header and a line per site."""
    rc, text = call.run([call.ctl.tool("wp-sites-status")])
    if rc != 0:
        raise Refuse("tool_failed", "wp-sites-status failed: " + text.strip()[-200:])
    lines = [l for l in text.splitlines() if l]
    head = lines[0].split("\t") if lines else []
    return [dict(zip(head, l.split("\t"))) for l in lines[1:]]


def h_list(call):
    return {"ok": True, "sites": status_rows(call)}


def h_show(call):
    site = call.args["site"]
    row = next((r for r in status_rows(call) if r.get("site") == site), None)
    registry, err = None, None
    path = Path(call.ctl.cfg["data_dir"]) / site / "site.json"
    if path.is_file():
        try:
            registry = json.loads(path.read_text())
        except (OSError, ValueError):
            err = "the registry record of %s cannot be read; nothing was guessed" % site
    reply = {"ok": True, "site": site, "status": row, "registry": registry}
    if err:
        reply["registry_error"] = err
    return reply


def h_check_name(call):
    check_new_name(call.ctl.cfg, call.args["name"])
    return {"ok": True}


def job_dir(ctl, job):
    jd = ctl.state / "jobs" / job
    if not jd.is_dir():
        raise Refuse("unknown_job", "there is no such job (it may have been removed after %d days)" % ctl.cfg["job_keep_days"])
    return jd


def job_state(ctl, jd):
    try:
        return {"state": "done", "rc": int((jd / "exit").read_text().strip())}
    except (OSError, ValueError):
        pass
    try:
        spec = json.loads((jd / "spec.json").read_text())
        started, timeout = spec["started"], spec["timeout_s"]
    except (OSError, ValueError, KeyError):
        return {"state": "lost"}
    if ctl.now() - started > timeout + LOST_GRACE_S:
        return {"state": "lost"}                                  # no exit code and past its time limit: the unit died without writing one
    return {"state": "running"}


def h_jobs(call):
    ctl, rows = call.ctl, []
    try:
        names = sorted((e.name for e in os.scandir(ctl.state / "jobs") if JOB_RE.fullmatch(e.name)), reverse=True)[:50]
    except OSError:
        names = []
    for n in names:
        jd = ctl.state / "jobs" / n
        try:
            spec = json.loads((jd / "spec.json").read_text())
        except (OSError, ValueError):
            continue
        rows.append({"job": n, "verb": spec.get("verb"), "site": spec.get("site"), **job_state(ctl, jd)})
    return {"ok": True, "jobs": rows}


def h_job_status(call):
    return {"ok": True, "job": call.args["job"], **job_state(call.ctl, job_dir(call.ctl, call.args["job"]))}


def h_job_log(call):
    a = call.args
    jd = job_dir(call.ctl, a["job"])
    try:
        with open(jd / "log", "rb") as f:
            f.seek(a["offset"])
            data = f.read(READ_CHUNK)
    except OSError:
        data = b""
    return {"ok": True, "job": a["job"], "data": data.decode("utf-8", "replace"), "offset": a["offset"] + len(data), **job_state(call.ctl, jd)}


def h_audit_tail(call):
    try:
        with open(call.ctl.cfg["audit_file"], "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 262144))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        lines = []
    out = []
    for l in lines[-call.args["lines"]:]:
        try:
            out.append(json.loads(l))
        except ValueError:
            out.append({"raw": l[:200]})
    return {"ok": True, "lines": out}


def h_import_list(call):
    """What the intake holds and which imports wait for a decision: the tool's one JSON line, checked and passed on."""
    rc, text = call.run([call.ctl.tool("wp-site-import"), "list"])
    try:
        data = json.loads(text.strip().splitlines()[-1])
        files, imports = data["files"], data["imports"]
        if rc != 0 or not isinstance(files, list) or not isinstance(imports, list):
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError):
        raise Refuse("tool_output", "wp-site-import list did not answer with its one JSON line (status %d)" % rc)
    return {"ok": True, "files": files, "imports": imports}


def h_vuln_check(call):
    """What a site runs against the vulnerability index. The tool's own statuses are answers, not failures: 0 nothing known, 1 something found, 3 not run (no key, no index, or one too old to trust)."""
    rc, text = call.run([call.ctl.tool("wp-vulnfeed"), "check", call.args["site"]])
    return {"ok": rc in (0, 1, 3), "rc": rc, "ran": True, "out": text}


def h_update_check(call):
    return call.job([call.ctl.tool("wp-site-update"), call.args["site"], "--check"])


def h_update_apply(call):
    """Apply every available update by hand, as the nightly job's `--apply`: snapshot first, probe after, roll back by itself when the site does not come up healthy."""
    return call.job([call.ctl.tool("wp-site-update"), call.args["site"], "--apply"])


def h_import_new(call):
    """A new site and the import into it as one job: create the site (on hold, empty), then load the file. The name is checked as `create` does before anything starts; if the import fails the empty site stays (reset or delete it)."""
    a = call.args
    if not (call.rec.get("req") and find_job(call.ctl, call.rec["req"])[0]):
        check_new_name(call.ctl.cfg, a["name"])
    create = [call.ctl.tool("wp-site-provision"), "create", a["name"]] + (["--set", "label=" + a["label"]] if "label" in a else [])
    return call.chain([create, [call.ctl.tool("wp-site-import"), a["name"], a["file"]]])


def h_import_report(call):
    return call.reply([call.ctl.tool("wp-site-import"), "report", call.args["site"]])


def h_backup(call):
    a = call.args
    return call.job([call.ctl.tool("wp-site-backup"), a["site"]] + (["--force"] if a["force"] else []) + (["--tag=" + a["tag"]] if "tag" in a else []))


def h_delete(call):
    """A delete is confirmed by the name typed TWICE (the convention of the older handoff, 12.3: in the script and again on the VM; the page asks them as two separate entries). Both are checked
    equal to the site before anything starts, and the second is what the tool's own prompt is answered with."""
    return call.job([call.ctl.tool("wp-site-delete"), call.args["site"]], tty=[call.args["confirm_again"]])


def h_create(call):
    """A new, empty site on hold (no WordPress in it yet: `install` does that). The name is checked here as `check-name` does, and again by the tool, which also allocates the ports and ids."""
    a = call.args
    if not (call.rec.get("req") and find_job(call.ctl, call.rec["req"])[0]):         # a repeat of a request that already started a job is answered with that job (start_job), though the site exists by now
        check_new_name(call.ctl.cfg, a["name"])
    return call.job([call.ctl.tool("wp-site-provision"), "create", a["name"]] + (["--set", "label=" + a["label"]] if "label" in a else []))


def h_apply(call):
    return call.job([call.ctl.tool("wp-site-provision"), "apply", call.args["site"]])


def h_set(call):
    """One editable field of a site's record, changed and applied. The key must be one of the editable ones; the tool checks the value against that field's rule and that the sites still fit."""
    a = call.args
    return call.job([call.ctl.tool("wp-site-provision"), "set", a["site"], "%s=%s" % (a["key"], a["value"])])


def h_ensure_state(call):
    """Move a site to hold, live or stopped. Going live is confirmed by the site's name (a page that is wrong cannot make a site public); the move rules and the final backup before a stop are the tool's."""
    return call.job([call.ctl.tool("wp-site-provision"), "state", call.args["site"], call.args["state"]])


def h_install(call):
    """WordPress into an empty site. The password is the one line after the request, as for `password`; the title, user and email are options of the tool, so it asks only for the password (twice, from its terminal file)."""
    a = call.args
    line, complete = read_line(call.fd, 1024)
    try:
        pw = line.decode("utf-8")
    except UnicodeDecodeError:
        raise Refuse("bad_password", "the password is not text")
    if not complete or CONTROL_RE.search(pw) or len(pw) < MIN_PASSWORD or len(pw) > MAX_TEXT:
        raise Refuse("bad_password", "send the new password as one line of %d to %d characters, without control characters" % (MIN_PASSWORD, MAX_TEXT))
    return call.reply([call.ctl.tool("wp-site-install"), a["site"], "--title=" + a["title"], "--user=" + a["user"], "--email=" + a["email"]], tty=[pw, pw], scrub=(pw,))


def h_password(call):
    """The password is the one line after the request: checked here so a wrong one never reaches the tool, and never put in an argument, the audit or a log."""
    line, complete = read_line(call.fd, 1024)
    try:
        pw = line.decode("utf-8")
    except UnicodeDecodeError:
        raise Refuse("bad_password", "the password is not text")
    if not complete or CONTROL_RE.search(pw) or len(pw) < MIN_PASSWORD or len(pw) > MAX_TEXT:
        raise Refuse("bad_password", "send the new password as one line of %d to %d characters, without control characters" % (MIN_PASSWORD, MAX_TEXT))
    return call.reply([call.ctl.tool("wp-site-password"), call.args["site"], call.args["user"]], tty=[pw, pw], scrub=(pw,))


def h_import_receive(call):
    return call.reply([call.ctl.tool("wp-site-import"), "receive", call.args["file"]], stdin=call.fd)


def h_import_discard(call):
    return call.reply([call.ctl.tool("wp-site-import"), "discard", call.args["file"]] + (["--if-present"] if call.args["if_present"] else []))


def h_import_run(call):
    a = call.args
    argv = [call.ctl.tool("wp-site-import"), a["site"], a["file"]] + (["--plan"] if a["plan"] else []) + (["--replace"] if a["replace"] else [])
    return call.job(argv, tty=[a["site"]] if a["replace"] else None)


def h_import_approve(call):
    a = call.args
    return call.job([call.ctl.tool("wp-site-import"), "approve", a["site"]] + (["--reject", a["reject"]] if "reject" in a else []) + (["--accept-failures"] if a["accept_failures"] else []))


def h_import_rescan(call):
    return call.job([call.ctl.tool("wp-site-import"), "rescan", call.args["site"]])


def h_import_reset(call):
    return call.job([call.ctl.tool("wp-site-import"), "reset", call.args["site"]], tty=[call.args["site"]])


SITE = Arg("site")
VERBS = {
    "list": Verb("read", {}, h_list),
    "show": Verb("read", {"site": SITE}, h_show),
    "check-name": Verb("read", {"name": Arg("name")}, h_check_name),
    "jobs": Verb("read", {}, h_jobs),
    "job-status": Verb("read", {"job": Arg("text", rx=JOB_RE)}, h_job_status),
    "job-log": Verb("read", {"job": Arg("text", rx=JOB_RE), "offset": Arg("int", required=False, lo=0, hi=2 ** 40, default=0)}, h_job_log),
    "audit-tail": Verb("read", {"lines": Arg("int", required=False, lo=1, hi=200, default=50)}, h_audit_tail),
    "import-report": Verb("read", {"site": SITE}, h_import_report),
    "import-list": Verb("read", {}, h_import_list),
    "vuln-check": Verb("read", {"site": SITE}, h_vuln_check),
    "backup": Verb("job", {"site": SITE, "force": Arg("flag"), "tag": Arg("enum", required=False, values=BACKUP_TAGS)}, h_backup),
    "delete": Verb("job", {"site": SITE, "confirm_name": Arg("text", rx=NAME_RE, same="site"), "confirm_again": Arg("text", rx=NAME_RE, same="site")}, h_delete),
    "password": Verb("secret", {"site": SITE, "user": Arg("text", rx=USER_NAME_RE)}, h_password, lock=True),
    "create": Verb("job", {"name": Arg("name"), "label": Arg("text", required=False, rx=LABEL_RE)}, h_create),
    "apply": Verb("job", {"site": SITE}, h_apply),
    "set": Verb("job", {"site": SITE, "key": Arg("enum", values=SET_KEYS), "value": Arg("text", rx=ANY_RE, limit=SET_VALUE_MAX)}, h_set),
    "ensure-state": Verb("job", {"site": SITE, "state": Arg("enum", values=STATES), "confirm_name": Arg("text", rx=NAME_RE, same="site", when=("state", "live"))}, h_ensure_state),
    "install": Verb("secret", {"site": SITE, "title": Arg("text", rx=LABEL_RE), "user": Arg("text", rx=ADMIN_NAME_RE), "email": Arg("text", rx=EMAIL_RE)}, h_install, lock=True),
    "import-receive": Verb("stream", {"file": Arg("text", rx=IMPORT_FILE_RE)}, h_import_receive),
    "import-discard": Verb("inline", {"file": Arg("text", rx=IMPORT_FILE_RE), "if_present": Arg("flag")}, h_import_discard),
    "import-run": Verb("job", {"site": SITE, "file": Arg("text", rx=IMPORT_FILE_RE), "plan": Arg("flag"), "replace": Arg("flag"),
                               "confirm_name": Arg("text", rx=NAME_RE, same="site", needs="replace")}, h_import_run),
    "import-approve": Verb("job", {"site": SITE, "reject": Arg("text", required=False, rx=REJECT_RE), "accept_failures": Arg("flag"),
                                   "confirm_name": Arg("text", rx=NAME_RE, same="site", needs="accept_failures")}, h_import_approve),
    "import-rescan": Verb("job", {"site": SITE}, h_import_rescan),
    "import-new": Verb("job", {"name": Arg("name"), "label": Arg("text", required=False, rx=LABEL_RE), "file": Arg("text", rx=IMPORT_FILE_RE)}, h_import_new),
    "update-check": Verb("job", {"site": SITE}, h_update_check),
    "update-apply": Verb("job", {"site": SITE}, h_update_apply),
    "import-reset": Verb("job", {"site": SITE, "confirm_name": Arg("text", rx=NAME_RE, same="site")}, h_import_reset),
}


# ------------------------------------------------------------------------------------------------------------------------------------- one request
def dispatch(ctl, name, raw, req, coerce, fd):
    rec = {"req": req, "verb": name[:40], "caller": caller(ctl)}
    if isinstance(raw.get("site"), str) and NAME_RE.fullmatch(raw["site"]):
        rec["site"] = raw["site"]                                 # a refusal shows the site it was aimed at, when that is a plausible name; anything else is not copied into the audit
    try:
        if req is not None and not (isinstance(req, str) and REQ_RE.fullmatch(req)):
            raise Refuse("bad_request_id", "req is 1 to 64 letters, digits, dot, dash or underscore")
        verb = VERBS.get(name)
        if verb is None:
            raise Refuse("unknown_verb", "no such verb")
        args = parse_args(ctl, verb, raw, coerce)
        rec.update(site=target(args), args={k: v for k, v in args.items() if k not in ("site", "name")} or None)
        call = Call(ctl, name, args, rec, fd)
        if verb.kind == "read":
            return verb.handler(call)
        audit(ctl, "intent", rec)
        lock = None
        if verb.lock:
            lock = take_lock(ctl, args["site"])
            if lock is None:
                raise Refuse("busy", "another verb is running on %s" % args["site"])
        t0 = time.monotonic()
        try:
            reply = verb.handler(call)
        finally:
            if lock is not None:
                os.close(lock)
        phase = "duplicate" if reply.get("duplicate") else "started" if "job" in reply else "result"       # a job's own result line is written by the unit when the tool has finished
        if not audit(ctl, phase, dict(rec, ok=reply.get("ok"), rc=reply.get("rc"), job=reply.get("job"), duration_ms=millis(t0)), quiet=True):
            reply["audit"] = "the %s line could not be written" % phase
        return reply
    except Refuse as e:
        audit(ctl, "refused", dict(rec, error=e.code), quiet=True)
        raise


def main(cfg, argv, stdin_fd=0, out=None, env=None, now=time.time, run=subprocess.run):
    ctl = Ctl(cfg, os.environ if env is None else env, now, run)
    out = out or sys.stdout
    try:
        for d in ("jobs", "locks"):
            (ctl.state / d).mkdir(mode=0o700, parents=True, exist_ok=True)
        if argv[:1] == ["--run-job"]:
            return run_job(ctl, argv[1:])                         # the unit needs only what its job was given, so a table that went bad after the job started does not strand it
        check_limits(cfg, VERBS)
        prune_jobs(ctl)
        name, raw, req, coerce = read_request(ctl, argv, stdin_fd)
        reply = dispatch(ctl, name, raw, req, coerce, stdin_fd)
    except Refuse as e:
        reply = {"ok": False, "error": e.code, "detail": e.detail}
    except OSError as e:
        reply = {"ok": False, "error": "host_error", "detail": e.strerror or "an operating-system error"}
    out.write(json.dumps(reply, sort_keys=True) + "\n")
    out.flush()
    return 0 if reply.get("ok") else (1 if reply.get("ran") else 2)


if __name__ == "__main__":
    print("this module is run through templates/wp/wp_ctl.py.j2 (installed as wp-ctl)", file=sys.stderr)
    sys.exit(2)
