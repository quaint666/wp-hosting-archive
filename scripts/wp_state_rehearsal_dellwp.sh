#!/bin/bash
# Rehearses SET, ENSURE-STATE and DELETE (wp-ctl, over wp-site-provision, wp-site-backup and wp-site-delete) on dellwp against the real thing, from the ThinkPad:   bash scripts/wp_state_rehearsal_dellwp.sh [options]
# It works on a site that exists only in the registry, is on hold and has WordPress installed: the one scripts/wp_provision_rehearsal_dellwp.sh leaves standing (provtest1). That site is DELETED at the end, so its name
# leaves a tombstone in the registry (its port and quota id are never given again; its name may be). Steps:
#   0. preflight   the tools on the VM are the ones of this branch (close, plan_state, ensure-state, the tombstone directory in the wrapper); the site is whole, running, on hold, with a record and no tombstone
#   1. set         a note (registry only: no container, unit or file changes); a memory limit (the stack restarts and the php container really has the new limit); it is set back; a value the tool refuses fails
#                  the job with status 2 and changes nothing; a key that is not editable is refused by the dispatcher
#   2. live        a go-live request without the site's name, or with another, is refused; with it the vhost opens (a visitor's address is no longer refused), the cron timer comes on; then back to hold
#   3. stopped     a marker is written into WordPress; hold to stopped takes the final backup first (a new snapshot tagged final), the stack goes down, visitors get the paused page; stopped to live is refused by
#                  the tool and nothing changes; back to hold: the marker is intact; the same request again changes nothing
#   4. delete      stopped again, then wp-ctl delete with the name typed twice: the archive is complete, the registry holds a tombstone with the same name, port and quota id and no record, the files, containers,
#                  unit and vhost are gone; the name is free again (check-name); the audit shows the verbs
# It makes no commit and edits nothing in the repo. Options:  --site NAME (default provtest1)  --with-live (also fetch the site through the tunnel from here while it is live: for about a minute anyone can)
# --yes (do not ask before starting)
# It stops at the first step that fails badly. What it does not prove: the control app, two requests at once (the offline tests prove the locks), a site with a real friend's content, or that the Proxmox backups still hold
# the data. Paste the whole output, including any FAIL lines.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SITE=provtest1; LIVE=no; YES=no
usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case $1 in
    --site) SITE=${2:-}; shift 2 ;;
    --with-live) LIVE=yes; shift ;;
    --yes) YES=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ $SITE =~ ^[a-z][a-z0-9-]{1,23}$ ]] || { echo "a site name is lowercase letters, digits and hyphens, 2 to 24 characters: $SITE" >&2; exit 2; }
cd "$REPO" || exit 2

# everything the VM side needs comes from the repo's own variables, nothing is typed here twice
read -r DOMAIN BIN MOUNT LISTEN BDIR LIB ARCH PHPMEM < <(python3 -I - "$REPO" <<'PY'
import sys, yaml
repo = sys.argv[1]
h = {**yaml.safe_load(open(repo + "/vars/wp.yml")), **yaml.safe_load(open(repo + "/host_vars/dellwp/vars.yml"))}
m = h["wp_data_mount"]
def r(v):
    out = str(v).replace("{{ wp_data_mount }}", m)
    if "{{" in out:
        sys.exit("cannot resolve %r" % v)
    return out
print(h["wp_domain"], h["wp_tools_bin"], m, h["wp_nginx_listen_port"], r(h["wp_backup_dir"]), h["wp_tools_lib"], r(h["wp_archive_dir"]), h["wp_site_defaults"]["php_mem_mb"])
PY
) || exit 2
[ -n "${PHPMEM:-}" ] || exit 2
SITES=$MOUNT/sites; TOMB=$MOUNT/tombstones; HOST=$SITE.$DOMAIN
NEWMEM=$((PHPMEM + 64))
FAIL=0; WANT=""
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
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null </dev/null)" = true ]; }
finish() { [ "$FAIL" = 0 ] && echo "   (all ok)"; [ "$FAIL" -gt 100 ] && FAIL=100; exit "$FAIL"; }
field() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2" 2>&1; }
tombs() { ls -1 "$TOMB" 2>/dev/null | grep -Ec "^$S(\.[0-9]{8}T[0-9]{6}Z)?\.json$"; }        # how many times this name has been closed
newest_tag() { local d; d=$(ls -1 "$BDIR/$S" 2>/dev/null | grep -E '^[0-9]{8}T[0-9]{6}Z$' | sort | tail -n 1); echo "${d:-none} $(sed -n 's/^tag=//p' "$BDIR/$S/$d/MANIFEST" 2>/dev/null | head -n 1)"; }
# what a request that changes nothing must leave alone: when the containers and the unit started, and the files' change times
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
  { printf '%s\n' "$REMOTE_LIB"; cat; } | ssh dellwp "sudo -n env S=$SITE DOMAIN=$DOMAIN BIN=$BIN SITES=$SITES TOMB=$TOMB LISTEN=$LISTEN BDIR=$BDIR LIB=$LIB ARCH=$ARCH WANT=$WANT bash -s"
}
vmrun() { vmtest; local rc=$?; FAIL=$((FAIL + rc)); return 0; }

