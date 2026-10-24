#!/usr/bin/env python3
"""Offline tests of the status command: templates/wp/wp_sites_status.sh.j2 (the host's half, run here against a real temporary filesystem and stand-ins for systemctl, docker and
repquota) and scripts/wp_sites_status.py (the ThinkPad's half, driven with canned reports). They prove the logic: how each column is read, what is flagged and what is not, the exit
code. They cannot prove that the real `docker ps` prints what the tool reads or that the real systemctl answers the way the stand-in does: the first run on dellwp shows that.
Run: python3 -I tests/test_wp_status.py"""
import io
import os
import stat
import sys
import time
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from test_wp_backup import Base  # noqa: E402
from test_wp_sites_render import ROOT, fixture_vars_text  # noqa: E402
import wp_sites_status as W  # noqa: E402


def _fixture_repo():
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="wpstatus-fixture-")); (d / "host_vars/dellwp").mkdir(parents=True)
    (d / "host_vars/dellwp/vars.yml").write_text(fixture_vars_text())
    return d


FIXTURE_REPO = _fixture_repo()   # the real variables with one fixed live site: the repo's own sites come and go

REPQUOTA = '#!/bin/bash\n[ -f "$SHIM/repquota_fail" ] && exit 1\ncat "$SHIM/repquota.txt"\n'
SYSTEMCTL = (
    '#!/bin/bash\n[ "$1" = is-active ] || exit 0\n[ -f "$SHIM/systemctl_silent" ] && exit 1\n'
    's=$(grep -E "^$2 " "$SHIM/units" 2>/dev/null | cut -d" " -f2-)\n'
    'if [ -z "$s" ]; then echo inactive; exit 3; fi\necho "$s"; [ "$s" = active ]\n')   # like the real one: a unit it knows nothing about is "inactive", exit 3
DOCKER = '#!/bin/bash\n[ "$1" = ps ] && cat "$SHIM/ps.txt"\nexit 0\n'
HEALTHY = "wp_wptest_php|Up 2 hours\nwp_wptest_db|Up 2 hours (healthy)\n"


