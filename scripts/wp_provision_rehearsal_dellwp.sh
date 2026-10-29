#!/bin/bash
# Rehearses the PROVISIONING TOOL (wp-site-provision, and the wp-ctl verbs create, apply and install over it) on dellwp against the real thing, from the ThinkPad:   bash scripts/wp_provision_rehearsal_dellwp.sh [options]
#   1. create   `wp-ctl create` as a job: a site that exists only in the registry is built (record, directory, quota projects, database and php containers, unit, vhost, timers) and answers on hold, with no WordPress
#   2. refuse   a second create of the same name, a reserved name and an apply of a site that does not exist are refused (exit status 2) and change nothing
#   3. install  `wp-ctl install`: WordPress into that site with a password you type here (hidden; only its length is shown). The password must be in no audit line, journal, job file or reply on the VM
#   4. apply    `wp-ctl apply` on the finished site changes nothing
# The site made in 1 stays on dellwp, on hold, with WordPress in it: scripts/wp_monitor_rehearsal_dellwp.sh works on it and scripts/wp_state_rehearsal_dellwp.sh changes its settings and state and deletes
# it (a delete needs it stopped, with a final backup); until then it is used by nothing. The script makes no commit and edits nothing in the repo. Options:  --site NAME (default provtest1)  --yes (do not
# ask before starting)
# It stops at the first step that fails badly. What it does not prove: the tool's behaviour with a site that has data, a state change (the next batch), anything the control app does, or that two creates
# at once are safe (the offline tests prove the locks). Paste the whole output, including the FAIL lines if any.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SITE=provtest1; YES=no
usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case $1 in
    --site) SITE=${2:-}; shift 2 ;;
    --yes) YES=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ $SITE =~ ^[a-z][a-z0-9-]{1,23}$ ]] || { echo "a site name is lowercase letters, digits and hyphens, 2 to 24 characters: $SITE" >&2; exit 2; }
cd "$REPO" || exit 2

# everything the VM side needs comes from the repo's own variables, nothing is typed here twice
read -r DOMAIN BIN MOUNT LISTEN BDIR LIB SECRETS UID_PHP < <(python3 -I - "$REPO" <<'PY'
import sys, yaml
repo = sys.argv[1]
h = {**yaml.safe_load(open(repo + "/vars/wp.yml")), **yaml.safe_load(open(repo + "/host_vars/dellwp/vars.yml"))}
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
fatal() { echo "FAIL: $*"; FAIL=$((FAIL+1)); summary; echo "The step stopped here; the site '$SITE' may exist. Read the output above; nothing is cleaned up for you."; exit "$FAIL"; }
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

echo "Provisioning rehearsal on dellwp ($DOMAIN). Throwaway site: $SITE (registry only, left standing)."
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

VMSITE=$SITE
say "1. create $SITE through wp-ctl"
R=$(ctl create name="$SITE" label=Provtest req="prov-$SITE"); echo "   $R"
JOB=$(printf '%s' "$R" | jfield job); [ -n "$JOB" ] || fatal "no job was started"
waitjob "$JOB" 900; bump $? "the create job finished with exit 0" || fatal "the create job failed or did not finish: read its log above (wp-ctl job-log job=$JOB)"
vmrun <<'EOF'
rec=$(cat "$DIR/site.json" 2>&1)
check "the record is valid JSON with this name, state hold and numbers" "$(python3 -I -c 'import json,sys; r = json.loads(sys.argv[1]); print(r["name"] == sys.argv[2] and r["state"] == "hold" and r["port"] > 9100 and r["quota_id"] > 1000 and r["label"] == "Provtest")' "$rec" "$S" 2>&1)" True
check "the record is root's and private to writers" "$(stat -c '%U %a' "$DIR/site.json")" "root 644"
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

say "2. refusals change nothing"
for item in "create name=$SITE:name_taken" "create name=hub:reserved_name" "create name=Bad_Name:invalid_name" "apply site=nosuch$SITE:unknown_site" "create name=$SITE label=x set=1:unknown_argument"; do
  req=${item%:*}; want=${item##*:}
  out=$(ctl $req); rc=$?
  [ "$(printf '%s' "$out" | jfield error)" = "$want" ] && [ "$rc" = 2 ]; bump $? "'$req' is refused as $want with status 2"
done
vmrun <<'EOF'
check "still one record, and the site still runs" "$(ls "$SITES" | grep -c "^$S\$"):$(running "$UP" && echo up)" "1:up"
finish
EOF

say "3. install WordPress with a password typed here"
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

say "4. apply on the finished site changes nothing"
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
echo "   $SITE is standing on hold on dellwp. Nothing was committed or edited in the repo."
exit "$FAIL"
