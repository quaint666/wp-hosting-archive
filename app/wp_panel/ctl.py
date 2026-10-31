"""The app's one way to dellwp: send wp-ctl ONE JSON line over ssh and read ONE JSON line back. Standard library only.

The ssh key on the control VM is forced to `sudo -n /usr/local/sbin/wp-ctl` in dellwp's authorized_keys, so this client names no remote command: whatever it sends arrives as the request on wp-ctl's
standard input, and wp-ctl checks the verb against its own allowlist and every argument against its own rules. That is the real lock. This module is a second one on the app's side: it refuses a verb
that is not in READ_VERBS or ACTION_VERBS, an argument the verb does not take, and a value that does not look the way wp-ctl wants it, BEFORE anything is sent, so a bug in a page cannot ask for more
than the page is for. ACTION_VERBS are the verbs that change something and that a page has been written for; each step-6 batch adds its own (follow-ups: import-list, vuln-check, update-check, update-apply, import-new; batch 11: create, set, ensure-state; batch 12: delete, and the
two that take a password, SECRET_VERBS: password, install; batch 13: the import verbs, and the one upload, STREAM_VERBS: import-receive, whose bytes follow the request line on the same standard input). An action may carry a request id (`req`): the same id again returns the job the first one started, so a form sent
twice runs once. A secret verb sends the request line and then the password as a second line on the same standard input (wp-ctl hands it to the tool through a file in a RAM disk): the password is never an
argument, never in the request line, and never in a message of this module; what comes back is scrubbed here again before it is shown.

Nothing here builds a shell line: ssh gets an argument list, the request travels on standard input, and the environment ssh sees holds nothing but a PATH. What goes wrong is reported as CtlError with a
kind the pages can show: not_allowed (this client refused), unreachable (no answer from dellwp), refused (wp-ctl said no, status 2), failed (wp-ctl ran and failed, status 1), bad_reply (the answer
was not what wp-ctl sends). The messages are plain text for the page to escape."""
import hashlib
import json
import re
import subprocess
import threading

CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
REQ_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")                                 # wp-ctl's REQ_RE
LABEL_RE = re.compile(r"^[^\s].{0,98}[^\s]$|^[^\s]$")                          # wp-ctl's LABEL_RE
SET_KEYS = ("update_policy", "quota_mb", "backup_quota_mb", "php_mem_mb", "db_mem_mb", "upload_mb", "allow_xmlrpc", "allow_app_passwords", "allow_user_enum", "mail_mode", "cart_fragments", "label", "note")
STATES = ("hold", "live", "stopped")
SET_VALUE_MAX = 500
USER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,60}$")                         # wp-ctl's USER_NAME_RE (an existing WordPress user)
ADMIN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")                         # wp-ctl's ADMIN_NAME_RE
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")                          # wp-ctl's EMAIL_RE
MIN_PASSWORD, MAX_PASSWORD = 14, 200                                          # wp-ctl's MIN_PASSWORD and MAX_TEXT
PLUGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")                  # wp-ctl's PLUGIN_RE
PLUGIN_ACTIONS = ("activate", "deactivate", "delete")                          # wp-ctl's PLUGIN_ACTIONS
REQUEST_ID_RE = re.compile(r"^[a-f0-9]{8,24}$")                                # wp-ctl's REQUEST_ID_RE
REQUEST_DECISIONS = ("approve", "deny")                                        # wp-ctl's REQUEST_DECISIONS
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")                                  # wp-ctl's SLUG_RE (a wordpress.org plugin name)
REJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(,[A-Za-z0-9][A-Za-z0-9._-]*)*")      # wp-ctl's REJECT_RE: plugin and theme slugs, comma separated
FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.wpress")                                # wp-ctl's IMPORT_FILE_RE
FILE_MAX = 100
UPLOAD_MAX = 3072 * 1024 * 1024                                              # the intake quota (wp_intake_quota_mb): nothing larger could be kept
CHUNK = 64 * 1024
STREAM_TIMEOUT_S = 14400 + 600                                               # wp-ctl's own limit for import-receive (14400) and a margin
RECEIVED_RE = re.compile(r"^received ([0-9a-f]{64}) ([0-9]+) (\S+)$")        # the one line wp-site-import receive prints
TIMEOUTS = {"password": 330, "install": 660, "vuln-check": 330}                                  # seconds an answer may take: wp-ctl's own limit for the verb (300, 600) and a margin; every other verb uses timeout_s

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
    "import-report": {"site": (True, _site)},
    "import-list": {},
    "vuln-check": {"site": (True, _site)},
    "users": {"site": (True, _site)},
    "plugins": {"site": (True, _site)},
    "plugin-requests": {},
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
ACTION_VERBS["plugin-set"] = {"site": (True, _site), "plugin": (True, _text(100, PLUGIN_RE)), "action": (True, _one_of(PLUGIN_ACTIONS)), "confirm_plugin": (False, _text(100, PLUGIN_RE))}
ACTION_VERBS["plugin-requests-collect"] = {}
ACTION_VERBS["plugin-request-decide"] = {"site": (True, _site), "id": (True, _text(24, REQUEST_ID_RE)), "decision": (True, _one_of(REQUEST_DECISIONS)), "confirm_slug": (False, _text(100, SLUG_RE))}
ACTION_VERBS["delete"] = {"site": (True, _site), "confirm_name": (True, _site), "confirm_again": (True, _site)}


