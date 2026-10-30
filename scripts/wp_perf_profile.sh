#!/bin/bash
# Find out WHERE the time goes when WordPress builds a page, for ONE site on dellwp, before anything is tuned. It runs WordPress's own start-up (and the front page) a few times inside the site's php
# container, in a command-line PHP, and varies one thing at a time:
#   bash scripts/wp_perf_profile.sh <site> [--per-plugin] [--ini] [--runs N] [--out FILE] [--host NAME]
# Variants (each run N times, the first one reported apart because it is the cold one):
#   boot            load WordPress and the active plugins, no page
#   boot, cached    the same with PHP's compiled scripts kept in files (opcache's file cache), so the second run does no compiling
#   boot, no plugins the same with the plugins directory pointed at nothing, so what the plugins add is the difference
#   home            the front page's whole build (what the page cache keeps), and home, cached
# --per-plugin adds one more variant per plugin directory: boot, cached, with that one plugin left out (the others are linked into a throwaway directory in the container's /tmp), so each plugin's
# own cost is a number: time saved and files not loaded. It takes about a minute more per four plugins. A plugin that others depend on takes its dependents' work with it when left out.
# --ini adds three variants that change one php setting each on top of "boot, cached": open_basedir off (PHP is said to skip its path cache while open_basedir is set; the pool sets it), zend.assertions -1
# (what a production php.ini sets; with no php.ini PHP runs assertions), and both. What they save is shown against "boot, cached"; a few percent is noise.
# From the numbers: the gap between "boot" and "boot, cached" is what compiling PHP costs a request with no opcache; the gap between "boot" and "boot, no plugins" is the plugins; "db ms" is time inside
# MariaDB for that run (SAVEQUERIES), "files" how many PHP files the page loads, and the last section names which plugin or theme those files and bytes belong to, and the slowest queries (string
# values masked). Facts first: PHP version, the opcache settings, the php container's CPU limit, the filesystem under the site.
# READ-ONLY IN EFFECT, not by construction: it starts WordPress's bootstrap as the web user in a throwaway CLI process, and WordPress and its plugins may touch a transient or an option while starting,
# as every visitor's request does; cron is switched off for the run. Its one write is a directory of compiled scripts in the container's /tmp (a RAM disk), removed at the end. No HTTP, no secret read.
# WHAT IT DOES NOT SHOW: a CLI process is not php-fpm (no shared opcache memory, no HTTP request, some plugins act differently under the CLI), so compare the variants with each other, not with the
# milliseconds scripts/wp_perf_measure.sh gets from nginx; the gaps are what to read. Run it when the VM is quiet.
set -euo pipefail

HOST=dellwp; RUNS=6; OUT=""; SITE=""; PERPLUGIN=0; INI=0
usage() { sed -n '2,3p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit "${1:-2}"; }
while [ $# -gt 0 ]; do
  case $1 in
    --per-plugin) PERPLUGIN=1 ;;
    --ini) INI=1 ;;
    --runs) RUNS=${2:?--runs needs a number}; shift ;;
    --out) OUT=${2:?--out needs a file}; shift ;;
    --host) HOST=${2:?--host needs a name}; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) [ -z "$SITE" ] || { echo "one site at a time" >&2; exit 2; }; SITE=$1 ;;
  esac
  shift
done
[[ $SITE =~ ^[a-z]([a-z0-9]|-[a-z0-9]){0,22}$ ]] || { echo "name the site: bash scripts/wp_perf_profile.sh <site>" >&2; exit 2; }
[[ $RUNS =~ ^[0-9]+$ ]] && [ "$RUNS" -ge 3 ] && [ "$RUNS" -le 20 ] || { echo "--runs is 3 to 20" >&2; exit 2; }
[[ $HOST =~ ^[A-Za-z0-9.-]+$ ]] || { echo "not a host name: $HOST" >&2; exit 2; }
command -v ssh >/dev/null || { echo "missing command: ssh" >&2; exit 2; }
umask 077
if [ -z "$OUT" ]; then
  mkdir -p "${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf"
  OUT="${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf/wp_profile_${SITE}_$(date -u +%Y%m%dT%H%M%SZ).txt"
fi
: > "$OUT"

read -r -d '' REMOTE <<'REMOTE_END' || true
set -u
export LC_ALL=C
readonly SITES=/srv/wp/sites VHOSTS=/etc/nginx/conf.d
n=$1; RUNS=$2; PERPLUGIN=$3; INI=$4
php="wp_${n//-/_}_php"; OC=/tmp/wph_oc
tmp=$(mktemp -d)
cleanup() { docker exec "$php" rm -rf "$OC" >/dev/null 2>&1 </dev/null || true; rm -rf "$tmp"; }
trap cleanup EXIT
hr() { printf '\n==== %s\n' "$*"; }

