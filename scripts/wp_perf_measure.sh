#!/bin/bash
# Measure how fast the WordPress sites on dellwp are, before anything is tuned. READ-ONLY: it changes nothing on dellwp and prints no secret.
#   bash scripts/wp_perf_measure.sh [--no-http] [--out FILE] [--host NAME] [site ...]
# One ssh to dellwp (sudo, one script on stdin), everything it prints is also written to FILE (default: ~/.local/state/wp-perf/wp_perf_<UTC stamp>.txt, mode 0600), so the file is the thing to paste back.
# No site named = every site that has a compose file. A stopped site is listed and skipped.
# Per site it reports:
#   speed     5 requests each to /, /wp-login.php and /wp-json/ on nginx's loopback port with the site's Host header: status, time to first byte, total time, bytes. The first request is the one
#             that meets an idle php-fpm (pm = ondemand starts a worker after 20 s idle); the rest are warm. These are HTTP requests to 127.0.0.1 on dellwp, inside the ssh session (the same kind
#             as wp-site-update's probe); --no-http leaves them out and keeps everything else.
#   headers   whether a page and a static file come gzip-compressed and with what Cache-Control / Expires.
#   database  the InnoDB buffer pool: size, how many reads missed it (the number that says whether 64 MB is too small), slow queries, temp tables spilled to disk, connections, database size, and
#             how much autoloaded data wp_options carries (loaded on every page).
#   php       container memory against its limit, restarts, whether the kernel's OOM killer ever fired, and the last seven days of php-fpm slow-log entries and "reached max_children" warnings.
#   traffic   the current nginx access log: status codes, the ten most requested paths (query strings cut off) and the share of requests that are static files.
# And once: the VM's load, memory, swap and pressure-stall numbers.
# What this does not show: the page weight a visitor downloads, Cloudflare's cache hit rate, or anything from outside the apartment; time to first byte here is the origin alone. The request log has no
# request time (the access log format is the default), so "slow pages" in the traffic section are not known: only the php-fpm slow log (10 s and over) names those.
# Needs: ssh to dellwp as a user with passwordless sudo (it is what the other tools use), nothing else. Lesson 2026-10-24: every command that could read stdin has </dev/null, or the rest of this script is swallowed.
set -euo pipefail

readonly HOST_DEFAULT=dellwp
HOST=$HOST_DEFAULT; HTTP=1; OUT=""; names=()

usage() { sed -n '2,3p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit "${1:-2}"; }
while [ $# -gt 0 ]; do
  case $1 in
    --no-http) HTTP=0 ;;
    --out) OUT=${2:?--out needs a file}; shift ;;
    --host) HOST=${2:?--host needs a name}; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) [[ $1 =~ ^[a-z][a-z0-9-]{0,22}[a-z0-9]$ ]] || { echo "not a site name: $1" >&2; exit 2; }; names+=("$1") ;;
  esac
  shift
done
[[ $HOST =~ ^[A-Za-z0-9.-]+$ ]] || { echo "not a host name: $HOST" >&2; exit 2; }
command -v ssh >/dev/null || { echo "missing command: ssh" >&2; exit 2; }

umask 077
if [ -z "$OUT" ]; then
  mkdir -p "${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf"
  OUT="${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf/wp_perf_$(date -u +%Y%m%dT%H%M%SZ).txt"
fi
: > "$OUT"

