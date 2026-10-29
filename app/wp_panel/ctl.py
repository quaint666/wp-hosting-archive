"""The app's one way to dellwp: send wp-ctl ONE JSON line over ssh and read ONE JSON line back. Standard library only.

The ssh key on the control VM is forced to `sudo -n /usr/local/sbin/wp-ctl` in dellwp's authorized_keys, so this client names no remote command: whatever it sends arrives as the request on wp-ctl's
standard input, and wp-ctl checks the verb against its own allowlist and every argument against its own rules. That is the real lock. This module is a second one on the app's side: it refuses a verb
that is not in READ_VERBS or ACTION_VERBS, an argument the verb does not take, and a value that does not look the way wp-ctl wants it, BEFORE anything is sent, so a bug in a page cannot ask for more
than the page is for. ACTION_VERBS are the verbs that change something and that a page has been written for; each step-6 batch adds its own (batch 11: create, set, ensure-state; delete, import and the
password verbs are not here yet). An action may carry a request id (`req`): the same id again returns the job the first one started, so a form sent twice runs once.

Nothing here builds a shell line: ssh gets an argument list, the request travels on standard input, and the environment ssh sees holds nothing but a PATH. What goes wrong is reported as CtlError with a
kind the pages can show: not_allowed (this client refused), unreachable (no answer from dellwp), refused (wp-ctl said no, status 2), failed (wp-ctl ran and failed, status 1), bad_reply (the answer
was not what wp-ctl sends). The messages are plain text for the page to escape."""
import json
import re
import subprocess

CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
REQ_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")                                 # wp-ctl's REQ_RE
LABEL_RE = re.compile(r"^[^\s].{0,98}[^\s]$|^[^\s]$")                          # wp-ctl's LABEL_RE
SET_KEYS = ("update_policy", "quota_mb", "backup_quota_mb", "php_mem_mb", "db_mem_mb", "upload_mb", "allow_xmlrpc", "allow_app_passwords", "allow_user_enum", "mail_mode", "label", "note")
STATES = ("hold", "live", "stopped")
SET_VALUE_MAX = 500

NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,22}[a-z0-9])$")                  # the site-name rule of scripts/wp_site_model.py and scripts/wp_ctl.py
JOB_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")                      # the job id wp-ctl makes (scripts/wp_ctl.py JOB_RE)
MAX_REQUEST_BYTES = 4096                                                      # wp-ctl's limit is larger; a read request is tiny
MAX_REPLY_BYTES = 2 * 1024 * 1024                                             # a job log chunk is 64 KiB; nothing wp-ctl sends for a read is near this


def _site(v):
    return isinstance(v, str) and NAME_RE.fullmatch(v) is not None and "--" not in v


def _job(v):
    return isinstance(v, str) and JOB_RE.fullmatch(v) is not None


def _int(lo, hi):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


# verb -> {argument: (required, check)}; the same names and ranges as the Verb table in scripts/wp_ctl.py
READ_VERBS = {
    "list": {},
    "show": {"site": (True, _site)},
    "jobs": {},
    "job-status": {"job": (True, _job)},
    "job-log": {"job": (True, _job), "offset": (False, _int(0, 2 ** 40))},
    "audit-tail": {"lines": (False, _int(1, 200))},
}


def _text(limit, rx=None, empty=False):
    def check(v):
        return (isinstance(v, str) and (empty or v != "") and len(v) <= limit and CONTROL_RE.search(v) is None and (rx is None or rx.fullmatch(v) is not None))
    return check


def _one_of(values):
    return lambda v: isinstance(v, str) and v in values


# verb -> {argument: (required, check)}, for the verbs that change something; the names are the Verb table's in scripts/wp_ctl.py (a test compares them)
ACTION_VERBS = {
    "create": {"name": (True, _site), "label": (False, _text(100, LABEL_RE))},
    "set": {"site": (True, _site), "key": (True, _one_of(SET_KEYS)), "value": (True, _text(SET_VALUE_MAX, empty=True))},
    "ensure-state": {"site": (True, _site), "state": (True, _one_of(STATES)), "confirm_name": (False, _site)},
}
ALLOWED = dict(READ_VERBS, **ACTION_VERBS)


def _rules(verb, args):
    """What the argument table cannot say: going live needs the site's own name typed (wp-ctl: confirm_name, equal to the site), and nothing else takes one."""
    if verb == "ensure-state":
        if args.get("state") == "live" and args.get("confirm_name") != args.get("site"):
            raise CtlError("not_allowed", "going live needs the site's name typed exactly")
        if args.get("state") != "live" and "confirm_name" in args:
            raise CtlError("not_allowed", "only going live is confirmed by the name")