# ---- the ThinkPad side
say() { echo; echo "== $*"; }
bump() { local rc=$1; shift; if [ "$rc" != 0 ]; then echo "FAIL: $* (exit $rc)"; FAIL=$((FAIL+1)); return 1; fi; echo "ok: $*"; return 0; }
yn() { local a; read -r -p "$1 [y/N] " a; [ "$a" = y ]; }
summary() { echo; echo "== summary"; [ "$FAIL" = 0 ] && echo "All checks passed." || echo "$FAIL check(s) failed."; }
fatal() { echo "FAIL: $*"; FAIL=$((FAIL+1)); summary; echo "The run stopped here; the site '$SITE' is in whatever state the output above shows. Nothing is cleaned up for you."; exit "$FAIL"; }
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
job() {         # $1 = what it is, $2 = the wp-ctl request, $3 = seconds to wait, $4 = the exit status the job must end with (0 when absent)
  local out j want=${4:-0}
  out=$(ctl $2); echo "   $out"
  j=$(printf '%s' "$out" | jfield job); [ -n "$j" ] || { bump 1 "$1: no job was started"; return 1; }
  waitjob "$j" "$3"; local rc=$?
  [ "$rc" = "$want" ]; bump $? "$1: the job ended with status $rc (wanted $want)"
}
refused() {     # $1 = the wp-ctl request, $2 = the error code wp-ctl must answer with; status 2 and nothing started
  local out rc
  out=$(ctl $1); rc=$?
  [ "$(printf '%s' "$out" | jfield error)" = "$2" ] && [ "$rc" = 2 ]; bump $? "'$1' is refused as $2 with status 2"
}

echo "State and delete rehearsal on dellwp ($DOMAIN). Site: $SITE, which is DELETED at the end (its tombstone keeps its port and quota id). Public step: $([ "$LIVE" = yes ] && echo "also through the tunnel" || echo "from the VM only")."
[ "$YES" = yes ] || yn "Go on?" || { echo "Nothing was done."; exit 1; }

