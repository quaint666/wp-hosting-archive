#!/usr/bin/env python3
"""Mutation checks for the control dispatcher: break its code, tasks and templates on purpose in a scratch COPY of the repo and require tests/test_wp_ctl.py to fail each time.

A mutant that is not caught is a gap in the tests (or, rarely, a change that makes no difference: say which in the list below). The real tree is never touched: the directories the tests need
are copied to a temporary directory once, one line of one file is changed at a time, the suite is run there, and the line is put back. Each entry of M is (label, file, text to find, text to put
there); an entry whose text is no longer in the file is reported as NOT APPLICABLE, so a refactor that moves a line shows up here instead of silently dropping a check.

    python3 tests/mutate_wp_ctl.py            # all of them, about 10 seconds each (about 15 minutes)
    python3 tests/mutate_wp_ctl.py 12 61 82   # only these entries, counted from 0 in the order of M

Last full run 2026-10-28: 76 entries, all caught but two: 33 (the control-character check on text arguments, which every argument pattern already makes redundant: kept as defence in depth) and 61
(the lock on the job table; the test now uses a slow clock to widen the race window and catches it). Entries added since were run on their own and caught. Not part of CI (it takes minutes).
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KEEP = ["tests", "scripts", "templates", "tasks", "vars", "host_vars", "group_vars", "dellwp"]
W = "scripts/wp_ctl.py"
M = [
 ("a site need not exist", W, 'if not NAME_RE.fullmatch(value) or value not in known_sites(ctl.cfg):', 'if not NAME_RE.fullmatch(value):'),
 ("names may start with a capital", W, 'NAME_RE = re.compile(r"^[a-z](?:', 'NAME_RE = re.compile(r"^[A-Za-z](?:'),
 ("unknown arguments accepted", W, 'if unknown:\n        raise Refuse("unknown_argument"', 'if False:\n        raise Refuse("unknown_argument"'),
 ("typed confirmation not compared", W, 'if arg.same and key in out and out[key] != out.get(arg.same):', 'if False:'),
 ("replace needs no typed name", W, 'wanted = arg.required and (not arg.needs or out.get(arg.needs)) and', 'wanted = arg.required and not arg.needs and'),
 ("audit failure does not stop the action", W, 'if not ok and not quiet:', 'if False:'),
 ("no intent line before the action", W, '        audit(ctl, "intent", rec)\n', '        pass\n'),
 ("refusals not audited", W, '        audit(ctl, "refused", dict(rec, error=e.code), quiet=True)\n', '        pass\n'),
 ("result line not audited", W, 'if not audit(ctl, phase, dict(', 'if not True or audit(ctl, "x", dict('),
 ("audit file truncated, not appended", W, 'os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)\n        try:\n            os.write(fd, (line', 'os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o600)\n        try:\n            os.write(fd, (line'),
 ("password not scrubbed from output", W, '    for s in scrub:\n        text = text.replace(s, "REDACTED")', '    for s in ():\n        text = text.replace(s, "REDACTED")'),
 ("short passwords accepted", W, 'len(pw) < MIN_PASSWORD', 'len(pw) < 1'),
 ("password with control characters accepted", W, 'if not complete or CONTROL_RE.search(pw) or', 'if not complete or'),
 ("terminal file readable by others", W, 'path = d / ("tty-" + secrets.token_hex(8))\n    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)', 'path = d / ("tty-" + secrets.token_hex(8))\n    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)'),
 ("terminal file left behind", W, '        try:\n            os.unlink(path)\n        except OSError:\n            pass\n\n\ndef run_tool', '        pass\n\n\ndef run_tool'),
 ("the request line is read with read-ahead", W, '        b = os.read(fd, 1)\n        if not b or b == b"\\n":\n            return bytes(buf), bool(b)', '        chunk = os.read(fd, 4096)\n        return chunk.split(b"\\n")[0], True'),
 ("duplicate JSON keys accepted", W, 'object_pairs_hook=no_duplicates', 'object_pairs_hook=dict'),
 ("tools inherit the caller environment", W, '        e = {"PATH": self.cfg["path"], "LANG": "C.UTF-8"}', '        e = dict(self.env); e["PATH"] = self.cfg["path"]'),
 ("site lock never taken", W, '        fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)\n        return fd', '        return fd'),
 ("a lock outlives its job", W, '        if lock is not None:\n            os.close(lock)\n        try:\n            tty.unlink()', '        try:\n            tty.unlink()'),
 ("a job may run twice", W, '    if (jd / "exit").exists():\n        return 0', '    if False:\n        return 0'),
 ("a job that died is shown as running", W, 'return {"state": "lost"}                                  # no exit', 'return {"state": "running"}                                  # no exit'),
 ("old jobs never pruned", W, 'e.stat(follow_symlinks=False).st_mtime < cutoff', 'e.stat(follow_symlinks=False).st_mtime < 0'),
 ("every job directory pruned", W, 'e.stat(follow_symlinks=False).st_mtime < cutoff', 'True'),
 ("a failing tool exits 0", W, 'return 0 if reply.get("ok") else (1 if reply.get("ran") else 2)', 'return 0'),
 ("a refusal exits 1", W, 'return 0 if reply.get("ok") else (1 if reply.get("ran") else 2)', 'return 0 if reply.get("ok") else 1'),
 ("reserved names allowed", W, '    if name in cfg["reserved_names"]:', '    if False:'),
 ("a linked directory counts as a site", W, 'if e.is_dir(follow_symlinks=False) and NAME_RE.fullmatch(e.name) and os.path.isfile', 'if e.is_dir() and NAME_RE.fullmatch(e.name) and os.path.isfile'),
 ("a record is trusted without a valid directory name", W, 'if e.is_dir(follow_symlinks=False) and NAME_RE.fullmatch(e.name) and os.path.isfile', 'if e.is_dir(follow_symlinks=False) and os.path.isfile'),
 ("JSON flags may be text", W, '            if not isinstance(value, bool):\n                raise Refuse("bad_type", "%s must be true or false" % key)', '            if False:\n                raise Refuse("bad_type", "%s must be true or false" % key)'),
 ("booleans pass as numbers", W, 'if isinstance(value, bool) or not isinstance(value, int):', 'if not isinstance(value, int):'),
 ("number range not enforced", W, 'if not self.lo <= value <= self.hi:', 'if False:'),
 ("control characters in text accepted", W, 'if len(value) > MAX_TEXT or CONTROL_RE.search(value):', 'if len(value) > MAX_TEXT:'),
 ("request id not checked", W, 'if req is not None and not (isinstance(req, str) and REQ_RE.fullmatch(req)):', 'if False:'),
 ("oversized request accepted", W, 'if len(buf) > limit:', 'if False:'),
 ("a busy site does not stop a job", W, '        if held is None:\n            raise Refuse("busy", "another verb is running on %s" % site)', '        if False:\n            raise Refuse("busy", "another verb is running on %s" % site)'),
 ("the import stream is not passed on", W, '"receive", call.args["file"]], stdin=call.fd)', '"receive", call.args["file"]])'),
 ("the typed answer of a delete is not written", W, 'call.args["site"]], tty=[call.args["confirm_again"]])', 'call.args["site"]])'),
 ("the typed answer is left after the job", W, '        try:\n            tty.unlink()\n        except OSError:\n            pass\n    tmp = jd', '        pass\n    tmp = jd'),
 ("the sudo caller is always the panel", W, 'return "panel" if user and user == ctl.cfg["panel_user"] else', 'return "panel" if True else'),
 ("a hostile site name copied into the audit", W, 'if isinstance(raw.get("site"), str) and NAME_RE.fullmatch(raw["site"]):', 'if isinstance(raw.get("site"), str):'),
 ("the tool environment keeps sudo variables", W, 'e.update({k: self.env[k] for k in self.cfg.get("env_pass", ()) if k in self.env})', 'e.update({k: v for k, v in self.env.items() if k.startswith("SUDO_")})'),
 ("sudoers allows any arguments", "tasks/wp_ctl_common.yml", 'wp-ctl ""', 'wp-ctl'),
 ("key lines lose restrict", "tasks/wp_ctl_common.yml", 'restrict,command=', 'command='),
 ("key lines lose the forced command", "tasks/wp_ctl_common.yml", 'restrict,command="sudo -n {{ wp_tools_bin }}/wp-ctl" {{ key }}', 'restrict {{ key }}'),
 ("any key text allowed", "tasks/wp_ctl_common.yml", "( [A-Za-z0-9@._-]+)?$", "( .+)?$"),
 ("the account gets a password login", "tasks/wp_ctl_common.yml", 'password: "*"', 'password: "x"'),
 ("the account owns its keys", "tasks/wp_ctl_common.yml", 'dest: "/var/lib/{{ wp_ctl_user }}/.ssh/authorized_keys"\n    owner: root', 'dest: "/var/lib/{{ wp_ctl_user }}/.ssh/authorized_keys"\n    owner: "{{ wp_ctl_user }}"'),
 ("the audit file is rewritten each run", "tasks/wp_ctl_common.yml", '    force: false', '    force: true'),
 ("a filesystem without the attribute fails the play", "tasks/wp_ctl_common.yml", '  rescue:\n    - name: Say so', '  always:\n    - name: Say so'),
 ("wp-ctl not staged", "tasks/wp_tools_common.yml", '    - { src: wp_ctl.py.j2, dest: wp-ctl }\n', ''),
 ("module not staged", "tasks/wp_tools_common.yml", '    - wp_ctl.py\n', ''),
 ("access tasks lose their tag", "dellwp/wp_sites_dellwp.yml", 'tags: [wp_ctl]', 'tags: [wp_tools]'),
 ("require_site follows a linked record", "templates/wp/wp_tools_common.sh.j2", '[ ! -L "$WP_DATA/$site/site.json" ]', 'true'),
 ("require_site needs no record file", "templates/wp/wp_tools_common.sh.j2", '[ -f "$WP_DATA/$site/site.json" ]', '[ -d "$WP_DATA/$site" ]'),
 ("wrapper: reserved names dropped", "templates/wp/wp_ctl.py.j2", '{% for n in wp_reserved_names %}', '{% for n in [] %}'),
 ("a second reserved list appears", "host_vars/dellwp/vars.yml", "wp_restore_db_mem_mb: 384", "wp_reserved_names: [x]\nwp_restore_db_mem_mb: 384"),
 ("a repeat starts a second job", W, 'if old:\n                if (spec', 'if False:\n                if (spec'),
 ("a reused id for another job accepted", W, '!= (verb_name, site, rec.get("args")):', '!= (verb_name, site, rec.get("args")) and False:'),
 ("the job table is not locked", W, 'table = take_lock(ctl, "_jobs", wait=True)', 'table = take_lock(ctl, "_jobs") or os.open(os.devnull, os.O_RDONLY)'),
 ("no memory limit on a job unit", W, '"--property=MemoryMax=%dM" % ctl.limit(verb_name, "memory_mb"), ', ''),
 ("swap allowed for a job unit", W, '"--property=MemorySwapMax=0", ', ''),
 ("no process limit on a job unit", W, '"--property=TasksMax=%d" % ctl.limit(verb_name, "tasks"),\n', '\n'),
 ("no runtime backstop on a job unit", W, '"--property=RuntimeMaxSec=%d" % (timeout + UNIT_GRACE_S), ', ''),
 ("a verb's own limit ignored", W, 'return self.cfg["limits"].get(verb, {}).get(key, self.cfg["limits"]["default"][key])', 'return self.cfg["limits"]["default"][key]'),
 ("limits never checked", W, '        check_limits(cfg, VERBS)\n        prune_jobs(ctl)', '        prune_jobs(ctl)'),
 ("limits may name a verb that does not exist", W, 'if name != "default" and name not in verbs:', 'if False:'),
 ("a job may lack its own timeout", W, 'if verb.kind in ("job", "stream") and "timeout_s" not in', 'if False and "timeout_s" not in'),
 ("a limit of zero accepted", W, 'or not isinstance(value, int) or value < 1:', 'or not isinstance(value, int) or value < 0:'),
 ("a boolean accepted as a limit", W, 'if isinstance(value, bool) or not isinstance(value, int) or value < 1:', 'if not isinstance(value, int) or value < 1:'),
 ("the runner is blocked by a bad table", W, '        if argv[:1] == ["--run-job"]:\n            return run_job', '        check_limits(cfg, VERBS)\n        if argv[:1] == ["--run-job"]:\n            return run_job'),
 ("no duration on the quick result", W, 'job=reply.get("job"), duration_ms=millis(t0)), quiet=True)', 'job=reply.get("job")), quiet=True)'),
 ("no duration on the job result", W, 'rc=rc, duration_ms=millis(t0)), quiet=True)', 'rc=rc), quiet=True)'),
 ("a duplicate is audited as a start", W, 'phase = "duplicate" if reply.get("duplicate") else "started"', 'phase = "started"'),
 ("the key file may render empty", "tasks/wp_ctl_common.yml", "      # Managed by Ansible (tasks/wp_ctl_common.yml). Keys allowed to log in as {{ wp_ctl_user }}, each forced to wp-ctl; none while wp_ctl_panel_keys is empty.\n", ""),
 ("the audit file starts empty", "tasks/wp_ctl_common.yml", '    content: |\n      {"phase":"created","by":"tasks/wp_ctl_common.yml"}\n', '    content: ""\n'),
 ("the audit file starts with a line nobody can read", "tasks/wp_ctl_common.yml", '{"phase":"created","by":"tasks/wp_ctl_common.yml"}', 'created by tasks/wp_ctl_common.yml'),
 ("errors in the attribute step are ignored the old way", "tasks/wp_ctl_common.yml", '  block:\n    - name: Set the append-only attribute\n      ansible.builtin.file:\n        path: "{{ wp_ctl_audit_file }}"\n        attributes: a\n  rescue:', '  block:\n    - name: Set the append-only attribute\n      ansible.builtin.file:\n        path: "{{ wp_ctl_audit_file }}"\n        attributes: a\n      failed_when: false\n  rescue:'),
 ("the key file loses its owner", "tasks/wp_ctl_common.yml", '    dest: "/var/lib/{{ wp_ctl_user }}/.ssh/authorized_keys"\n    owner: root\n    group: root\n    mode: "0644"', '    dest: "/var/lib/{{ wp_ctl_user }}/.ssh/authorized_keys"\n    mode: "0644"'),
 ("a delete needs only one typing", W, '"confirm_again": Arg("text", rx=NAME_RE, same="site")}', '"confirm_again": Arg("text", required=False, rx=NAME_RE, same="site")}'),
 ("the second typing is not compared", W, '"confirm_again": Arg("text", rx=NAME_RE, same="site")}', '"confirm_again": Arg("text", rx=NAME_RE)}'),
 ("create skips the name check", W, 'check_new_name(call.ctl.cfg, a["name"])\n    return call.job', 'return call.job'),
 ("install password not scrubbed", W, 'tty=[pw, pw], scrub=(pw,))', 'tty=[pw, pw])'),
 ("install asks the tool for the password once", W, '"--email=" + a["email"]], tty=[pw, pw]', '"--email=" + a["email"]], tty=[pw]'),
 ("install holds no lock", W, 'h_install, lock=True)', 'h_install, lock=False)'),
 ("a job is about the wrong site", W, '    site = target(args)\n    table', '    site = args.get("site") or "x"\n    table'),
 ("the audit shows no new name", W, 'rec.update(site=target(args)', 'rec.update(site=args.get("site")'),
 ("create passes the label as a bare setting", W, '["--set", "label=" + a["label"]]', '["--set", a["label"]]'),
 ("any label accepted", W, r'LABEL_RE = re.compile(r"^[^\s].{0,98}[^\s]$|^[^\s]$")', 'LABEL_RE = re.compile(r".*")'),
 ("any email accepted", W, r'EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")', 'EMAIL_RE = re.compile(r".*")'),
 ("any administrator name accepted", W, r'ADMIN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")', 'ADMIN_NAME_RE = re.compile(r".*")'),
 ("a create repeat is refused once the site exists", W, 'if not (call.rec.get("req") and find_job(call.ctl, call.rec["req"])[0]):', 'if True:'),
 ("a create with a known request id skips every check", W, 'if not (call.rec.get("req") and find_job(call.ctl, call.rec["req"])[0]):', 'if not call.rec.get("req"):'),
 ("install passes the title without its option name", W, '"--title=" + a["title"]', 'a["title"]'),

 # step 3: set and ensure-state (dispatcher suite unless a suite is named)
 ("going live needs no confirmation", W, 'and (not arg.when or out.get(arg.when[0]) == arg.when[1])', 'and (not arg.when)'),
 ("every state needs the confirmation", W, 'and (not arg.when or out.get(arg.when[0]) == arg.when[1])', 'and True'),
 ("set takes any key", W, '"key": Arg("enum", values=SET_KEYS)', '"key": Arg("text", rx=ANY_RE)'),
 ("set may change the state and the port", W, '"label", "note")   # = wp_site_model.EDITABLE', '"label", "note", "state", "port")   # = wp_site_model.EDITABLE'),
 ("a note is cut at the general limit", W, 'len(value) > (self.limit or MAX_TEXT)', 'len(value) > MAX_TEXT'),
 ("a site can be closed by state", W, 'STATES = ("hold", "live", "stopped")', 'STATES = ("hold", "live", "stopped", "closed")'),
 ("set passes the value without its key", W, '"%s=%s" % (a["key"], a["value"])', 'a["value"]'),
 ("ensure-state passes the site and state swapped", W, '"state", call.args["site"], call.args["state"]]', '"state", call.args["state"], call.args["site"]]'),
 ("a stopped site may go straight to live", "scripts/wp_site_model.py", '"stopped": ("hold",)', '"stopped": ("hold", "live")', "tests/test_wp_site_model.py"),
 ("no backup is planned before a stop", "scripts/wp_site_model.py", '(["backup"] if target == "stopped" else [])', '([] if target == "stopped" else [])', "tests/test_wp_site_model.py"),
 ("a closed site can be moved", "scripts/wp_site_model.py", '    if current == "closed":\n        raise Refuse("the site is closed', '    if False:\n        raise Refuse("the site is closed', "tests/test_wp_site_model.py"),
 ("a move to the same state is a change", "scripts/wp_site_model.py", '    if current == target:\n        return []', '    if False:\n        return []', "tests/test_wp_site_model.py"),
 ("the final backup is not taken", "scripts/wp_site_provision.py", 'if "backup" in plan:', 'if False:', "tests/test_wp_site_provision.py"),
 ("a failed final backup is ignored", "scripts/wp_site_provision.py", '"--tag=final", timeout=self.cfg["backup_timeout_s"])', '"--tag=final", timeout=self.cfg["backup_timeout_s"], check=False)', "tests/test_wp_site_provision.py"),
 ("the final backup runs with the quick-command limit", "scripts/wp_site_provision.py", '"--tag=final", timeout=self.cfg["backup_timeout_s"])', '"--tag=final")', "tests/test_wp_site_provision.py"),
 ("a change skips the fit check", "scripts/wp_site_provision.py", '            self.check_fit([m for m in merged if m["name"] != name] + [model.merged(self.defaults, new)])\n', '', "tests/test_wp_site_provision.py"),
 ("the fit is judged on the old record", "scripts/wp_site_provision.py", '+ [model.merged(self.defaults, new)])\n            registry_only', '+ [model.merged(self.defaults, rec)])\n            registry_only', "tests/test_wp_site_provision.py"),
 ("the move rules are not consulted", "scripts/wp_site_provision.py", '            if check:\n                check(rec)', '            if False:\n                check(rec)', "tests/test_wp_site_provision.py"),
 ("a label is applied like a limit", "scripts/wp_site_provision.py", 'registry_only = set(changes) <= set(model.TEXT_KEYS)', 'registry_only = False', "tests/test_wp_site_provision.py"),
 ("an unchanged value is written and applied again", "scripts/wp_site_provision.py", '            if new == rec:\n', '            if False:\n', "tests/test_wp_site_provision.py"),
 ("the templates are not rendered before the write", "scripts/wp_site_provision.py", '                self.files(model.merged(self.defaults, new))\n            if before:', '                pass\n            if before:', "tests/test_wp_site_provision.py"),
 ("the record is written before the final backup", "scripts/wp_site_provision.py", '            if before:\n                before(rec)\n            self.registry.write_site(new)', '            self.registry.write_site(new)\n            if before:\n                before(rec)', "tests/test_wp_site_provision.py"),
 # step 3b: close, the tombstone check and the delete hook
 ("close keeps the record", "scripts/wp_site_provision.py", '            self.registry.site_file(name).unlink()\n', '', "tests/test_wp_site_provision.py"),
 ("delete never closes the registry", "templates/wp/wp_site_delete.sh.j2", '"$PROVISION" close "$site" </dev/null || die', 'true || die', "tests/test_wp_delete.py"),
 ("a failed close does not stop the delete", "templates/wp/wp_site_delete.sh.j2", '|| die "the registry could not close', '|| echo "the registry could not close', "tests/test_wp_delete.py"),
 ("a linked record counts", "templates/wp/wp_site_delete.sh.j2", ' && [ ! -L "$dir/site.json" ]; then\n  "$PROVISION"', '; then\n  "$PROVISION"', "tests/test_wp_delete.py"),
 ("a site with no record is asked of the registry", "templates/wp/wp_site_delete.sh.j2", 'if [ -f "$dir/site.json" ] && [ ! -L', 'if [ ! -L', "tests/test_wp_delete.py"),
 # step 4 (a and b): the registry is the one source of sites
 ('the backup check treats an unreadable registry as no sites', 'templates/wp/wp_backup_check.sh.j2', 'rows=$(site_rows) || die "cannot read the site registry (wp-site-provision rows failed): no site\'s backups can be judged"', 'rows=$(site_rows) || rows=""', 'tests/test_wp_backup.py'),
 ('the restore drill treats an unreadable registry as no sites', 'templates/wp/wp_restore_drill.sh.j2', 'rows=$(site_rows) || die "cannot read the site registry (wp-site-provision rows failed): a drill that cannot even start is a finding"', 'rows=$(site_rows) || rows=""', 'tests/test_wp_backup.py'),
 ('the update tool takes the wrong column for the policy', 'templates/wp/wp_site_update.sh.j2', "'$1 == s {print $7}'", "'$1 == s {print $6}'", 'tests/test_wp_update.py'),
 ('the update tool ignores an unreadable registry', 'templates/wp/wp_site_update.sh.j2', 'rows=$(site_rows) || die "cannot read the site registry (wp-site-provision rows failed): no update policy for $site"', 'rows=$(site_rows) || rows=""', 'tests/test_wp_update.py'),
 ('the status tool treats an unreadable registry as no sites', 'templates/wp/wp_sites_status.sh.j2', 'rows=$(site_rows) || die "cannot read the site registry (wp-site-provision rows failed)"', 'rows=$(site_rows) || rows=""', 'tests/test_wp_status.py'),
 ('the disk check does not alert on an unreadable registry', 'templates/wp/wp_disk_check.sh.j2', 'flag registry-unreadable 1 ', 'flag registry-unreadable 0 ', 'tests/test_monitoring_checks.py'),
 ('the disk check forgets the backup projects', 'templates/wp/wp_disk_check.sh.j2', 'printf "%s:%s:%s backup-%s:%s:%s ", $1, $3, $4, $1, $5, $6', 'printf "%s:%s:%s ", $1, $3, $4', 'tests/test_monitoring_checks.py'),
 ("a link counts as a site's record", 'templates/wp/wp_tools_common.sh.j2', '{ [ -f "$WP_DATA/$site/site.json" ] && [ ! -L "$WP_DATA/$site/site.json" ]; } || die "\'$site\' is not a site (it has no registry record: create it with wp-ctl create)"', '[ -f "$WP_DATA/$site/site.json" ] || die "\'$site\' is not a site (it has no registry record: create it with wp-ctl create)"', 'tests/test_wp_tools.py'),
 ('a directory counts as a site', 'templates/wp/wp_tools_common.sh.j2', '{ [ -f "$WP_DATA/$site/site.json" ] && [ ! -L "$WP_DATA/$site/site.json" ]; } || die "\'$site\' is not a site (it has no', '[ -d "$WP_DATA/$site" ] || die "\'$site\' is not a site (it has no', 'tests/test_wp_tools.py'),
 ('rows lists the closed sites', 'scripts/wp_site_provision.py', 'sites = [s for s in self.all_merged() if s["state"] != "closed"]', 'sites = list(self.all_merged())', 'tests/test_wp_site_provision.py'),
 ('rows gives the backup project without the offset', 'scripts/wp_site_provision.py', 'int(s["quota_id"]) + offset, int(s["backup_quota_mb"])', 'int(s["quota_id"]), int(s["backup_quota_mb"])', 'tests/test_wp_site_provision.py'),
 ('rows swallows a registry that cannot be read', 'scripts/wp_site_provision.py', 'except model.RegistryError as e:\n            raise Failed(str(e))\n        offset = int(', 'except model.RegistryError as e:\n            return []\n        offset = int(', 'tests/test_wp_site_provision.py'),
 ('a delete needs neither a record nor a tombstone', 'templates/wp/wp_site_delete.sh.j2', '|| die "\'$site\' is not a site (it has no registry record and no tombstone)"', '|| true', 'tests/test_wp_delete.py'),
 ('the status tool no longer flags a vhost that differs from the record', 'scripts/wp_sites_status.py', 'if r["deployed"] != r["actual"]:\n            out.append', 'if False:\n            out.append', 'tests/test_wp_status.py'),
 ('the status tool flags a registry-only site as a stray', 'scripts/wp_sites_status.py', 'out = []\n    for r in rows:\n        s = r["site"]\n', 'out = []\n    for r in rows:\n        s = r["site"]\n        out.append((s, "the host reports it but the repo does not declare it"))\n', 'tests/test_wp_status.py'),
 ('the policy is not set back to what it was', 'scripts/wp_monitor_rehearsal_dellwp.sh', 'job "set update_policy back to $POLICY" "set site=$SITE key=update_policy value=$POLICY"', 'job "set update_policy back to $POLICY" "set site=$SITE key=update_policy value=notify"', 'tests/test_wp_monitor_script.py'),
 ('the policy is never set back', 'scripts/wp_monitor_rehearsal_dellwp.sh', 'job "set update_policy back to $POLICY" "set site=$SITE key=update_policy value=$POLICY" 600 || fatal "setting the policy back failed"', 'true', 'tests/test_wp_monitor_script.py'),
 ('a failed policy job does not stop the run', 'scripts/wp_monitor_rehearsal_dellwp.sh', '|| fatal "set update_policy failed"', '|| true', 'tests/test_wp_monitor_script.py'),
 ('a site already on freeze is not refused', 'scripts/wp_monitor_rehearsal_dellwp.sh', '[ "$POLICY" != freeze ] || fatal', 'true || fatal', 'tests/test_wp_monitor_script.py'),
 ('a bad preflight does not stop the run', 'scripts/wp_monitor_rehearsal_dellwp.sh', '[ "$FAIL" = 0 ] || fatal "the preflight found problems; fix them first"', 'true', 'tests/test_wp_monitor_script.py'),
 ('the cleanup leaves the broken record', 'scripts/wp_monitor_rehearsal_dellwp.sh', 'rm -rf "$SITES/$BROKEN" /var/lib/wp-monitor-rehearsal "$SITES/$S/.monitor', 'rm -rf /var/lib/wp-monitor-rehearsal "$SITES/$S/.monitor', 'tests/test_wp_monitor_script.py'),
 ('no cleanup at exit after the confirmation', 'scripts/wp_monitor_rehearsal_dellwp.sh', 'trap \'[ "$CLEANED" = yes ] || cleanup_vm; rm -rf "$TMPD"\' EXIT', 'true', 'tests/test_wp_monitor_script.py'),
 ('declining the prompt still touches the VM', 'scripts/wp_monitor_rehearsal_dellwp.sh', 'trap \'rm -rf "$TMPD"\' EXIT ', 'trap \'[ "$CLEANED" = yes ] || cleanup_vm; rm -rf "$TMPD"\' EXIT ', 'tests/test_wp_monitor_script.py'),
 ('an unconfirmed cleanup is not a failure', 'scripts/wp_monitor_rehearsal_dellwp.sh', 'bump "$([ "$CLEANED" = yes ] && echo 0 || echo 1)"', 'bump 0', 'tests/test_wp_monitor_script.py'),
 ('the ntfy stand-in is not on the path', 'scripts/wp_monitor_rehearsal_dellwp.sh', 'export PATH="$R/bin:$PATH"', 'export PATH="$PATH"', 'tests/test_wp_monitor_script.py'),
 ('a stray site is not a failure', 'scripts/wp_monitor_rehearsal_dellwp.sh', '[ $? -ne 0 ]; bump $? "the ThinkPad half', '[ 0 -ne 0 ]; bump 0 "the ThinkPad half', 'tests/test_wp_monitor_script.py'),
 ('the feed treats an unreadable registry as no sites', 'scripts/wp_vulnfeed.py', 'if p.returncode != 0:\n        raise FeedError("cannot read the site registry', 'if False:\n        raise FeedError("cannot read the site registry', 'tests/test_wp_vulnfeed.py'),
 ('the feed takes the wrong column for the name', 'scripts/wp_vulnfeed.py', 'return {line.split()[0]: {}', 'return {line.split()[1]: {}', 'tests/test_wp_vulnfeed.py'),
 ('check-all does not ask the registry', 'scripts/wp_vulnfeed.py', 'def cmd_check_all(cfg, out, run, clock, notify):\n    with_sites(cfg, run)\n', 'def cmd_check_all(cfg, out, run, clock, notify):\n', 'tests/test_wp_vulnfeed.py'),
 ('check does not ask the registry', 'scripts/wp_vulnfeed.py', 'def cmd_check(cfg, site, out, run, clock):\n    with_sites(cfg, run)\n', 'def cmd_check(cfg, site, out, run, clock):\n', 'tests/test_wp_vulnfeed.py'),
 ('the import treats an unreadable registry as no sites', 'scripts/wp_site_import.py', '    if p.returncode != 0:\n        raise Refuse("cannot read the site registry (wp-site-provision rows failed)', '    if False:\n        raise Refuse("cannot read the site registry (wp-site-provision rows failed)', 'tests/test_wp_import.py'),
 ('the import accepts a row that is cut short', 'scripts/wp_site_import.py', 'if len(f) != 7 or not f[3].isdigit():', 'if False:', 'tests/test_wp_import.py'),
 ('the import takes the wrong column for the quota', 'scripts/wp_site_import.py', 'sites[f[0]] = {"state": f[1], "quota_mb": int(f[3])}', 'sites[f[0]] = {"state": f[1], "quota_mb": int(f[2])}', 'tests/test_wp_import.py'),
 ('the import takes the wrong column for the state', 'scripts/wp_site_import.py', 'sites[f[0]] = {"state": f[1], "quota_mb"', 'sites[f[0]] = {"state": f[6], "quota_mb"', 'tests/test_wp_import.py'),
 ('the import lets a missing tool escape as an OSError', 'scripts/wp_site_import.py', 'except (OSError, subprocess.SubprocessError) as e:\n        raise Refuse("cannot read the site registry (wp-site-provision rows could not run)', 'except ZeroDivisionError as e:\n        raise Refuse("cannot read the site registry (wp-site-provision rows could not run)', 'tests/test_wp_import.py'),
 ('the import never asks the registry', 'scripts/wp_site_import.py', 'if "sites" not in cfg:           # the sites', 'if False:           # the sites', 'tests/test_wp_import.py'),
 ('the feed wrapper bakes a site table in again', 'templates/wp/wp_vulnfeed.py.j2', '    "host_name": "{{ inventory_hostname }}",\n', '    "host_name": "{{ inventory_hostname }}",\n    "sites": {},\n', 'tests/test_wp_scans.py'),
 ('the import wrapper bakes a site table in again', 'templates/wp/wp_site_import.py.j2', '    "approvals": {', '    "sites": {},\n    "approvals": {', 'tests/test_wp_import.py'),
 ('a re-run reuses the request id', 'scripts/wp_monitor_rehearsal_dellwp.sh', 'req=mon-$SITE-freeze-$STAMP"', 'req=mon-$SITE-freeze"', 'tests/test_wp_monitor_script.py'),
 ('the update tool runs without the freeze', 'scripts/wp_monitor_rehearsal_dellwp.sh', 'if [ "$(rowline | awk \'{print $7}\')" = freeze ]; then', 'if true; then', 'tests/test_wp_monitor_script.py'),
 ('a linked tombstone counts', 'scripts/wp_site_model.py', 'if m and m["name"] == name and path.is_file() and not path.is_symlink():', 'if m and m["name"] == name and path.is_file():', 'tests/test_wp_site_model.py'),
 ('a tombstone with any stamp is accepted', 'scripts/wp_site_model.py', 'TOMB_RE = re.compile(r"(?P<name>[a-z][a-z0-9-]{1,23})(?:\\.(?P<stamp>[0-9]{8}T[0-9]{6}Z))?\\.json")', 'TOMB_RE = re.compile(r"(?P<name>[a-z][a-z0-9-]{1,23})(?:\\.(?P<stamp>[^.]*))?\\.json")', 'tests/test_wp_site_model.py'),
 ('a tombstone is overwritten', 'scripts/wp_site_model.py', '            raise RegistryError("%s exists already" % path)', '            pass', 'tests/test_wp_site_model.py'),
 ('two sites may share all numbers', 'scripts/wp_site_model.py', 'if (port, qid) == key and other["name"] != rec["name"]:', 'if False:', 'tests/test_wp_site_model.py'),
 ('two sites may share a port or a quota id', 'scripts/wp_site_model.py', 'if (port, qid) != key and (port == key[0] or qid == key[1]):', 'if False:', 'tests/test_wp_site_model.py'),
 ("a closed site's name still counts in the uniqueness rule", 'scripts/wp_site_model.py', 'len({s["name"] for s in exist}) == len(exist),', 'len(set(names)) == n,', 'tests/test_wp_site_model.py'),
 ('the uniqueness rule is gone', 'scripts/wp_site_model.py', 'len({s["name"] for s in exist}) == len(exist),', 'True,', 'tests/test_wp_site_model.py'),
 ('a record with its own tombstone is not reported', 'scripts/wp_site_provision.py', '            if key in records:\n                raise model.RegistryError', '            if False:\n                raise model.RegistryError', 'tests/test_wp_site_provision.py'),
 ('a closed name is refused again', 'scripts/wp_site_provision.py', 'self.cfg["fit"]["reserved_names"], {s["name"] for s in everything if s["state"] != "closed"})', 'self.cfg["fit"]["reserved_names"], {s["name"] for s in everything})', 'tests/test_wp_site_provision.py'),
 ('a new site may take the numbers of a closed one', 'scripts/wp_site_provision.py', 'port, quota_id = model.next_numbers(everything)', 'port, quota_id = model.next_numbers([s for s in everything if s["state"] != "closed"])', 'tests/test_wp_site_provision.py'),
 ('an interrupted close writes a second tombstone', 'scripts/wp_site_provision.py', '            if same:\n                tomb = same[-1]', '            if False:\n                tomb = same[-1]', 'tests/test_wp_site_provision.py'),
 ('closing a name with an older tombstone and no record is refused', 'scripts/wp_site_provision.py', '                if self.registry.tombstone_files(name):\n                    self.out', '                if False:\n                    self.out', 'tests/test_wp_site_provision.py'),
 ('the delete accepts any file as a dated tombstone', 'templates/wp/wp_site_delete.sh.j2', '$b =~ ^$site\\.[0-9]{8}T[0-9]{6}Z\\.json$', 'true', 'tests/test_wp_delete.py'),
 ('the delete ignores the unstamped tombstone', 'templates/wp/wp_site_delete.sh.j2', '[[ $b == "$site.json" || ', '[[ ', 'tests/test_wp_delete.py'),
 ('the delete follows a tombstone link', 'templates/wp/wp_site_delete.sh.j2', ' && [ ! -L "$f" ]; then return 0; fi', '; then return 0; fi', 'tests/test_wp_delete.py'),
 ('close writes no tombstone', 'scripts/wp_site_provision.py', '                tomb = self.registry.write_tombstone(rec, datetime', '                tomb = None; (rec, datetime', 'tests/test_wp_site_provision.py'),
 ('a directory without a record is a site', 'scripts/wp_ctl.py', 'and os.path.isfile(os.path.join(e.path, "site.json")):', 'and True:', 'tests/test_wp_ctl.py'),
 ('a linked directory is followed', 'scripts/wp_ctl.py', 'if e.is_dir(follow_symlinks=False) and NAME_RE.fullmatch(e.name)', 'if e.is_dir() and NAME_RE.fullmatch(e.name)', 'tests/test_wp_ctl.py'),
 ("the dispatcher's wrapper bakes a site list in again", 'templates/wp/wp_ctl.py.j2', '    "reserved_names": [', '    "sites": ["wptest"],\n    "reserved_names": [', 'tests/test_wp_ctl.py'),
 ("the fit rule counts a closed site's name as taken", 'scripts/wp_site_model.py', 'len({s["name"] for s in exist}) == len(exist),', 'len(set(names)) == n,', 'tests/test_wp_site_model.py'),
 ('a floor entry that says live is a live site', 'scripts/wp_site_provision.py', '{**self.defaults, **k, "state": "closed"} for k in cfg["closed_floor"]', '{**self.defaults, **k} for k in cfg["closed_floor"]', 'tests/test_wp_site_provision.py'),
 ('the wrapper hands the tool no floor', 'templates/wp/wp_site_provision.py.j2', '  "closed_floor": wp_closed_floor,', '  "closed_floor": [],', 'tests/test_wp_site_provision_wrapper.py'),
 ('the floor is not used for the numbers of a new site', 'scripts/wp_site_provision.py', 'everything = model.everything(self.registry, self.floor)', 'everything = model.everything(self.registry, [])', 'tests/test_wp_site_provision.py'),
]
only = sys.argv[1:]
surv, ran = [], 0
with tempfile.TemporaryDirectory(prefix="wpctl-mut-") as td:
    root = Path(td) / "repo"
    root.mkdir()
    for d in KEEP:
        shutil.copytree(REPO / d, root / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for i, (label, rel, old, new, *suite) in enumerate(M):
        if only and str(i) not in only:
            continue
        suite = suite[0] if suite else "tests/test_wp_ctl.py"
        path = root / rel
        orig = path.read_text()
        if orig.count(old) < 1:
            print("NOT APPLICABLE (pattern missing):", i, label, flush=True); surv.append((i, label, "pattern missing")); continue
        path.write_text(orig.replace(old, new, 1))
        r = subprocess.run([sys.executable, "-I", str(root / suite)], capture_output=True, text=True, timeout=300, cwd=str(root))
        path.write_text(orig)
        ran += 1
        killed = r.returncode != 0
        print(("killed  " if killed else "SURVIVED"), i, label, flush=True)
        if not killed:
            surv.append((i, label, "survived"))
print("\n%d mutants run, %d not killed" % (ran, len(surv)))
for s in surv:
    print("  ", s)