def _bool(v):
    return isinstance(v, bool)


def _file(v):
    return isinstance(v, str) and len(v) <= FILE_MAX and FILE_RE.fullmatch(v) is not None


ACTION_VERBS.update({
    "import-run": {"site": (True, _site), "file": (True, _file), "plan": (False, _bool), "replace": (False, _bool), "confirm_name": (False, _site)},
    "import-approve": {"site": (True, _site), "reject": (False, _text(200, REJECT_RE)), "accept_failures": (False, _bool), "confirm_name": (False, _site)},
    "import-rescan": {"site": (True, _site)},
    "import-new": {"name": (True, _site), "label": (False, _text(100, LABEL_RE)), "file": (True, _file)},
    "update-check": {"site": (True, _site)},
    "update-apply": {"site": (True, _site)},
    "import-reset": {"site": (True, _site), "confirm_name": (True, _site)},
    "import-discard": {"file": (True, _file), "if_present": (False, _bool)},
})
STREAM_VERBS = {"import-receive": {"file": (True, _file)}}
SECRET_VERBS = {
    "password": {"site": (True, _site), "user": (True, _text(60, USER_NAME_RE))},
    "install": {"site": (True, _site), "title": (True, _text(100, LABEL_RE)), "user": (True, _text(30, ADMIN_NAME_RE)), "email": (True, _text(254, EMAIL_RE))},
}
ALLOWED = dict(READ_VERBS, **ACTION_VERBS, **SECRET_VERBS, **STREAM_VERBS)


def good_password(v):
    return isinstance(v, str) and MIN_PASSWORD <= len(v) <= MAX_PASSWORD and CONTROL_RE.search(v) is None


