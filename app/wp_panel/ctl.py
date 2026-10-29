"""The app's one way to dellwp: send wp-ctl ONE JSON line over ssh and read ONE JSON line back. Standard library only.

The ssh key on the control VM is forced to `sudo -n /usr/local/sbin/wp-ctl` in dellwp's authorized_keys, so this client names no remote command: whatever it sends arrives as the request on wp-ctl's
standard input, and wp-ctl checks the verb against its own allowlist and every argument against its own rules. That is the real lock. This module is a second one on the app's side: it refuses a verb
that is not in READ_VERBS or ACTION_VERBS, an argument the verb does not take, and a value that does not look the way wp-ctl wants it, BEFORE anything is sent, so a bug in a page cannot ask for more
than the page is for. ACTION_VERBS are the verbs that change something and that a page has been written for; each step-6 batch adds its own (batch 11: create, set, ensure-state; batch 12: delete, and the
two that take a password, SECRET_VERBS: password, install; the import verbs are not here yet). An action may carry a request id (`req`): the same id again returns the job the first one started, so a form sent
twice runs once. A secret verb sends the request line and then the password as a second line on the same standard input (wp-ctl hands it to the tool through a file in a RAM disk): the password is never an
argument, never in the request line, and never in a message of this module; what comes back is scrubbed here again before it is shown.

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
USER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,60}$")                         # wp-ctl's USER_NAME_RE (an existing WordPress user)
ADMIN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")                         # wp-ctl's ADMIN_NAME_RE
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")                          # wp-ctl's EMAIL_RE
MIN_PASSWORD, MAX_PASSWORD = 14, 200                                          # wp-ctl's MIN_PASSWORD and MAX_TEXT
TIMEOUTS = {"password": 330, "install": 660}                                  # seconds an answer may take: wp-ctl's own limit for the verb (300, 600) and a margin; every other verb uses timeout_s

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
ACTION_VERBS["delete"] = {"site": (True, _site), "confirm_name": (True, _site), "confirm_again": (True, _site)}
SECRET_VERBS = {
    "password": {"site": (True, _site), "user": (True, _text(60, USER_NAME_RE))},
    "install": {"site": (True, _site), "title": (True, _text(100, LABEL_RE)), "user": (True, _text(30, ADMIN_NAME_RE)), "email": (True, _text(254, EMAIL_RE))},
}
ALLOWED = dict(READ_VERBS, **ACTION_VERBS, **SECRET_VERBS)


def good_password(v):
    return isinstance(v, str) and MIN_PASSWORD <= len(v) <= MAX_PASSWORD and CONTROL_RE.search(v) is None


def _rules(verb, args):
    """What the argument table cannot say: going live needs the site's own name typed (wp-ctl: confirm_name, equal to the site), and nothing else takes one."""
    if verb == "delete" and not (args.get("confirm_name") == args.get("site") == args.get("confirm_again")):
        raise CtlError("not_allowed", "a delete needs the site's name typed twice, exactly")
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

    def call(self, verb, secret=None, **args):
        """wp-ctl's reply (a dict with ok true) or a CtlError. `secret` is the password of a SECRET verb (and of no other)."""
        line = self.request(verb, args)
        if verb in SECRET_VERBS:
            if not good_password(secret):
                raise CtlError("not_allowed", "the password must be %d to %d characters on one line, without control characters" % (MIN_PASSWORD, MAX_PASSWORD))
            payload = line + secret + "\n"
        elif secret is not None:
            raise CtlError("not_allowed", "%s takes no password" % verb)
        else:
            payload = line
        timeout = TIMEOUTS.get(verb, self.timeout_s)
        try:
            p = self.run(self.command(), input=payload.encode(), capture_output=True, timeout=timeout, env={"PATH": self.path})
        except subprocess.TimeoutExpired:
            raise CtlError("unreachable", "dellwp did not answer within %d seconds" % timeout)
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
        if not detail and isinstance(reply.get("out"), str):
            detail = reply["out"]
        if secret:
            detail = detail.replace(secret, "REDACTED")                   # before the text is cut or its spaces folded, so no piece of the password survives either
        detail = " ".join(detail.split())[-300:]
        raise CtlError(kind, "wp-ctl %s: %s%s" % ("refused" if kind == "refused" else "failed", code or "no error code", (" (%s)" % detail) if detail else ""), code)

    @staticmethod
    def _why(raw):
        """A short, one-line piece of ssh's own message for the page: first line, control characters out, at most 160 characters."""
        text = (raw or b"").decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        first = text.strip().splitlines()[0] if text.strip() else "no message"
        return re.sub(r"[\x00-\x1f\x7f]", " ", first)[:160]
