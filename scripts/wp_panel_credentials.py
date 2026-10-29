#!/usr/bin/env python3
"""Make, enrol and check the control app's login secrets and its ssh key. Standard library only (ssh-keygen makes the key); run it on the ThinkPad.

    python3 scripts/wp_panel_credentials.py make OUT        ask for a password (hidden, twice), make the session key, the authenticator secret and an ed25519 ssh key, write all four to OUT; the PUBLIC key goes to OUT.pub
    python3 scripts/wp_panel_credentials.py show-totp OUT   show the authenticator secret and its otpauth link, ONCE, on this terminal, to enrol an authenticator app (ask to confirm first)
    python3 scripts/wp_panel_credentials.py check OUT       ask for the password and a code from the authenticator (both hidden) and say only whether OUT accepts them

OUT is a plain YAML file of four variables, aopenpanel_panel_session_key, aopenpanel_panel_password_hash, aopenpanel_panel_totp_secret and aopenpanel_panel_ssh_key (the private key, a block): the names
the play will read from host_vars/aopenpanel/vault.yml. OUT.pub is the matching public key line: not a secret, it goes into host_vars/dellwp/vars.yml (wp_ctl_panel_keys) and may be read and committed. It is created with mode 0600, never overwritten, and must be OUTSIDE this repo (a secret is never written into the repo, not even for a minute). It is PLAIN TEXT until
you encrypt it: move it to host_vars/aopenpanel/vault.yml, run `ansible-vault encrypt` on it yourself (this script never runs ansible-vault and never reads the vault password), and delete the original
with `shred -u`. The password itself is never stored: only a scrypt hash in the format the app checks (Werkzeug's). `make` prints the path, the lengths and what to do next, and no secret.
`show-totp` is the one command that prints a secret: run it alone, in a terminal nobody is watching, and do not paste its output anywhere.

The authenticator code is RFC 6238 (HMAC-SHA1, 6 digits, 30 seconds), what every authenticator app does by default, checked one step either way like the app does."""
import base64
import getpass
import hashlib
import hmac
import os
import re
import secrets
import stat
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KEYS = ("aopenpanel_panel_session_key", "aopenpanel_panel_password_hash", "aopenpanel_panel_totp_secret", "aopenpanel_panel_ssh_key")
KEY_COMMENT = "wp-panel@aopenpanel"
PUBLIC_RE = re.compile(r"ssh-ed25519 AAAA[0-9A-Za-z+/]+=*( [A-Za-z0-9@._-]+)?")        # what tasks/wp_ctl_common.yml accepts in wp_ctl_panel_keys
MIN_PASSWORD = 14
SALT_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
N, R, P = 2 ** 15, 8, 1                       # Werkzeug's scrypt defaults: what the app's check_password_hash reads from the hash itself
ISSUER = "dellwp panel"


class Refused(Exception):
    pass


def password_hash(password, salt=None):
    """Werkzeug's scrypt hash: scrypt:32768:8:1$<16 salt characters>$<128 hex characters>."""
    salt = salt or "".join(secrets.choice(SALT_CHARS) for _ in range(16))
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=N, r=R, p=P, maxmem=132 * N * R * P).hex()
    return "scrypt:%d:%d:%d$%s$%s" % (N, R, P, salt, digest)


def password_ok(stored, password):
    m = re.fullmatch(r"scrypt:(\d+):(\d+):(\d+)\$([A-Za-z0-9]{16})\$([0-9a-f]{128})", stored)
    if not m:
        return False
    n, r, p = int(m[1]), int(m[2]), int(m[3])
    got = hashlib.scrypt(password.encode(), salt=m[4].encode(), n=n, r=r, p=p, maxmem=132 * n * r * p).hex()
    return hmac.compare_digest(got.encode(), m[5].encode())


def totp_code(secret, step):
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000)


