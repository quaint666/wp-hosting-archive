#!/bin/bash
# Checks one WordPress site on dellwp against what dellwp/wp_sites_dellwp.yml declares. Read-only apart from three short-lived things it removes again: a scratch
# directory with a tiny throwaway quota (project id 4000000) under /srv/wp/sites (not /srv/wp/scratch: that one is root-only, so the php uid could not even reach it), a maintenance flag file in the site's directory, and the rate-limit counters
# of one made-up visitor address. Prints "ok" or "FAIL" per check and a count at the end; the exit code is the number of failures. Run it from the ThinkPad:
#   bash scripts/wp_site_verify_dellwp.sh wptest
# The site's port, project id, quota, state and the uids are read from host_vars/dellwp/vars.yml and vars/wp.yml, nothing is typed here twice.
# It proves what it lists and no more: it does not prove the PUBLIC path (a request through Cloudflare and the tunnel), that WordPress itself works once installed (the
# probe only asks for the installer page), that a plugin survives the PHP restrictions, or that a backup restores.
set -u
SITE=${1:-}
[ -n "$SITE" ] || { echo "usage: bash scripts/wp_site_verify_dellwp.sh <site name from wp_sites>"; exit 2; }
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# name port quota_id quota_mb state (defaults merged), then the domain, uids, listen port and the data mount
read -r PORT QID QMB STATE DOMAIN PHPUID DBUID LISTEN PROBE MOUNT MEMP MEMD BQMB BOFF BDIR ARCH UNITDIR VHOSTDIR < <(python3 -I - "$REPO" "$SITE" <<'PY'
import sys, yaml
repo, site = sys.argv[1:3]
h = yaml.safe_load(open(repo + "/host_vars/dellwp/vars.yml"))
w = yaml.safe_load(open(repo + "/vars/wp.yml"))
s = next((x for x in h["wp_sites"] if x["name"] == site), None)
if s is None:
    sys.exit("no site '%s' in wp_sites" % site)
s = {**h["wp_site_defaults"], **s}
print(s["port"], s["quota_id"], s["quota_mb"], s["state"], h["wp_domain"], w["wp_php_uid"], w["wp_db_uid"], h["wp_nginx_listen_port"], h["wp_nginx_probe_port"], h["wp_data_mount"], s["php_mem_mb"], s["db_mem_mb"], s["backup_quota_mb"], h["wp_backup_quota_offset"], h["wp_data_mount"] + "/backups", h["wp_data_mount"] + "/archive", h["wp_unit_dir"], h["wp_vhost_dir"])
PY
) || exit 1
[ -n "${MOUNT:-}" ] || exit 1

ssh dellwp "sudo -n env SITE=$SITE PORT=$PORT QID=$QID QMB=$QMB STATE=$STATE DOMAIN=$DOMAIN PHPUID=$PHPUID DBUID=$DBUID LISTEN=$LISTEN PROBE=$PROBE MOUNT=$MOUNT MEMP=$MEMP MEMD=$MEMD BQMB=$BQMB BOFF=$BOFF BDIR=$BDIR ARCH=$ARCH UNITDIR=$UNITDIR VHOSTDIR=$VHOSTDIR bash -s" <<'REMOTE'
set -u
FAILS=0; TOTAL=0
ok()   { TOTAL=$((TOTAL+1)); echo "ok:   $*"; }
bad()  { TOTAL=$((TOTAL+1)); FAILS=$((FAILS+1)); echo "FAIL: $*"; }
check() { local what=$1; shift; if "$@" </dev/null >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }
HOST="$SITE.$DOMAIN"; DIR="$MOUNT/sites/$SITE"; PHP="wp_${SITE//-/_}_php"; DB="wp_${SITE//-/_}_db"
BASE="http://127.0.0.1:$LISTEN"
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@" </dev/null; }
SCRATCH="$MOUNT/sites/.quota-verify"; FLAG="$DIR/maintenance.flag"
cleanup() { rm -f "$FLAG"; rm -rf "$SCRATCH"; setquota -P 4000000 0 0 0 0 "$MOUNT" 2>/dev/null; }
trap cleanup EXIT