class HostTool(Base):
    def setUp(self):
        super().setUp()
        self.shim("systemctl", SYSTEMCTL); self.shim("docker", DOCKER); self.shim("repquota", REPQUOTA)
        self.vhosts = self.tmp / "conf.d"; self.vhosts.mkdir()
        self.table = "wptest:live:1001:3072:2001:3072"
        self.units("wp-wptest active", "wp-cron@wptest.timer active", "wp-backup@wptest.timer active")
        self.put("ps.txt", HEALTHY)
        self.put("repquota.txt", "#1001     --  314573       0 3145728     4475     0     0\n#2001     --  94372       0 3145728     600     0     0\n")
        self.vhost("wptest", hold=False)
        (self.backups / "wptest" / ".last-success").write_text("20261024T201500Z")
        self.age(5)

    def units(self, *lines):
        self.put("units", "\n".join(lines) + "\n")

    def vhost(self, name, hold):
        (self.vhosts / ("wp_site_%s.conf" % name)).write_text("server {\n    server_name x;\n%s}\n" % ("    allow 127.0.0.1;\n    deny all;\n" if hold else ""))

    def age(self, hours):
        t = time.time() - hours * 3600
        os.utime(self.backups / "wptest" / ".last-success", (t, t))

    def rows(self, table=None):
        script = self.install("wp_sites_status.sh.j2", "bin/wp-sites-status", wp_status_sites=table or self.table)
        r = self.run_script(script, WP_STATUS_VHOST_DIR=str(self.vhosts))
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0].split("\t"), W.COLUMNS, "the header is the contract with the ThinkPad half")
        return [dict(zip(W.COLUMNS, l.split("\t"))) for l in lines[1:]], r.stdout

    def one(self, **kw):
        return self.rows()[0][0]

    def test_a_healthy_live_site_reads_as_it_is(self):
        r = self.rows()[0][0]
        self.assertEqual((r["site"], r["deployed"], r["actual"], r["unit"], r["containers"], r["core"], r["plugins"]), ("wptest", "live", "live", "active", "2/2", "7.1.3", "1"))
        self.assertEqual((r["backup_age_h"], r["backup_stamp"], r["frozen"]), ("5", "20261024T201500Z", "no"))
        self.assertEqual((r["quota_pct"], r["backup_quota_pct"], r["cron_timer"], r["backup_timer"]), ("10", "3", "active", "active"))

    def test_a_single_file_plugin_counts_and_the_index_stub_does_not(self):
        plugins = self.site / "html/wp-content/plugins"
        (plugins / "hello.php").write_text("<?php // Hello Dolly\n")
        (plugins / "index.php").write_text("<?php // Silence is golden.\n")
        (plugins / "notes.txt").write_text("not a plugin")
        self.assertEqual(self.one()["plugins"], "2", "the directory p plus hello.php; not index.php, not notes.txt")

    def test_every_line_has_the_same_number_of_fields_and_no_stray_tabs(self):
        self.table = "wptest:live:1001:3072:2001:3072 other-site:hold:1002:3072:2002:3072"
        _, out = self.rows()
        for l in out.splitlines():
            self.assertEqual(len(l.split("\t")), len(W.COLUMNS), l)

    def test_the_vhost_on_disk_decides_actual_not_the_declaration(self):
        self.vhost("wptest", hold=True)
        self.assertEqual(self.one()["actual"], "hold")
        (self.vhosts / "wp_site_wptest.conf").unlink()
        self.assertEqual(self.one()["actual"], "none")
        self.assertEqual(self.one()["deployed"], "live", "deployed is what the play rendered, whatever the vhost says")

    def test_a_container_that_is_unhealthy_starting_restarting_or_gone_is_not_counted_up(self):
        for ps, want in ((HEALTHY, "2/2"), ("wp_wptest_php|Up 2 hours\nwp_wptest_db|Up 2 hours (unhealthy)\n", "1/2"),
                         ("wp_wptest_php|Up 2 seconds\nwp_wptest_db|Up 3 seconds (health: starting)\n", "1/2"),
                         ("wp_wptest_php|Restarting (1) 5 seconds ago\nwp_wptest_db|Up 2 hours (healthy)\n", "1/2"),
                         ("wp_wptest_php|Exited (0) 2 hours ago\n", "0/2"), ("", "0/2"), ("other|Up 1 hour\n", "0/2")):
            self.put("ps.txt", ps)
            self.assertEqual(self.one()["containers"], want, ps)

    def test_a_site_name_with_hyphens_matches_its_underscore_container_names(self):
        self.table = "my-site:live:1001:3072:2001:3072"
        (self.site.parent / "my-site").mkdir()
        self.put("ps.txt", "wp_my_site_php|Up 1 hour\nwp_my_site_db|Up 1 hour (healthy)\n")
        self.units("wp-my-site active")
        r = self.rows()[0][0]
        self.assertEqual((r["site"], r["containers"], r["unit"]), ("my-site", "2/2", "active"))

    def test_no_backup_yet_and_unreadable_quotas_are_shown_as_such_and_an_unknown_unit_is_inactive(self):
        (self.backups / "wptest" / ".last-success").unlink()
        self.put("repquota_fail", ""); self.units()
        r = self.one()
        self.assertEqual((r["backup_age_h"], r["backup_stamp"], r["quota_pct"], r["backup_quota_pct"]), ("-", "never", "?", "?"))
        self.assertEqual((r["unit"], r["cron_timer"], r["backup_timer"]), ("inactive", "inactive", "inactive"))

    def test_a_systemctl_that_says_nothing_is_shown_as_unknown_not_as_a_state(self):
        self.put("systemctl_silent", "")
        r = self.one()
        self.assertEqual((r["unit"], r["cron_timer"], r["backup_timer"]), ("unknown", "unknown", "unknown"))

    def test_both_freeze_flags_show_and_bad_entries_are_skipped(self):
        (self.tmp / "freeze").write_text("")
        self.assertEqual(self.one()["frozen"], "yes")
        (self.tmp / "freeze").unlink(); (self.site / "freeze").write_text("")
        self.assertEqual(self.one()["frozen"], "yes")
        self.table = "../x:live:1:1:1:1 a;id:live:1:1:1:1 zero:live:1:0:1:1 wptest:live:1001:3072:2001:3072 bad"
        self.assertEqual([r["site"] for r in self.rows()[0]], ["wptest"])

    def test_the_tool_is_installed_root_only_from_the_merged_site_list(self):
        t = {x["name"]: x for x in yaml.safe_load((ROOT / "tasks/wp_status_common.yml").read_text())}["Deploy the sites status tool"]
        self.assertEqual((t["ansible.builtin.template"]["owner"], t["ansible.builtin.template"]["mode"]), ("root", "0750"))
        import jinja2
        # a fixture, not the repo's list: the repo gains sites, and this test is about how the list is built
        merged = [{**self.v["wp_site_defaults"], **s} for s in [{"name": "wptest", "port": 9101, "quota_id": 1001, "state": "live"}]]
        got = jinja2.Environment().from_string(t["vars"]["wp_status_sites"]).render(wp_sites_merged=merged, wp_backup_quota_offset=self.v["wp_backup_quota_offset"]).strip()
        self.assertEqual(got, "wptest:live:1001:3072:2001:3072")
        closed = merged + [{**self.v["wp_site_defaults"], "name": "gone", "port": 9103, "quota_id": 1003, "state": "closed"}]
        self.assertEqual(jinja2.Environment().from_string(t["vars"]["wp_status_sites"]).render(wp_sites_merged=closed, wp_backup_quota_offset=self.v["wp_backup_quota_offset"]).strip(), got, "the host is not asked about a tombstone")
        sites = [x.get("name", "") for x in yaml.safe_load((ROOT / "tasks/wp_sites_common.yml").read_text())]
        self.assertIn("Install the status tool (tasks/wp_status_common.yml)", sites)