def totp_ok(secret, code, now):
    if not re.fullmatch(r"[0-9]{6}", code or ""):
        return False
    base = int(now // 30)
    return any([hmac.compare_digest(totp_code(secret, s).encode(), code.encode()) for s in (base - 1, base, base + 1)])


def new_ssh_key(parent, run=subprocess.run):
    """(private key text, public key line) of a new ed25519 key made by ssh-keygen in a private directory beside OUT, which is removed again."""
    work = tempfile.mkdtemp(prefix=".panel-key-", dir=str(parent))          # mode 0700
    try:
        keyfile = os.path.join(work, "k")
        try:
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", KEY_COMMENT, "-f", keyfile], check=True, capture_output=True, stdin=subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
            private = Path(keyfile).read_text()
            public = Path(keyfile + ".pub").read_text().strip()
        except (OSError, subprocess.CalledProcessError) as e:
            raise Refused("ssh-keygen could not make the key (is OpenSSH installed?): %s" % (getattr(e, "strerror", None) or "it failed"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----") or not PUBLIC_RE.fullmatch(public):
        raise Refused("ssh-keygen did not make an ed25519 key in the form the play accepts")
    return private, public


def derive_public(private, parent, run=subprocess.run):
    """The public key (type and base64 part) of a private key text, by ssh-keygen -y, or None when ssh-keygen is not there."""
    if shutil.which("ssh-keygen") is None:
        return None
    work = tempfile.mkdtemp(prefix=".panel-key-", dir=str(parent))
    try:
        keyfile = os.path.join(work, "k")
        fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(private)
        p = run(["ssh-keygen", "-y", "-f", keyfile], capture_output=True, text=True, stdin=subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return " ".join(p.stdout.split()[:2]) if p.returncode == 0 else ""


def check_out_path(raw):
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise Refused("OUT must be an absolute path (for example ~/panel_vault_plain.yml)")
    resolved = path.parent.resolve() / path.name
    if REPO == resolved or REPO in resolved.parents:
        raise Refused("OUT is inside the repo (%s): a secret is never written there, not even for a minute" % REPO)
    if not resolved.parent.is_dir():
        raise Refused("the directory %s does not exist" % resolved.parent)
    if resolved.parent.stat().st_mode & stat.S_IWOTH:
        raise Refused("the directory %s is writable by everyone" % resolved.parent)
    for candidate in (resolved, resolved.parent / (resolved.name + ".pub")):
        if candidate.exists() or candidate.is_symlink():
            raise Refused("%s exists already; this script never overwrites a file it made" % candidate)
    return resolved


def read_values(raw):
    path = Path(raw).expanduser()
    if not path.is_file() or path.is_symlink():
        raise Refused("%s is not a plain file" % path)
    if path.stat().st_mode & 0o077:
        raise Refused("%s is readable by others (mode %o); fix with chmod 600" % (path, path.stat().st_mode & 0o777))
    values = {}
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        m = re.fullmatch(r'(aopenpanel_panel_[a-z_]+): "([^"\n]*)"', line)
        if m:
            values[m[1]] = m[2]
        elif line == "aopenpanel_panel_ssh_key: |":
            block = []
            for follow in lines[i + 1:]:
                if not follow.startswith("  "):
                    break
                block.append(follow[2:])
            values["aopenpanel_panel_ssh_key"] = "\n".join(block) + "\n" if block else ""
    missing = [k for k in KEYS if not values.get(k)]
    if missing:
        raise Refused("%s lacks %s" % (path, ", ".join(missing)))
    return values


def ask_new_password(getpw):
    one = getpw("New password for the control app (at least %d characters, not shown): " % MIN_PASSWORD)
    if len(one) < MIN_PASSWORD:
        raise Refused("the password has fewer than %d characters" % MIN_PASSWORD)
    if getpw("Again: ") != one:
        raise Refused("the two entries differ")
    return one


def make(raw_out, getpw=getpass.getpass, out=print, rand=os.urandom, keygen=new_ssh_key):
    path = check_out_path(raw_out)
    password = ask_new_password(getpw)
    private, public = keygen(path.parent)
    session_key = base64.b64encode(rand(48)).decode()
    totp = base64.b32encode(rand(20)).decode()
    hashed = password_hash(password)
    block = "".join("  %s\n" % line for line in private.rstrip("\n").split("\n"))
    text = ("---\n# PLAIN TEXT until encrypted. Move to host_vars/aopenpanel/vault.yml, ansible-vault encrypt it, then shred -u this copy. Made by scripts/wp_panel_credentials.py.\n"
            'aopenpanel_panel_session_key: "%s"\naopenpanel_panel_password_hash: "%s"\naopenpanel_panel_totp_secret: "%s"\naopenpanel_panel_ssh_key: |\n%s' % (session_key, hashed, totp, block))
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    pub_path = path.parent / (path.name + ".pub")
    fd = os.open(str(pub_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(public + "\n")
    out("wrote %s (mode 600): session key %d characters, password hash %d characters, authenticator secret %d characters, ssh private key %d lines. No secret is shown." % (path, len(session_key), len(hashed), len(totp), len(block.splitlines())))
    out("wrote %s (mode 644): the PUBLIC ssh key. It is not a secret; it goes into host_vars/dellwp/vars.yml (wp_ctl_panel_keys)." % pub_path)
    out("Next, in this order:")
    out("  1. python3 scripts/wp_panel_credentials.py show-totp %s     (alone; enrol your authenticator app; do not paste its output)" % path)
    out("  2. python3 scripts/wp_panel_credentials.py check %s         (proves the password and the app's code both work)" % path)
    out("  3. install -d -m 700 host_vars/aopenpanel && install -m 600 %s host_vars/aopenpanel/vault.yml && ansible-vault encrypt host_vars/aopenpanel/vault.yml" % path)
    out("  4. shred -u %s     (the .pub file stays: it is the public key)" % path)
    return 0


def show_totp(raw_out, ask=input, out=print):
    values = read_values(raw_out)
    secret = values["aopenpanel_panel_totp_secret"]
    out("This prints the authenticator secret on this terminal. Nobody should be able to see the screen, and the output must not be pasted anywhere.")
    if ask("Show it now? [y/N] ").strip().lower() != "y":
        out("Nothing was shown.")
        return 1
    uri = "otpauth://totp/%s:operator?secret=%s&issuer=%s&algorithm=SHA1&digits=6&period=30" % (urllib.parse.quote(ISSUER), secret, urllib.parse.quote(ISSUER))
    out("Secret (base32): %s" % " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
    out("Link for an app that takes one: %s" % uri)
    out("Enter the secret in your authenticator app (time based, 6 digits, 30 seconds), then clear this terminal (`clear`).")
    return 0


def check(raw_out, getpw=getpass.getpass, out=print, now=time.time):
    values = read_values(raw_out)
    pw_ok = password_ok(values["aopenpanel_panel_password_hash"], getpw("Password (not shown): "))
    code_ok = totp_ok(values["aopenpanel_panel_totp_secret"], getpw("Code from the authenticator (not shown): ").strip(), now())
    out("password: %s" % ("ok" if pw_ok else "WRONG"))
    out("code: %s" % ("ok" if code_ok else "WRONG (is your phone's clock right? the code changes every 30 seconds)"))
    key_ok = True
    pub_file = Path(raw_out).expanduser().parent / (Path(raw_out).name + ".pub")
    private = values["aopenpanel_panel_ssh_key"]
    if not private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----") or not private.rstrip().endswith("-----END OPENSSH PRIVATE KEY-----"):
        key_ok = False
        out("ssh key: WRONG (not an OpenSSH private key block)")
    elif pub_file.is_file() and not pub_file.is_symlink():
        derived = derive_public(private, pub_file.parent)
        wanted = " ".join(pub_file.read_text().split()[:2])
        if derived is None:
            out("ssh key: block looks right; ssh-keygen is not here, so it was not compared with %s" % pub_file.name)
        else:
            key_ok = derived == wanted
            out("ssh key: %s" % ("ok (it is the private half of %s)" % pub_file.name if key_ok else "WRONG (it does not match %s)" % pub_file.name))
    else:
        out("ssh key: block looks right; %s is not beside the file, so it was not compared" % pub_file.name)
    return 0 if pw_ok and code_ok and key_ok else 1


def main(argv, **kw):
    try:
        if len(argv) == 2 and argv[0] == "make":
            return make(argv[1], **{k: v for k, v in kw.items() if k in ("getpw", "out", "rand", "keygen")})
        if len(argv) == 2 and argv[0] == "show-totp":
            return show_totp(argv[1], **{k: v for k, v in kw.items() if k in ("ask", "out")})
        if len(argv) == 2 and argv[0] == "check":
            return check(argv[1], **{k: v for k, v in kw.items() if k in ("getpw", "out", "now")})
    except Refused as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    print(__doc__.split("\n\n")[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
