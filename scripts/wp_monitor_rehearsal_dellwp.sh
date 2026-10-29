#!/bin/bash
# Rehearses that the MONITORS and TOOLS on dellwp find a site through the registry (wp-site-provision rows), from the ThinkPad:   bash scripts/wp_monitor_rehearsal_dellwp.sh [options]
# It works on a site that exists only in the registry, has WordPress installed and is not stopped: the one scripts/wp_provision_rehearsal_dellwp.sh --skip-parity --site NAME leaves standing. Nothing is deleted, and every
# change is put back. ntfy-notify is replaced by a stand-in for the run (PATH), so the messages are captured and read here, and nothing reaches your phone. Steps:
#   0. preflight   the tools on the VM are the ones of this branch (they call rows); the site has a record, is whole, and `wp-site-provision rows` lists it with its state, quota ids and policy; no leftovers from an earlier run
#   1. backup      wp-backup-check with a limit of 0 hours names the site as stale; with the normal limit it says the backup is fresh again
#   2. policy      update_policy is set to freeze (wp-ctl set): wp-site-update reads it from the record and skips the site; it is set back, and rows shows the old value
#   3. status      wp-sites-status lists the site, and scripts/wp_sites_status.py does not call it a stray
#   4. disk        the site's quota is filled to 90% as the php uid (a fallocate file): wp-disk-check, finding the site through rows alone, warns about it; the file is removed and it says "back to"
#   5. broken      a record that is not JSON is put into the registry for a few seconds: rows fails, wp-backup-check, wp-restore-drill, wp-sites-status and wp-site-update stop with status 2 and say why,
#                  wp-disk-check tells (once) that the registry is unreadable and tells again when it reads; the record is removed
# It makes no commit and edits nothing in the repo. Options:  --site NAME (default provtest2)  --yes (do not ask before starting)
# For a few seconds in step 5 every reader of the registry on dellwp fails, a real timer or request that fires in that window sees the broken record (the VM has no site that matters). It stops at the first step that
# fails badly. What it does not prove: the timers firing, real ntfy delivery, the restore drill's work (only that it refuses to start on a broken registry), or a site with friends' content. Paste the whole output.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SITE=provtest2; YES=no
usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
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
read -r DOMAIN BIN MOUNT LIB BDIR STATE_DIR OFFSET PHPUID < <(python3 -I - "$REPO" <<'PY'
import sys, yaml
repo = sys.argv[1]
h = {**yaml.safe_load(open(repo + "/vars/wp.yml")), **yaml.safe_load(open(repo + "/host_vars/dellwp/vars.yml"))}
m = h["wp_data_mount"]
def r(v):
    out = str(v).replace("{{ wp_data_mount }}", m)
    if "{{" in out:
        sys.exit("cannot resolve %r" % v)
    return out
print(h["wp_domain"], h["wp_tools_bin"], m, h["wp_tools_lib"], r(h["wp_backup_dir"]), h["wp_disk_state_dir"], h["wp_backup_quota_offset"], h["wp_php_uid"])
PY
) || exit 2
[ -n "${PHPUID:-}" ] || exit 2
SITES=$MOUNT/sites; BROKEN=zzbroken
FAIL=0; CLEANED=no
TMPD=$(mktemp -d) || exit 2

