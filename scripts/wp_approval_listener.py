#!/usr/bin/env python3
"""wp-approval-listener: follows the reply topic on the independent ntfy and hands each token to `wp-site-import resolve`. Runs on dellwp as root under systemd (templates/wp/wp_approval_listener.*.j2).

It decides nothing. A message is acted on only if its whole body is a token-shaped string (wp_approvals.TOKEN_RE, fullmatch); the string is passed as ONE argument of a command list (no shell) to the
tool, which verifies the signature, the expiry, the import it was issued for and its report, and consumes it. What the tool printed (its last line, with every control character replaced) goes to the
phone through ntfy-notify. Nothing from a message is ever logged in full, run, or written anywhere else. The listener is a reader with a read-only user: it cannot publish to the reply topic.

Robustness: it reconnects with a growing pause, remembers the id of the last message it handled (so a restart does not replay), ignores anything that is not a message event, handles at most
max_per_minute tokens a minute (a flood of junk cannot start a process per message), and a refused login pauses it for five minutes and tells you once an hour."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import wp_approvals as ap
import wp_wpress as w

BACKOFF = (1, 2, 4, 8, 16, 32, 60)
AUTH_PAUSE_S = 300
AUTH_TELL_EVERY_S = 3600
FIRST_SINCE = "10m"


class Listener:
    def __init__(self, cfg, opener=urllib.request.urlopen, run=subprocess.run, sleep=time.sleep, clock=time.time, out=print):
        self.cfg, self.opener, self.run, self.sleep, self.clock, self.out = cfg, opener, run, sleep, clock, out
        self.recent, self.last_auth_tell, self.last_id = [], None, None

    # --- state
    def load_id(self):
        try:
            text = Path(self.cfg["state_file"]).read_text().strip()
        except OSError:
            return None
        return text if re.fullmatch(r"[A-Za-z0-9]{1,40}", text) else None

    def save_id(self, msg_id):
        if not isinstance(msg_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,40}", msg_id):
            return
        p = Path(self.cfg["state_file"])
        tmp = p.with_name(p.name + ".new")
        tmp.write_text(msg_id + "\n")
        os.replace(tmp, p)
        self.last_id = msg_id

    # --- one message
    def tell(self, text):
        """To the phone, through ntfy-notify, which never fails the caller. Only text this program composed goes here."""
        try:
            self.run([self.cfg["notify_cmd"], self.cfg["result_topic"], text], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            self.out("could not notify: %s" % e)

    def handle(self, msg):
        """Process one decoded line. Returns a short word for the log and the tests."""
        if not isinstance(msg, dict) or msg.get("event") != "message":
            return "ignored (not a message)"
        msg_id = msg.get("id")
        if msg_id is not None and msg_id == self.last_id:
            return "ignored (already handled)"
        body = msg.get("message")
        if not isinstance(body, str) or not ap.TOKEN_RE.fullmatch(body):
            self.save_id(msg_id)
            self.out("ignored a message that is not a token")
            return "ignored (not a token)"
        now = self.clock()
        self.recent = [t for t in self.recent if now - t < 60]
        if len(self.recent) >= int(self.cfg["max_per_minute"]):
            self.save_id(msg_id)
            self.out("ignored a token: more than %s a minute" % self.cfg["max_per_minute"])
            return "ignored (too many)"
        self.recent.append(now)
        self.save_id(msg_id)
        self.out("resolving token %s..." % body[:10])
        try:
            p = self.run([self.cfg["tool"], "resolve", body], capture_output=True, text=True, timeout=int(self.cfg["resolve_timeout_s"]), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            self.tell("An import decision from the phone did not finish in time. Check the import at the control page.")
            return "timeout"
        except OSError as e:
            self.tell("An import decision from the phone could not be run: %s" % w.printable(e, limit=80))
            return "error"
        lines = [l for l in ((p.stdout or "") + "\n" + (p.stderr or "")).splitlines() if l.strip()]
        last = w.printable(lines[-1], limit=200) if lines else "no output"
        ok = p.returncode == 0
        self.tell(("Phone decision done: " if ok else "Phone decision refused: ") + last)
        self.out("resolve exit %s: %s" % (p.returncode, last))
        return "done" if ok else "refused"

    # --- the connection
    def connect(self):
        url, _ = ap.read_conf(self.cfg["ntfy_conf"])
        login, sep, password = Path(self.cfg["listener_file"]).read_text().strip().partition(":")
        if not sep or not login or not password:
            raise ap.ApprovalError("%s must hold   login:password   on one line" % self.cfg["listener_file"])
        since = self.last_id or self.load_id() or FIRST_SINCE
        req = urllib.request.Request("%s/%s/json?since=%s" % (url, self.cfg["reply_topic"], since), headers={"Authorization": ap.basic(login, password), "Accept": "application/x-ndjson",
                                                                                                           "User-Agent": "wp-approval-listener"})
        return self.opener(req, timeout=int(self.cfg["read_timeout_s"]))

    def once(self):
        """One connection until it ends. Returns the number of lines it delivered."""
        n = 0
        with self.connect() as resp:
            for raw in resp:
                n += 1
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self.handle(msg)
        return n

    def serve(self, rounds=None):
        fails, i = 0, 0
        while rounds is None or i < rounds:
            i += 1
            try:
                got = self.once()
                fails = 0 if got else fails + 1       # a connection that delivered something was a good one; one that delivered nothing counts as a failure
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    self.out("ntfy refused the login (%d): pausing %d s" % (e.code, AUTH_PAUSE_S))
                    now = self.clock()
                    if self.last_auth_tell is None or now - self.last_auth_tell > AUTH_TELL_EVERY_S:
                        self.last_auth_tell = now
                        self.tell("The approval listener on the VM is refused by ntfy (%d). Phone approvals do not work until the listener's login is fixed." % e.code)
                    self.sleep(AUTH_PAUSE_S)
                    continue
                self.out("ntfy answered %d" % e.code)
                fails += 1
            except (ap.ApprovalError, OSError, urllib.error.URLError) as e:
                self.out("cannot follow the topic: %s" % w.printable(e, limit=160))
                fails += 1
            self.sleep(BACKOFF[min(max(fails, 1) - 1, len(BACKOFF) - 1)])


def main(cfg, **kw):
    Listener(cfg, **kw).serve()
    return 0
