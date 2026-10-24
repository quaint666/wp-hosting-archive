#!/bin/bash
# Rehearses the WordPress disk alert (wp-disk-check) on dellwp by causing the condition, on a THROWAWAY quota project that no site uses: a directory with project id 4000001
# and a 5 MB limit, filled as the php uid (root may exceed a quota). The check is pointed at that project alone through WP_DISK_SITES, so no real site is touched. Three runs:
#   about 85% used -> ntfy "dellwp site rehearsal ... is at 85% (warn: ...)"       about 97% -> ntfy "... is at 97% (crit: ...)"       emptied -> ntfy "... is back to 0%"
# Run it yourself from the ThinkPad:   bash scripts/wp_disk_alert_rehearsal_dellwp.sh
# Needs sudo without a password. The directory, the quota and the state file are removed on every exit path. What it does not prove: that the messages reach your phone, or
# that the timer fires (the verifier checks the timer is enabled). Tell me what the three messages said.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
yget() { python3 -I -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); print(d[sys.argv[2]])' "$REPO/$1" "$2"; }
MOUNT=$(yget host_vars/dellwp/vars.yml wp_data_mount) || exit 1
STATE_DIR=$(yget host_vars/dellwp/vars.yml wp_disk_state_dir) || exit 1
BIN=$(yget host_vars/dellwp/vars.yml wp_tools_bin) || exit 1
PHPUID=$(yget vars/wp.yml wp_php_uid) || exit 1

ssh dellwp "sudo -n env MOUNT=$MOUNT STATE_DIR=$STATE_DIR BIN=$BIN PHPUID=$PHPUID bash -s" <<'REMOTE'
set -u
DIR="$MOUNT/sites/.disk-rehearsal"; PID=4000001; FAIL=0
[ -x "$BIN/wp-disk-check" ] || { echo "$BIN/wp-disk-check is missing: run dellwp/wp_sites_dellwp.yml"; exit 1; }
command -v ntfy-notify >/dev/null || { echo "ntfy-notify is missing: the check could not send anything"; exit 1; }
[ -e "$DIR" ] && { echo "$DIR exists already: not touching it"; exit 1; }
cleanup() { echo "--- cleanup: removing $DIR, its quota and its state file"; rm -rf "$DIR"; setquota -P $PID 0 0 0 0 "$MOUNT" 2>/dev/null; rm -f "$STATE_DIR/site-rehearsal.level"; }
trap cleanup EXIT
run() { WP_DISK_SITES="rehearsal:$PID:5" "$BIN/wp-disk-check" </dev/null; echo "   level file: $(cat "$STATE_DIR/site-rehearsal.level" 2>/dev/null || echo none)"; }
fill() { setpriv --reuid "$PHPUID" --regid "$PHPUID" --clear-groups dd if=/dev/zero of="$DIR/$1" bs=1K count="$2" 2>/dev/null </dev/null; sync; }
mkdir -p "$DIR" && chown "$PHPUID:$PHPUID" "$DIR" && chattr +P -p $PID "$DIR" && setquota -P $PID 0 5120 0 0 "$MOUNT" || { echo "could not set up the throwaway quota"; exit 1; }

echo "== 0%: no message"; run
echo "== about 85% (4400 of 5120 KiB): expect a warning"; fill a 4400; repquota -P -n "$MOUNT" </dev/null | grep "^#$PID" ; run
[ "$(cat "$STATE_DIR/site-rehearsal.level" 2>/dev/null)" = warn ] && echo "ok: warn" || { echo "FAIL: expected warn"; FAIL=$((FAIL+1)); }
echo "== about 97% (5000 of 5120 KiB): expect a critical"; fill b 600; repquota -P -n "$MOUNT" </dev/null | grep "^#$PID"; run
[ "$(cat "$STATE_DIR/site-rehearsal.level" 2>/dev/null)" = crit ] && echo "ok: crit" || { echo "FAIL: expected crit"; FAIL=$((FAIL+1)); }
echo "== emptied: expect 'back to'"; rm -f "$DIR/a" "$DIR/b"; sync; run
[ -e "$STATE_DIR/site-rehearsal.level" ] && { echo "FAIL: the level file should be gone"; FAIL=$((FAIL+1)); } || echo "ok: back to normal, level file gone"
echo
[ "$FAIL" = 0 ] && echo "On your phone: three messages, warn, crit, back to. Tell me what they said." || echo "$FAIL check(s) failed"
exit "$FAIL"
REMOTE