# ---- the VM side: a small library of checks, and a helper that runs a snippet as root on dellwp with it. ntfy-notify is a stand-in that appends what it was told to $R/msgs.
read -r -d '' REMOTE_LIB <<'LIB'
set -u
FAIL=0; DIR=$SITES/$S; R=/run/wp-monitor-rehearsal; FILL=$DIR/html/wp-content/.monitor-rehearsal-fill
ok() { echo "ok: $*"; }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
check() { if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1 (wanted '$3', got '$2')"; fi; }
finish() { [ "$FAIL" = 0 ] && echo "   (all ok)"; [ "$FAIL" -gt 100 ] && FAIL=100; exit "$FAIL"; }
mkdir -p "$R/bin"
[ -x "$R/bin/ntfy-notify" ] || { printf '#!/bin/bash\nprintf "%%s\\n" "$*" >> /run/wp-monitor-rehearsal/msgs\n' > "$R/bin/ntfy-notify"; chmod 755 "$R/bin/ntfy-notify"; }
export PATH="$R/bin:$PATH"
forget() { : > "$R/msgs"; }
told() { grep -c -F -- "$1" "$R/msgs" 2>/dev/null || true; }
shown() { sed 's/^/   | /' "$R/msgs" 2>/dev/null; }
rowline() { "$BIN/wp-site-provision" rows </dev/null | awk -v s="$S" '$1 == s'; }
LIB
vmtest() { { printf '%s\n' "$REMOTE_LIB"; cat; } | ssh dellwp "sudo -n env S=$SITE DOMAIN=$DOMAIN BIN=$BIN SITES=$SITES LIB=$LIB BDIR=$BDIR STATE_DIR=$STATE_DIR OFFSET=$OFFSET PHPUID=$PHPUID BROKEN=$BROKEN WANT=${WANT:-} bash -s"; }
vmrun() { vmtest; local rc=$?; FAIL=$((FAIL + rc)); return 0; }
cleanup_vm() {  # whatever happens: the stand-in, the throwaway record, the fill file and the alert state of this run are gone
  ssh dellwp "sudo -n env S=$SITE SITES=$SITES STATE_DIR=$STATE_DIR BROKEN=$BROKEN bash -s" </dev/null >"$TMPD/cleanup" 2>&1 <<'EOF'
rm -rf "$SITES/$BROKEN" /run/wp-monitor-rehearsal "$SITES/$S/html/wp-content/.monitor-rehearsal-fill"
rm -f "$STATE_DIR/backup-stale-$S.flag" "$STATE_DIR/site-$S.level" "$STATE_DIR/registry-unreadable.flag"
[ ! -e "$SITES/$BROKEN" ] && [ ! -e /run/wp-monitor-rehearsal ] && [ ! -e "$SITES/$S/html/wp-content/.monitor-rehearsal-fill" ] && echo cleaned
EOF
  CLEANED=$(grep -q '^cleaned$' "$TMPD/cleanup" && echo yes || echo no)
}
trap 'rm -rf "$TMPD"' EXIT      # until the run is confirmed nothing has touched the VM, so there is nothing to clean up there

# ---- the ThinkPad side
say() { echo; echo "== $*"; }
bump() { local rc=$1; shift; if [ "$rc" != 0 ]; then echo "FAIL: $* (exit $rc)"; FAIL=$((FAIL+1)); return 1; fi; echo "ok: $*"; return 0; }
yn() { local a; read -r -p "$1 [y/N] " a; [ "$a" = y ]; }
summary() { echo; echo "== summary"; [ "$FAIL" = 0 ] && echo "All checks passed." || echo "$FAIL check(s) failed."; }
fatal() { echo "FAIL: $*"; FAIL=$((FAIL+1)); cleanup_vm; echo "cleanup on the VM: $CLEANED"; summary; exit "$FAIL"; }
ctl() { ssh dellwp "sudo -n $BIN/wp-ctl $*" </dev/null; }
jfield() { python3 -I -c 'import json,sys; v = json.loads(sys.stdin.read()).get(sys.argv[1]); print("" if v is None else v)' "$1"; }
waitjob() {
  local t=0 st
  while [ "$t" -lt "$2" ]; do
    st=$(ctl job-status job="$1"); if [ "$(printf '%s' "$st" | jfield state)" = done ]; then
      ctl job-log job="$1" | python3 -I -c 'import json,sys; print(json.loads(sys.stdin.read()).get("data", "")[-1000:])' | sed 's/^/   | /'
      return "$(printf '%s' "$st" | jfield rc)"
    fi
    sleep 5; t=$((t+5))
  done
  return 99
}
job() {         # $1 = what it is, $2 = the wp-ctl request, $3 = seconds to wait
  local out j
  out=$(ctl $2); echo "   $out"
  j=$(printf '%s' "$out" | jfield job); [ -n "$j" ] || { bump 1 "$1: no job was started"; return 1; }
  waitjob "$j" "$3"; local rc=$?
  [ "$rc" = 0 ]; bump $? "$1: the job ended with status $rc (wanted 0)"
}

echo "Monitor rehearsal on dellwp ($DOMAIN). Site: $SITE (nothing is deleted; ntfy is a stand-in for the run, so nothing reaches your phone). Step 5 breaks the registry for a few seconds."
[ "$YES" = yes ] || yn "Go on?" || { echo "Nothing was done."; exit 1; }
trap '[ "$CLEANED" = yes ] || cleanup_vm; rm -rf "$TMPD"' EXIT

say "0. preflight"
ssh -o BatchMode=yes dellwp true </dev/null 2>/dev/null; bump $? "ssh to dellwp works without a password" || fatal "cannot ssh to dellwp"
ssh dellwp 'sudo -n true' </dev/null 2>/dev/null; bump $? "sudo -n works on dellwp" || fatal "sudo -n does not work"
vmrun <<'EOF'
for x in wp-ctl wp-site-provision wp-backup-check wp-restore-drill wp-site-update wp-sites-status wp-disk-check; do [ -x "$BIN/$x" ] && ok "$x is installed" || bad "$BIN/$x is missing: run dellwp/wp_sites_dellwp.yml --tags wp_tools,wp_ctl"; done
grep -q 'site_rows()' "$LIB/common.sh" && ok "the deployed common.sh reads the registry (site_rows)" || bad "the deployed $LIB/common.sh has no site_rows: run the tools play (dellwp/wp_sites_dellwp.yml --tags wp_tools,wp_ctl)"
grep -q 'def rows' "$LIB/wp_site_provision.py" && ok "the deployed provisioning tool has rows" || bad "the deployed wp_site_provision.py has no rows: run the tools play"
grep -q 'site_rows' "$BIN/wp-backup-check" && ok "the backup check asks the registry" || bad "$BIN/wp-backup-check still has its own site list: run the tools play"
grep -q 'PROVISION' "$BIN/wp-disk-check" && ok "the disk check asks the registry" || bad "$BIN/wp-disk-check still has its own site list: run the tools play"
[ -f "$DIR/site.json" ] && [ ! -L "$DIR/site.json" ] && ok "$S has a registry record" || bad "$S has no registry record: run scripts/wp_provision_rehearsal_dellwp.sh --skip-parity --site $S first"
[ -e "$DIR/stopped.flag" ] && bad "$S is stopped" || ok "$S is not stopped"
[ -e "$DIR/freeze" ] || [ -e "$(sed -n "s/^readonly WP_FREEZE='\(.*\)'/\1/p" "$LIB/common.sh" | head -n 1)" ] && bad "a freeze file exists: the checks would skip the site" || ok "no freeze file"
check "WordPress is installed (else the backup check skips the site as waiting for an import)" "$("$BIN/wp-site-cli" "$S" core is-installed >/dev/null 2>&1 </dev/null && echo yes || echo no)" yes
[ -e "$SITES/$BROKEN" ] || [ -e "$R/msgs" -a -s "$R/msgs" ] && bad "leftovers of an earlier run ($SITES/$BROKEN or $R)" || ok "no leftovers of an earlier run"
row=$(rowline)
[ -n "$row" ] && ok "rows lists $S: $row" || bad "rows does not list $S"
set -- $row
check "rows: its state" "${2:-}" "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["state"])' "$DIR/site.json")"
check "rows: the backup project id is the quota id plus $OFFSET" "${5:-0}" "$(( ${3:-0} + OFFSET ))"
printf '%s %s %s\n' "${3:-}" "${4:-}" "${7:-}" > "$R/row"
finish
EOF
[ "$FAIL" = 0 ] || fatal "the preflight found problems; fix them first"
read -r QID QMB POLICY < <(ssh dellwp "sudo -n cat /run/wp-monitor-rehearsal/row" </dev/null) || fatal "cannot read the row back"
echo "   quota id $QID, quota $QMB MB, update policy $POLICY"
[ "$POLICY" != freeze ] || fatal "the site's policy is freeze already: the policy step would prove nothing"

say "1. backup check finds the site"
WANT=$STATE_DIR vmrun <<'EOF'
forget; WP_BACKUP_STALE_HOURS=0 "$BIN/wp-backup-check" </dev/null; check "wp-backup-check with a 0-hour limit exits 0" "$?" 0; shown
check "it told once that $S is stale" "$(told "site $S has no backup newer than 0 hours")" 1
[ -f "$STATE_DIR/backup-stale-$S.flag" ] && ok "the once-only flag is up" || bad "no flag file for $S"
forget; "$BIN/wp-backup-check" </dev/null; shown
check "with the normal limit it tells that $S is fresh again" "$(told "site $S has a fresh backup again")" 1
[ -f "$STATE_DIR/backup-stale-$S.flag" ] && bad "the flag is still up" || ok "the flag is gone"
finish
EOF

say "2. update policy comes from the record"
job "set update_policy=freeze" "set site=$SITE key=update_policy value=freeze req=mon-$SITE-freeze" 600 || fatal "set update_policy failed"
WANT=freeze vmrun <<'EOF'
check "rows shows the policy" "$(rowline | awk '{print $7}')" freeze
out=$("$BIN/wp-site-update" "$S" </dev/null 2>&1); rc=$?
echo "   | $out"
check "wp-site-update exits 0" "$rc" 0
case $out in *"has update policy freeze; skipped"*) ok "it read freeze from the record and skipped" ;; *) bad "it did not say the site is on freeze" ;; esac
finish
EOF
job "set update_policy back to $POLICY" "set site=$SITE key=update_policy value=$POLICY" 600 || fatal "setting the policy back failed"
WANT=$POLICY vmrun <<'EOF'
check "rows shows the old policy again" "$(rowline | awk '{print $7}')" "$WANT"
finish
EOF

