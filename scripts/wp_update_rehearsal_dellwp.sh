#!/bin/bash
# Rehearses the nightly update job of one WordPress site on dellwp against the real thing, end to end:   bash scripts/wp_update_rehearsal_dellwp.sh wptest
#   1. a small plugin (classic-editor) is installed at an old version from wordpress.org and activated, so there is something to update; --check must list it and change nothing
#   2. the good path, by the real unit (systemctl start wp-update@<site>.service): the site answers 503 on the visitor port and 200 on the probe port while it runs; the plugin ends
#      at the newest version; a snapshot tagged pre-update exists; .update-last says "updated"; the flag is gone; the public port answers 200   (ntfy: "was updated and checked")
#   3. the bad path: the old version is installed again and the job is run with the probe forced to fail (WP_UPDATE_FAIL_PROBE=1): the job must exit non-zero, wp-site-recover must
#      put the plugin back to the OLD version, the flag must be gone, the site must answer 200, and .update-failed must exist   (ntfy: "ROLLED BACK")
#   4. the same set is not retried by the timer's path (no --apply): the job says so and changes nothing; then --apply, without the forced failure, updates it for real
#      (ntfy: "was updated and checked")
# Expect THREE messages on your phone: updated, ROLLED BACK, updated. Needs sudo without a password. It removes the plugin at the end, on every exit path; the pre-update snapshots stay (real
# backups, pruned to the newest two of that kind). The site is offline for about half a minute per run of the job. What it does not prove: that the policies, the notify path or
# the 02:00 timer do what they say (offline tests and the first night do), that a plugin whose update really breaks the site is caught (the forced failure stands in), or that the
# php fatal-error check sees a real fatal error (look at the first night's journal).
set -u
SITE=${1:-}
[ -n "$SITE" ] || { echo "usage: bash scripts/wp_update_rehearsal_dellwp.sh <site>"; exit 2; }
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
read -r BDIR BIN DOMAIN SITES LISTEN PROBE < <(python3 -I -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); print(d["wp_data_mount"] + "/backups", d["wp_tools_bin"], d["wp_domain"], d["wp_data_mount"] + "/sites", d["wp_nginx_listen_port"], d["wp_nginx_probe_port"])' "$REPO/host_vars/dellwp/vars.yml") || exit 1

