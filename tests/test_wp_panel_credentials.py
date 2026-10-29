#!/usr/bin/env python3
"""Offline tests of scripts/wp_panel_credentials.py, the helper that makes the control app's login secrets.

The helper uses only the standard library, but what it writes must be exactly what the app reads, so these tests check it against the real thing: Werkzeug's check_password_hash, pyotp's codes and the app's
own settings loader. They prove: the hash and the codes are the ones the app checks; the file is private, is never written inside the repo or over another file, and holds the three variables; no secret
value reaches the terminal except in `show-totp`, which asks first; a wrong password or code is reported without saying more; short or mismatched passwords are refused before anything is written. They do not
prove anything about a real authenticator app or the vault. Needs Werkzeug and pyotp (app/requirements.txt). Run: python3 -I tests/test_wp_panel_credentials.py"""
import base64
import io
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "app"))

import pyotp  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import wp_panel_credentials as cred  # noqa: E402
from wp_panel import config  # noqa: E402

PASSWORD = "a long passphrase for the panel 2026"


def getpw(*answers):
    it = iter(answers)
    return lambda prompt: next(it)


class Capture:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.dir = Path(self.d.name)
        self.out = self.dir / "plain.yml"

    def make(self, password=PASSWORD, out=None):
        cap = Capture()
        rc = cred.main(["make", str(out or self.out)], getpw=getpw(password, password), out=cap)
        return rc, cap

    def values(self):
        return cred.read_values(str(self.out))


class Hash(unittest.TestCase):
    def test_the_hash_is_the_one_the_app_checks(self):
        h = cred.password_hash(PASSWORD)
        self.assertRegex(h, r"^scrypt:32768:8:1\$[A-Za-z0-9]{16}\$[0-9a-f]{128}$")
        self.assertTrue(check_password_hash(h, PASSWORD))
        self.assertFalse(check_password_hash(h, PASSWORD + " "))
        self.assertFalse(check_password_hash(h, "x"))

    def test_the_helper_reads_hashes_werkzeug_made_too(self):
        h = generate_password_hash(PASSWORD)
        self.assertTrue(cred.password_ok(h, PASSWORD))
        self.assertFalse(cred.password_ok(h, "wrong"))
        self.assertFalse(cred.password_ok("plaintext", PASSWORD))
        self.assertFalse(cred.password_ok("pbkdf2:sha256:1000$abcdefghijklmnop$00", PASSWORD))

    def test_two_hashes_of_one_password_differ_by_their_salt(self):
        self.assertNotEqual(cred.password_hash(PASSWORD), cred.password_hash(PASSWORD))

    def test_unicode_passwords_work(self):
        h = cred.password_hash("pässwörd-日本語-ünïcode-123")
        self.assertTrue(check_password_hash(h, "pässwörd-日本語-ünïcode-123"))