[ -f "$VHOSTS/wp_site_$n.conf" ] || { echo "no vhost for $n"; exit 1; }
docker inspect "$php" >/dev/null 2>&1 </dev/null || { echo "container $php is not running"; exit 1; }
host=$(awk '$1=="server_name"{gsub(";","",$2); print $2; exit}' "$VHOSTS/wp_site_$n.conf")

hr "facts, site $n ($host), $(date -u +%FT%TZ)"
echo "cpu: $(lscpu | awk -F: '/Model name/{gsub(/^ +/,"",$2); print $2; exit}'), $(nproc) cores in the VM; load $(cut -d' ' -f1-3 /proc/loadavg)"
echo "php container cpu limit: $(docker inspect -f '{{.HostConfig.NanoCpus}}' "$php" </dev/null | awk '{printf "%.2f cpus", $1/1000000000}'), memory limit $(docker inspect -f '{{.HostConfig.Memory}}' "$php" </dev/null | awk '{printf "%d MB", $1/1048576}')"
echo "filesystem under the site: $(findmnt -T "$SITES/$n/html" -n -o FSTYPE,SOURCE,OPTIONS 2>/dev/null | cut -c1-160)"
docker exec "$php" php -v </dev/null 2>&1 | head -n1
echo "opcache loaded: $(docker exec "$php" php -m </dev/null 2>/dev/null | grep -ci '^zend opcache$')"
docker exec "$php" php -r 'foreach (["opcache.enable","zend.assertions","opcache.save_comments","opcache.max_accelerated_files","opcache.memory_consumption","opcache.interned_strings_buffer","opcache.validate_timestamps","opcache.revalidate_freq","opcache.jit","opcache.jit_buffer_size","realpath_cache_size","realpath_cache_ttl","memory_limit","open_basedir"] as $k) printf("  %s = %s\n", $k, ini_get($k));' </dev/null 2>&1
echo "  (the CLI reads the same ini files as php-fpm; opcache.enable_cli and the shared memory are not what the pool uses)"
echo "plugin directories (active or not): $(docker exec "$php" sh -c 'ls /var/www/html/wp-content/plugins | wc -l' </dev/null 2>&1); themes: $(docker exec "$php" sh -c 'ls /var/www/html/wp-content/themes | tr "\n" " "' </dev/null 2>&1)"

read -r -d '' HARNESS <<'PHP_END' || true
<?php
$t0 = microtime(true);
$h = getenv('WPH_HOST');
$_SERVER = array_merge($_SERVER, ['HTTP_HOST' => $h, 'SERVER_NAME' => $h, 'REQUEST_URI' => '/', 'REQUEST_METHOD' => 'GET', 'HTTPS' => 'on', 'SERVER_PORT' => '443',
    'REMOTE_ADDR' => '127.0.0.1', 'SERVER_PROTOCOL' => 'HTTP/1.1', 'SCRIPT_NAME' => '/index.php', 'PHP_SELF' => '/index.php']);
