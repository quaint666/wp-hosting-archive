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
#   php       the number of .php files against opcache's file limit, container memory against its limit, restarts, whether the kernel's OOM killer ever fired, and the last seven days of php-fpm slow-log entries and "reached max_children" warnings.
#   traffic   the current nginx access log: status codes, the ten most requested paths (query strings cut off), the share of requests that are static files and, for the lines the timed
#             format wrote (templates/wp/site_nginx.conf.j2, tasks/wp_nginx_base_common.yml), how long requests took by kind, with the page cache's verdict, and the five slowest.
# And once: the VM's load, memory, swap and pressure-stall numbers.
# What this does not show: the page weight a visitor downloads, Cloudflare's cache hit rate, or anything from outside the apartment; time to first byte here is the origin alone. Request times exist only for lines written after the timed log format was deployed; older lines have none.
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
  curl -s -o /dev/null -D - --max-time 30 -H "Host: $1" -H 'X-Forwarded-Proto: https' -H 'Accept-Encoding: gzip' "http://127.0.0.1:$2$3" </dev/null | tr -d '\r' | grep -iE '^(HTTP/|content-encoding|cache-control|expires|vary|content-length|x-cache-status)' | sed 's/^/  /' || echo "  no answer"
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
    # wp-login.php gets two requests: the login limiter (10 a minute per address, burst 5) answers 429 after a few, and a 429 says nothing about speed.
    for spec in "/:5" "/wp-login.php:2" "/wp-json/:5"; do
      path=${spec%:*}; reps=${spec##*:}
      echo "speed $path (first = after whatever idle time there was, then warm)"
      for i in $(seq 1 "$reps"); do req "$host" "$port" "$path"; done
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
  echo "  php files in the site: $(find "$SITES/$n/html" -name '*.php' 2>/dev/null | wc -l), opcache.max_accelerated_files: $(docker exec "$php" php -r 'echo ini_get("opcache.max_accelerated_files");' 2>&1 </dev/null | tail -n1), opcache.memory_consumption: $(docker exec "$php" php -r 'echo ini_get("opcache.memory_consumption");' 2>&1 </dev/null | tail -n1) MB (more files than the first number thrashes the cache)"
  slow=$(mktemp)
  docker logs --since 168h "$php" > "$slow" 2>&1 </dev/null || true
  thr=$(docker exec "$php" sh -c 'cat /usr/local/etc/php-fpm.d/*.conf 2>/dev/null | grep -h "^request_slowlog_timeout" | tail -n1' </dev/null 2>/dev/null || true)
  echo "  last 7 days: $(grep -c 'max_children' "$slow" || true) max_children warnings; slow log threshold ${thr:-unknown} (a request over it has its PHP stack logged, whole seconds only)"
  python3 - "$slow" <<'PYSLOW'
import collections, re, sys
entries, cur = [], None
for line in open(sys.argv[1], errors="replace"):
    line = line.rstrip("\n")
    if re.search(r"\[pool \w+\] pid \d+", line):
        cur = {"script": "", "frames": []}
        entries.append(cur)
        continue
    if cur is None:
        continue
    m = re.match(r"script_filename = (.*)$", line)
    if m:
        cur["script"] = m.group(1)
        continue
    m = re.match(r"\[0x[0-9a-f]+\] (.+?) (/\S+):(\d+)$", line)
    if m:
        cur["frames"].append((m.group(1), m.group(2), m.group(3)))


def owner(path):
    m = re.search(r"/wp-content/plugins/([^/]+)/", path)
    if m:
        return "plugin " + m.group(1)
    m = re.search(r"/wp-content/themes/([^/]+)/", path)
    if m:
        return "theme " + m.group(1)
    if "/wp-mu/" in path:
        return "mu-plugins (ours)"
    if "/wp-includes/" in path:
        return "wp-includes"
    if "/wp-admin/" in path:
        return "wp-admin"
    return "other"


print("  slow-log entries in the last 7 days: %d" % len(entries))
if entries:
    scripts, tops, blame = collections.Counter(), collections.Counter(), collections.Counter()
    for e in entries:
        scripts[re.sub(r"^/var/www/html", "", e["script"]) or "?"] += 1
        if e["frames"]:
            fn, path, line = e["frames"][0]
            tops["%s  %s:%s" % (fn, re.sub(r"^/var/www/html/", "", path), line)] += 1
            third = next((owner(f[1]) for f in e["frames"] if owner(f[1]).startswith(("plugin ", "theme ", "mu-"))), owner(path))
            blame[third] += 1
    print("    by script:")
    for k, c in scripts.most_common(5):
        print("      %4d  %s" % (c, k))
    print("    by the plugin or theme nearest the top of the stack (the code that was running):")
    for k, c in blame.most_common(6):
        print("      %4d  %s" % (c, k))
    print("    most common place the stack was in:")
    for k, c in tops.most_common(5):
        print("      %4d  %s" % (c, k[:110]))
PYSLOW
  rm -f "$slow"
  tim=$(mktemp)
  docker logs --since 168h "$php" 2>&1 </dev/null | grep -F 'wph-timing {' > "$tim" || true
  if [ -s "$tim" ]; then
    python3 - "$tim" <<'PYTIMING'
import collections, json, re, statistics, sys
ORDER = ["muplugins_loaded", "plugins_loaded", "after_setup_theme", "init", "woocommerce_init", "elementor/init", "wp_loaded", "parse_request", "admin_init", "rest_api_init", "wp",
         "template_redirect", "wp_enqueue_scripts", "wp_head", "wp_footer", "shutdown"]
rows = []
for line in open(sys.argv[1], errors="replace"):
    m = re.search(r"wph-timing (\{.*\})\s*$", line)
    if m:
        try:
            rows.append(json.loads(m.group(1)))
        except ValueError:
            pass
print("  timing measured inside php-fpm (templates/wp/mu/40-wp-hosting-timing.php): %d requests that reached PHP" % len(rows))
groups = collections.defaultdict(list)
for r in rows:
    q = r.get("q") or {}
    groups[" ".join([r.get("m", "?"), r.get("p", "?")] + ["?%s=%s" % kv for kv in sorted(q.items())])].append(r)
med = statistics.median
for name, rs in sorted(groups.items(), key=lambda kv: -sum(r["ms"] for r in kv[1]))[:5]:
    ms = sorted(r["ms"] for r in rs)
    print("    %-60s n=%d  median %.0f ms  p95 %.0f ms  queries %s  peak %.0f MB" % (name[:60], len(rs), med(ms), ms[int(0.95 * (len(ms) - 1))], med([r.get("queries") or 0 for r in rs]), max(r.get("mem_mb", 0) for r in rs)))
    seen = [k for k in ORDER if sum(1 for r in rs if k in (r.get("marks") or {})) * 2 >= len(rs)]
    seen.sort(key=lambda k: med([r["marks"][k] for r in rs if k in r["marks"]]))          # in the order they were reached, whatever ORDER says
    prev, parts = None, []
    for k in seen:
        at = med([r["marks"][k] for r in rs if k in r["marks"]])
        parts.append("%s %.0f" % (("start to " if prev is None else "") + k, at - (prev or 0)))
        prev = at
    if prev is not None:
        parts.append("after the last stage %.0f" % max(0, med(ms) - prev))
    print("      where the time goes (ms, the median of each stretch): " + "; ".join(parts))
    inc = collections.defaultdict(list)
    own = collections.defaultdict(list)
    for r in rs:
        for k, v in (r.get("inc") or {}).items():
            inc[k].append(v)
        for k, v in (r.get("own") or {}).items():
            own[k].append(v)
    if inc:
        print("      time to include each plugin's file: " + "; ".join("%s %.0f" % (k, med(v)) for k, v in sorted(inc.items(), key=lambda kv: -med(kv[1]))[:5]))
    if own:
        print("      time in each owner's callbacks (inclusive, level 2): " + "; ".join("%s %.0f" % (k, med(v)) for k, v in sorted(own.items(), key=lambda kv: -med(kv[1]))[:6]))
oc = [r["oc"] for r in rows if isinstance(r.get("oc"), dict) and r["oc"].get("on")]
if oc:
    last = oc[-1]
    print("    opcache now: %.0f MB used, %.0f MB free, %.1f %% wasted; %d scripts cached, %d keys of %d allowed; hit rate %.1f %%; strings %.0f MB used, %.0f MB free" %
          (last["used_mb"], last["free_mb"], last["wasted_pct"], last["scripts"], last["keys"], last["max_keys"], last["hit_pct"], last["str_used_mb"], last["str_free_mb"]))
    print("    restarts forced by a full cache: %d (memory), %d (keys); cache full: %s" % (max(o["oom_restarts"] for o in oc), max(o["hash_restarts"] for o in oc), "YES" if any(o["full"] for o in oc) else "no"))
PYTIMING
  else
    echo "  timing measured inside php-fpm: no lines (set wp_php_timing to 1 or 2 in host_vars/dellwp/vars.yml, run the sites play and apply the site)"
  fi
  rm -f "$tim"

  echo "traffic (current nginx access log)"
  log="$LOGS/wp_$n.access.log"
  if [ -s "$log" ]; then
    awk '{ n++; st[$9]++; p=$7; sub(/\?.*/, "", p); if (p ~ /\.(js|css|png|jpe?g|gif|webp|avif|svg|woff2?|ico|map)$/) s++ }
         END { printf "  %d requests, %d static (%.0f %%)\n  status:", n, s, (n ? 100*s/n : 0); for (k in st) printf " %s x%d", k, st[k]; printf "\n" }' "$log" </dev/null
    echo "  top paths:"
    awk '{ p=$7; sub(/\?.*/, "", p); c[p]++ } END { for (k in c) printf "%d %s\n", c[k], k }' "$log" </dev/null | sort -rn | head -10 | sed 's/^/    /'
    python3 - "$log" <<'PY'
import re, sys
rx = re.compile(r'"(?P<method>[A-Z]+) (?P<uri>\S+) [^"]*" (?P<status>\d{3}) \d+ .*rt=(?P<rt>[\d.]+) urt=(?P<urt>.*?) cache=(?P<cache>\S+)$')
keep = re.compile(r"(?:^|&)(wc-ajax|rest_route|action|s)=([^&]*)")
static = re.compile(r"\.(js|css|png|jpe?g|gif|webp|avif|svg|woff2?|ttf|otf|eot|map)$", re.I)
groups, slow, php_time, untimed = {}, [], {}, 0
for line in open(sys.argv[1], errors="replace"):
    m = rx.search(line)
    if not m:
        untimed += 1
        continue
    uri, cache = m.group("uri"), m.group("cache")
    path, _, query = uri.partition("?")
    status, ms = int(m.group("status")), float(m.group("rt")) * 1000
    what = m.group("method") + " " + path + "".join(" ?%s=%s" % (k, v[:30]) for k, v in keep.findall(query))
    if static.search(path):
        klass = "static files (nginx)"
    elif cache == "HIT":
        klass = "page cache HIT"
    elif cache == "BYPASS":
        klass = "php, cache skipped (login, admin, cart, search, json...)"
    elif cache != "-":
        klass = "page cache " + cache
    else:
        klass = "other, no cache in play"
    groups.setdefault(klass, []).append(ms)
    slow.append((ms, status, cache, path))
    if cache in ("BYPASS", "-") and not static.search(path):
        php_time.setdefault(what, []).append(ms)
print("  request times, requests logged with the timed format (%d older lines have none):" % untimed)
if not groups:
    print("    none yet")
else:
    print("    %-58s %6s %8s %8s %8s" % ("what", "n", "median", "p95", "max"))
    for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        v.sort()
        print("    %-58s %6d %7.1f ms %6.1f ms %6.1f ms" % (k, len(v), v[len(v) // 2], v[int(0.95 * (len(v) - 1))], v[-1]))
    print("  where the uncached php time goes (requests php built, by total time; a page the cache keeps costs php once per refresh, these cost it every time):")
    print("    %-62s %6s %8s %9s" % ("request", "n", "each", "total"))
    for what, v in sorted(php_time.items(), key=lambda kv: -sum(kv[1]))[:8]:
        print("    %-62s %6d %5.0f ms %7.1f s" % (what[:62], len(v), sum(v) / len(v), sum(v) / 1000))
    print("  the five slowest:")
    for ms, status, cache, path in sorted(slow, reverse=True)[:5]:
        print("    %7.1f ms  %d  cache %-8s %s" % (ms, status, cache, path[:80]))
PY
  else echo "  no access log or it is empty"; fi
done
hr "done"
REMOTE_END

echo "writing to $OUT" >&2
printf '%s\n' "$REMOTE" | ssh -o BatchMode=yes "$HOST" "sudo bash -s -- $HTTP ${names[*]:-}" 2>&1 | tee -a "$OUT"
echo "saved: $OUT" >&2
