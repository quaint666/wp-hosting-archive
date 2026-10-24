#!/bin/bash
# Rehearses wp-site-recover on one WordPress site on dellwp against the real thing, end to end:   bash scripts/wp_recover_rehearsal_dellwp.sh wptest [--core]
#   1. a marker is written (a WordPress option and a file in uploads) and backed up; then the marker is CHANGED (the option, and a second file); the site is recovered from that
#      snapshot: the option and the file must be back as they were, the second file gone, the maintenance flag and the undo directory gone, and the home page and login must answer
#   2. during step 1 the site must have answered 503 while it ran (a request loop in the background) - nginx's maintenance flag, php stopped, then the site back to 200
#   3. a recover is made to FAIL half way: a copy of the snapshot whose dump passes every check but contains a broken statement is recovered from. The tool must roll back: the
#      state from before this step (the option "after", the second file) must be exactly there, the flag and the undo directory gone, the site answering
#   4. with --core: a copy of the snapshot whose MANIFEST says an older core (the previous patch version) is recovered from, so the real core download and its checksum check run; then
#      the real snapshot is recovered from again, which puts core back to the version the image carries
# Needs sudo without a password. It leaves the snapshot it made (a real backup) and removes the copies it crafted, on every exit path. The site is offline for about half a minute
# per recover. It changes one option (recover_rehearsal) and two files under uploads/, and removes them at the end. What it does not prove: that the recover works on a site with
# real content (run it on a copy first), or that the update job calls it correctly (that job has its own rehearsal).
set -u
SITE=${1:-}; CORE=no
[ -n "$SITE" ] || { echo "usage: bash scripts/wp_recover_rehearsal_dellwp.sh <site> [--core]"; exit 2; }
[ "${2:-}" = "--core" ] && CORE=yes
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
read -r BDIR BIN DOMAIN SITES PORT < <(python3 -I -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); print(d["wp_data_mount"] + "/backups", d["wp_tools_bin"], d["wp_domain"], d["wp_data_mount"] + "/sites", d["wp_nginx_listen_port"])' "$REPO/host_vars/dellwp/vars.yml") || exit 1

ssh dellwp "sudo -n env SITE=$SITE CORE=$CORE BDIR=$BDIR BIN=$BIN DOMAIN=$DOMAIN SITES=$SITES PORT=$PORT bash -s" <<'REMOTE'
set -u
FAIL=0; DIR=$SITES/$SITE; HOST=$SITE.$DOMAIN; UP=$DIR/html/wp-content/uploads
for x in "$BIN/wp-site-backup" "$BIN/wp-site-recover" "$BIN/wp-site-cli"; do [ -x "$x" ] || { echo "$x is missing: run dellwp/wp_sites_dellwp.yml"; exit 1; }; done
systemctl is-active "wp-$SITE.service" </dev/null >/dev/null || { echo "wp-$SITE.service is not active"; exit 1; }
[ ! -e "$DIR/.recover-undo" ] && [ ! -e "$DIR/maintenance.flag" ] || { echo "a leftover $DIR/.recover-undo or maintenance.flag exists: look at it first"; exit 1; }
CRAFTED=()
cleanup() { for c in "${CRAFTED[@]}"; do rm -rf -- "$BDIR/$SITE/$c"; done; rm -f -- "$UP/rehearsal-before.txt" "$UP/rehearsal-after.txt"; "$BIN/wp-site-cli" "$SITE" option delete recover_rehearsal </dev/null >/dev/null 2>&1; }
trap cleanup EXIT
wpc() { "$BIN/wp-site-cli" "$SITE" "$@" </dev/null; }
http() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Host: $HOST" -H 'X-Forwarded-Proto: https' "http://127.0.0.1:$PORT$1" </dev/null; }
newest() { find "$BDIR/$SITE" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9]{8}T[0-9]{6}Z' -printf '%f\n' </dev/null | LC_ALL=C sort | tail -n 1; }
check() { if [ "$2" = "$3" ]; then echo "ok: $1 ($3)"; else echo "FAIL: $1 (wanted '$3', got '$2')"; FAIL=$((FAIL+1)); fi; }
clean() { [ ! -e "$DIR/maintenance.flag" ] && [ ! -e "$DIR/.recover-undo" ] && echo "ok: the maintenance flag and the undo directory are gone" || { echo "FAIL: flag or undo directory left behind"; ls -la "$DIR" | grep -E 'maintenance|recover'; FAIL=$((FAIL+1)); }; }