def row(**kw):
    base = dict(site="wptest", deployed="live", actual="live", unit="active", containers="2/2", core="7.1.3", plugins="3", backup_age_h="5", backup_stamp="20261024T201500Z",
                quota_pct="12", backup_quota_pct="3", cron_timer="active", backup_timer="active", frozen="no")
    base.update(kw)
    return base


def tsv(*rows):
    return "\n".join(["\t".join(W.COLUMNS)] + ["\t".join(r[c] for c in W.COLUMNS) for r in rows]) + "\n"


class Wrapper(unittest.TestCase):
    def check(self, r, declared=None, stale=30, warn=80):
        return [m for _, m in W.attention([r], declared or {"wptest": r["deployed"]}, stale, warn)]

    def stopped(self, **kw):
        base = dict(deployed="stopped", actual="stopped", unit="inactive", containers="0/2", cron_timer="inactive", backup_timer="inactive", backup_stamp="20261024T201500Z", backup_age_h="400")
        base.update(kw)
        return row(**base)

    def test_a_stopped_site_is_healthy_when_it_is_really_stopped_and_is_not_judged_on_its_backups(self):
        self.assertEqual(self.check(self.stopped()), [])
        self.assertEqual(self.check(self.stopped(backup_stamp="never", backup_age_h="-")), [], "no backup is expected from a site that is stopped")
        self.assertEqual(self.check(self.stopped(frozen="yes")), [])

    def test_a_stopped_site_that_is_still_running_something_is_flagged(self):
        for kw, text in ((dict(unit="active"), "stopped but its unit"), (dict(containers="2/2"), "stopped but 2/2 containers are up"), (dict(backup_timer="active"), "stopped but its nightly backup timer"),
                         (dict(cron_timer="active"), "stopped but its cron timer"), (dict(actual="live"), "vhost on the host is live")):
            r = self.stopped(); r.update(kw)
            self.assertTrue([m for m in self.check(r) if text in m], (kw, self.check(r)))

    def test_a_running_site_that_the_repo_says_is_stopped_is_flagged_as_not_deployed(self):
        self.assertTrue([m for m in self.check(row(), declared={"wptest": "stopped"}) if "repo says stopped but the host was last deployed as live" in m])

    def test_the_table_says_stopped_for_a_stopped_site_and_shouts_for_one_that_is_not_really(self):
        out = W.render([self.stopped(site="a"), self.stopped(site="b", containers="2/2", unit="active")], {"a": "stopped", "b": "stopped"})
        lines = {l.split()[0]: l for l in out.splitlines()[1:]}
        self.assertIn("stopped", lines["a"].split()[2]); self.assertIn("NOT STOPPED", lines["b"])

    def test_a_closed_site_is_a_tombstone_the_host_is_not_expected_to_report(self):
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="wpstatus-")); (d / "host_vars/dellwp").mkdir(parents=True)
        text = fixture_vars_text()
        text = text.rstrip("\n") + "\n"
        a = text.index("wp_sites:\n") + len("wp_sites:\n")
        text = text[:a] + "  - name: gone\n    port: 9190\n    quota_id: 1090\n    state: closed\n" + text[a:]
        (d / "host_vars/dellwp/vars.yml").write_text(text)
        declared, *_ = W.load_declaration(d)
        self.assertNotIn("gone", declared, "a tombstone is not declared as something the host should run")
        self.assertEqual(W.closed_sites(d), ["gone"])
        self.assertEqual([s for s, _ in W.attention([], declared, 30, 80) if s == "gone"], [], "and nobody is told it is missing")
        out = io.StringIO()
        old = sys.stdout; sys.stdout = out
        try:
            W.main(["--file", "-"], repo=d, stdin=io.StringIO(tsv(*[row(site=s, deployed=declared[s], actual=declared[s], cron_timer="active" if declared[s] == "live" else "inactive") for s in declared])))
        finally:
            sys.stdout = old
        self.assertIn("Closed (tombstones, nothing on the VM): gone", out.getvalue())
        self.assertNotIn("gone:", out.getvalue().split("Closed (tombstones")[1].split("\n", 1)[1] if "Needs attention" in out.getvalue() else "")

    def test_a_healthy_site_needs_no_attention(self):
        self.assertEqual(self.check(row()), [])
        self.assertEqual(self.check(row(deployed="hold", actual="hold", cron_timer="inactive")), [])

    def test_the_repo_and_the_host_disagreeing_is_the_first_thing_flagged(self):
        m = self.check(row(), declared={"wptest": "hold"})
        self.assertEqual(len(m), 1); self.assertIn("the repo says hold but the host was last deployed as live: run dellwp/wp_sites_dellwp.yml", m[0])
        m = self.check(row(actual="hold"))
        self.assertEqual(len(m), 1); self.assertIn("the vhost on the host is hold but the play deployed live", m[0])
        self.assertIn("does not declare it", W.attention([row(site="stray")], {}, 30, 80)[0][1])
        self.assertIn("not deployed yet", W.attention([], {"ghost": "live"}, 30, 80)[0][1])

    def test_a_stack_that_is_not_fully_up_is_flagged_once(self):
        self.assertIn("unit wp-wptest is failed", self.check(row(unit="failed", containers="0/2"))[0])
        self.assertEqual(len(self.check(row(unit="failed", containers="0/2"))), 1, "one line, not two, for one cause")
        self.assertIn("only 1/2 containers", self.check(row(containers="1/2"))[0])

    def test_backups_are_judged_by_age_and_not_while_frozen(self):
        self.assertEqual(self.check(row(backup_age_h="29")), [])
        self.assertIn("31 hours old (limit 30)", self.check(row(backup_age_h="31"))[0])
        self.assertIn("30 hours old", self.check(row(backup_age_h="30"))[0])
        self.assertIn("no backup has run yet", self.check(row(backup_age_h="-", backup_stamp="never"))[0])
        self.assertEqual(self.check(row(backup_age_h="99", frozen="yes")), [], "a freeze pauses backups on purpose")
        self.assertEqual(self.check(row(backup_age_h="-", backup_stamp="never", frozen="yes")), [])
        self.assertIn("backup timer is inactive", self.check(row(backup_timer="inactive"))[0])

    def test_cron_must_match_the_state(self):
        self.assertIn("live but its cron timer is inactive", self.check(row(cron_timer="inactive"))[0])
        self.assertIn("on hold but its cron timer is running", self.check(row(deployed="hold", actual="hold"))[0])

    def test_quotas_warn_at_the_limit_and_an_unreadable_one_is_said(self):
        self.assertEqual(self.check(row(quota_pct="79")), [])
        self.assertIn("site quota is 80% used (warning at 80%)", self.check(row(quota_pct="80"))[0])
        self.assertIn("backup quota is 95% used", self.check(row(backup_quota_pct="95"))[0])
        self.assertIn("site quota could not be read", self.check(row(quota_pct="?"))[0])

    def test_the_table_shows_the_state_with_a_star_when_the_repo_disagrees_and_marks_a_freeze(self):
        text = W.render([row(), row(site="b", actual="hold", frozen="yes", backup_stamp="never", backup_age_h="-", unit="inactive", containers="0/2", quota_pct="?")], {"wptest": "live", "b": "live"})
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ["SITE", "STATE", "STACK", "WP", "PLUGINS", "BACKUP", "QUOTA", "BACKUPS", "CRON"])
        self.assertIn("live", lines[2]); self.assertNotIn("*", lines[2])
        self.assertIn("hold* (frozen)", lines[1]); self.assertIn("DOWN 0/2", lines[1]); self.assertIn("never", lines[1]); self.assertIn("?", lines[1])
        self.assertEqual({len(l) - len(l.lstrip()) for l in lines}, {0}, "no ragged indentation")

    def test_main_prints_the_table_then_the_attention_list_and_returns_how_many(self):
        declared, stale, warn, bin_ = W.load_declaration(FIXTURE_REPO)
        site = list(declared)[0]
        # every declared site is reported (the repo may declare more than one); only the first is broken in the second half
        ok_row = lambda x: row(site=x, deployed=declared[x], actual=declared[x], cron_timer="active" if declared[x] == "live" else "inactive")
        good = tsv(*[ok_row(x) for x in declared])
        out = io.StringIO()
        old = sys.stdout; sys.stdout = out
        try:
            self.assertEqual(W.main(["--file", "-"], repo=FIXTURE_REPO, stdin=io.StringIO(good)), 0)
            self.assertIn("Nothing needs attention.", out.getvalue())
            out.truncate(0); out.seek(0)
            bad = tsv(row(site=site, deployed=declared[site], actual="none", unit="failed", containers="0/2", cron_timer="inactive", backup_timer="inactive"), *[ok_row(x) for x in list(declared)[1:]])
            n = W.main(["--file", "-"], repo=FIXTURE_REPO, stdin=io.StringIO(bad))
        finally:
            sys.stdout = old
        self.assertGreaterEqual(n, 3)
        self.assertIn("Needs attention:", out.getvalue())
        self.assertEqual(len([l for l in out.getvalue().splitlines() if l.startswith("  %s: " % site)]), n)

    def test_it_asks_dellwp_over_ssh_with_the_tools_directory_from_the_repo_and_reports_failures(self):
        declared, stale, warn, bin_ = W.load_declaration(FIXTURE_REPO)
        calls = []

        class R:
            def __init__(self, rc, out="", err=""): self.returncode, self.stdout, self.stderr = rc, out, err

        def ok(cmd, **kw):
            calls.append(cmd); return R(0, tsv(row(site=list(declared)[0], deployed=declared[list(declared)[0]], actual=declared[list(declared)[0]])))
        old = sys.stdout; sys.stdout = io.StringIO()
        try:
            W.main([], repo=FIXTURE_REPO, runner=ok)
            self.assertEqual(calls[0], ["ssh", "dellwp", "sudo -n %s/wp-sites-status" % bin_])
            err = io.StringIO(); olde = sys.stderr; sys.stderr = err
            self.assertEqual(W.main([], repo=FIXTURE_REPO, runner=lambda c, **k: R(255, err="Connection refused")), 99)
            self.assertEqual(W.main([], repo=FIXTURE_REPO, runner=lambda c, **k: R(0, "not a report\n")), 99)
            sys.stderr = olde
        finally:
            sys.stdout = old
        self.assertIn("Connection refused", err.getvalue()); self.assertIn("expected header", err.getvalue())

    def test_the_tools_directory_is_whatever_it_is_told_not_a_constant(self):
        seen = []

        class R:
            returncode, stdout, stderr = 0, "", ""
        W.fetch("/opt/somewhere/else", runner=lambda c, **k: seen.append(c) or R())
        self.assertEqual(seen[0][2], "sudo -n /opt/somewhere/else/wp-sites-status")

    def test_the_thresholds_come_from_the_repo_not_from_this_file(self):
        v = yaml.safe_load((ROOT / "host_vars/dellwp/vars.yml").read_text())
        declared, stale, warn, bin_ = W.load_declaration(ROOT)
        self.assertEqual((stale, warn, bin_), (v["wp_backup_stale_hours"], v["wp_disk_warn_pct"], v["wp_tools_bin"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
