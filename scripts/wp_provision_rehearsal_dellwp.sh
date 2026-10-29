#!/bin/bash
# Rehearses the PROVISIONING TOOL (wp-site-provision, and the wp-ctl verbs create, apply and install over it) on dellwp against the real thing, from the ThinkPad:   bash scripts/wp_provision_rehearsal_dellwp.sh [options]
#   1. parity   a throwaway site is made the OLD way (wp_sites.py create --no-install, the Ansible play); a registry record is written for it from its wp_sites entry; then `wp-site-provision render --diff` must
#               say every file is the same as the play wrote it (this is the proof that the Jinja environment of the tool equals Ansible's), and `apply` on it must change nothing (containers, unit and files
#               untouched). Then the site is stopped and deleted the old way (you type its name twice) and you re-run the tools play, so the tool's list of the repo's sites is current
#   2. create   `wp-ctl create` as a job: a site that exists only in the registry is built (record, directory, quota projects, database and php containers, unit, vhost, timers) and answers on hold, with no WordPress
#   3. refuse   a second create of the same name, a reserved name and an apply of a site that does not exist are refused (exit status 2) and change nothing
#   4. install  `wp-ctl install`: WordPress into that site with a password you type here (hidden; only its length is shown). The password must be in no audit line, journal, job file or reply on the VM
#   5. apply    `wp-ctl apply` on the finished site changes nothing
# The site made in 2 stays on dellwp, on hold, because the verbs that stop and delete a registry-only site arrive with the next batch (a delete needs it stopped, with a final backup); until then it is
# used by nothing, and the next batch's rehearsal deletes it. The parity site is deleted by step 1 and its tombstone stays in vars.yml for good (a name is never reused). Both names are used up.
# Step 1 makes real commits to host_vars/dellwp/vars.yml on your current branch, which must not be main. Options:  --parity-site NAME (default parity1)  --site NAME (default provtest1)
# --skip-parity (steps 2 to 5 only)  --yes (do not ask before starting)
# It stops at the first step that fails badly. What it does not prove: the tool's behaviour with a site that has data, a state change (the next batch), anything the control app does, or that two creates
# at once are safe (the offline tests prove the locks). Paste the whole output, including the FAIL lines if any.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PARITY=parity1; SITE=provtest1; SKIP=no; YES=no
usage() { sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case $1 in
    --parity-site) PARITY=${2:-}; shift 2 ;;
    --site) SITE=${2:-}; shift 2 ;;
    --skip-parity) SKIP=yes; shift ;;
    --yes) YES=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
for n in "$PARITY" "$SITE"; do [[ $n =~ ^[a-z][a-z0-9-]{1,23}$ ]] || { echo "a site name is lowercase letters, digits and hyphens, 2 to 24 characters: $n" >&2; exit 2; }; done
[ "$PARITY" != "$SITE" ] || { echo "--parity-site and --site must differ" >&2; exit 2; }
cd "$REPO" || exit 2
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$SKIP" = no ]; then
  [ "$BRANCH" != main ] || { echo "you are on main: the parity step commits to vars.yml, do it on a branch" >&2; exit 2; }
  git diff --quiet HEAD -- host_vars/dellwp/vars.yml || { echo "host_vars/dellwp/vars.yml has uncommitted changes: commit or revert them first" >&2; exit 2; }
fi