class CtlError(Exception):
    """kind: not_allowed | unreachable | refused | failed | bad_reply. `code` is wp-ctl's error code when it gave one. str(e) is a sentence for the page."""

    def __init__(self, kind, message, code=None):
        super().__init__(message)
        self.kind, self.code = kind, code


class Ctl:
    def __init__(self, host, user, key_file, known_hosts, timeout_s=30, run=subprocess.run, ssh="ssh", path="/usr/bin:/bin"):
        self.host, self.user, self.key_file, self.known_hosts = host, user, key_file, known_hosts
        self.timeout_s, self.run, self.ssh, self.path = timeout_s, run, ssh, path
        for label, value in (("host", host), ("user", user)):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", value):
                raise ValueError("the %s must be a plain name: %r" % (label, value))

    def command(self):
        """The ssh argument list. No remote command: the key is forced to wp-ctl on dellwp. Nothing is asked of a user, an agent or a terminal, and no forwarding is set up."""
        return [self.ssh, "-T", "-F", "/dev/null", "-i", self.key_file,
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "PasswordAuthentication=no", "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile=" + self.known_hosts, "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
                "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "LogLevel=ERROR",
                "%s@%s" % (self.user, self.host)]

    def request(self, verb, args):
        """The one JSON line to send, or CtlError(not_allowed). Checks the verb, the argument names and every value before anything leaves."""
        spec = ALLOWED.get(verb) if isinstance(verb, str) else None
        if spec is None:
            raise CtlError("not_allowed", "this page may not ask for %r" % (verb if isinstance(verb, str) else "that"))
        args = dict(args)
        has_req = verb in ACTION_VERBS and "req" in args
        req = args.pop("req") if has_req else None
        if has_req and not (isinstance(req, str) and REQ_RE.fullmatch(req)):
            raise CtlError("not_allowed", "%s: the request id is not valid" % verb)
        extra = sorted(set(args) - set(spec))
        if extra:
            raise CtlError("not_allowed", "%s takes no argument %s" % (verb, ", ".join(repr(k) for k in extra)))
        for name, (required, check) in spec.items():
            if name not in args:
                if required:
                    raise CtlError("not_allowed", "%s needs %s" % (verb, name))
                continue
            if not check(args[name]):
                raise CtlError("not_allowed", "%s is not a valid %s" % (verb, name))
        _rules(verb, args)
        if has_req:
            args["req"] = req
        line = json.dumps(dict(args, verb=verb), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        if len(line.encode()) > MAX_REQUEST_BYTES:
            raise CtlError("not_allowed", "the request is too long")
        return line

    def call(self, verb, **args):
        """wp-ctl's reply (a dict with ok true) or a CtlError."""
        line = self.request(verb, args)
        try:
            p = self.run(self.command(), input=line.encode(), capture_output=True, timeout=self.timeout_s, env={"PATH": self.path})
        except subprocess.TimeoutExpired:
            raise CtlError("unreachable", "dellwp did not answer within %d seconds" % self.timeout_s)
        except OSError as e:
            raise CtlError("unreachable", "ssh could not be started: %s" % (e.strerror or "an operating-system error"))
        out = (p.stdout or b"")[:MAX_REPLY_BYTES + 1]
        if p.returncode == 255:
            raise CtlError("unreachable", "dellwp could not be reached: %s" % self._why(p.stderr))
        if len(out) > MAX_REPLY_BYTES:
            raise CtlError("bad_reply", "the answer from dellwp is larger than this page accepts")
        try:
            text = out.decode("utf-8")
            lines = text.splitlines()
            reply = json.loads(lines[0]) if len(lines) == 1 else None
        except (UnicodeDecodeError, ValueError, IndexError):
            reply = None
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise CtlError("bad_reply", "dellwp did not answer with one JSON line (status %s): %s" % (p.returncode, self._why(p.stderr or out)))
        if reply["ok"] and p.returncode == 0:
            return reply
        if reply["ok"] or p.returncode == 0:
            raise CtlError("bad_reply", "the answer and the exit status of wp-ctl disagree (ok=%s, status %s)" % (reply["ok"], p.returncode))
        code = reply.get("error") if isinstance(reply.get("error"), str) else None
        detail = reply.get("detail") if isinstance(reply.get("detail"), str) else ""
        kind = "refused" if p.returncode == 2 else "failed"
        raise CtlError(kind, "wp-ctl %s: %s%s" % ("refused" if kind == "refused" else "failed", code or "no error code", (" (%s)" % detail[:200]) if detail else ""), code)

    @staticmethod
    def _why(raw):
        """A short, one-line piece of ssh's own message for the page: first line, control characters out, at most 160 characters."""
        text = (raw or b"").decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        first = text.strip().splitlines()[0] if text.strip() else "no message"
        return re.sub(r"[\x00-\x1f\x7f]", " ", first)[:160]