say "0. preflight"
ssh -o BatchMode=yes dellwp true </dev/null 2>/dev/null; bump $? "ssh to dellwp works without a password" || fatal "cannot ssh to dellwp"
ssh dellwp 'sudo -n true' </dev/null 2>/dev/null; bump $? "sudo -n works on dellwp" || fatal "sudo -n does not work"
vmrun <<'EOF'
for x in wp-ctl wp-site-provision wp-site-delete wp-site-backup wp-site-cli; do [ -x "$BIN/$x" ] && ok "$x is installed" || bad "$BIN/$x is missing: run dellwp/wp_sites_dellwp.yml --tags wp_tools,wp_ctl"; done
grep -q 'def close' "$LIB/wp_site_provision.py" && ok "the deployed provisioning tool has close" || bad "the deployed wp_site_provision.py has no close: run the tools play (dellwp/wp_sites_dellwp.yml --tags wp_tools,wp_ctl)"
grep -q 'def plan_state' "$LIB/wp_site_model.py" && ok "the deployed model has plan_state" || bad "the deployed wp_site_model.py has no plan_state: run the tools play"
grep -q '"ensure-state"' "$LIB/wp_ctl.py" && ok "the deployed dispatcher has ensure-state" || bad "the deployed wp_ctl.py has no ensure-state: run the tools play"
grep -q 'closed_sites' "$BIN/wp-ctl" && bad "the wrapper $BIN/wp-ctl still carries a closed-name list: run the tools play (a closed name may be used again)" || ok "the dispatcher's wrapper has no closed-name list"
grep -q 'wp-site-provision' "$BIN/wp-site-delete" && grep -q ' close ' "$BIN/wp-site-delete" && ok "the delete tool closes the registry" || bad "$BIN/wp-site-delete does not call close: run the tools play"
grep -q 'backup_timeout_s' "$BIN/wp-site-provision" && ok "the provisioning wrapper has the backup tool and its limit" || bad "the wrapper $BIN/wp-site-provision has no backup_timeout_s: run the tools play"
[ -f "$DIR/site.json" ] && [ ! -L "$DIR/site.json" ] && ok "$S has a registry record" || bad "$S has no registry record: run scripts/wp_provision_rehearsal_dellwp.sh first (it leaves provtest1 standing)"
check "its state is hold" "$(field "$DIR/site.json" state)" hold
echo "   tombstones of earlier sites called $S: $(tombs) (a delete adds exactly one)"
running "$UP" && running "$DB" && ok "both containers are running" || bad "the site is not running"
check "WordPress is installed" "$(wpc core is-installed >/dev/null 2>&1 && echo yes || echo no)" yes
check "not stopped" "$([ -e "$DIR/stopped.flag" ] && echo yes || echo no)" no
[ -d "$ARCH" ] && ok "the archive directory exists" || bad "$ARCH does not exist"
check "no archive of $S" "$(ls -d "$ARCH/$S"-* 2>/dev/null | wc -l)" 0
check "the backup directory is private" "$(stat -c '%a' "$BDIR/$S" 2>&1)" 700
hold_checks
finish
EOF
[ "$FAIL" = 0 ] || fatal "the preflight found problems; fix them first"
ssh dellwp "sudo -n cat $SITES/$SITE/site.json" </dev/null > "$TMPD/record.json" 2>/dev/null; bump $? "the site's record was read (its name, port and quota id are checked against the tombstone at the end)" || fatal "cannot read the record"
read -r PORT QID < <(python3 -I -c 'import json,sys; r = json.load(open(sys.argv[1])); print(r["port"], r["quota_id"])' "$TMPD/record.json") || fatal "the record has no port and quota id"

