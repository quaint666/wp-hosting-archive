#!/usr/bin/env python3
"""Offline tests of scripts/wp_wpress.py: the .wpress reader and `preflight`.

A synthetic archive is written in the real format (4377-byte headers) with both end markers (all zeros, and the last block Local writes, with no name and no path and the archive's
length in the size field). wordpress.org is a stand-in that answers from a table; nothing touches the network. They prove the verdicts (core newer, PHP requirement above ours, an
unsafe path, a plugin that wordpress.org does not know, a truncated archive, a network failure that must not read as GO), that our versions come from vars/wp.yml and fail loudly
when the line is missing, and that nothing from package.json beyond the printed fields reaches the output (it holds the archive owner's local paths and user name). They do not prove that
wordpress.org's real answers have this shape: `python3 scripts/wp_wpress.py preflight wpress/<file>` does, by hand. Run: python3 -I tests/test_wp_wpress.py"""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import wp_wpress as w  # noqa: E402

HEADER = 4377
SECRET = "SecretUserName"  # stands in for the archive owner's user name, which package.json carries


def block(name, path, size):
    return name.encode().ljust(255, b"\0") + str(size).encode().ljust(14, b"\0") + b"0".ljust(12, b"\0") + path.encode().ljust(HEADER - 281, b"\0")


def plugin_php(version, extra=""):
    return ("<?php\n/*\n * Plugin Name: X\n * Version: %s\n%s */\n" % (version, extra)).encode()


def theme_css(version):
    return ("/*\nTheme Name: T\nVersion: %s\n*/\n" % version).encode()


def build(files, tail="zeros", truncate=0):
    """files: [(path, name, bytes)]. tail: 'zeros' or 'local'."""
    out = io.BytesIO()
    for path, name, data in files:
        out.write(block(name, path, len(data)) + data)
    if tail == "zeros":
        out.write(b"\0" * HEADER)
    elif tail == "local":
        out.write(block("", "", out.tell()))
    data = out.getvalue()
    return data[:len(data) - truncate] if truncate else data


def meta(**over):
    m = {"WordPress": {"Version": "7.1.3"}, "PHP": {"Version": "8.5.12", "System": "WINNT"}, "Plugins": ["good/good.php", "single.php"], "Template": "blocksy", "Stylesheet": "blocksy",
         "Database": {"Username": SECRET, "Path": "C:\\Users\\%s\\Local Sites" % SECRET}, "Server": {"Path": "C:\\Users\\%s" % SECRET}}
    m.update(over)
    return m


def good_files(m=None):
    return [(".", "package.json", json.dumps(m or meta()).encode()), (".", "database.sql", b"SELECT 1;"), ("plugins/good", "good.php", plugin_php("1.0")),
            ("plugins", "single.php", plugin_php("2.0")), ("themes/blocksy", "style.css", theme_css("3.0")), ("themes/blocksy", "functions.php", b"<?php"), ("plugins/dormant", "dormant.php", plugin_php("9"))]


ANSWERS = {
    ("plugins", "good"): {"version": "1.1", "requires_php": "7.4"},
    ("plugins", "single"): {"version": "2.0", "requires_php": False},
    ("themes", "blocksy"): {"version": "3.1", "requires_php": "7.0"},
}


def get_from(answers):
    def get(url):
        kind = "plugins" if "/plugins/" in url else "themes"
        slug = url.split("request[slug]=")[1].split("&")[0]
        if (kind, slug) not in answers:
            return 404, None
        return 200, answers[(kind, slug)]
    return get


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.tmp = Path(self.d.name)
        (self.tmp / "vars").mkdir()
        (self.tmp / "vars/wp.yml").write_text('# renovate: depName=wordpress\nwp_image_php: "wordpress:7.1.3-php8.5-fpm"\n')
        self.out = []

    def tearDown(self):
        self.d.cleanup()

    def archive(self, files=None, **kw):
        p = self.tmp / "a.wpress"
        p.write_bytes(build(files if files is not None else good_files(), **kw))
        return str(p)

    def run_main(self, path, get=None):
        code = w.main(["preflight", path], repo=self.tmp, get=get or get_from(ANSWERS), out=self.out.append)
        return code, "\n".join(self.out)


