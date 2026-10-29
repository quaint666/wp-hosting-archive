"""The app's settings: where dellwp is, which files hold the secrets, how long a session lasts. Read from environment variables and root-only files; NOTHING secret is in the image or the repo.

Files in PANEL_SECRET_DIR (delivered by the play, root-only, mounted read-only into the container):
  session_key     at least 32 bytes of random data: signs the session cookie
  password_hash   a Werkzeug hash of the operator's password (starts with scrypt: or pbkdf2:), never the password itself
  totp_secret     the authenticator's base32 secret (at least 16 characters)
A missing or malformed file stops the app at start, naming the file and never printing a value."""
import os
import re

SECRET_DIR_DEFAULT = "/run/secrets"
STATE_DIR_DEFAULT = "/run/panel"


class SettingsError(Exception):
    pass


class Settings:
    def __init__(self, ctl_host, ctl_user, ctl_key, known_hosts, state_dir, session_s, session_key, password_hash, totp_secret):
        self.ctl_host, self.ctl_user, self.ctl_key, self.known_hosts = ctl_host, ctl_user, ctl_key, known_hosts
        self.state_dir, self.session_s = state_dir, session_s
        self.session_key, self.password_hash, self.totp_secret = session_key, password_hash, totp_secret


def read_file(path):
    with open(path, "rb") as f:
        return f.read(65536)


def load(env=None, read=read_file):
    env = os.environ if env is None else env
    problems = []

    def need(name):
        value = env.get(name, "")
        if not value:
            problems.append("%s is not set" % name)
        return value

    host = need("PANEL_CTL_HOST")
    secret_dir = env.get("PANEL_SECRET_DIR", SECRET_DIR_DEFAULT)
    hours = env.get("PANEL_SESSION_HOURS", "3")
    if not re.fullmatch(r"[1-9][0-9]{0,2}", hours) or not 1 <= int(hours) <= 24:
        problems.append("PANEL_SESSION_HOURS must be a whole number of hours from 1 to 24")
        hours = "3"

    def secret(name):
        try:
            return read(os.path.join(secret_dir, name))
        except OSError:
            problems.append("%s/%s cannot be read" % (secret_dir, name))
            return b""

    key = secret("session_key")
    if key and len(key) < 32:
        problems.append("session_key is shorter than 32 bytes")
    pw = secret("password_hash").decode("utf-8", "replace").strip()
    if pw and not re.fullmatch(r"(scrypt|pbkdf2):[^\s]+", pw):
        problems.append("password_hash is not a Werkzeug hash (it must start with scrypt: or pbkdf2:); the password itself must never be put there")
    totp = secret("totp_secret").decode("utf-8", "replace").strip().replace(" ", "").upper()
    if totp and not re.fullmatch(r"[A-Z2-7]{16,128}", totp):
        problems.append("totp_secret is not a base32 secret of at least 16 characters")
    if problems:
        raise SettingsError("; ".join(problems))
    return Settings(host, env.get("PANEL_CTL_USER", "wpctl"), env.get("PANEL_CTL_KEY", os.path.join(secret_dir, "panel_ssh_key")), env.get("PANEL_KNOWN_HOSTS", os.path.join(secret_dir, "known_hosts")),
                    env.get("PANEL_STATE_DIR", STATE_DIR_DEFAULT), int(hours) * 3600, key, pw, totp)