class Totp(unittest.TestCase):
    def test_the_codes_are_pyotps_at_many_times(self):
        secret = pyotp.random_base32()
        t = pyotp.TOTP(secret)
        for now in range(1_800_000_000, 1_800_003_000, 37):
            self.assertEqual(cred.totp_code(secret, now // 30), t.at(now), now)

    def test_a_code_one_step_away_is_accepted_two_are_not(self):
        secret = pyotp.random_base32()
        now = 1_800_000_015.0
        t = pyotp.TOTP(secret)
        for off, want in ((-1, True), (0, True), (1, True), (-2, False), (2, False)):
            self.assertEqual(cred.totp_ok(secret, t.at(now + 30 * off), now), want, off)

    def test_a_code_of_the_wrong_shape_is_refused(self):
        secret = pyotp.random_base32()
        for code in ("", "12345", "1234567", "abcdef", " 123456", None):
            self.assertFalse(cred.totp_ok(secret, code, 1_800_000_000.0), repr(code))


class Make(Base):
    def test_it_writes_the_three_variables_in_a_private_file_and_the_app_accepts_them(self):
        rc, cap = self.make()
        self.assertEqual(rc, 0)
        mode = stat.S_IMODE(self.out.stat().st_mode)
        self.assertEqual(mode, 0o600)
        v = self.values()
        self.assertEqual(set(v), set(cred.KEYS))
        self.assertTrue(check_password_hash(v["aopenpanel_panel_password_hash"], PASSWORD))
        self.assertEqual(len(base64.b64decode(v["aopenpanel_panel_session_key"])), 48)
        self.assertEqual(len(v["aopenpanel_panel_totp_secret"]), 32)
        pyotp.TOTP(v["aopenpanel_panel_totp_secret"]).now()                  # a secret pyotp can use
        # what the app's own loader makes of the three values, written the way the play will write them
        secrets_dir = self.dir / "secrets"
        secrets_dir.mkdir()
        (secrets_dir / "session_key").write_text(v["aopenpanel_panel_session_key"])
        (secrets_dir / "password_hash").write_text(v["aopenpanel_panel_password_hash"])
        (secrets_dir / "totp_secret").write_text(v["aopenpanel_panel_totp_secret"])
        s = config.load({"PANEL_CTL_HOST": "dellwp", "PANEL_SECRET_DIR": str(secrets_dir)})
        self.assertTrue(check_password_hash(s.password_hash, PASSWORD))
        self.assertGreaterEqual(len(s.session_key), 32)

    def test_nothing_secret_is_printed_and_the_file_says_what_to_do(self):
        rc, cap = self.make()
        v = self.values()
        for value in v.values():
            self.assertNotIn(value, cap.text)
        self.assertNotIn(PASSWORD, cap.text)
        self.assertNotIn(base64.b64decode(v["aopenpanel_panel_session_key"]).hex(), cap.text)
        self.assertIn("No secret is shown", cap.text)
        self.assertIn("ansible-vault encrypt host_vars/aopenpanel/vault.yml", cap.text)
        self.assertIn("shred -u", cap.text)
        self.assertIn("PLAIN TEXT until encrypted", self.out.read_text())
        self.assertNotIn(PASSWORD, self.out.read_text(), "the password itself is never stored")

    def test_two_runs_make_different_secrets(self):
        self.make()
        a = self.values()
        b_path = self.dir / "other.yml"
        self.make(out=b_path)
        b = cred.read_values(str(b_path))
        for k in cred.KEYS:
            self.assertNotEqual(a[k], b[k], k)

    def test_it_never_overwrites_a_file_or_follows_a_link(self):
        self.out.write_text("precious")
        rc, _ = self.make()
        self.assertEqual((rc, self.out.read_text()), (2, "precious"))
        link = self.dir / "link.yml"
        link.symlink_to(self.dir / "target.yml")
        self.assertEqual(self.make(out=link)[0], 2)
        self.assertFalse((self.dir / "target.yml").exists())

    def test_it_refuses_a_path_inside_the_repo_a_relative_path_and_a_missing_or_open_directory(self):
        for bad in (ROOT / "host_vars/aopenpanel.yml", ROOT / "x.yml", "plain.yml", "~/../../nonexistent-dir/x.yml", self.dir / "nodir" / "x.yml"):
            with self.subTest(str(bad)):
                self.assertEqual(self.make(out=bad)[0], 2)
        self.assertFalse((ROOT / "host_vars/aopenpanel.yml").exists() or (ROOT / "x.yml").exists())
        open_dir = self.dir / "open"
        open_dir.mkdir()
        os.chmod(open_dir, 0o777)
        self.assertEqual(self.make(out=open_dir / "x.yml")[0], 2)

    def test_a_link_into_the_repo_does_not_get_round_the_rule(self):
        link = self.dir / "inrepo"
        link.symlink_to(ROOT)
        self.assertEqual(self.make(out=link / "x.yml")[0], 2)
        self.assertFalse((ROOT / "x.yml").exists())

    def test_a_short_or_mismatched_password_is_refused_before_anything_is_written(self):
        cap = Capture()
        self.assertEqual(cred.main(["make", str(self.out)], getpw=getpw("short", "short"), out=cap), 2)
        self.assertEqual(cred.main(["make", str(self.out)], getpw=getpw(PASSWORD, PASSWORD + "x"), out=cap), 2)
        self.assertFalse(self.out.exists())

    def test_the_prompts_name_the_rule_and_never_the_password(self):
        prompts = []
        cred.main(["make", str(self.out)], getpw=lambda p: prompts.append(p) or PASSWORD, out=Capture())
        self.assertTrue(prompts and all(PASSWORD not in p for p in prompts))
        self.assertIn("at least 14", prompts[0])


class ShowAndCheck(Base):
    def setUp(self):
        super().setUp()
        self.make()
        self.v = self.values()

    def test_show_asks_first_and_shows_nothing_on_a_no(self):
        cap = Capture()
        rc = cred.main(["show-totp", str(self.out)], ask=lambda p: "n", out=cap)
        self.assertEqual(rc, 1)
        self.assertNotIn(self.v["aopenpanel_panel_totp_secret"], cap.text)
        self.assertNotIn("otpauth", cap.text)

    def test_show_prints_the_secret_and_a_link_pyotp_understands_after_a_yes(self):
        cap = Capture()
        rc = cred.main(["show-totp", str(self.out)], ask=lambda p: "y", out=cap)
        self.assertEqual(rc, 0)
        secret = self.v["aopenpanel_panel_totp_secret"]
        self.assertIn(" ".join(secret[i:i + 4] for i in range(0, 32, 4)), cap.text)
        uri = re.search(r"otpauth://\S+", cap.text).group(0)
        parsed = pyotp.parse_uri(uri)
        self.assertEqual((parsed.secret, parsed.digits, parsed.interval), (secret, 6, 30))
        self.assertEqual(parsed.issuer, "dellwp panel")
        self.assertNotIn(self.v["aopenpanel_panel_password_hash"], cap.text)
        self.assertNotIn(self.v["aopenpanel_panel_session_key"], cap.text)

    def test_the_only_command_that_prints_a_secret_is_show_totp(self):
        """By the syntax tree: in every function but show_totp, no call to out() has a secret variable among its arguments, except inside len() (a length is not a secret)."""
        import ast
        tree = ast.parse((ROOT / "scripts/wp_panel_credentials.py").read_text())
        secret_names = {"secret", "session_key", "totp", "hashed", "password", "values", "uri", "text", "stored", "digest"}
        seen_out = 0
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "out"]:
                seen_out += 1
                bare = set()

                def walk(node, in_len=False):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
                        in_len = True
                    if isinstance(node, ast.Name) and not in_len:
                        bare.add(node.id)
                    for child in ast.iter_child_nodes(node):
                        walk(child, in_len)
                for arg in call.args:
                    walk(arg)
                if fn.name != "show_totp":
                    self.assertFalse(bare & secret_names, "%s prints %s" % (fn.name, sorted(bare & secret_names)))
        self.assertGreater(seen_out, 5)

    def test_check_accepts_the_right_password_and_code_and_says_only_that(self):
        now = 1_800_000_010.0
        code = pyotp.TOTP(self.v["aopenpanel_panel_totp_secret"]).at(now)
        cap = Capture()
        rc = cred.main(["check", str(self.out)], getpw=getpw(PASSWORD, code), out=cap, now=lambda: now)
        self.assertEqual((rc, cap.lines), (0, ["password: ok", "code: ok"]))

    def test_check_names_the_wrong_half_without_saying_anything_else(self):
        now = 1_800_000_010.0
        code = pyotp.TOTP(self.v["aopenpanel_panel_totp_secret"]).at(now)
        for pw, c, lines in ((PASSWORD + "x", code, ["password: WRONG", "code: ok"]), (PASSWORD, "000000", ["password: ok", "code: WRONG"])):
            cap = Capture()
            rc = cred.main(["check", str(self.out)], getpw=getpw(pw, c), out=cap, now=lambda: now)
            self.assertEqual(rc, 1)
            self.assertEqual(cap.lines[0], lines[0])
            self.assertTrue(cap.lines[1].startswith(lines[1]))
            self.assertNotIn(code, cap.text)

    def test_a_file_that_others_can_read_or_that_is_not_a_plain_file_or_lacks_a_value_is_refused(self):
        os.chmod(self.out, 0o644)
        self.assertEqual(cred.main(["check", str(self.out)], getpw=getpw("a", "b"), out=Capture()), 2)
        os.chmod(self.out, 0o600)
        broken = self.dir / "broken.yml"
        broken.write_text('aopenpanel_panel_session_key: "x"\n')
        os.chmod(broken, 0o600)
        self.assertEqual(cred.main(["check", str(broken)], getpw=getpw("a", "b"), out=Capture()), 2)
        self.assertEqual(cred.main(["check", str(self.dir)], getpw=getpw("a", "b"), out=Capture()), 2)
        self.assertEqual(cred.main(["check", str(self.dir / "missing.yml")], getpw=getpw("a", "b"), out=Capture()), 2)


class Source(unittest.TestCase):
    def test_standard_library_only_and_it_never_runs_the_vault_or_reads_a_secret_file_of_the_repo(self):
        text = (ROOT / "scripts/wp_panel_credentials.py").read_text()
        imports = {l.split()[1].split(".")[0] for l in text.splitlines() if l.startswith(("import ", "from "))}
        self.assertLessEqual(imports, {"base64", "getpass", "hashlib", "hmac", "os", "re", "secrets", "stat", "struct", "sys", "time", "urllib", "pathlib"})
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for bad in ("subprocess", "os.system", "vault_pass", ".vault", "ansible-vault\"", "clipboard", "xclip", "pbcopy", "wl-copy"):
            self.assertNotIn(bad, code, bad)

    def test_usage_is_a_refusal_not_a_traceback(self):
        r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/wp_panel_credentials.py")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