define('DISABLE_WP_CRON', true);
define('SAVEQUERIES', true);
if (getenv('WPH_NOPLUGINS') === '1') {
    define('WP_PLUGIN_DIR', '/tmp/wph-no-plugins');
} elseif (getenv('WPH_PLUGIN_DIR')) {
    define('WP_PLUGIN_DIR', getenv('WPH_PLUGIN_DIR'));
}
ob_start();
register_shutdown_function(function () use ($t0) {
    $t1 = microtime(true);
    while (ob_get_level() > 0) {
        ob_end_clean();
    }
    global $wpdb;
    $q = (isset($wpdb) && is_object($wpdb) && is_array($wpdb->queries)) ? $wpdb->queries : [];
    $db = 0.0;
    foreach ($q as $row) {
        $db += (float) $row[1];
    }
    $out = ['ms' => round(($t1 - $t0) * 1000, 1), 'db_ms' => round($db * 1000, 1), 'queries' => count($q), 'files' => count(get_included_files()), 'peak_mb' => round(memory_get_peak_usage(true) / 1048576, 1)];
    if (getenv('WPH_DETAIL') === '1') {
        $g = [];
        foreach (get_included_files() as $f) {
            if (preg_match('#/wp-content/plugins/([^/]+)/#', $f, $m)) {
                $k = 'plugin ' . $m[1];
            } elseif (preg_match('#/wp-content/themes/([^/]+)/#', $f, $m)) {
                $k = 'theme ' . $m[1];
            } elseif (strpos($f, '/wp-mu/') !== false) {
                $k = 'mu-plugins (ours)';
            } elseif (strpos($f, '/wp-includes/') !== false) {
                $k = 'wp-includes';
            } elseif (strpos($f, '/wp-admin/') !== false) {
                $k = 'wp-admin';
            } else {
                $k = 'other';
            }
            if (!isset($g[$k])) {
                $g[$k] = [0, 0];
            }
            $g[$k][0]++;
            $g[$k][1] += (int) @filesize($f);
        }
        uasort($g, function ($a, $b) {
            return $b[1] <=> $a[1];
        });
        $out['groups'] = array_slice($g, 0, 10, true);
        usort($q, function ($a, $b) {
            return $b[1] <=> $a[1];
        });
        $w = [];
        foreach ($q as $row) {
            if (preg_match('/^\s*(UPDATE|INSERT|DELETE|REPLACE)/i', $row[0]) && preg_match("/option_name`?\s*=\s*'([^']+)'|VALUES\s*\('([^']+)'/i", $row[0], $mm)) {
                $parts = explode(', ', (string) $row[2]);
                $key = (isset($mm[2]) && $mm[2] !== '' ? $mm[2] : $mm[1]) . '  <-  ' . end($parts);
                $w[$key] = ($w[$key] ?? 0) + 1;
            }
        }
        arsort($w);
        $out['writes'] = array_slice($w, 0, 12, true);
        $out['slowest'] = [];
        foreach (array_slice($q, 0, 6) as $row) {
            $parts = explode(', ', (string) $row[2]);
            $out['slowest'][] = [round($row[1] * 1000, 2), end($parts), substr(preg_replace("/'[^']*'/", "'..'", preg_replace('/\s+/', ' ', $row[0])), 0, 90)];
        }
    }
    echo json_encode($out), "\n";
});
if (getenv('WPH_MODE') === 'home') {
    define('WP_USE_THEMES', true);
    require '/var/www/html/wp-blog-header.php';
} else {
    require '/var/www/html/wp-load.php';
}
PHP_END

docker exec "$php" mkdir -p "$OC" </dev/null 2>&1 || true

# variant: label, mode (boot|home), no plugins (0|1), compiled scripts cached in files (0|1)
variant() {
  local label=$1 mode=$2 nop=$3 cached=$4 pdir=${5:-} extra=${6:-} flags="-d opcache.enable_cli=0" f="$tmp/$(echo "$1" | tr -c 'a-z0-9' _)" i
  [ "$cached" = 1 ] && flags="-d opcache.enable_cli=1 -d opcache.file_cache=$OC -d opcache.file_cache_only=1"
  [ "$cached" = 1 ] && docker exec "$php" sh -c "rm -rf $OC/* 2>/dev/null; true" </dev/null >/dev/null 2>&1
  flags="$flags $extra"
  : > "$f"; : > "$f.err"
  for i in $(seq 1 "$RUNS"); do
    # shellcheck disable=SC2086
    printf '%s' "$HARNESS" | docker exec -i -e WPH_HOST="$host" -e WPH_MODE="$mode" -e WPH_NOPLUGINS="$nop" -e WPH_PLUGIN_DIR="$pdir" "$php" php $flags 2>>"$f.err" | tail -n1 >> "$f" || true
  done
  printf '%s\t%s\t%s\n' "$label" "$f" "$(head -c 300 "$f.err" | tr '\n' ' ')" >> "$tmp/index"
}

hr "running $RUNS runs of each variant (the first is the cold one); this takes about a minute"
variant "boot" boot 0 0
variant "boot, cached" boot 0 1
variant "boot, no plugins" boot 1 0
variant "boot, no plugins, cached" boot 1 1
variant "home" home 0 0
variant "home, cached" home 0 1

if [ "$PERPLUGIN" = 1 ]; then
  for p in $(docker exec "$php" ls -1 /var/www/html/wp-content/plugins </dev/null 2>/dev/null | grep -E '^[A-Za-z0-9._-]+$' | grep -v '^index\.php$'); do
    docker exec "$php" sh -c 'd=/tmp/wph-minus-$1; rm -rf "$d"; mkdir -p "$d"; for x in /var/www/html/wp-content/plugins/*; do [ "$(basename "$x")" = "$1" ] || ln -s "$x" "$d/$(basename "$x")"; done' sh "$p" </dev/null >/dev/null 2>&1
    variant "without $p" boot 0 1 "/tmp/wph-minus-$p"
  done
fi

if [ "$INI" = 1 ]; then
  variant "ini: no open_basedir" boot 0 1 "" "-d open_basedir="
  variant "ini: assertions off" boot 0 1 "" "-d zend.assertions=-1"
  variant "ini: both" boot 0 1 "" "-d open_basedir= -d zend.assertions=-1"
