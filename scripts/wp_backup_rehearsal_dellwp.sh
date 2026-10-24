#!/bin/bash
# Rehearses the backups of one WordPress site on dellwp against the real thing, end to end:   bash scripts/wp_backup_rehearsal_dellwp.sh wptest
#   1. a backup is taken by the unit (the first is a full copy), and what it made is shown
#   2. a second one: unchanged files must be hard links, so it costs almost nothing (the link counts and the disk use show it)
#   3. the restore drill restores the newest snapshot into a scratch copy and boots WordPress on it (ntfy: "restore drill of <site> from <stamp>: OK (...)")
#   4. a backup is made to FAIL by stopping the site's database container; the unit fails and OnFailure= tells ntfy ("wp-backup@<site> failed"). The database is started again
#      at once, on every exit path
#   5. the freshness check is run with a limit of 0 hours (ntfy: "site <site> has no backup newer than 0 hours"), then normally (ntfy: "site <site> has a fresh backup again")
# Expect FOUR messages on your phone: the drill result, the failed backup, the stale site, the fresh site. Needs sudo without a password. It leaves the snapshots it made (they are
# real backups). The site is down for about half a minute during step 4. What it does not prove: that the messages reach your phone, or that the timer fires at 03:15.
set -u
SITE=${1:-}
[ -n "$SITE" ] || { echo "usage: bash scripts/wp_backup_rehearsal_dellwp.sh <site>"; exit 2; }
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BDIR=$(python3 -I -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); print(d["wp_data_mount"] + "/backups")' "$REPO/host_vars/dellwp/vars.yml") || exit 1
BIN=$(python3 -I -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["wp_tools_bin"])' "$REPO/host_vars/dellwp/vars.yml") || exit 1

ssh dellwp "sudo -n env SITE=$SITE BDIR=$BDIR BIN=$BIN bash -s" <<'REMOTE'
set -u
FAIL=0; DB="wp_${SITE//-/_}_db"
for x in "$BIN/wp-site-backup" "$BIN/wp-site-restore" "$BIN/wp-backup-check"; do [ -x "$x" ] || { echo "$x is missing: run dellwp/wp_sites_dellwp.yml"; exit 1; }; done
systemctl cat "wp-backup@$SITE.service" </dev/null >/dev/null 2>&1 || { echo "wp-backup@$SITE.service is not deployed"; exit 1; }
command -v ntfy-notify >/dev/null || { echo "ntfy-notify is missing"; exit 1; }
docker inspect "$DB" </dev/null >/dev/null 2>&1 || { echo "no container $DB: is the site running?"; exit 1; }
restore_db() { docker start "$DB" </dev/null >/dev/null 2>&1; for _ in $(seq 1 30); do [ "$(docker inspect -f '{{.State.Health.Status}}' "$DB" </dev/null 2>/dev/null)" = healthy ] && return 0; sleep 2; done; return 1; }
trap 'echo "--- making sure the database is running"; restore_db && echo "database healthy" || echo "DATABASE NOT HEALTHY: look at it"' EXIT
newest() { find "$BDIR/$SITE" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9]{8}T[0-9]{6}Z' -printf '%f\n' </dev/null | LC_ALL=C sort | tail -n 1; }

echo "== 1. a backup by the unit"
T0=$(date +%s); systemctl start "wp-backup@$SITE.service" </dev/null || { echo "FAIL: the backup unit failed"; FAIL=$((FAIL+1)); }
S1=$(newest); echo "   $(( $(date +%s) - T0 )) s; newest snapshot: $S1"; cat "$BDIR/$SITE/$S1/MANIFEST" 2>/dev/null | sed 's/^/   /'
[ -s "$BDIR/$SITE/$S1/db.sql.gz" ] && gzip -t "$BDIR/$SITE/$S1/db.sql.gz" && echo "ok: the dump is there and passes gzip -t" || { echo "FAIL: no good dump"; FAIL=$((FAIL+1)); }

echo "== 2. a second backup: unchanged files must be hard links"
sleep 2; systemctl start "wp-backup@$SITE.service" </dev/null || { echo "FAIL: the second backup failed"; FAIL=$((FAIL+1)); }
S2=$(newest); [ "$S2" != "$S1" ] || { echo "FAIL: no second snapshot"; FAIL=$((FAIL+1)); }
TOTAL=$(find "$BDIR/$SITE/$S2/files" -type f </dev/null | wc -l); LINKED=$(find "$BDIR/$SITE/$S2/files" -type f -links +1 </dev/null | wc -l)
echo "   files in the newest snapshot: $TOTAL, shared with an older one (hard links): $LINKED"
echo "   disk use: first $(du -sk "$BDIR/$SITE/$S1" </dev/null | cut -f1) KiB, second counted on its own $(du -sk --exclude=files "$BDIR/$SITE/$S2" </dev/null | cut -f1) KiB plus its changed files"
[ "$LINKED" -ge $(( TOTAL * 9 / 10 )) ] && echo "ok: at least 90% of the files are hard links" || { echo "FAIL: the files were copied again, not linked"; FAIL=$((FAIL+1)); }

echo "== 3. the restore drill"
"$BIN/wp-site-restore" "$SITE" latest --notify </dev/null || { echo "FAIL: the restore drill failed"; FAIL=$((FAIL+1)); }

echo "== 4. a failing backup (the database is stopped for a moment)"
docker stop "$DB" </dev/null >/dev/null
systemctl start "wp-backup@$SITE.service" </dev/null && { echo "FAIL: the backup succeeded with the database stopped"; FAIL=$((FAIL+1)); } || echo "ok: the unit failed as it should"
systemctl is-failed "wp-backup@$SITE.service" </dev/null
[ "$(newest)" = "$S2" ] && echo "ok: no new snapshot appeared" || { echo "FAIL: a snapshot appeared although the dump failed"; FAIL=$((FAIL+1)); }
ls -d "$BDIR/$SITE"/.partial-* 2>/dev/null </dev/null && { echo "FAIL: a .partial directory was left"; FAIL=$((FAIL+1)); } || echo "ok: no .partial directory left"
restore_db && echo "ok: database healthy again" || { echo "FAIL: database did not come back"; FAIL=$((FAIL+1)); }
systemctl reset-failed "wp-backup@$SITE.service" </dev/null 2>/dev/null

echo "== 5. the freshness check"
WP_BACKUP_STALE_HOURS=0 "$BIN/wp-backup-check" </dev/null; echo "   (stale limit 0 hours: one message)"
"$BIN/wp-backup-check" </dev/null; echo "   (normal limit: the 'fresh again' message)"
echo
[ "$FAIL" = 0 ] && echo "On your phone, four messages: the drill result, 'wp-backup@$SITE failed', 'no backup newer than 0 hours', 'a fresh backup again'. Tell me what they said." || echo "$FAIL check(s) failed"
exit "$FAIL"
REMOTE