# everything the VM side needs comes from the repo's own variables, nothing is typed here twice
read -r DOMAIN BIN MOUNT LISTEN BDIR LIB SECRETS UID_PHP < <(python3 -I - "$REPO" "$PARITY" "$SITE" "$SKIP" <<'PY'
import sys, yaml
repo, parity, site, skip = sys.argv[1:5]
h = {**yaml.safe_load(open(repo + "/vars/wp.yml")), **yaml.safe_load(open(repo + "/host_vars/dellwp/vars.yml"))}
names = {s["name"] for s in h["wp_sites"]}
for n in ([site] if skip == "yes" else [parity, site]):
    if n in names:
        sys.exit("'%s' is already in wp_sites (a name is never reused): choose another" % n)
m = h["wp_data_mount"]
def r(v):
    out = str(v).replace("{{ wp_data_mount }}", m)
    if "{{" in out:
        sys.exit("cannot resolve %r" % v)
    return out
print(h["wp_domain"], h["wp_tools_bin"], m, h["wp_nginx_listen_port"], r(h["wp_backup_dir"]), h["wp_tools_lib"], h["wp_provision_secret_dir"], h["wp_php_uid"])
PY
) || exit 2
[ -n "${UID_PHP:-}" ] || exit 2
SITES=$MOUNT/sites; TOMB=$MOUNT/tombstones
FAIL=0
TMPD=$(mktemp -d) || exit 2; trap 'rm -rf "$TMPD"' EXIT