say "1. set"
vmtest > "$TMPD/fp0" <<'EOF'
fingerprint
EOF
job "set note (registry only)" "set site=$SITE key=note value=rehearsal-note req=state-$SITE-note" 300 || fatal "set note failed"
vmtest > "$TMPD/fp1" <<'EOF'
fingerprint
EOF
cmp -s "$TMPD/fp0" "$TMPD/fp1"; bump $? "a note changed no container, unit or file"
diff "$TMPD/fp0" "$TMPD/fp1" | head -n 12 | sed 's/^/   (diff) /'
WANT=rehearsal-note vmrun <<'EOF'
check "the record holds the note" "$(field "$DIR/site.json" note)" "$WANT"
hold_checks
finish
EOF
refused "set site=$SITE key=port value=9999" bad_value
refused "set site=$SITE key=state value=live" bad_value
refused "set site=$SITE" missing_argument
job "a memory limit the tool refuses (99999)" "set site=$SITE key=php_mem_mb value=99999" 300 2
WANT=$PHPMEM vmrun <<'EOF'
check "the refused value changed nothing: the record has no php_mem_mb or the old one" "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("php_mem_mb", int(sys.argv[2])))' "$DIR/site.json" "$WANT")" "$WANT"
hold_checks
finish
EOF
job "set php_mem_mb=$NEWMEM" "set site=$SITE key=php_mem_mb value=$NEWMEM req=state-$SITE-mem" 600 || fatal "set php_mem_mb failed"
WANT=$NEWMEM vmrun <<'EOF'
check "the record holds the new limit" "$(field "$DIR/site.json" php_mem_mb)" "$WANT"
check "the php container really has that limit" "$(docker inspect -f '{{.HostConfig.Memory}}' "$UP" </dev/null)" "$((WANT * 1048576))"
check "and the same limit for memory plus swap" "$(docker inspect -f '{{.HostConfig.MemorySwap}}' "$UP" </dev/null)" "$((WANT * 1048576))"
running "$UP" && running "$DB" && ok "both containers are running" || bad "a container is not running"
check "WordPress still works" "$(wpc core is-installed >/dev/null 2>&1 && echo yes || echo no)" yes
hold_checks
finish
EOF
job "set php_mem_mb back to $PHPMEM" "set site=$SITE key=php_mem_mb value=$PHPMEM" 600 || fatal "setting the limit back failed"
WANT=$PHPMEM vmrun <<'EOF'
check "the php container has the old limit again" "$(docker inspect -f '{{.HostConfig.Memory}}' "$UP" </dev/null)" "$((WANT * 1048576))"
hold_checks
finish
EOF

say "2. live, and back to hold"
refused "ensure-state site=$SITE state=live" missing_argument
refused "ensure-state site=$SITE state=live confirm_name=wrongname" confirm_mismatch
refused "ensure-state site=$SITE state=closed" bad_value
vmrun <<'EOF'
check "refused requests changed nothing: still hold" "$(field "$DIR/site.json" state)" hold
hold_checks
finish
EOF
echo "   The next step puts $HOST live: the vhost opens$([ "$LIVE" = yes ] && echo ", and with --with-live the site is fetched through the tunnel, so anyone can reach it for about a minute")."
[ "$YES" = yes ] || yn "Go live?" || fatal "not confirmed"
job "ensure-state live" "ensure-state site=$SITE state=live confirm_name=$SITE req=state-$SITE-live" 900 || fatal "going live failed"
vmrun <<'EOF'
check "the record says live" "$(field "$DIR/site.json" state)" live
check "a visitor's address is no longer refused" "$(code / -H 'CF-Connecting-IP: 203.0.113.9' | grep -E -c '^(200|301|302)$')" 1
check "the cron timer is on while the site is live" "$(systemctl is-enabled "wp-cron@$S.timer" 2>/dev/null || true)" enabled
check "the backup timer is on" "$(systemctl is-enabled "wp-backup@$S.timer" 2>/dev/null || true)" enabled
running "$UP" && running "$DB" && ok "both containers are running" || bad "a container is not running"
check "no maintenance flag is up" "$([ -e "$DIR/maintenance.flag" ] && echo yes || echo no)" no
finish
EOF
if [ "$LIVE" = yes ]; then
  sleep 5
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$HOST/" </dev/null)
  case $C in 200|301|302) bump 0 "https://$HOST/ answers $C through the tunnel" ;; *) bump 1 "https://$HOST/ answered '$C' (is there a DNS record and a tunnel route for $HOST?)" ;; esac
fi
job "ensure-state hold" "ensure-state site=$SITE state=hold" 900 || fatal "going back to hold failed"
vmrun <<'EOF'
check "the record says hold" "$(field "$DIR/site.json" state)" hold
check "the cron timer is off again" "$(systemctl is-enabled "wp-cron@$S.timer" 2>/dev/null || true)" disabled
hold_checks
finish
EOF
if [ "$LIVE" = yes ]; then
  sleep 5
  H=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$HOST/" </dev/null)
  [ "$H" != 200 ] && bump 0 "back on hold the public no longer gets the site (answer: '$H')" || bump 1 "back on hold, https://$HOST/ still answers 200: the site is still public"
fi