class Verdicts(Base):
    def test_clean_archive_is_go_in_both_end_marker_shapes(self):
        for tail in ("zeros", "local"):
            self.out.clear()
            code, text = self.run_main(self.archive(tail=tail))
            self.assertEqual(code, 0, text)
            self.assertIn(text.splitlines()[-1], ("GO", "GO with notes"))

    def test_inactive_plugin_is_a_note_not_a_failure(self):
        code, text = self.run_main(self.archive())
        self.assertEqual(code, 0)
        self.assertIn("dormant", text)
        self.assertTrue(text.rstrip().endswith("GO with notes"))

    def test_core_newer_than_ours_is_no_go(self):
        code, text = self.run_main(self.archive(good_files(meta(WordPress={"Version": "7.2.0"}))))
        self.assertEqual(code, 1)
        self.assertIn("newer than the sites'", text)

    def test_core_older_is_a_note(self):
        code, text = self.run_main(self.archive(good_files(meta(WordPress={"Version": "6.8.1"}))))
        self.assertEqual(code, 0)
        self.assertIn("update-db", text)

    def test_other_php_minor_is_a_note_same_minor_is_silent(self):
        _, text = self.run_main(self.archive(good_files(meta(PHP={"Version": "7.4.33"}))))
        self.assertIn("ran PHP 7.4.33", text)
        self.out.clear()
        _, text = self.run_main(self.archive())
        self.assertNotIn("ran PHP", text)

    def test_a_different_minor_of_the_same_major_php_is_a_note(self):
        _, text = self.run_main(self.archive(good_files(meta(PHP={"Version": "8.4.1"}))))
        self.assertIn("ran PHP 8.4.1", text)

    def test_a_nameless_block_in_the_middle_is_not_the_end_marker(self):
        # only the LAST block may end the archive; an earlier one must not hide what follows it
        files = good_files()
        p = self.tmp / "a.wpress"
        p.write_bytes(block("", "", 0) + build(files))
        code, text = self.run_main(str(p))
        self.assertEqual(code, 0, text)

    def test_plugin_that_needs_newer_php_is_no_go(self):
        answers = dict(ANSWERS); answers[("plugins", "good")] = {"version": "1.1", "requires_php": "8.6"}
        code, text = self.run_main(self.archive(), get_from(answers))
        self.assertEqual(code, 1)
        self.assertIn("needs PHP 8.6", text)

    def test_plugin_at_exactly_our_php_is_fine(self):
        answers = dict(ANSWERS); answers[("plugins", "good")] = {"version": "1.1", "requires_php": "8.5"}
        self.assertEqual(self.run_main(self.archive(), get_from(answers))[0], 0)

    def test_plugin_not_on_wordpress_org_is_a_note_for_the_operator(self):
        answers = {k: v for k, v in ANSWERS.items() if k != ("plugins", "good")}
        code, text = self.run_main(self.archive(), get_from(answers))
        self.assertEqual(code, 0)
        self.assertIn("not on wordpress.org", text)

    def test_version_no_longer_offered_is_a_note(self):
        answers = dict(ANSWERS); answers[("plugins", "good")] = {"version": "1.1", "requires_php": "7.4", "versions": {"1.1": "u", "trunk": "u"}}
        _, text = self.run_main(self.archive(), get_from(answers))
        self.assertIn("no longer offered", text)

    def test_single_file_plugin_is_read(self):
        _, text = self.run_main(self.archive())
        self.assertRegex(text, r"plugin\s+single\s+2\.0\s+2\.0")

    def test_unsafe_paths_are_no_go(self):
        for path, name in (("plugins/../../etc", "x.php"), ("/etc", "passwd"), ("C:\\Windows", "x.php"), ("plugins/a\\b", "x.php"), ("plugins", "..")):
            self.out.clear()
            code, text = self.run_main(self.archive(good_files() + [(path, name, b"x")]))
            self.assertEqual(code, 1, (path, name))
            self.assertIn("unsafe path", text)

    def test_control_characters_in_a_name_are_no_go(self):
        for name in ("a\x01.php", "a\x7f.php", "a\n.php"):
            self.out.clear()
            code, text = self.run_main(self.archive(good_files() + [("plugins/good", name, b"x")]))
            self.assertEqual(code, 1, repr(name))
            self.assertIn("unsafe path", text)

    def test_a_multisite_archive_is_no_go(self):
        code, text = self.run_main(self.archive(good_files() + [(".", "multisite.json", b"{}")]))
        self.assertEqual(code, 1)
        self.assertIn("multisite", text)

    def test_pin_reads_the_image_reference(self):
        self.assertEqual(w.pin("wordpress:7.1.3-php8.5-fpm"), ("7.1.3", "8.5"))
        with self.assertRaises(w.ArchiveError):
            w.pin("wordpress:latest")

    def test_invisible_and_terminal_moving_characters_in_a_name_are_no_go(self):
        for name in ("a\x85.php", "a\u202eb.php", "a\u200bb.php", "a\ufeff.php", "a\u2028.php"):
            self.out.clear()
            code, text = self.run_main(self.archive(good_files() + [("plugins/good", name, b"x")]))
            self.assertEqual(code, 1, repr(name))
            self.assertIn("unsafe path", text)

    def test_ordinary_non_ascii_names_are_fine(self):
        code, _ = self.run_main(self.archive(good_files() + [("plugins/good", "\u00fcber-\u65e5\u672c.php", b"x")]))
        self.assertEqual(code, 0)

    def test_a_dotted_file_name_is_not_unsafe(self):
        code, _ = self.run_main(self.archive(good_files() + [("plugins/good", "..hidden.php", b"x"), ("plugins/good", "a..b.php", b"x")]))
        self.assertEqual(code, 0)