say "3. status lists the site"
ssh dellwp "sudo -n $BIN/wp-sites-status" </dev/null > "$TMPD/status.tsv" 2>"$TMPD/status.err"; bump $? "wp-sites-status on the VM exits 0"
awk -F'\t' -v s="$SITE" '$1 == s {f = 1; print "   " $1 "  deployed=" $2 "  actual=" $3 "  containers=" $5 "  quota=" $10 "%"} END {exit !f}' "$TMPD/status.tsv"; bump $? "the host's report has a line for $SITE"
python3 -I scripts/wp_sites_status.py --file "$TMPD/status.tsv" > "$TMPD/status.out" 2>&1; RC=$?
sed 's/^/   | /' "$TMPD/status.out"
grep -q "^ *$SITE: .*does not declare" "$TMPD/status.out"; [ $? -ne 0 ]; bump $? "the ThinkPad half does not call $SITE a stray (it exited $RC; attention items other than a stray are not a failure here)"

say "4. disk check finds the quota"
WANT=$QMB vmrun <<'EOF'
used_kb() { repquota -P -n "$MOUNT" </dev/null 2>/dev/null | awk -v id="#$QID" '$1 == id {print $3}'; }
MOUNT=$(dirname "$SITES"); QID=$(rowline | awk '{print $3}')
u=$(used_kb); [ -n "$u" ] && ok "the quota project $QID reports $u KiB used" || bad "repquota does not report project $QID"
add=$(( WANT * 1024 * 90 / 100 - ${u:-0} ))
[ "$add" -gt 0 ] || { bad "the site is above 90% already"; finish; }
setpriv --reuid "$PHPUID" --regid "$PHPUID" --clear-groups fallocate -l "${add}K" "$FILL" </dev/null 2>&1 && sync && ok "filled ${add} KiB as the php uid" || bad "could not fill the quota as the php uid"
repquota -P -n "$MOUNT" </dev/null | grep "^#$QID"
forget; "$BIN/wp-disk-check" </dev/null; check "wp-disk-check exits 0" "$?" 0; shown
check "it told about $S's quota, found through the registry" "$(told "site $S has used")" 1
case $(cat "$STATE_DIR/site-$S.level" 2>/dev/null) in warn|crit) ok "the level file says $(cat "$STATE_DIR/site-$S.level")" ;; *) bad "no warn or crit level for $S" ;; esac
rm -f "$FILL"; sync
forget; "$BIN/wp-disk-check" </dev/null; shown
check "after the file is removed it tells that $S is back" "$(told "site $S has used")" 1
[ -e "$STATE_DIR/site-$S.level" ] && bad "the level file is still there" || ok "the level file is gone"
finish
EOF