ssh dellwp "sudo -n env SITE=$SITE BDIR=$BDIR BIN=$BIN DOMAIN=$DOMAIN SITES=$SITES LISTEN=$LISTEN PROBE=$PROBE bash -s" <<'REMOTE'
set -u
FAIL=0; DIR=$SITES/$SITE; HOST=$SITE.$DOMAIN; PLUGIN=classic-editor; OLD=1.6.3
for x in wp-site-update wp-site-ext wp-site-cli wp-site-recover wp-site-backup; do [ -x "$BIN/$x" ] || { echo "$BIN/$x is missing: run dellwp/wp_sites_dellwp.yml"; exit 1; }; done
systemctl cat "wp-update@$SITE.service" </dev/null >/dev/null 2>&1 || { echo "wp-update@$SITE.service is not deployed"; exit 1; }
[ ! -e "$DIR/maintenance.flag" ] && [ ! -e "$DIR/.recover-undo" ] && [ ! -e "$DIR/.update-failed" ] || { echo "a leftover maintenance.flag, .recover-undo or .update-failed in $DIR: look at it first"; exit 1; }
[ ! -d "$DIR/html/wp-content/plugins/$PLUGIN" ] || { echo "$PLUGIN is already installed on $SITE: remove it first, this script owns it"; exit 1; }
cleanup() { "$BIN/wp-site-ext" "$SITE" plugin delete "$PLUGIN" </dev/null >/dev/null 2>&1; rm -f "$DIR/.update-failed" "$DIR/.update-notified"; }
trap cleanup EXIT
wpc() { "$BIN/wp-site-cli" "$SITE" "$@" </dev/null; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Host: $HOST" -H 'X-Forwarded-Proto: https' "http://127.0.0.1:$1/" </dev/null; }
version() { wpc plugin get "$PLUGIN" --field=version 2>/dev/null; }
check() { if [ "$2" = "$3" ]; then echo "ok: $1 ($3)"; else echo "FAIL: $1 (wanted '$3', got '$2')"; FAIL=$((FAIL+1)); fi; }
clean() { [ ! -e "$DIR/maintenance.flag" ] && [ ! -e "$DIR/.recover-undo" ] && echo "ok: the maintenance flag and the undo directory are gone" || { echo "FAIL: flag or undo directory left behind"; ls -la "$DIR" | grep -E 'maintenance|recover'; FAIL=$((FAIL+1)); }; }
lastkey() { sed -n "s/^$1=//p" "$DIR/.update-last" 2>/dev/null; }
# install refuses to change a plugin that is already installed ("already installed", seen 2026-10-24), so the old version goes in over a clean slate
install_old() { "$BIN/wp-site-ext" "$SITE" plugin delete "$PLUGIN" </dev/null >/dev/null 2>&1; "$BIN/wp-site-ext" "$SITE" plugin install "$PLUGIN" --version=$OLD --activate </dev/null; }

echo "== 1. something to update"
install_old || { echo "FAIL: could not install $PLUGIN $OLD"; exit 1; }
check "installed at the old version" "$(version)" "$OLD"
"$BIN/wp-site-update" "$SITE" --check </dev/null | tee /tmp/update-rehearsal.check
grep -q "$PLUGIN:$OLD>" /tmp/update-rehearsal.check && echo "ok: --check lists $PLUGIN" || { echo "FAIL: --check does not list $PLUGIN"; FAIL=$((FAIL+1)); }
check "--check changed nothing" "$(version)" "$OLD"; clean

echo "== 2. the good path, by the unit"
rm -f /tmp/update-rehearsal.codes
( for _ in $(seq 1 160); do echo "$(code "$LISTEN")/$(code "$PROBE")" >> /tmp/update-rehearsal.codes; sleep 0.5; done ) & POLL=$!
T0=$(date +%s); systemctl start "wp-update@$SITE.service" </dev/null; RC=$?
echo "   the unit took $(( $(date +%s) - T0 )) s, exit $RC"; kill "$POLL" 2>/dev/null; wait "$POLL" 2>/dev/null
journalctl -u "wp-update@$SITE.service" --since "@$T0" --no-pager -o cat </dev/null | tail -n 12 | sed 's/^/   | /'
check "the unit succeeded" "$RC" 0
NEW=$(version); [ -n "$NEW" ] && [ "$NEW" != "$OLD" ] && echo "ok: $PLUGIN is now $NEW" || { echo "FAIL: $PLUGIN is still '$NEW'"; FAIL=$((FAIL+1)); }
check ".update-last says updated" "$(lastkey result)" updated; clean
CODES=$(sort /tmp/update-rehearsal.codes | uniq -c | tr '\n' ' ')
grep -q '^503/200$' /tmp/update-rehearsal.codes && echo "ok: visitors got 503 while the probe port answered 200 ($CODES)" || { echo "FAIL: never saw 503 on the visitor port with 200 on the probe port: $CODES"; FAIL=$((FAIL+1)); }
check "the public port answers" "$(code "$LISTEN")" 200
SNAP=$(grep -l '^tag=pre-update$' "$BDIR/$SITE"/*/MANIFEST 2>/dev/null | tail -n 1); [ -n "$SNAP" ] && echo "ok: a pre-update snapshot exists ($(basename "$(dirname "$SNAP")"))" || { echo "FAIL: no snapshot tagged pre-update"; FAIL=$((FAIL+1)); }

echo "== 3. the bad path: the probe is forced to fail, the job must roll back"
install_old >/dev/null || { echo "FAIL: could not install the old version again"; FAIL=$((FAIL+1)); }
check "installed at the old version again" "$(version)" "$OLD"
env WP_UPDATE_FAIL_PROBE=1 "$BIN/wp-site-update" "$SITE" </dev/null; RC=$?
check "the job failed as it should" "$RC" 1
check "the plugin is back at the old version (the rollback worked)" "$(version)" "$OLD"
check ".update-last says rolled-back" "$(lastkey result)" rolled-back
[ -f "$DIR/.update-failed" ] && echo "ok: the failed set is remembered" || { echo "FAIL: no .update-failed"; FAIL=$((FAIL+1)); }
clean; check "the public port answers after the rollback" "$(code "$LISTEN")" 200

echo "== 4. the same set is not retried; --apply does it"
OUT=$("$BIN/wp-site-update" "$SITE" </dev/null); RC=$?; echo "   $OUT"
check "the timer's path exit code" "$RC" 0
echo "$OUT" | grep -q 'not retrying' && echo "ok: it said it is not retrying" || { echo "FAIL: it did not say so"; FAIL=$((FAIL+1)); }
check "nothing changed" "$(version)" "$OLD"
"$BIN/wp-site-update" "$SITE" --apply </dev/null; RC=$?
check "--apply exit code" "$RC" 0
NEW=$(version); [ -n "$NEW" ] && [ "$NEW" != "$OLD" ] && echo "ok: $PLUGIN is now $NEW" || { echo "FAIL: $PLUGIN is still '$NEW'"; FAIL=$((FAIL+1)); }
[ ! -f "$DIR/.update-failed" ] && echo "ok: the failure marker is cleared by a success" || { echo "FAIL: .update-failed is still there"; FAIL=$((FAIL+1)); }
clean; check "the public port answers" "$(code "$LISTEN")" 200
echo
[ "$FAIL" = 0 ] && echo "All checks passed. On your phone, three messages: updated, ROLLED BACK, updated. Tell me what they said." || echo "$FAIL check(s) failed"
exit "$FAIL"
REMOTE