class Broken(Base):
    def test_truncated_archive_is_no_go(self):
        for cut in (10, HEADER + 5):
            self.out.clear()
            code, text = self.run_main(self.archive(tail="none", truncate=cut))
            self.assertEqual(code, 1)
            self.assertIn("truncated", text)

    def test_cut_in_the_middle_of_data_names_the_entry(self):
        files = good_files()
        p = self.tmp / "a.wpress"
        data = build(files)
        p.write_bytes(data[:data.index(b"SELECT 1;") + 3])
        code, text = self.run_main(str(p))
        self.assertEqual(code, 1)
        self.assertIn("truncated inside", text)

    def test_no_package_json_and_no_database_are_no_go(self):
        code, text = self.run_main(self.archive([f for f in good_files() if f[1] != "package.json"]))
        self.assertEqual(code, 1); self.assertIn("package.json", text)
        self.out.clear()
        code, text = self.run_main(self.archive([f for f in good_files() if f[1] != "database.sql"]))
        self.assertEqual(code, 1); self.assertIn("database.sql", text)

    def test_garbage_package_json_is_no_go(self):
        code, text = self.run_main(self.archive([(".", "package.json", b"{not json"), (".", "database.sql", b"x")]))
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", text)

    def test_network_failure_is_not_judged_and_never_go(self):
        def down(url):
            raise urllib.error.URLError("no route")
        code, text = self.run_main(self.archive(), down)
        self.assertEqual(code, 2)
        self.assertIn("NOT JUDGED", text)
        self.assertNotIn("\nGO", text)

    def test_a_plugin_without_a_readable_header_is_a_note(self):
        files = [f for f in good_files() if f[1] != "good.php"]
        code, text = self.run_main(self.archive(files))
        self.assertEqual(code, 0)
        self.assertIn("no readable header", text)