# The part that runs on dellwp. Quoted heredoc: nothing in it is expanded here.
read -r -d '' REMOTE <<'REMOTE_END' || true
set -u
export LC_ALL=C
readonly SITES=/srv/wp/sites VHOSTS=/etc/nginx/conf.d LOGS=/var/log/nginx
HTTP=$1; shift
if [ "$#" -gt 0 ]; then names=("$@"); else names=(); for d in "$SITES"/*/; do [ -f "${d}compose.yml" ] && names+=("$(basename "$d")"); done; fi

hr() { printf '\n==== %s\n' "$*"; }
cname() { printf 'wp_%s_%s' "${1//-/_}" "$2"; }
dbq() { printf '%s\n' "$2" | docker exec -i "$1" sh -c 'MYSQL_PWD="$MARIADB_PASSWORD" exec mariadb -u"$MARIADB_USER" "$MARIADB_DATABASE" -N -B' 2>&1; }
req() {   # host port path
  curl -s -o /dev/null --max-time 30 -H "Host: $1" -H 'X-Forwarded-Proto: https' -w '  %{http_code}  ttfb %{time_starttransfer}s  total %{time_total}s  %{size_download} bytes\n' "http://127.0.0.1:$2$3" </dev/null || echo "  curl failed"
}
hdrs() {  # host port path
  curl -s -o /dev/null -D - --max-time 30 -H "Host: $1" -H 'X-Forwarded-Proto: https' -H 'Accept-Encoding: gzip' "http://127.0.0.1:$2$3" </dev/null | tr -d '\r' | grep -iE '^(HTTP/|content-encoding|cache-control|expires|vary|content-length)' | sed 's/^/  /' || echo "  no answer"
}

hr "measured $(date -u +%FT%TZ) on $(hostname), http=$HTTP, sites: ${names[*]:-none}"
hr "the VM"
uptime
free -m
for p in cpu memory io; do printf 'pressure %s: ' "$p"; head -n1 "/proc/pressure/$p" 2>/dev/null || echo n/a; done

for n in "${names[@]}"; do
  conf="$VHOSTS/wp_site_$n.conf"; db=$(cname "$n" db); php=$(cname "$n" php)
  hr "site $n"
  if [ ! -f "$SITES/$n/compose.yml" ] || [ ! -f "$conf" ]; then echo "no compose file or vhost: skipped"; continue; fi
  state=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("state","?"))' "$SITES/$n/site.json" 2>/dev/null </dev/null || echo "?")
  host=$(awk '$1=="server_name"{gsub(";","",$2); print $2; exit}' "$conf")
  port=$(awk '$1=="listen"{sub(".*:","",$2); gsub(";","",$2); print $2; exit}' "$conf")
  echo "state $state, host $host, loopback port $port"
  if [ "$state" = stopped ]; then echo "stopped: skipped"; continue; fi

  if [ "$HTTP" = 1 ]; then
    for path in / /wp-login.php /wp-json/; do
      echo "speed $path (first = after whatever idle time there was, then warm)"
      for i in 1 2 3 4 5; do req "$host" "$port" "$path"; done
    done
    echo "headers for / (gzip asked for)";                 hdrs "$host" "$port" /
    echo "headers for a static file (jquery.min.js)";      hdrs "$host" "$port" /wp-includes/js/jquery/jquery.min.js
  fi

  echo "database"
  if docker inspect "$db" >/dev/null 2>&1 </dev/null; then
    dbq "$db" "SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime','Questions','Slow_queries','Created_tmp_tables','Created_tmp_disk_tables','Max_used_connections','Threads_connected','Innodb_buffer_pool_read_requests','Innodb_buffer_pool_reads','Innodb_buffer_pool_wait_free')" \
      | awk '{v[$1]=$2} END {
          printf "  uptime %d s, questions %d, slow queries %d, connections now %d (peak %d)\n", v["Uptime"], v["Questions"], v["Slow_queries"], v["Threads_connected"], v["Max_used_connections"];
          printf "  temp tables %d, of which on disk %d\n", v["Created_tmp_tables"], v["Created_tmp_disk_tables"];
          rr=v["Innodb_buffer_pool_read_requests"]; r=v["Innodb_buffer_pool_reads"];
          printf "  buffer pool: %d read requests, %d went to disk (%.3f %% missed), waited for a free page %d times\n", rr, r, (rr ? 100*r/rr : 0), v["Innodb_buffer_pool_wait_free"] }'
    dbq "$db" "SELECT CONCAT('  buffer pool size ', ROUND(@@innodb_buffer_pool_size/1048576), ' MB; database ', ROUND(SUM(data_length+index_length)/1048576,1), ' MB (data+index) in ', COUNT(*), ' tables') FROM information_schema.tables WHERE table_schema=DATABASE()"
    opt=$(dbq "$db" "SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name LIKE '%\\_options' LIMIT 1")
    if [[ $opt =~ ^[A-Za-z0-9_]+$ ]]; then
      dbq "$db" "SELECT CONCAT('  autoloaded options: ', COUNT(*), ' rows, ', ROUND(SUM(LENGTH(option_value))/1024), ' KB loaded on every page') FROM \`$opt\` WHERE autoload IN ('yes','on','auto','auto-on')"
    else echo "  no options table found (WordPress is not installed?)"; fi
  else echo "  container $db is not there"; fi

  echo "php"
  docker inspect -f '  {{.Name}} started {{.State.StartedAt}}, restarts {{.RestartCount}}, OOM-killed {{.State.OOMKilled}}' "$db" "$php" 2>&1 </dev/null
  docker stats --no-stream --format '  {{.Name}}: cpu {{.CPUPerc}}, memory {{.MemUsage}} ({{.MemPerc}})' "$db" "$php" 2>&1 </dev/null
  logs=$(docker logs --since 168h "$php" 2>&1 </dev/null || true)
  echo "  last 7 days: $(printf '%s\n' "$logs" | grep -c 'script_filename =' || true) slow-log entries (10 s and over), $(printf '%s\n' "$logs" | grep -c 'max_children' || true) max_children warnings"
  printf '%s\n' "$logs" | grep 'script_filename =' | sort | uniq -c | sort -rn | head -3 | sed 's/^/    /' || true

  echo "traffic (current nginx access log)"
  log="$LOGS/wp_$n.access.log"
  if [ -s "$log" ]; then
    awk '{ n++; st[$9]++; p=$7; sub(/\?.*/, "", p); if (p ~ /\.(js|css|png|jpe?g|gif|webp|avif|svg|woff2?|ico|map)$/) s++ }
         END { printf "  %d requests, %d static (%.0f %%)\n  status:", n, s, (n ? 100*s/n : 0); for (k in st) printf " %s x%d", k, st[k]; printf "\n" }' "$log" </dev/null
    echo "  top paths:"
    awk '{ p=$7; sub(/\?.*/, "", p); c[p]++ } END { for (k in c) printf "%d %s\n", c[k], k }' "$log" </dev/null | sort -rn | head -10 | sed 's/^/    /'
  else echo "  no access log or it is empty"; fi
done
hr "done"
REMOTE_END

echo "writing to $OUT" >&2
printf '%s\n' "$REMOTE" | ssh -o BatchMode=yes "$HOST" "sudo bash -s -- $HTTP ${names[*]:-}" 2>&1 | tee -a "$OUT"
echo "saved: $OUT" >&2
