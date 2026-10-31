#!/usr/bin/env python3
"""Approvals by phone for wp-site-import: signed one-time tokens, and the ntfy notification that carries them. Standard library only; copied to the VM with wp_site_import.py.

A token is   v1.<payload>.<mac>   where payload is base64url of a small JSON object {s: site, a: approve|deny, n: nonce, e: expiry (epoch seconds), r: sha256 of the report} and mac is
HMAC-SHA256 over the payload text with a key that only root on the VM can read (/etc/wp-approvals/key, derived from the master secret by the play). The signature is checked BEFORE the payload is
parsed. A token proves that the VM itself produced it for exactly this site, this action and this report, and not after it expired; the tool that accepts it (wp-site-import resolve) also checks the
import is still waiting, that the nonce is one it issued, and consumes it. Nothing here runs anything: a caller decides what a valid token may do.

The notification goes to the independent ntfy (the address and topic are read from /etc/ntfy-notify.conf, the same file the alert fallback uses, so the address exists in one place) as JSON with two
HTTP action buttons. Each button posts its token to the reply topic as a user that may only write there; its login is inside the button, which is why it can do nothing but post a token."""
import base64
import hashlib
import hmac
import json
import netrc
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_RE = re.compile(r"v1\.[A-Za-z0-9_-]{40,700}\.[0-9a-f]{64}")      # fullmatch
PLUGIN_TOKEN_RE = re.compile(r"p1\.[A-Za-z0-9_-]{40,700}\.[0-9a-f]{64}")   # fullmatch; a plugin request's token: another prefix, and the signature covers the prefix, so neither kind can be passed off as the other
SITE_RE = re.compile(r"[a-z][a-z0-9-]{1,23}")                            # fullmatch
ACTIONS = ("approve", "deny")
MIN_KEY = 32
BODY_MAX = 1800          # ntfy refuses a message over 4096 bytes; the summary stays well under it


class ApprovalError(Exception):
    pass


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def read_key(path):
    """The signing key, or ApprovalError. A missing or short key file means phone approval is off, which is a state, not a crash."""
    try:
        text = Path(path).read_text().strip()
    except OSError as e:
        raise ApprovalError("the signing key %s cannot be read (%s)" % (path, e.strerror or e))
    if len(text) < MIN_KEY:
        raise ApprovalError("the signing key %s is shorter than %d characters" % (path, MIN_KEY))
    return text.encode()


def sign(key, payload_text):
    return hmac.new(key, payload_text.encode(), hashlib.sha256).hexdigest()


def mint(key, site, action, report_sha, ttl_s, now=None, nonce=None):
    """(token, nonce) for one action on one site's report."""
    if action not in ACTIONS or not SITE_RE.fullmatch(site) or not re.fullmatch(r"[0-9a-f]{64}", report_sha):
        raise ApprovalError("cannot mint a token for %r %r" % (site, action))
    nonce = nonce or secrets.token_hex(16)
    payload = {"s": site, "a": action, "n": nonce, "e": int(now if now is not None else time.time()) + int(ttl_s), "r": report_sha}
    text = b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return "v1.%s.%s" % (text, sign(key, text)), nonce


def check_payload(d, now):
    """The shape every token's payload has, and its expiry; ApprovalError when either is wrong."""
    if (not isinstance(d, dict) or set(d) != {"s", "a", "n", "e", "r"} or not isinstance(d["s"], str) or not SITE_RE.fullmatch(d["s"]) or d["a"] not in ACTIONS
            or not isinstance(d["n"], str) or not re.fullmatch(r"[0-9a-f]{32}", d["n"]) or not isinstance(d["e"], int) or isinstance(d["e"], bool)
            or not isinstance(d["r"], str) or not re.fullmatch(r"[0-9a-f]{64}", d["r"])):
        raise ApprovalError("the payload has the wrong shape")
    if (now if now is not None else time.time()) > d["e"]:
        raise ApprovalError("the token has expired")
    return d


def verify(key, token, now=None):
    """The payload of a valid, unexpired token, or ApprovalError saying which check failed (the message is for the log and the operator, never for the sender)."""
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ApprovalError("not a token")
    _, text, mac = token.split(".")
    if not hmac.compare_digest(sign(key, text), mac):
        raise ApprovalError("the signature does not match")
    try:
        d = json.loads(unb64(text))
    except (ValueError, UnicodeDecodeError):
        raise ApprovalError("the payload is not readable")
    return check_payload(d, now)


