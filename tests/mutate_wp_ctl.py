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
 ("replace needs no typed name", W, 'wanted = arg.required and (not arg.needs or out.get(arg.needs))', 'wanted = arg.required and not arg.needs'),
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
 ("closed names reusable", W, '    if name in cfg["closed_sites"]:', '    if False:'),
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
 ("wrapper: closed sites become sites", "templates/wp/wp_ctl.py.j2", "== 'closed' %}\n        \"{{ s.name }}\",\n{% endfor %}\n    ],\n    \"sites\"", "!= 'closed' %}\n        \"{{ s.name }}\",\n{% endfor %}\n    ],\n    \"sites\""),
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
 ("apply works on a site with no record", W, 'def h_apply(call):\n    registered(call)', 'def h_apply(call):\n    pass'),
 ("install password not scrubbed", W, 'tty=[pw, pw], scrub=(pw,))', 'tty=[pw, pw])'),
 ("install asks the tool for the password once", W, '"--email=" + a["email"]], tty=[pw, pw]', '"--email=" + a["email"]], tty=[pw]'),
 ("install holds no lock", W, 'h_install, lock=True)', 'h_install, lock=False)'),
 ("a job is about the wrong site", W, '    site = target(args)\n    table', '    site = args.get("site") or "x"\n    table'),
 ("the audit shows no new name", W, 'rec.update(site=target(args)', 'rec.update(site=args.get("site")'),
 ("create passes the label as a bare setting", W, '["--set", "label=" + a["label"]]', '["--set", a["label"]]'),
 ("any label accepted", W, r'LABEL_RE = re.compile(r"^[^\s].{0,98}[^\s]$|^[^\s]$")', 'LABEL_RE = re.compile(r".*")'),
 ("any email accepted", W, r'EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")', 'EMAIL_RE = re.compile(r".*")'),
 ("any administrator name accepted", W, r'ADMIN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")', 'ADMIN_NAME_RE = re.compile(r".*")'),
 ("install passes the title without its option name", W, '"--title=" + a["title"]', 'a["title"]'),
]
only = sys.argv[1:]
surv, ran = [], 0
with tempfile.TemporaryDirectory(prefix="wpctl-mut-") as td:
    root = Path(td) / "repo"
    root.mkdir()
    for d in KEEP:
        shutil.copytree(REPO / d, root / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for i, (label, rel, old, new) in enumerate(M):
        if only and str(i) not in only:
            continue
        path = root / rel
        orig = path.read_text()
        if orig.count(old) < 1:
            print("NOT APPLICABLE (pattern missing):", i, label, flush=True); surv.append((i, label, "pattern missing")); continue
        path.write_text(orig.replace(old, new, 1))
        r = subprocess.run([sys.executable, "-I", str(root / "tests/test_wp_ctl.py")], capture_output=True, text=True, timeout=300, cwd=str(root))
        path.write_text(orig)
        ran += 1
        killed = r.returncode != 0
        print(("killed  " if killed else "SURVIVED"), i, label, flush=True)
        if not killed:
            surv.append((i, label, "survived"))
print("\n%d mutants run, %d not killed" % (ran, len(surv)))
for s in surv:
    print("  ", s)