say "3. stopped, refused moves, and back"
MARK=rehearsal-$RANDOM-$RANDOM
WANT=$MARK vmrun <<'EOF'
wpc option update rehearsal_marker "$WANT" >/dev/null 2>&1; check "a marker was written into WordPress" "$(wpc option get rehearsal_marker 2>/dev/null)" "$WANT"
echo "before: $(newest_tag)"
finish
EOF
vmtest > "$TMPD/snap0" <<'EOF'
newest_tag
EOF
job "ensure-state stopped (the final backup first)" "ensure-state site=$SITE state=stopped req=state-$SITE-stop" 3600 || fatal "stopping failed"
vmtest > "$TMPD/snap1" <<'EOF'
newest_tag
EOF
[ "$(cut -d' ' -f1 "$TMPD/snap0")" != "$(cut -d' ' -f1 "$TMPD/snap1")" ] && [ "$(cut -d' ' -f2 "$TMPD/snap1")" = final ]; bump $? "a NEW snapshot tagged final was taken ($(tr '\n' ' ' < "$TMPD/snap0")-> $(tr '\n' ' ' < "$TMPD/snap1"))"
vmrun <<'EOF'
check "the record says stopped" "$(field "$DIR/site.json" state)" stopped
check "no container of the site is running" "$(docker ps -q --filter "name=^wp_${S//-/_}_" </dev/null | wc -l)" 0
[ -e "$DIR/stopped.flag" ] && ok "stopped.flag" || bad "no stopped.flag"
check "no maintenance flag is left" "$([ -e "$DIR/maintenance.flag" ] && echo yes || echo no)" no
check "visitors get the paused page (503)" "$(code / -H 'CF-Connecting-IP: 198.51.100.9')" 503
check "the backup timer is off" "$(systemctl is-enabled "wp-backup@$S.timer" 2>/dev/null || true)" disabled
check "the update timer is off" "$(systemctl is-enabled "wp-update@$S.timer" 2>/dev/null || true)" disabled
[ -d "$DIR/db" ] && [ -d "$DIR/html/wp-content" ] && ok "the data is intact" || bad "the data is not intact"
finish
EOF
job "stopped to live is refused by the tool" "ensure-state site=$SITE state=live confirm_name=$SITE" 300 2
vmrun <<'EOF'
check "it is still stopped" "$(field "$DIR/site.json" state)" stopped
check "still no container running" "$(docker ps -q --filter "name=^wp_${S//-/_}_" </dev/null | wc -l)" 0
check "visitors still get the paused page" "$(code / -H 'CF-Connecting-IP: 198.51.100.9')" 503
finish
EOF
job "ensure-state hold (back from stopped)" "ensure-state site=$SITE state=hold req=state-$SITE-back" 900 || fatal "coming back from stopped failed"
WANT=$MARK vmrun <<'EOF'
check "the record says hold" "$(field "$DIR/site.json" state)" hold
check "no stopped.flag" "$([ -e "$DIR/stopped.flag" ] && echo yes || echo no)" no
running "$UP" && running "$DB" && ok "both containers are running again" || bad "containers did not come back"
check "the marker survived the stop and the start" "$(wpc option get rehearsal_marker 2>/dev/null)" "$WANT"
hold_checks
finish
EOF
vmtest > "$TMPD/fp2" <<'EOF'
fingerprint
EOF
job "the same request again (hold to hold)" "ensure-state site=$SITE state=hold" 300 || fatal "the repeat failed"
vmtest > "$TMPD/fp3" <<'EOF'
fingerprint
EOF
cmp -s "$TMPD/fp2" "$TMPD/fp3"; bump $? "a request for the state it is in changed no container, unit or file"
diff "$TMPD/fp2" "$TMPD/fp3" | head -n 12 | sed 's/^/   (diff) /'

say "4. stop again, then delete (the name typed twice, as the tool needs)"
job "ensure-state stopped (before the delete)" "ensure-state site=$SITE state=stopped" 3600 || fatal "the second stop failed"
vmtest > "$TMPD/tomb0" <<'EOF'
tombs
EOF
job "delete" "delete site=$SITE confirm_name=$SITE confirm_again=$SITE req=state-$SITE-delete" 3600 || fatal "the delete failed: read its log above; the archive and the tombstone are only written once it has verified"
WANT=$(cat "$TMPD/tomb0") vmrun <<'EOF'
check "the site directory is gone" "$([ -e "$DIR" ] && echo yes || echo no)" no
check "its containers are gone" "$(docker ps -aq --filter "name=^wp_${S//-/_}_" </dev/null | wc -l)" 0
check "its unit is gone" "$([ -e "/etc/systemd/system/wp-$S.service" ] && echo yes || echo no)" no
check "its vhost is gone" "$([ -e "/etc/nginx/conf.d/wp_site_$S.conf" ] && echo yes || echo no)" no
check "its backup directory is gone" "$([ -e "$BDIR/$S" ] && echo yes || echo no)" no
check "its archive is on the VM (kept 14 days)" "$(ls -d "$ARCH/$S"-* 2>/dev/null | wc -l)" 1
arch=$(ls -d "$ARCH/$S"-* 2>/dev/null | head -n 1)
check "the archive holds the dump, the files and a manifest" "$(ls "$arch" 2>/dev/null | tr '\n' ' ')" "MANIFEST db.sql.gz wp-content.tar.gz "
check "the archived dump passes gzip -t" "$(gzip -t "$arch/db.sql.gz" 2>/dev/null && echo yes || echo no)" yes
T=$(ls -1 "$TOMB/$S".*.json 2>/dev/null | tail -n 1)
[ -n "$T" ] && [ -f "$T" ] && [ ! -L "$T" ] && ok "a dated tombstone exists ($T)" || bad "no dated tombstone in $TOMB"
check "the tombstone says closed" "$(field "$T" state)" closed
check "the tombstone keeps the name" "$(field "$T" name)" "$S"
check "no site record is left" "$([ -e "$DIR/site.json" ] && echo yes || echo no)" no
check "the tombstone is root's, 644" "$(stat -c '%U %a' "$T" 2>&1)" "root 644"
check "the tombstone count of $S grew by one" "$(tombs)" "$((WANT + 1))"
nginx -t >/dev/null 2>&1 && ok "nginx still accepts its configuration" || bad "nginx -t fails"
finish
EOF
TP=$(ssh dellwp "sudo -n sh -c 'cat \$(ls -1 $TOMB/$SITE.*.json | tail -n 1)'" </dev/null 2>/dev/null | python3 -I -c 'import json,sys; r = json.load(sys.stdin); print(r["port"], r["quota_id"])' 2>&1)
[ "$TP" = "$PORT $QID" ]; bump $? "the tombstone keeps the port and the quota id of the record ($PORT $QID; the tombstone says: $TP)"
OUT=$(ctl check-name name=$SITE); [ "$(printf '%s' "$OUT" | jfield ok)" = True ]; bump $? "the name $SITE is free again (check-name answers ok; nothing was created)"
ssh dellwp "sudo -n $BIN/wp-site-provision close $SITE" </dev/null >"$TMPD/reclose" 2>&1; RC=$?
[ "$RC" = 0 ] && grep -q 'already closed' "$TMPD/reclose"; bump $? "closing it again says so and changes nothing (status $RC)"
A=$(ctl audit-tail lines=200 | python3 -I -c 'import json,sys; [print(l.get("verb", "-"), l.get("phase", "-")) for l in json.loads(sys.stdin.read()).get("lines", [])]' 2>&1)
for v in set ensure-state delete; do printf '%s\n' "$A" | grep -q "^$v intent\$"; bump $? "the audit has the intent line of $v, written before it ran"; done
printf '%s\n' "$A" | grep -q "^ensure-state refused\$"; bump $? "the audit has the refused ensure-state requests"
printf '%s\n' "$A" | grep -q "^delete result\$"; bump $? "the audit has the delete's result line"

summary
echo "   $SITE is deleted and its tombstone is in the registry (the archive stays on dellwp for 14 days). Nothing was committed or edited in the repo."
exit "$FAIL"