def mint_plugin(key, site, action, request_sha, ttl_s, now=None, nonce=None):
    """(token, nonce) for one action on one plugin request. The payload has the import token's shape, `r` being the sha256 of the request's own identity; the prefix is p1 and the signature covers it."""
    if action not in ACTIONS or not SITE_RE.fullmatch(site) or not re.fullmatch(r"[0-9a-f]{64}", request_sha):
        raise ApprovalError("cannot mint a token for %r %r" % (site, action))
    nonce = nonce or secrets.token_hex(16)
    payload = {"s": site, "a": action, "n": nonce, "e": int(now if now is not None else time.time()) + int(ttl_s), "r": request_sha}
    text = b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return "p1.%s.%s" % (text, sign(key, "p1." + text)), nonce


def verify_plugin(key, token, now=None):
    """verify() for a plugin request's token."""
    if not isinstance(token, str) or not PLUGIN_TOKEN_RE.fullmatch(token):
        raise ApprovalError("not a plugin request token")
    _, text, mac = token.split(".")
    if not hmac.compare_digest(sign(key, "p1." + text), mac):
        raise ApprovalError("the signature does not match")
    try:
        d = json.loads(unb64(text))
    except (ValueError, UnicodeDecodeError):
        raise ApprovalError("the payload is not readable")
    return check_payload(d, now)


def read_conf(path):
    """NTFY_FALLBACK_URL and NTFY_FALLBACK_TOPIC out of /etc/ntfy-notify.conf (KEY="value" lines): the independent ntfy and the topic the phone already follows."""
    found = {}
    try:
        for line in Path(path).read_text().splitlines():
            m = re.fullmatch(r'\s*(NTFY_FALLBACK_URL|NTFY_FALLBACK_TOPIC)="([^"\n]*)"\s*', line)
            if m:
                found[m.group(1)] = m.group(2)
    except OSError as e:
        raise ApprovalError("%s cannot be read (%s)" % (path, e.strerror or e))
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+", found.get("NTFY_FALLBACK_URL", "")) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", found.get("NTFY_FALLBACK_TOPIC", "")):
        raise ApprovalError("%s has no usable NTFY_FALLBACK_URL (https) and NTFY_FALLBACK_TOPIC" % path)
    return found["NTFY_FALLBACK_URL"], found["NTFY_FALLBACK_TOPIC"]


def basic(login, password):
    return "Basic " + base64.b64encode(("%s:%s" % (login, password)).encode()).decode()


def sender_auth(netrc_path, url):
    """Basic auth for the write-only sender (the same netrc ntfy-notify uses), looked up by the server's name."""
    host = re.sub(r"^https://", "", url)
    try:
        found = netrc.netrc(netrc_path).authenticators(host)
    except (OSError, netrc.NetrcParseError) as e:
        raise ApprovalError("the sender's credentials %s cannot be read (%s)" % (netrc_path, getattr(e, "strerror", None) or e))
    if not found or not found[0] or not found[2]:
        raise ApprovalError("%s has no login for %s" % (netrc_path, host))
    return basic(found[0], found[2])


def approver_auth(path):
    """Basic auth of the button user (write-only on the reply topic), from a root-only file holding   login:password   on one line."""
    try:
        line = Path(path).read_text().strip()
    except OSError as e:
        raise ApprovalError("the button credentials %s cannot be read (%s)" % (path, e.strerror or e))
    login, sep, password = line.partition(":")
    if not sep or not login or not password:
        raise ApprovalError("%s must hold   login:password   on one line" % path)
    return basic(login, password)


def build_message(url, ask_topic, reply_topic, approver_header, site, body, tokens, title=None, tags=None):
    """The JSON that ntfy publishes: the summary and one http button per token. A button needs no reply from the server; clear=true removes the notification when tapped. The title and tags
    default to an import's."""
    labels = {"approve": "Approve", "deny": "Deny"}
    actions = [{"action": "http", "label": labels[a], "url": "%s/%s" % (url, reply_topic), "method": "POST", "headers": {"Authorization": approver_header}, "body": t, "clear": True}
               for a, t in tokens.items()]
    return {"topic": ask_topic, "title": title or "Import ready: %s" % site, "message": body[:BODY_MAX], "priority": 4, "tags": tags or ["package"], "actions": actions}


def post_json(url, auth_header, payload, timeout=15):
    """POST the JSON to the ntfy server's root. Returns the HTTP status; raises ApprovalError on a refusal or an unreachable server."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": auth_header, "User-Agent": "wp-site-import"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        e.close()
        raise ApprovalError("ntfy answered %d" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise ApprovalError("ntfy could not be reached (%s)" % (getattr(e, "reason", None) or e))