echo "== 1. a marker, a backup, a change, a recover"
wpc option update recover_rehearsal before >/dev/null; echo before > "$UP/rehearsal-before.txt"
systemctl start "wp-backup@$SITE.service" </dev/null || { echo "FAIL: the backup failed"; exit 1; }
S=$(newest); echo "   snapshot $S"
wpc option update recover_rehearsal after >/dev/null; echo after > "$UP/rehearsal-after.txt"; rm -f "$UP/rehearsal-before.txt"
check "the marker was changed" "$(wpc option get recover_rehearsal)" after
rm -f /tmp/recover-rehearsal.codes
( for _ in $(seq 1 120); do http / >> /tmp/recover-rehearsal.codes; echo >> /tmp/recover-rehearsal.codes; sleep 0.5; done ) & POLL=$!
T0=$(date +%s); "$BIN/wp-site-recover" "$SITE" "$S" --yes </dev/null; RC=$?
echo "   recover took $(( $(date +%s) - T0 )) s, exit $RC"; kill "$POLL" 2>/dev/null; wait "$POLL" 2>/dev/null
check "recover exit code" "$RC" 0
check "the option is back" "$(wpc option get recover_rehearsal)" before
[ -f "$UP/rehearsal-before.txt" ] && [ ! -e "$UP/rehearsal-after.txt" ] && echo "ok: the file from before is back and the later one is gone" || { echo "FAIL: files not as the snapshot had them"; FAIL=$((FAIL+1)); }
clean
CODES=$(tr '\n' ' ' < /tmp/recover-rehearsal.codes | tr -s ' ')
case "$CODES" in *503*) echo "ok: visitors got 503 while it ran (codes seen: $(echo "$CODES" | tr ' ' '\n' | sort | uniq -c | tr '\n' ' '))" ;; *) echo "FAIL: no 503 was seen (the poll may have missed the window, or the flag does not work): $CODES"; FAIL=$((FAIL+1)) ;; esac
check "the home page answers" "$(http /)" 200
check "the login page answers" "$(http /wp-login.php)" 200
check "php container is up" "$(docker inspect -f '{{.State.Status}}' "wp_${SITE//-/_}_php" </dev/null)" running

echo "== 2. a recover that fails half way must roll back"
wpc option update recover_rehearsal after >/dev/null; echo after > "$UP/rehearsal-after.txt"
BAD=$(date -u -d "+1 year" +%Y%m%dT%H%M%SZ); CRAFTED+=("$BAD")
cp -a "$BDIR/$SITE/$S" "$BDIR/$SITE/$BAD" || { echo "FAIL: could not craft the broken copy"; exit 1; }
# a statement MariaDB rejects, placed before the completion marker so every pre-check passes
gzip -dc "$BDIR/$SITE/$S/db.sql.gz" | sed '$d' > /tmp/recover-rehearsal.sql; printf 'THIS IS NOT SQL;\n-- Dump completed on 2099-01-01\n' >> /tmp/recover-rehearsal.sql
gzip -c /tmp/recover-rehearsal.sql > "$BDIR/$SITE/$BAD/db.sql.gz"; rm -f /tmp/recover-rehearsal.sql
"$BIN/wp-site-recover" "$SITE" "$BAD" --yes </dev/null; RC=$?
check "recover exit code (it must fail)" "$RC" 1
check "the option is as before the failed recover" "$(wpc option get recover_rehearsal)" after
[ -f "$UP/rehearsal-after.txt" ] && echo "ok: the file from before the failed recover is there" || { echo "FAIL: the file was lost"; FAIL=$((FAIL+1)); }
clean
check "the home page answers after the rollback" "$(http /)" 200
rm -rf -- "$BDIR/$SITE/$BAD"

if [ "$CORE" = yes ]; then
  echo "== 3. core: back one patch version from wordpress.org, then forward again"
  NOW=$(sed -n "s/^\$wp_version = '\(.*\)';.*/\1/p" "$DIR/html/wp-includes/version.php" | head -n 1)
  OLD=$(echo "$NOW" | awk -F. '{ if ($3 > 0) printf "%s.%s.%d", $1, $2, $3 - 1; }')
  if [ -z "$OLD" ]; then echo "   core $NOW has no previous patch version to go back to; skipped"
  else
    OLDS=$(date -u -d "+2 years" +%Y%m%dT%H%M%SZ); CRAFTED+=("$OLDS")
    cp -a "$BDIR/$SITE/$S" "$BDIR/$SITE/$OLDS" && sed -i "s/^core=.*/core=$OLD/" "$BDIR/$SITE/$OLDS/MANIFEST"
    "$BIN/wp-site-recover" "$SITE" "$OLDS" --yes </dev/null; RC=$?
    check "going back to $OLD: exit code" "$RC" 0
    check "core on disk" "$(sed -n "s/^\$wp_version = '\(.*\)';.*/\1/p" "$DIR/html/wp-includes/version.php" | head -n 1)" "$OLD"
    "$BIN/wp-site-recover" "$SITE" "$S" --yes </dev/null; RC=$?
    check "going forward to $NOW again: exit code" "$RC" 0
    check "core on disk" "$(sed -n "s/^\$wp_version = '\(.*\)';.*/\1/p" "$DIR/html/wp-includes/version.php" | head -n 1)" "$NOW"
    clean
    [ ! -e "$DIR/html/wp-config-sample.php" ] && echo "ok: no wp-config-sample.php left" || { echo "FAIL: wp-config-sample.php is back"; FAIL=$((FAIL+1)); }
    check "the home page answers" "$(http /)" 200
  fi
fi
echo
[ "$FAIL" = 0 ] && echo "All checks passed. The snapshot $S stays as a real backup." || echo "$FAIL check(s) failed"
exit "$FAIL"
REMOTE