# ---- the VM side: a small library of checks, and a helper that runs a snippet as root on dellwp with it
read -r -d '' REMOTE_LIB <<'LIB'
set -u
FAIL=0; DIR=$SITES/$S; HOST=$S.$DOMAIN; UP=wp_${S//-/_}_php; DB=wp_${S//-/_}_db
ok() { echo "ok: $*"; }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
check() { if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1 (wanted '$3', got '$2')"; fi; }
code() { local path=$1; shift; curl -s -o /dev/null -w '%{http_code}' --max-time 30 -H "Host: $HOST" -H 'X-Forwarded-Proto: https' "$@" "http://127.0.0.1:$LISTEN$path" </dev/null; }
wpc() { "$BIN/wp-site-cli" "$S" "$@" </dev/null; }
db() { docker exec -e SQL="$1" "$DB" sh -c 'MYSQL_PWD="$MARIADB_PASSWORD" exec mariadb -u"$MARIADB_USER" -N -B -e "$SQL" "$MARIADB_DATABASE"' </dev/null; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null </dev/null)" = true ]; }
finish() { [ "$FAIL" = 0 ] && echo "   (all ok)"; [ "$FAIL" -gt 100 ] && FAIL=100; exit "$FAIL"; }
# what an apply must leave alone: when the containers and the unit started, and the files' change times
fingerprint() {
  docker inspect -f '{{.Name}} {{.State.StartedAt}}' "$UP" "$DB" 2>/dev/null </dev/null
  systemctl show "wp-$S.service" -p ActiveEnterTimestampMonotonic </dev/null
  stat -c '%n %Z %a %U:%G' "$DIR/.env" "$DIR/wp-config.php" "$DIR/php.ini" "$DIR/fpm-pool.conf" "$DIR/compose.yml" "/etc/systemd/system/wp-$S.service" "/etc/nginx/conf.d/wp_site_$S.conf" 2>&1
}
hold_checks() {
  check "hold: a request from the VM itself is answered" "$(code / | grep -E -c '^(200|301|302)$')" 1
  check "hold: a request that carries a visitor address is refused" "$(code / -H 'CF-Connecting-IP: 203.0.113.9')" 403
}
LIB
vmtest() {      # S = the site; the snippet's exit status is the answer
  { printf '%s\n' "$REMOTE_LIB"; cat; } | ssh dellwp "sudo -n env S=$VMSITE DOMAIN=$DOMAIN BIN=$BIN SITES=$SITES TOMB=$TOMB LISTEN=$LISTEN BDIR=$BDIR LIB=$LIB SECRETS=$SECRETS UID_PHP=$UID_PHP bash -s"
}
vmrun() { vmtest; local rc=$?; FAIL=$((FAIL + rc)); return 0; }
VMSITE=$SITE

# ---- the ThinkPad side
say() { echo; echo "== $*"; }
bump() { local rc=$1; shift; if [ "$rc" != 0 ]; then echo "FAIL: $* (exit $rc)"; FAIL=$((FAIL+1)); return 1; fi; echo "ok: $*"; return 0; }
yn() { local a; read -r -p "$1 [y/N] " a; [ "$a" = y ]; }
summary() { echo; echo "== summary"; [ "$FAIL" = 0 ] && echo "All checks passed." || echo "$FAIL check(s) failed."; }
fatal() { echo "FAIL: $*"; FAIL=$((FAIL+1)); summary; echo "The step stopped here; the sites '$PARITY' and '$SITE' may exist. Read the output above; nothing is cleaned up for you."; exit "$FAIL"; }
ctl() { ssh dellwp "sudo -n $BIN/wp-ctl $*" </dev/null; }                       # one JSON line on standard output
jfield() { python3 -I -c 'import json,sys; v = json.loads(sys.stdin.read()).get(sys.argv[1]); print("" if v is None else v)' "$1"; }
waitjob() {     # $1 = job id, $2 = seconds; prints the job's log tail; returns the job's exit code (or 99 when it did not finish)
  local t=0 st
  while [ "$t" -lt "$2" ]; do
    st=$(ctl job-status job="$1"); if [ "$(printf '%s' "$st" | jfield state)" = done ]; then
      ctl job-log job="$1" | python3 -I -c 'import json,sys; print(json.loads(sys.stdin.read()).get("data", "")[-1500:])' | sed 's/^/   | /'
      return "$(printf '%s' "$st" | jfield rc)"
    fi
    sleep 5; t=$((t+5))
  done
  return 99
}

echo "Provisioning rehearsal on dellwp ($DOMAIN), branch $BRANCH. Throwaway sites: $([ "$SKIP" = yes ] && echo "none for parity" || echo "$PARITY (old way, deleted again)"), $SITE (registry only, left standing)."
[ "$YES" = yes ] || yn "Go on?" || { echo "Nothing was done."; exit 1; }

say "0. preflight"
ssh -o BatchMode=yes dellwp true </dev/null 2>/dev/null; bump $? "ssh to dellwp works without a password" || fatal "cannot ssh to dellwp"
ssh dellwp 'sudo -n true' </dev/null 2>/dev/null; bump $? "sudo -n works on dellwp" || fatal "sudo -n does not work"
vmrun <<'EOF'
for x in wp-site-provision wp-site-install wp-site-delete wp-site-cli wp-ctl; do [ -x "$BIN/$x" ] && ok "$x is installed" || bad "$BIN/$x is missing: run dellwp/wp_sites_dellwp.yml --tags wp_tools,wp_ctl"; done
for m in wp_site_model.py wp_site_renderer.py wp_site_provision.py; do [ -f "$LIB/$m" ] && ok "$m is deployed" || bad "$LIB/$m is missing"; done
check "the site templates are deployed (7)" "$(ls "$LIB/templates/wp" "$LIB/templates/compose_stack.service.j2" 2>/dev/null | grep -c 'j2$')" 7
check "the secret directory is root's and private" "$(stat -c '%U %a' "$SECRETS" 2>&1)" "root 700"
check "the master secret is root's and read-only" "$(stat -c '%U %a' "$SECRETS/master_secret" 2>&1)" "root 400"
python3 -c 'import jinja2' 2>/dev/null && ok "python3-jinja2 imports" || bad "python3-jinja2 is missing on the VM"
# rehearsal7 (the handoff said it waited for the VM; the repo says it was deleted): nothing of it may be left
r7=0; [ -e "$SITES/rehearsal7" ] && { bad "rehearsal7: a site directory is still there"; r7=1; }
[ -e /etc/systemd/system/wp-rehearsal7.service ] && { bad "rehearsal7: its unit is still there"; r7=1; }
[ -e /etc/nginx/conf.d/wp_site_rehearsal7.conf ] && { bad "rehearsal7: its vhost is still there"; r7=1; }
[ -e "$BDIR/rehearsal7" ] && { bad "rehearsal7: its backup directory is still there"; r7=1; }
[ "$(docker ps -aq --filter 'name=^wp_rehearsal7_' </dev/null | wc -l)" = 0 ] || { bad "rehearsal7: containers are still there"; r7=1; }
[ "$r7" = 0 ] && ok "nothing of rehearsal7 is left"
for n in "$S"; do [ ! -e "$SITES/$n" ] && ok "no directory called $n yet" || bad "$SITES/$n exists already"; done
finish
EOF
[ "$FAIL" = 0 ] || fatal "the preflight found problems; fix them first"

if [ "$SKIP" = yes ]; then echo; echo "(1. parity: skipped)"; else
say "1. parity: a site made the old way, then the tool's view of it"
python3 scripts/wp_sites.py create --name "$PARITY" --no-install --policy notify --yes --commit; bump $? "wp_sites.py create --no-install" || fatal "the old-way create failed"
REC=$(python3 -I - "$PARITY" "$REPO" <<'PY'
import json, sys, yaml, datetime
name, repo = sys.argv[1:3]
h = yaml.safe_load(open(repo + "/host_vars/dellwp/vars.yml"))
e = next(s for s in h["wp_sites"] if s["name"] == name)
print(json.dumps({"schema": 1, **e, "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}))
PY
) || fatal "could not build the record from the wp_sites entry"
DEF=$(python3 -I -c 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))["wp_site_defaults"]))' "$REPO/host_vars/dellwp/vars.yml")
VMSITE=$PARITY
ssh dellwp "sudo -n env LIB=$LIB DATA=$SITES TOMB=$TOMB REC='$REC' DEF='$DEF' python3 -I -c 'import json,os,sys; sys.path.insert(0, os.environ[\"LIB\"]); import wp_site_model as m; m.Registry(os.environ[\"DATA\"], os.environ[\"TOMB\"], json.loads(os.environ[\"DEF\"])).write_site(json.loads(os.environ[\"REC\"]))'" </dev/null
bump $? "a registry record was written for $PARITY from its wp_sites entry (through the tool's own model, which checks it)" || fatal "could not write the record"
vmrun <<'EOF'
out=$("$BIN/wp-site-provision" render "$S" --diff </dev/null 2>&1); rc=$?
echo "$out" | sed 's/^/   | /'
check "render --diff: every file of the play's site is the same as the tool renders it" "$rc" 0
check "none of the seven files differs or is missing" "$(echo "$out" | grep -c -E '^(differs|missing) ')" 0
before=$(fingerprint)
"$BIN/wp-site-provision" apply "$S" </dev/null 2>&1 | sed 's/^/   | /'; check "apply on the play's site succeeds" "${PIPESTATUS[0]}" 0
check "apply left the containers, the unit and the files exactly as they were" "$([ "$(fingerprint)" = "$before" ] && echo same || echo changed)" same
diff <(echo "$before") <(fingerprint) | head -n 12 | sed 's/^/   (diff) /' || true
running "$UP" && running "$DB" && ok "both containers are still running" || bad "a container is not running"
hold_checks
finish
EOF
[ "$FAIL" = 0 ] || fatal "the tool does not see the play's site as the play made it: do not go on; the output above says which file"
python3 scripts/wp_sites.py stop "$PARITY" --yes --commit; bump $? "wp_sites.py stop (final backup first)" || fatal "stop failed"
python3 scripts/wp_sites.py delete "$PARITY" --commit; bump $? "wp_sites.py delete (type the name twice)" || fatal "delete failed"
echo
echo "   The tool's list of the repo's sites is baked into its wrapper, so it must be refreshed now (the closed $PARITY must be in it, or a new site could be given its port):"
echo "     ansible-playbook -i inventory.yml dellwp/wp_sites_dellwp.yml --tags wp_tools,wp_ctl"
yn "Did that run finish without errors?" || fatal "re-run the tools play, then run this again with --skip-parity"
ssh dellwp "sudo -n grep -c '\"name\": \"$PARITY\"' $BIN/wp-site-provision" </dev/null | grep -q '^[1-9]'; bump $? "the wrapper names $PARITY (so its port and quota id are never reused)" || fatal "the wrapper does not name $PARITY: the play did not refresh it"
fi

VMSITE=$SITE
say "2. create $SITE through wp-ctl"
R=$(ctl create name="$SITE" label=Provtest req="prov-$SITE"); echo "   $R"
JOB=$(printf '%s' "$R" | jfield job); [ -n "$JOB" ] || fatal "no job was started"
waitjob "$JOB" 900; bump $? "the create job finished with exit 0" || fatal "the create job failed or did not finish: read its log above (wp-ctl job-log job=$JOB)"
vmrun <<'EOF'
rec=$(cat "$DIR/site.json" 2>&1)
check "the record is valid JSON with this name, state hold and numbers" "$(python3 -I -c 'import json,sys; r = json.loads(sys.argv[1]); print(r["name"] == sys.argv[2] and r["state"] == "hold" and r["port"] > 9100 and r["quota_id"] > 1000 and r["label"] == "Provtest")' "$rec" "$S" 2>&1)" True
check "the record is root's and private to writers" "$(stat -c '%U %a' "$DIR/site.json")" "root 644"
check "no tombstone for it" "$([ -e "$TOMB/$S.json" ] && echo yes || echo no)" no
running "$UP" && ok "the php container is running" || bad "$UP is not running"
running "$DB" && ok "the database container is running" || bad "$DB is not running"
check "the database is empty" "$(db 'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()')" 0
check "WordPress is not installed" "$(wpc core is-installed >/dev/null 2>&1 && echo yes || echo no)" no
check "not stopped" "$([ -e "$DIR/stopped.flag" ] && echo yes || echo no)" no
check ".env is 600 root" "$(stat -c '%a %U' "$DIR/.env")" "600 root"
check "wp-config.php is 640, root, group $UID_PHP" "$(stat -c '%a %U %g' "$DIR/wp-config.php")" "640 root $UID_PHP"
check "the quota project of the directory is the record's" "$(lsattr -pd "$DIR" | awk '{print $1}')" "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["quota_id"])' "$DIR/site.json")"
check "the site's backup directory is private" "$(stat -c '%a' "$BDIR/$S" 2>&1)" 700
check "nginx accepts its configuration" "$(nginx -t >/dev/null 2>&1 && echo yes || echo no)" yes
check "the unit is enabled" "$(systemctl is-enabled "wp-$S.service" 2>/dev/null || true)" enabled
check "the backup timer is on" "$(systemctl is-enabled "wp-backup@$S.timer" 2>/dev/null || true)" enabled
check "the update timer is on" "$(systemctl is-enabled "wp-update@$S.timer" 2>/dev/null || true)" enabled
check "the cron timer is off while the site is on hold" "$(systemctl is-enabled "wp-cron@$S.timer" 2>/dev/null || true)" disabled
check "hold: the installer answers a request from the VM" "$(code /wp-admin/install.php)" 200
hold_checks
finish
EOF
R2=$(ctl create name="$SITE" label=Provtest req="prov-$SITE"); echo "   $R2"
check_same=$([ "$(printf '%s' "$R2" | jfield job)" = "$JOB" ] && echo yes || echo no)
[ "$check_same" = yes ]; bump $? "the same request id again returns the same job (no second build)"

say "3. refusals change nothing"
for item in "create name=$SITE:name_taken" "create name=hub:reserved_name" "create name=Bad_Name:invalid_name" "apply site=nosuch$SITE:unknown_site" "create name=$SITE label=x set=1:unknown_argument"; do
  req=${item%:*}; want=${item##*:}
  out=$(ctl $req); rc=$?
  [ "$(printf '%s' "$out" | jfield error)" = "$want" ] && [ "$rc" = 2 ]; bump $? "'$req' is refused as $want with status 2"
done
vmrun <<'EOF'
check "still one record, and the site still runs" "$(ls "$SITES" | grep -c "^$S\$"):$(running "$UP" && echo up)" "1:up"
finish
EOF

say "4. install WordPress with a password typed here"
echo "   Choose the administrator password for $SITE (14 or more characters). It is not shown and not stored anywhere but on the site."
read -r -s -p "   Password: " PW; echo; read -r -s -p "   Again:    " PW2; echo
[ "$PW" = "$PW2" ] && [ "${#PW}" -ge 14 ] || fatal "the two entries differ or are shorter than 14 characters"
echo "   (password: ${#PW} characters)"
OUT=$(printf '%s\n' "$PW" | ssh dellwp "sudo -n $BIN/wp-ctl install site=$SITE title=Provtest user=owner email=owner@example.org"); RC=$?
echo "   exit $RC: $(printf '%s' "$OUT" | python3 -I -c 'import json,sys; r = json.loads(sys.stdin.read()); print({k: r.get(k) for k in ("ok", "rc", "error")})' 2>&1)"
[ "$RC" = 0 ]; bump $? "wp-ctl install finished with exit 0" || { unset PW PW2; fatal "install failed (the tool's own output is in the reply; the password is redacted)"; }
printf '%s' "$OUT" | grep -qF -- "$PW"; [ $? -ne 0 ]; bump $? "the reply does not contain the password" || true
# the VM script reads the password from its standard input and hands it to grep as a pattern file (a process substitution), so it is in no argument list
LEAK='read -r pw; pat() { printf "%s\n" "$pw"; }; grep -rlF -f <(pat) /var/log/wp-ctl /var/lib/wp-ctl /run/wp-ctl 2>/dev/null | sed "s/^/file: /"; echo "journal: $(journalctl --since=-30min -q 2>/dev/null | grep -cF -f <(pat))"'
printf '%s\n' "$PW" | ssh dellwp "sudo -n bash -c \"\$(echo $(printf '%s' "$LEAK" | base64 -w0) | base64 -d)\"" > "$TMPD/leak" 2>/dev/null
grep -q '^journal: ' "$TMPD/leak"; bump $? "the leak check itself ran (it printed a journal count)" || true
[ "$(grep -c '^file: ' "$TMPD/leak")" = 0 ] && [ "$(sed -n 's/^journal: //p' "$TMPD/leak")" = 0 ]; bump $? "the password is in no file of wp-ctl (audit, jobs, run) and not in the last 30 minutes of the journal ($(grep '^file: ' "$TMPD/leak" | head -n 3 | tr '\n' ' '))"
unset PW PW2 OUT
vmrun <<'EOF'
check "WordPress is installed" "$(wpc core is-installed >/dev/null 2>&1 && echo yes || echo no)" yes
check "the administrator exists" "$(wpc user list --field=user_login --skip-plugins --skip-themes | grep -c '^owner$')" 1
check "search engines are discouraged" "$(wpc option get blog_public --skip-plugins --skip-themes)" 0
check "the stored siteurl is this site's name" "$(db "SELECT option_value FROM $(sed -n "s/.*\$table_prefix *= *'\([A-Za-z0-9_]*\)'.*/\1/p" "$DIR/wp-config.php" | head -n 1)options WHERE option_name='siteurl'")" "https://$HOST"
hold_checks
finish
EOF

say "5. apply on the finished site changes nothing"
vmtest <<'EOF' > $TMPD/fp
fingerprint
EOF
R=$(ctl apply site="$SITE"); echo "   $R"
JOB=$(printf '%s' "$R" | jfield job); [ -n "$JOB" ] || fatal "no job was started"
waitjob "$JOB" 600; bump $? "the apply job finished with exit 0" || fatal "the apply job failed: read its log above"
vmtest <<'EOF' > $TMPD/fp2
fingerprint
EOF
cmp -s $TMPD/fp $TMPD/fp2; bump $? "containers, unit and files are exactly as before the apply"
diff $TMPD/fp $TMPD/fp2 | head -n 12 | sed 's/^/   (diff) /'; rm -f $TMPD/fp $TMPD/fp2
vmrun <<'EOF'
wpc core is-installed >/dev/null 2>&1 && ok "WordPress is still installed" || bad "WordPress is not installed any more"
hold_checks
finish
EOF

summary
echo "   $SITE is standing on hold on dellwp. The parity site ${PARITY} is closed in vars.yml (commits: $(git log --oneline "$BRANCH" | head -n 6 | grep -c "$PARITY"))."
exit "$FAIL"