say "5. a registry that cannot be read"
vmrun <<'EOF'
mkdir -p "$SITES/$BROKEN" && printf '{ not json' > "$SITES/$BROKEN/site.json" && ok "a broken record is in the registry (for a few seconds)" || bad "could not write the broken record"
"$BIN/wp-site-provision" rows </dev/null >"$R/out" 2>&1; rc=$?
check "wp-site-provision rows fails" "$rc" 1
sed 's/^/   | /' "$R/out"
for t in wp-backup-check wp-restore-drill wp-sites-status; do
  "$BIN/$t" </dev/null >"$R/out" 2>&1; rc=$?
  check "$t stops with status 2" "$rc" 2
  grep -q 'cannot read the site registry' "$R/out" && ok "$t says why" || bad "$t does not say the registry cannot be read: $(head -c 200 "$R/out")"
done
"$BIN/wp-site-update" "$S" </dev/null >"$R/out" 2>&1; rc=$?
check "wp-site-update stops with status 2" "$rc" 2
grep -q 'cannot read the site registry' "$R/out" && ok "wp-site-update says why" || bad "wp-site-update does not say why: $(head -c 200 "$R/out")"
forget; "$BIN/wp-disk-check" </dev/null; check "wp-disk-check exits 0" "$?" 0; shown
check "it told that the registry cannot be read" "$(told "wp-site-provision rows failed")" 1
forget; "$BIN/wp-disk-check" </dev/null
check "a second run says nothing new (once only)" "$(told "wp-site-provision rows failed")" 0
rm -rf "$SITES/$BROKEN"; [ ! -e "$SITES/$BROKEN" ] && ok "the broken record is removed" || bad "the broken record is still there"
forget; "$BIN/wp-disk-check" </dev/null; shown
check "it tells that the registry reads again" "$(told "the site registry reads again")" 1
[ -e "$STATE_DIR/registry-unreadable.flag" ] && bad "the flag is still up" || ok "the flag is gone"
check "rows lists $S again" "$(rowline | awk '{print $1}')" "$S"
finish
EOF

say "cleanup"
cleanup_vm; bump "$([ "$CLEANED" = yes ] && echo 0 || echo 1)" "the stand-in, the throwaway record, the fill file and this run's alert state are gone"
summary
echo "   $SITE is untouched (same policy, nothing filled). Next: scripts/wp_state_rehearsal_dellwp.sh --site $SITE deletes it, or keep it for more work. Nothing was committed or edited in the repo."
exit "$FAIL"