fi

hr "results (ms; cold = the first run, warm = the median of the rest)"
python3 - "$tmp/index" <<'PY'
import json, statistics, sys
rows = []
for line in open(sys.argv[1]):
    label, path, err = (line.rstrip("\n").split("\t") + ["", ""])[:3]
    runs = []
    for l in open(path):
        try:
            runs.append(json.loads(l))
        except ValueError:
            pass
    rows.append((label, runs, err))
print("  %-28s %8s %8s %8s %8s %7s %6s %7s" % ("variant", "cold", "warm", "min", "db ms", "queries", "files", "peak MB"))
for label, runs, err in rows:
    if not runs:
        print("  %-28s no result. %s" % (label, err[:200]))
        continue
    warm = runs[1:] or runs
    med = lambda k: statistics.median(r[k] for r in warm)
    print("  %-28s %8.0f %8.0f %8.0f %8.0f %7d %6d %7.1f" % (label, runs[0]["ms"], med("ms"), min(r["ms"] for r in warm), med("db_ms"), med("queries"), med("files"), med("peak_mb")))
w = {label: statistics.median(r["ms"] for r in (runs[1:] or runs)) for label, runs, _ in rows if runs}
def gap(a, b, what):
    if a in w and b in w:
        print("  %-52s %6.0f ms" % (what, w[a] - w[b]))
base = next((runs for label, runs, _ in rows if label == "boot, cached" and runs), None)
if base:
    brow = [r for r in base[1:] or base]
    bms, bfiles = statistics.median(r["ms"] for r in brow), statistics.median(r["files"] for r in brow)
    each, ini_rows = [], []
    for label, runs, _ in rows:
        if label.startswith("ini: ") and runs:
            warm = runs[1:] or runs
            ini_rows.append((bms - statistics.median(r["ms"] for r in warm), label[5:]))
        if label.startswith("without ") and runs:
            warm = runs[1:] or runs
            each.append((bms - statistics.median(r["ms"] for r in warm), bfiles - statistics.median(r["files"] for r in warm), label[8:]))
    if each:
        print("")
        print("  what each plugin costs (boot, cached = %.0f ms, %d files; the time and files saved when that one is left out; noise is a few percent):" % (bms, bfiles))
        for saved, files, name in sorted(each, reverse=True):
            print("    %-44s %6.0f ms  %5d files" % (name, saved, files))
if base and ini_rows:
    print("")
    print("  what one php setting saves on a boot, cached (%.0f ms; negative = slower; a few percent is noise):" % bms)
    for saved, name in ini_rows:
        print("    %-44s %6.0f ms  (%+.1f %%)" % (name, saved, 100.0 * saved / bms))
print("")
gap("boot", "boot, cached", "compiling PHP (boot, opcache off minus cached)")
gap("boot", "boot, no plugins", "the active plugins (boot minus boot without them)")
gap("boot, cached", "boot, no plugins, cached", "the active plugins, with compiling taken out")
gap("home", "boot", "building the front page on top of boot")
gap("home", "home, cached", "compiling PHP for the front page")
PY

hr "what the front page loads, and the slowest queries (home, opcache off, one run)"
printf '%s' "$HARNESS" | docker exec -i -e WPH_HOST="$host" -e WPH_MODE=home -e WPH_NOPLUGINS=0 -e WPH_DETAIL=1 "$php" php -d opcache.enable_cli=0 2>/dev/null | tail -n1 | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    print("  no detail (the run printed nothing)"); sys.exit()
print("  total %.0f ms, db %.0f ms in %d queries, %d files, peak %.1f MB" % (d["ms"], d["db_ms"], d["queries"], d["files"], d["peak_mb"]))
print("  files and code size by owner (largest first):")
for k, (c, b) in d.get("groups", {}).items():
    print("    %-34s %5d files %8.0f KB" % (k, c, b / 1024))
print("  options written during one boot, and who writes them (every page that is not cached does this):")
for k, c in d.get("writes", {}).items():
    print("    %3dx  %s" % (c, k[:100]))
if not d.get("writes"):
    print("    none")
print("  slowest queries (ms, where from, the statement with its strings masked):")
for ms, who, sql in d.get("slowest", []):
    print("    %7.2f  %-34s %s" % (ms, who[:34], sql))
'
hr "done"
REMOTE_END

echo "writing to $OUT" >&2
printf '%s\n' "$REMOTE" | ssh -o BatchMode=yes "$HOST" "sudo bash -s -- $SITE $RUNS $PERPLUGIN $INI" 2>&1 | tee -a "$OUT"
echo "saved: $OUT" >&2
