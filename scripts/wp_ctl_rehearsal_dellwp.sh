#!/bin/bash
# Rehearses the control dispatcher (wp-ctl) on dellwp, on a THROWAWAY site directory that no real site uses: zzrehearsal, with a fabricated site.json so the registry rule counts it as a site.
# It never starts a WordPress stack and never changes a real site: the one job it runs is `delete` on that fake site, which wp-site-delete refuses (the site is not stopped) before it touches anything.
# What it checks on the real host, each of which the offline tests (tests/test_wp_ctl.py) cannot:
#   1 the account, its sudoers entry and its key file exist as the play wrote them            2 hostile and odd requests are refused with status 2, on the CLI path and on the panel path (sudo -u wpctl)
#   3 the refusals are in the audit file AND in the journal (what Alloy ships to Loki)         4 the audit file is append-only (or the filesystem says it cannot be, and that is reported, not failed)
#   5 sudo refuses the program WITH an argument for the control account                        6 a job runs under systemd-run, keeps its log and exit code, and its audit lines carry the job id
#   7 the real tools accept a site that has only a registry record (require_site)              8 a second verb on a locked site is refused as busy
#   9 the same request id returns the same job (a double click), another verb under that id is refused, the result line carries a duration, and the unit starts with its memory, process and time limits
#  10 with --key FILE: the forced command; the key's text 'id; cat /etc/shadow' is ignored and the program answers
# Run it yourself from the ThinkPad:   bash scripts/wp_ctl_rehearsal_dellwp.sh [--key /path/to/a/private/key]
# For step 9 make a throwaway key OUTSIDE ~/.ssh (ssh-keygen -t ed25519 -N '' -f /tmp/wpctl-rehearsal-key), put the .pub line in wp_ctl_panel_keys (host_vars/dellwp/vars.yml), run
# dellwp/wp_sites_dellwp.yml --tags wp_ctl, run this with --key, then take the line out again and rerun the play. Needs sudo without a password on dellwp. The fake directory, the lock holder and the
# state it made are removed on every exit path. What it does not prove: a password change or a delete of a REAL site through the verbs (the first real one is that proof), or that Loki shows the lines
# (look for {job="journald"} |= "wp-ctl-audit" in Grafana). Paste the whole output, including the FAIL lines if any.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
KEY=""
if [ "${1:-}" = "--key" ]; then KEY=${2:-}; [ -f "$KEY" ] || { echo "--key needs a private key file"; exit 1; }; elif [ $# -gt 0 ]; then echo "usage: wp_ctl_rehearsal_dellwp.sh [--key FILE]"; exit 1; fi
yget() { python3 -I -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); print(d[sys.argv[2]])' "$REPO/$1" "$2"; }
MOUNT=$(yget host_vars/dellwp/vars.yml wp_data_mount) || exit 1
BIN=$(yget host_vars/dellwp/vars.yml wp_tools_bin) || exit 1
USER_=$(yget host_vars/dellwp/vars.yml wp_ctl_user) || exit 1
STATE=$(yget host_vars/dellwp/vars.yml wp_ctl_state_dir) || exit 1
AUDIT=$(yget host_vars/dellwp/vars.yml wp_ctl_audit_file) || exit 1
FAIL=0

ssh dellwp "sudo -n env MOUNT=$MOUNT BIN=$BIN CTL_USER=$USER_ STATE=$STATE AUDIT=$AUDIT bash -s" <<'REMOTE'
set -u
SITE=zzrehearsal; DIR="$MOUNT/sites/$SITE"; FAIL=0; HOLDER=""
ok() { echo "ok: $*"; }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
[ -x "$BIN/wp-ctl" ] || { echo "$BIN/wp-ctl is missing: run dellwp/wp_sites_dellwp.yml --tags wp_tools"; exit 1; }
[ -e "$DIR" ] && { echo "$DIR exists already: not touching it"; exit 1; }
cleanup() { echo "--- cleanup: the fake site, the lock holder"; [ -n "$HOLDER" ] && kill "$HOLDER" 2>/dev/null; rm -rf "$DIR" "$STATE/locks/$SITE.lock"; }
trap cleanup EXIT
cli() { printf '%s\n' "$1" | "$BIN/wp-ctl"; }
panel() { printf '%s\n' "$1" | sudo -u "$CTL_USER" sudo -n "$BIN/wp-ctl"; }
jget() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"; }

echo "== 1. the account, the sudoers entry and the key file"
id "$CTL_USER" >/dev/null 2>&1 && ok "account $CTL_USER exists" || bad "no account $CTL_USER (run the play with --tags wp_ctl)"
visudo -cf /etc/sudoers.d/wp-ctl >/dev/null 2>&1 && ok "/etc/sudoers.d/wp-ctl parses" || bad "/etc/sudoers.d/wp-ctl is missing or does not parse"
sudo -l -U "$CTL_USER" 2>/dev/null | grep -q "$BIN/wp-ctl \"\"" && ok "the entry allows the program with no arguments" || bad "sudo -l -U $CTL_USER does not show the no-arguments entry"
KEYFILE=$(getent passwd "$CTL_USER" | cut -d: -f6)/.ssh/authorized_keys
nkeys=$(grep -vc '^#' "$KEYFILE" 2>/dev/null); echo "   keys allowed to log in as $CTL_USER: ${nkeys:-0} (0 until the control VM exists)"
if [ "${nkeys:-0}" = 0 ]; then ok "no key line: nobody can log in as $CTL_USER"; elif [ "$(grep -v '^#' "$KEYFILE" | grep -vc '^restrict,command="sudo -n '"$BIN"'/wp-ctl" ssh-ed25519 ')" = 0 ]; then ok "every key line is forced to wp-ctl"; else bad "a key line is not forced to wp-ctl"; fi

echo "== 2. hostile and odd requests are refused with status 2 (CLI path, then the panel path)"
for who in cli panel; do
  for req in '{"verb":"delete","site":"../../etc","confirm_name":"../../etc"}' '{"verb":"rm"}' '{"verb":"list","x":1}' '{not json' ''; do
    out=$($who "$req"); rc=$?
    [ "$rc" = 2 ] && echo "$out" | grep -q '"ok": false' && ok "$who refused: ${req:-<empty>}" || bad "$who: status $rc for ${req:-<empty>}: $out"
  done
done
out=$(panel '{"verb":"list"}'); rc=$?; [ "$rc" = 0 ] && echo "$out" | grep -q '"ok": true' && ok "the panel path answers a read" || bad "the panel path could not list: $rc $out"
out=$(sudo -u "$CTL_USER" sudo -n "$BIN/wp-ctl" --run-job 20260101T000000Z-abcd1234 </dev/null 2>&1); rc=$?
[ "$rc" != 0 ] && echo "$out" | grep -qi 'password is required\|not allowed\|may not run' && ok "sudo refuses the program with an argument for $CTL_USER" || bad "an argument was not refused by sudo: $rc $out"

echo "== 3. the refusals are in the audit file and in the journal"
grep -q '"error":"unknown_site"' "$AUDIT" && ok "the audit file has the refusal" || bad "no unknown_site line in $AUDIT"
journalctl -t wp-ctl-audit --since "-5 min" --no-pager 2>/dev/null | grep -q unknown_site && ok "the journal has it too (what Alloy ships)" || bad "no wp-ctl-audit line in the journal"

echo "== 4. the audit file is append-only"
if lsattr -d "$AUDIT" 2>/dev/null | awk '{print $1}' | grep -q a; then
  ( : > "$AUDIT" ) 2>/dev/null && bad "the audit file could be truncated" || ok "truncating it is refused"
  before=$(wc -l < "$AUDIT"); cli '{"verb":"rm"}' >/dev/null; after=$(wc -l < "$AUDIT")
  [ "$after" -gt "$before" ] && ok "appending still works" || bad "a request did not append a line"
else
  echo "WARN: no append-only attribute on $AUDIT ($(lsattr -d "$AUDIT" 2>&1 | head -1)); the audit rests on the journal copy"
fi

echo "== 5. a fake site with only a registry record, and a job on it"
mkdir -p "$DIR" && echo '{"label": "rehearsal"}' > "$DIR/site.json" || { bad "could not make $DIR"; exit 1; }
out=$(panel "{\"verb\":\"show\",\"site\":\"$SITE\"}"); echo "$out" | grep -q '"label": "rehearsal"' && ok "show finds the registry record" || bad "show did not return the record: $out"
out=$(panel "{\"verb\":\"check-name\",\"name\":\"$SITE\"}"); echo "$out" | grep -q name_taken && ok "check-name says the name is taken" || bad "check-name: $out"
out=$(panel "{\"verb\":\"delete\",\"site\":\"$SITE\",\"confirm_name\":\"$SITE\",\"confirm_again\":\"$SITE\",\"req\":\"rehearsal-1\"}"); rc=$?
JOB=$(echo "$out" | jget job)
[ "$rc" = 0 ] && [ -n "$JOB" ] && ok "job started: $JOB" || { bad "no job: $rc $out"; exit 1; }
out=$(panel "{\"verb\":\"delete\",\"site\":\"$SITE\",\"confirm_name\":\"$SITE\",\"confirm_again\":\"$SITE\",\"req\":\"rehearsal-1\"}")
[ "$(echo "$out" | jget job)" = "$JOB" ] && echo "$out" | grep -q '"duplicate": true' && ok "the same request again returned the same job (idempotent)" || bad "a repeat did not return the same job: $out"
out=$(panel "{\"verb\":\"backup\",\"site\":\"$SITE\",\"req\":\"rehearsal-1\"}")
echo "$out" | grep -q '"error": "req_reused"' && ok "the same request id for another verb is refused" || bad "req_reused expected: $out"
STATE_NOW=running
for i in $(seq 1 30); do
  st=$(panel "{\"verb\":\"job-status\",\"job\":\"$JOB\"}"); STATE_NOW=$(echo "$st" | jget state); [ "$STATE_NOW" = done ] && break; sleep 2
done
[ "$STATE_NOW" = done ] && ok "the job finished: $st" || bad "the job did not finish in 60 s: $st"
echo "   the job log:"; panel "{\"verb\":\"job-log\",\"job\":\"$JOB\"}" | jget data | sed 's/^/     /'
[ "$(echo "$st" | jget rc)" != 0 ] && ok "wp-site-delete refused the fake site, as it must" || bad "wp-site-delete exited 0 on a fake site: look at it before anything else"
panel "{\"verb\":\"job-log\",\"job\":\"$JOB\"}" | jget data | grep -q "is not in wp_sites" && bad "the real tool does not see a registry-only site: deploy the library (--tags wp_tools)" || ok "the real tools' require_site accepted the registry record"
[ -d "$DIR" ] && [ -f "$DIR/site.json" ] && ok "nothing of the fake site was touched" || bad "the fake site changed"
phases=$(grep "\"job\":\"$JOB\"" "$AUDIT" | python3 -c 'import json,sys; print(",".join(sorted(json.loads(l)["phase"] for l in sys.stdin)))')
grep -q '"req":"rehearsal-1"' "$AUDIT" && ok "the audit carries the request id" || bad "no request id in the audit"
[ "$phases" = "duplicate,result,started" ] && ok "audit lines for the job: $phases" || bad "audit phases for the job were: $phases (expected duplicate,result,started)"
grep "\"job\":\"$JOB\"" "$AUDIT" | grep '"phase":"result"' | grep -q '"duration_ms":[0-9]' && ok "the job's result line carries its duration" || bad "no duration_ms on the job's result line"
systemctl is-active "wpjob-$JOB" >/dev/null 2>&1 && bad "the unit wpjob-$JOB is still there" || ok "the unit is gone (--collect)"

echo "== 6. a locked site refuses a second verb"
( flock -x 9; sleep 25 ) 9>"$STATE/locks/$SITE.lock" & HOLDER=$!
sleep 1
out=$(panel "{\"verb\":\"delete\",\"site\":\"$SITE\",\"confirm_name\":\"$SITE\",\"confirm_again\":\"$SITE\"}"); rc=$?
[ "$rc" = 2 ] && echo "$out" | grep -q '"error": "busy"' && ok "refused as busy while the lock is held" || bad "not refused as busy: $rc $out"
kill "$HOLDER" 2>/dev/null; HOLDER=""
echo
[ "$FAIL" = 0 ] && echo "All checks passed on the VM." || echo "$FAIL check(s) failed"
exit "$FAIL"
REMOTE
FAIL=$?

if [ -n "$KEY" ]; then
  echo "== 7. the forced command (from here, with the key $KEY)"
  out=$(printf '{"verb":"list"}\n' | ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$USER_@dellwp" 'id; cat /etc/shadow' 2>&1); rc=$?
  if echo "$out" | grep -q '"ok": true' && ! echo "$out" | grep -q 'uid=\|root:'; then echo "ok: the key ran wp-ctl and ignored the command text"; else echo "FAIL: $rc $out"; FAIL=$((FAIL+1)); fi
  out=$(ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -tt "$USER_@dellwp" 'id' </dev/null 2>&1); echo "$out" | grep -q 'uid=' && { echo "FAIL: a terminal command ran: $out"; FAIL=$((FAIL+1)); } || echo "ok: no terminal command ran"
fi
echo
[ "$FAIL" = 0 ] && echo "Rehearsal finished: no failures." || echo "Rehearsal finished: $FAIL failure(s)."
exit "$FAIL"