def _rules(verb, args):
    """What the argument table cannot say: going live needs the site's own name typed (wp-ctl: confirm_name, equal to the site), and nothing else takes one."""
    if verb == "delete" and not (args.get("confirm_name") == args.get("site") == args.get("confirm_again")):
        raise CtlError("not_allowed", "a delete needs the site's name typed twice, exactly")
    if verb == "import-approve":
        if args.get("accept_failures") is True and args.get("confirm_name") != args.get("site"):
            raise CtlError("not_allowed", "accepting the failures of an import needs the site's name typed exactly")
        if args.get("accept_failures") is not True and "confirm_name" in args:
            raise CtlError("not_allowed", "only accepting failures is confirmed by the name")
    if verb == "plugin-set":
        if args.get("action") == "delete" and args.get("confirm_plugin") != args.get("plugin"):
            raise CtlError("not_allowed", "deleting a plugin needs its name typed exactly")
        if args.get("action") != "delete" and "confirm_plugin" in args:
            raise CtlError("not_allowed", "only a delete is confirmed by the plugin's name")
    if verb == "plugin-request-decide" and "confirm_slug" in args and args.get("decision") != "approve":
        raise CtlError("not_allowed", "only an approval is confirmed by the plugin's name")
    if verb == "import-reset" and args.get("confirm_name") != args.get("site"):
        raise CtlError("not_allowed", "a reset needs the site's name typed exactly")
    if verb == "import-run":
        if args.get("replace") is True and args.get("confirm_name") != args.get("site"):
            raise CtlError("not_allowed", "replacing a site's data needs the site's name typed exactly")
        if args.get("replace") is not True and "confirm_name" in args:
            raise CtlError("not_allowed", "only a replace is confirmed by the name")
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
        if verb in STREAM_VERBS:
            raise CtlError("not_allowed", "%s is sent with stream(): its bytes follow the request" % verb)
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
        return self._interpret(p.returncode, p.stdout, p.stderr, secret)

    def _interpret(self, returncode, stdout, stderr, secret=None):
        """The one JSON line wp-ctl answered with, as a dict with ok true, or the CtlError that says what went wrong."""
        out = (stdout or b"")[:MAX_REPLY_BYTES + 1]
        if returncode == 255:
            raise CtlError("unreachable", "dellwp could not be reached: %s" % self._why(stderr))
        if len(out) > MAX_REPLY_BYTES:
            raise CtlError("bad_reply", "the answer from dellwp is larger than this page accepts")
        try:
            text = out.decode("utf-8")
            lines = text.splitlines()
            reply = json.loads(lines[0]) if len(lines) == 1 else None
        except (UnicodeDecodeError, ValueError, IndexError):
            reply = None
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise CtlError("bad_reply", "dellwp did not answer with one JSON line (status %s): %s" % (returncode, self._why(stderr or out)))
        if reply["ok"] and returncode == 0:
            return reply
        if reply["ok"] or returncode == 0:
            raise CtlError("bad_reply", "the answer and the exit status of wp-ctl disagree (ok=%s, status %s)" % (reply["ok"], returncode))
        code = reply.get("error") if isinstance(reply.get("error"), str) else None
        detail = reply.get("detail") if isinstance(reply.get("detail"), str) else ""
        kind = "refused" if returncode == 2 else "failed"
        if not detail and isinstance(reply.get("out"), str):
            detail = reply["out"]
        if secret:
            detail = detail.replace(secret, "REDACTED")                   # before the text is cut or its spaces folded, so no piece of the password survives either
        detail = " ".join(detail.split())[-300:]
        raise CtlError(kind, "wp-ctl %s: %s%s" % ("refused" if kind == "refused" else "failed", code or "no error code", (" (%s)" % detail) if detail else ""), code)

    def stream(self, verb, chunks, popen=subprocess.Popen, **args):
        """Send a STREAM verb: the request line, then every chunk of `chunks` (an iterable of bytes), then end of input. Nothing is kept in memory but the chunk in hand, and nothing is written to disk. Returns
        {"file", "bytes", "sha256"} once wp-ctl's one line (`received <sha256> <bytes> <name>`) matches what was sent: a different count, hash or name is a CtlError(bad_reply), because a file of another
        content than the browser's may now be in the intake. If `chunks` raises (the browser went away), ssh is stopped and the exception goes on: wp-site-import cannot tell that end of input from a
        finished file, so the caller removes what may have been kept (import-discard). A stalled ssh is stopped after STREAM_TIMEOUT_S."""
        if verb not in STREAM_VERBS:
            raise CtlError("not_allowed", "this page may not stream %r" % (verb if isinstance(verb, str) else "that"))
        line = self.request(verb, args)
        try:
            p = popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={"PATH": self.path})
        except OSError as e:
            raise CtlError("unreachable", "ssh could not be started: %s" % (e.strerror or "an operating-system error"))
        timer = threading.Timer(STREAM_TIMEOUT_S, p.kill)
        timer.daemon = True
        timer.start()
        digest, sent = hashlib.sha256(), 0
        try:
            try:
                p.stdin.write(line.encode())
                for chunk in chunks:
                    p.stdin.write(chunk)
                    digest.update(chunk)
                    sent += len(chunk)
            except BrokenPipeError:
                pass                                                          # dellwp stopped reading (a refusal): its answer says why
            except BaseException:
                p.kill()
                p.wait()
                raise
            try:
                p.stdin.close()
            except (OSError, ValueError):
                pass
            p.stdin = None                                                    # before Python 3.13 communicate() flushes a stdin that is already closed and raises ValueError
            out, err = p.communicate()
        finally:
            timer.cancel()
            for f in (p.stdin, p.stdout, p.stderr):
                try:
                    f and f.close()
                except OSError:
                    pass
        reply = self._interpret(p.returncode, out, err)
        m = RECEIVED_RE.match(" ".join(str(reply.get("out", "")).split()))
        if not m or (m.group(1), int(m.group(2)), m.group(3)) != (digest.hexdigest(), sent, args["file"]):
            raise CtlError("bad_reply", "dellwp's account of the file does not match what was sent (%d bytes): it was not trusted" % sent)
        return {"file": args["file"], "bytes": sent, "sha256": digest.hexdigest()}

    @staticmethod
    def _why(raw):
        """A short, one-line piece of ssh's own message for the page: first line, control characters out, at most 160 characters."""
        text = (raw or b"").decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        first = text.strip().splitlines()[0] if text.strip() else "no message"
        return re.sub(r"[\x00-\x1f\x7f]", " ", first)[:160]
