"""The login: a password and a one-time code from an authenticator, and a lockout that every worker process shares.

One operator, so one lockout for the whole login (not per address: behind the proxy every request comes from one address anyway). A wrong login of either kind counts the same and is answered the same,
so a visitor cannot tell which half was wrong. A code is accepted once: a code that was just used is refused again, so a code seen over a shoulder or in a log cannot be replayed within its 30 seconds.
The state (recent failures, the lock, the last code step used) is one small file in the state directory, changed under a file lock, so three gunicorn workers see the same count. The directory is
a RAM disk inside the container: a restart clears it, and an attacker cannot restart the container."""
import contextlib
import fcntl
import hmac
import json
import os
import re
import tempfile

import pyotp
from werkzeug.security import check_password_hash

STEP_S = 30
MAX_PASSWORD = 256


class Throttle:
    def __init__(self, state_dir, clock, max_fails=5, window_s=900, lock_s=900):
        self.dir, self.clock, self.max_fails, self.window_s, self.lock_s = state_dir, clock, max_fails, window_s, lock_s

    @contextlib.contextmanager
    def _state(self):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        with open(os.path.join(self.dir, "login.lock"), "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            path = os.path.join(self.dir, "login.json")
            try:
                with open(path) as f:
                    state = json.load(f)
                if not isinstance(state, dict):
                    raise ValueError
            except (OSError, ValueError):
                state = {}
            state = {"fails": [t for t in state.get("fails", []) if isinstance(t, (int, float))], "locked_until": state.get("locked_until", 0) if isinstance(state.get("locked_until"), (int, float)) else 0,
                     "last_step": state.get("last_step", 0) if isinstance(state.get("last_step"), int) else 0}
            yield state
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".login-")
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)

    def locked_for(self):
        """Seconds the login is still locked (0 when it is open)."""
        with self._state() as s:
            return max(0, int(s["locked_until"] - self.clock() + 0.999))

    def record_failure(self):
        now = self.clock()
        with self._state() as s:
            s["fails"] = [t for t in s["fails"] if now - t < self.window_s] + [now]
            if len(s["fails"]) >= self.max_fails:
                s["locked_until"], s["fails"] = now + self.lock_s, []

    def accept_step(self, step):
        """A right login at this code step: false when the step was already used (a replay); else remember it and clear the failures."""
        with self._state() as s:
            if step <= s["last_step"]:
                return False
            s["last_step"], s["fails"], s["locked_until"] = step, [], 0
            return True


def totp_step(secret, code, now):
    """The code step (30 seconds each) that `code` is the right code for, looking one step back and one forward for clock drift, or None. Every step is compared, in constant time."""
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        return None
    totp = pyotp.TOTP(secret)
    base = int(now // STEP_S)
    found = None
    for step in (base - 1, base, base + 1):
        if hmac.compare_digest(totp.at(step * STEP_S).encode(), code.encode()) and found is None:
            found = step
    return found


def check_credentials(settings, password, code, throttle, clock):
    """("ok", 0), ("wrong", 0) or ("locked", seconds). Both halves are always checked, so the answer and its time do not say which was wrong."""
    wait = throttle.locked_for()
    if wait:
        return "locked", wait
    pw = password if isinstance(password, str) and len(password) <= MAX_PASSWORD else ""
    pw_ok = check_password_hash(settings.password_hash, pw) and pw != ""
    step = totp_step(settings.totp_secret, code, clock())
    if pw_ok and step is not None and throttle.accept_step(step):
        return "ok", 0
    throttle.record_failure()
    return "wrong", 0