class Ours(Base):
    def test_versions_come_from_vars_wp_yml(self):
        (self.tmp / "vars/wp.yml").write_text('wp_image_php: "wordpress:7.2.0-php8.6-fpm"\n')
        self.assertEqual(w.ours(self.tmp), ("7.2.0", "8.6"))
        code, text = self.run_main(self.archive())
        self.assertIn("sites: WordPress 7.2.0, PHP 8.6", text)

    def test_a_missing_line_fails_loudly_with_no_default(self):
        (self.tmp / "vars/wp.yml").write_text("something_else: 1\n")
        code, text = self.run_main(self.archive())
        self.assertEqual(code, 1)
        self.assertIn("wp_image_php", text)

    def test_the_repos_real_pin_parses(self):
        wp, php = w.ours(Path(__file__).resolve().parent.parent)
        self.assertRegex(wp, r"^\d+\.\d+")
        self.assertRegex(php, r"^\d+\.\d+$")

    def test_version_compare(self):
        self.assertEqual(w.vtuple("7.1"), w.vtuple("7.1.0"))
        self.assertGreater(w.vtuple("7.10.0"), w.vtuple("7.9.9"))
        self.assertEqual(w.vtuple("8.5.12-beta1"), (8, 5, 12))


class Hostile(Base):
    """What a crafted archive can do to the reader: it must come out as a NO-GO, never as a traceback, a hang or text that moves the operator's terminal."""
    def pj(self, m, extra=()):
        raw = m if isinstance(m, bytes) else json.dumps(m).encode()
        return self.archive([(".", "package.json", raw), (".", "database.sql", b"x")] + list(extra))

    def test_a_negative_size_is_an_unreadable_header(self):
        for size in (b"-4400", b"-1", b"-99999999999"):
            h = block("x.php", "plugins/a", 5).replace(b"5".ljust(14, b"\0"), size.ljust(14, b"\0"), 1)
            p = self.tmp / "neg.wpress"
            p.write_bytes(h + b"abcde" + b"\0" * HEADER)
            code, text = self.run_main(str(p))
            self.assertEqual(code, 1, size)
            self.assertIn("negative size", text)

    def test_a_size_that_walks_back_to_an_earlier_header_cannot_loop(self):
        first = block("a.php", "plugins/a", 3) + b"abc"
        back = block("b.php", "plugins/a", 3)
        back = back.replace(b"3".ljust(14, b"\0"), str(-(len(first) + HEADER)).encode().ljust(14, b"\0"), 1)
        p = self.tmp / "loop.wpress"
        p.write_bytes(first + back + b"xyz" + b"\0" * HEADER)
        self.assertEqual(self.run_main(str(p))[0], 1)

    def test_package_json_of_the_wrong_shape_is_no_go_not_a_traceback(self):
        cases = [[1, 2], "text", 5, None, {"WordPress": "7.1.3"}, {"WordPress": {"Version": ["7"]}}, {"WordPress": {"Version": "7.1.3"}, "PHP": {"Version": []}, "Template": "t"},
                 {"WordPress": {"Version": "7.1.3"}, "Plugins": "abc", "Template": "t"}, {"WordPress": {"Version": "7.1.3"}, "Plugins": [1, None], "Template": "t"},
                 {"WordPress": {"Version": "7.1.3"}, "Plugins": [["a"]], "Template": "t"}, {"WordPress": {"Version": "7.1.3"}, "Plugins": [], "Template": ["t"]},
                 {"WordPress": {"Version": "7.1.3"}, "Plugins": []},
                 {"WordPress": "7.1.3", "Template": "t"}, {"WordPress": {"Version": ["7"]}, "Template": "t"}, {"WordPress": {"Version": ""}, "Template": "t"},
                 {"WordPress": {}, "Template": "t"}, {"Template": "t"}]
        for m in cases:
            self.out.clear()
            code, text = self.run_main(self.pj(m))
            self.assertEqual(code, 1, m)
            self.assertIn("NO-GO", text)

    def test_deeply_nested_json_is_no_go(self):
        code, text = self.run_main(self.pj(b"[" * 200000 + b"]" * 200000))
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", text)

    def test_a_package_json_over_the_limit_is_no_go(self):
        code, text = self.run_main(self.pj(b'{"x":"' + b"a" * (w.JSON_LIMIT + 10) + b'"}'))
        self.assertEqual(code, 1)
        self.assertIn("larger than", text)

    def test_plugin_and_theme_names_must_be_plain(self):
        for entry in ("../../x/y.php", "a/b/c.php", "..", "a b/c.php", "x/\x1b[2J.php", "/abs.php", "", "a" * 150 + "/b.php", "good\n/good.php", "good/good.php\n", "good\n"):
            self.out.clear()
            code, text = self.run_main(self.pj(meta(Plugins=[entry])))
            self.assertEqual(code, 1, entry)
            self.assertIn("not a plain", text)
        for theme in ("../..", "a/b", ".hidden", "", "blocksy\n"):
            self.out.clear()
            code, text = self.run_main(self.pj(meta(Template=theme, Stylesheet=theme)))
            self.assertEqual(code, 1, theme)

    def test_an_archive_with_no_theme_is_no_go(self):
        m = meta(); del m["Template"]; del m["Stylesheet"]
        code, text = self.run_main(self.pj(m))
        self.assertEqual(code, 1)
        self.assertIn("no active theme", text)

    def test_too_many_plugins_are_refused_before_any_lookup(self):
        asked = []

        def counting(url):
            asked.append(url)
            return 404, None
        m = meta(Plugins=["p%d/p.php" % i for i in range(w.MAX_ITEMS + 1)])
        code, text = self.run_main(self.pj(m), get=counting)
        self.assertEqual(code, 1)
        self.assertIn("more than", text)
        self.assertEqual(asked, [])

    def test_escape_sequences_in_a_header_never_reach_the_output(self):
        evil = plugin_php("1.0\x1b[2J\x1b[31mPWNED\u202e\x07 and a very long tail " + "z" * 200)
        code, text = self.run_main(self.pj(meta(Plugins=["good/good.php"]), [("plugins/good", "good.php", evil.replace("\\u202e".encode(), "\u202e".encode()))]))
        self.assertEqual(code, 0, text)
        for bad in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(bad, text)
        self.assertIn("1.0?[2J", text)
        self.assertLess(max(len(l) for l in text.splitlines()), 200)

    def test_printable_keeps_text_and_replaces_controls(self):
        self.assertEqual(w.printable("abc \u00e9\u00fc 1.2-beta"), "abc \u00e9\u00fc 1.2-beta")
        self.assertEqual(w.printable("a\x1bb\x7fc\x85d\ne", keep_newline=True), "a?b?c?d\ne")
        self.assertEqual(w.printable("a\nb"), "a?b")
        self.assertEqual(w.printable("x" * 50, limit=10), "x" * 10 + "...")


class Privacy(Base):
    def test_nothing_from_package_json_beyond_the_printed_fields_is_shown(self):
        for tail in ("zeros", "local"):
            self.out.clear()
            _, text = self.run_main(self.archive(tail=tail))
            self.assertNotIn(SECRET, text)
            self.assertNotIn("C:\\", text)


class Index(Base):
    def test_header_parsing_survives_junk_after_the_first_nul(self):
        # a field's bytes after its first NUL are not part of it (the real last block has such bytes)
        h = bytearray(block("good.php", "plugins/good", 4)); h[20:30] = b"JUNKJUNKJU"; h[300:310] = b"JUNKJUNKJU"
        p = self.tmp / "a.wpress"
        p.write_bytes(bytes(h) + b"data" + b"\0" * HEADER)
        with w.Archive(p) as a:
            self.assertEqual([(e[0], e[1], e[2]) for e in a.entries], [("good.php", "plugins/good", 4)])

    def test_header_version_reads_the_version_line_only(self):
        self.assertEqual(w.header_version(plugin_php("4.2.3", " * Requires PHP: 7.4\n")), "4.2.3")
        self.assertIsNone(w.header_version(b"<?php // no header"))


if __name__ == "__main__":
    unittest.main()