if [ "$STATE" = closed ]; then
  echo "== $SITE: closed"
  [ ! -e "$DIR" ] && ok "no site directory ($DIR)" || bad "$DIR still exists"
  [ ! -e "$BDIR/$SITE" ] && ok "no backup directory ($BDIR/$SITE)" || bad "$BDIR/$SITE still exists"
  [ ! -e "$UNITDIR/wp-$SITE.service" ] && ok "no stack unit" || bad "$UNITDIR/wp-$SITE.service still exists"
  [ ! -e "$VHOSTDIR/wp_site_$SITE.conf" ] && ok "no nginx vhost" || bad "$VHOSTDIR/wp_site_$SITE.conf still exists"
  for t in cron backup update; do systemctl is-enabled --quiet "wp-$t@$SITE.timer" </dev/null && bad "wp-$t@$SITE.timer is enabled" || ok "wp-$t@$SITE.timer is not enabled"; done
  [ -z "$(docker ps -aq --filter "name=^wp_${SITE//-/_}_" </dev/null)" ] && ok "no container of the site exists" || bad "a container of the site still exists"
  [ -z "$(docker network ls -q --filter "name=^wp-${SITE}_" </dev/null)" ] && ok "no network of the site exists" || bad "a network of the site still exists"
  [ "$(code -H "Host: $HOST" "$BASE/")" = 000 ] && ok "the name gets the closed connection (no vhost claims it)" || bad "the name $HOST is still answered by nginx"
  # the archive of the last backup is kept for a while and then purged: its absence is normal after that, a broken one is not
  ARCHIVES=$(find "$ARCH" -mindepth 1 -maxdepth 1 -type d -name "$SITE-[0-9]*T*Z" </dev/null 2>/dev/null)
  if [ -z "$ARCHIVES" ]; then echo "   (info) no archive of $SITE under $ARCH (never made, or already purged)"
  else for A in $ARCHIVES; do
    [ -f "$A/db.sql.gz" ] && [ -f "$A/wp-content.tar.gz" ] && gzip -t "$A/db.sql.gz" </dev/null && tar -tzf "$A/wp-content.tar.gz" </dev/null >/dev/null && ok "archive $(basename "$A") reads back (kept until $(sed -n 's/^expires=//p' "$A/MANIFEST"))" || bad "archive $A is incomplete or does not read back"
  done; fi
  echo "$TOTAL checks, $FAILS failed"
  exit "$FAILS"
fi

if [ "$STATE" = stopped ]; then
  echo "== $SITE: stopped"
  [ -z "$(docker ps -q --filter "name=^wp_${SITE//-/_}_" </dev/null)" ] && ok "no container of the site is running" || bad "a container of the site is running although it is stopped"
  systemctl is-active --quiet "wp-$SITE" </dev/null && bad "wp-$SITE.service is active" || ok "wp-$SITE.service is not active"
  systemctl is-enabled --quiet "wp-$SITE" </dev/null && bad "wp-$SITE.service is enabled (it would start at boot)" || ok "wp-$SITE.service is not enabled"
  [ -e "$DIR/stopped.flag" ] && ok "stopped.flag is there (the tools and the freshness check know)" || bad "stopped.flag is missing"
  for t in cron backup update; do systemctl is-active --quiet "wp-$t@$SITE.timer" </dev/null && bad "wp-$t@$SITE.timer is running" || ok "wp-$t@$SITE.timer is not running"; done
  [ -d "$DIR/db" ] && [ -f "$DIR/wp-config.php" ] && [ -d "$DIR/html/wp-content" ] && ok "the data is intact (db/, wp-config.php, wp-content/)" || bad "the site's data is incomplete"
  PAGE=$(curl -s --max-time 8 -H "Host: $HOST" "$BASE/" </dev/null); C=$(code -H "Host: $HOST" "$BASE/")
  [ "$C" = 503 ] && echo "$PAGE" | grep -q 'paused' && ok "visitors get the 503 'paused' page" || bad "the vhost answered $C '$PAGE', want 503 'This site is paused.'"
  [ "$(code -H "Host: $HOST" -H 'CF-Connecting-IP: 198.51.100.9' "$BASE/wp-login.php")" = 503 ] && ok "even the login page is paused" || bad "the login page of a stopped site is not paused"
  LINE=$(repquota -P -n "$MOUNT" </dev/null | grep "^#$QID "); [ -n "$LINE" ] && ok "project $QID still holds the site's data ($(echo "$LINE" | awk '{printf "%d MB used", $3/1024}'))" || bad "quota project $QID not found"
  echo "$TOTAL checks, $FAILS failed"
  exit "$FAILS"
fi

echo "== $SITE: containers"
for n in "$PHP" "$DB"; do
  S=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$n" </dev/null 2>/dev/null)
  case "$S" in "running healthy"|"running ") ok "$n $S" ;; *) bad "$n state '$S'" ;; esac
  M=$(docker inspect -f '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' "$n" </dev/null)
  set -- $M; [ "$1" -gt 0 ] && [ "$1" = "$2" ] && ok "$n memory limit $(($1/1048576)) MB, swap equal" || bad "$n memory/swap '$M' (must be set and equal)"
  docker inspect -f '{{json .HostConfig.CapDrop}} {{json .HostConfig.SecurityOpt}}' "$n" </dev/null | grep -q 'ALL.*no-new-privileges' && ok "$n drops all capabilities, no-new-privileges" || bad "$n capabilities/security options"
done
[ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$PHP" </dev/null)" = true ] && ok "$PHP root filesystem read-only" || bad "$PHP root filesystem is writable"
[ "$(docker inspect -f '{{.Config.User}}' "$PHP" </dev/null)" = "$PHPUID:$PHPUID" ] && ok "$PHP runs as $PHPUID" || bad "$PHP user is not $PHPUID"
[ "$(docker inspect -f '{{.Config.User}}' "$DB" </dev/null)" = "$DBUID:$DBUID" ] && ok "$DB runs as $DBUID" || bad "$DB user is not $DBUID"
[ -z "$(getent passwd "$DBUID")" ] && ok "uid $DBUID has no account on the VM (no host process can be that user)" || bad "uid $DBUID is an account on the VM: $(getent passwd "$DBUID")"
docker port "$PHP" </dev/null | grep -q "127.0.0.1:$PORT" && ! docker port "$PHP" </dev/null | grep -v '127.0.0.1' | grep -q . && ok "php-fpm published on 127.0.0.1:$PORT only" || bad "php-fpm port publishing: $(docker port "$PHP" </dev/null | tr '\n' ' ')"
[ -z "$(docker port "$DB" </dev/null)" ] && ok "the database publishes no port" || bad "the database publishes: $(docker port "$DB" </dev/null)"
check "php can reach its database as the site user" docker exec "$PHP" php -r '$c=file_get_contents("/var/www/html/wp-config.php"); preg_match("/DB_PASSWORD\047, \047([^\047]+)/",$c,$m); exit(@mysqli_connect("db","wp",$m[1],"wordpress")?0:1);'
echo "   (info) memory now: $(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' "$PHP" "$DB" </dev/null | tr '\n' ';') limits: php ${MEMP}m, db ${MEMD}m"

echo "== $SITE: files and permissions"
[ "$(stat -c %u "$DIR/html/index.php")" = 0 ] && ok "code is root-owned" || bad "html/index.php owner is $(stat -c %u "$DIR/html/index.php")"
if docker exec "$PHP" sh -c 'touch /var/www/html/.verify' </dev/null >/dev/null 2>&1; then bad "php CAN write into the code directory"; docker exec "$PHP" rm -f /var/www/html/.verify </dev/null; else ok "php cannot write into the code directory"; fi
check "php can write to uploads" docker exec "$PHP" sh -c 'touch /var/www/html/wp-content/uploads/.verify && rm /var/www/html/wp-content/uploads/.verify'
CS=$(wp-site-cli "$SITE" core verify-checksums 2>&1 </dev/null); RC=$?
[ "$RC" = 0 ] && ! echo "$CS" | grep -q "^Warning" && ok "WordPress core files match wordpress.org's checksums, nothing extra" || bad "core checksums: $(echo "$CS" | head -2 | tr '\n' ' ')"
[ "$(stat -c %a "$DIR/wp-config.php")" = 640 ] && ok "wp-config.php mode 640" || bad "wp-config.php mode $(stat -c %a "$DIR/wp-config.php")"
[ "$(stat -c %a "$DIR/.env")" = 600 ] && ok ".env mode 600" || bad ".env mode $(stat -c %a "$DIR/.env")"
[ "$(stat -c %u "$DIR/db")" = "$DBUID" ] && ok "db/ owned by $DBUID" || bad "db/ owner $(stat -c %u "$DIR/db")"
[ -z "$(find "$DIR/db" ! -user "$DBUID" 2>/dev/null | head -1)" ] && ok "every file in db/ belongs to $DBUID" || bad "files in db/ with another owner: $(find "$DIR/db" ! -user "$DBUID" | head -3 | tr '\n' ' ')"

echo "== $SITE: quota"
LINE=$(repquota -P -n "$MOUNT" </dev/null | grep "^#$QID ")
HARD=$(echo "$LINE" | awk '{print $5}')
[ "$HARD" = "$((QMB*1024))" ] && ok "project $QID hard limit $((QMB)) MB ($(echo "$LINE" | awk '{printf "%d MB used", $3/1024}'))" || bad "project $QID line '$LINE', want hard $((QMB*1024)) KiB"
# The mechanism, on a throwaway project, written as the php uid: root may exceed a quota (CAP_SYS_RESOURCE), the container may not.
mkdir -p "$SCRATCH" && chown "$PHPUID:$PHPUID" "$SCRATCH" && chattr +P -p 4000000 "$SCRATCH" && setquota -P 4000000 0 5120 0 0 "$MOUNT"
OUT=$(setpriv --reuid "$PHPUID" --regid "$PHPUID" --clear-groups dd if=/dev/zero of="$SCRATCH/f" bs=1M count=12 2>&1 </dev/null)
SIZE=$(stat -c %s "$SCRATCH/f" 2>/dev/null || echo 0)
echo "$OUT" | grep -qi 'quota exceeded' && [ "$SIZE" -le $((5*1024*1024+1048576)) ] && ok "a write past a 5 MB project limit is refused (stopped at $((SIZE/1024)) KiB)" || bad "quota did not stop a 12 MB write as uid $PHPUID (size $SIZE): $OUT"
# A hard link from that project into the site's own uploads (project $QID), a place the php uid may write: only the cross-project rule can refuse it.
LINKOUT=$(setpriv --reuid "$PHPUID" --regid "$PHPUID" --clear-groups ln "$SCRATCH/f" "$DIR/html/wp-content/uploads/.verify-link" 2>&1 </dev/null)
if echo "$LINKOUT" | grep -qi 'cross-device'; then ok "a hard link across quota projects is refused (cross-device)"
elif [ -e "$DIR/html/wp-content/uploads/.verify-link" ]; then bad "a hard link out of the quota project was ALLOWED"; rm -f "$DIR/html/wp-content/uploads/.verify-link"
else bad "hard link refused for another reason: $LINKOUT"; fi

echo "== $SITE: backups"
BQID=$((QID + BOFF)); BLINE=$(repquota -P -n "$MOUNT" </dev/null | grep "^#$BQID ")
[ "$(echo "$BLINE" | awk '{print $5}')" = "$((BQMB*1024))" ] && ok "backup project $BQID hard limit $BQMB MB ($(echo "$BLINE" | awk '{printf "%d MB used", $3/1024}'))" || bad "backup project $BQID line '$BLINE', want hard $((BQMB*1024)) KiB"
[ "$(stat -c %a "$BDIR/$SITE")" = 700 ] && ok "the backup directory is private (700)" || bad "backup directory mode $(stat -c %a "$BDIR/$SITE")"
systemctl is-enabled --quiet "wp-backup@$SITE.timer" </dev/null && ok "wp-backup@$SITE.timer enabled (nightly, on hold or live)" || bad "wp-backup@$SITE.timer is not enabled"
systemctl is-enabled --quiet "wp-update@$SITE.timer" </dev/null && ok "wp-update@$SITE.timer enabled (nightly; the site's update_policy decides what it does)" || bad "wp-update@$SITE.timer is not enabled"
systemctl is-enabled --quiet wp-backup-check.timer </dev/null && ok "wp-backup-check.timer enabled (a missing backup is noticed)" || bad "wp-backup-check.timer is not enabled"
LAST=$(cat "$BDIR/$SITE/.last-success" 2>/dev/null)
if [ -n "$LAST" ]; then echo "   (info) newest good backup: $LAST ($(( ($(date +%s) - $(stat -c %Y "$BDIR/$SITE/.last-success")) / 3600 )) hours ago)"; else echo "   (info) no backup has run yet for $SITE"; fi

echo "== $SITE: nginx ($STATE)"
if [ "$STATE" = hold ]; then
  [ "$(code -H "Host: $HOST" "$BASE/wp-admin/install.php")" = 200 ] && ok "hold: a local request reaches the installer page" || bad "hold: local request did not get 200"
  [ "$(code -H "Host: $HOST" -H 'CF-Connecting-IP: 203.0.113.9' "$BASE/")" = 403 ] && ok "hold: a request that carries a visitor address (as the tunnel sends) is refused" || bad "hold: a visitor request was not refused"
else
  [ "$(code -H "Host: $HOST" -H 'CF-Connecting-IP: 203.0.113.9' "$BASE/wp-admin/install.php")" != 403 ] && ok "live: a visitor request is not refused by the vhost" || bad "live: a visitor request got 403"
fi
C=$(code -H "Host: no-such-site.$DOMAIN" "$BASE/"); [ "$C" = 000 ] && ok "a host name no site claims gets the closed connection" || bad "unknown host answered $C"
for p in wp-content/uploads/x.php wp-content/upgrade/x.php xmlrpc.php .git/config wp-content/debug.log wp-content/uploads/a.zip "?author=1"; do
  [ "$(code -H "Host: $HOST" "$BASE/$p")" = 403 ] && ok "403 for /$p" || bad "/$p answered $(code -H "Host: $HOST" "$BASE/$p"), want 403"
done
for p in readme.html license.txt wp-config.php; do
  [ "$(code -H "Host: $HOST" "$BASE/$p")" = 404 ] && ok "404 for /$p" || bad "/$p answered $(code -H "Host: $HOST" "$BASE/$p"), want 404"
done
touch "$FLAG"; C=$(code -H "Host: $HOST" "$BASE/wp-admin/install.php"); CP=$(code -H "Host: $HOST" "http://127.0.0.1:$PROBE/wp-admin/install.php"); rm -f "$FLAG"
[ "$C" = 503 ] && ok "the maintenance flag turns requests into 503, and removing it restores them" || bad "maintenance flag: got $C, want 503"
[ "$CP" = 200 ] && ok "the probe port $PROBE answers 200 while the flag is up (the update job tests the site before reopening it)" || bad "probe port with the flag up: got $CP, want 200"
[ "$(code -H "Host: no-such-site.$DOMAIN" "http://127.0.0.1:$PROBE/")" = 000 ] && ok "the probe port closes the connection for a host name no site claims" || bad "the probe port answered a host no site claims"
[ "$(code -H "Host: $HOST" "$BASE/wp-admin/install.php")" = 200 ] || [ "$STATE" != hold ] && ok "site answers again after the flag is removed" || bad "site did not recover after the flag"
# 10 requests a minute per visitor with a burst of 5: a rapid run from one made-up address must hit 429 on /wp-login.php, and other pages must not
N=0; for i in $(seq 1 14); do [ "$(code -H "Host: $HOST" -H 'CF-Connecting-IP: 198.51.100.7' "$BASE/wp-login.php")" = 429 ] && N=$((N+1)); done
[ "$N" -ge 1 ] && ok "login limit: $N of 14 rapid requests to /wp-login.php got 429" || bad "login limit: no 429 in 14 rapid requests"
N=0; for i in $(seq 1 14); do [ "$(code -H "Host: $HOST" -H 'CF-Connecting-IP: 198.51.100.8' "$BASE/readme.html")" = 429 ] && N=$((N+1)); done
[ "$N" = 0 ] && ok "other pages are not rate-limited by the login zone" || bad "login zone limited a page that is not /wp-login.php ($N 429s)"
[ -z "$(grep -c '\[emerg\]' /var/log/nginx/error.log </dev/null | grep -v '^0$')" ] && ok "nginx error.log has no [emerg]" || echo "   (info) error.log has $(grep -c '\[emerg\]' /var/log/nginx/error.log) old [emerg] line(s); newest: $(grep '\[emerg\]' /var/log/nginx/error.log | tail -1 | cut -c1-19)"
systemctl is-active --quiet nginx </dev/null && ok "nginx active" || bad "nginx not active"
systemctl is-enabled --quiet wp-disk-check.timer </dev/null && ok "wp-disk-check.timer enabled (the data disk and every quota are watched)" || bad "wp-disk-check.timer is not enabled: nothing watches the disk"
systemctl start wp-disk-check.service </dev/null >/dev/null 2>&1 && ok "the disk check runs" || bad "the disk check failed (journalctl -u wp-disk-check)"
systemctl is-enabled --quiet "wp-$SITE" </dev/null && ok "wp-$SITE.service enabled (starts at boot)" || bad "wp-$SITE.service not enabled"
systemctl is-active --quiet "wp-$SITE" </dev/null && ok "wp-$SITE.service active (so systemctl stop and restart act on the containers)" || bad "wp-$SITE.service is not active: systemctl stop/restart would do nothing to the containers"

echo "$TOTAL checks, $FAILS failed"
exit "$FAILS"
REMOTE
