#!/bin/bash
# A/B test of php.ini settings on one live site, in one go and with ONE confirmation: this asks you once, then runs scripts/wp_ab_test.py on dellwp over ssh and nothing more is asked. Read what it
# does at the top of that file. Short version: A is the site's php.ini as it is, B is the same file with your --set values; the phases run A B A B, each one restarts the site (about ten seconds offline, so
# use it on a site that can take that), empties the page cache and times only requests PHP has to build, from nginx on dellwp's own loopback. The file is put back exactly on every way out, and at the end
# wp-site-provision confirms that php.ini is again what the repo renders. A run takes about 4 to 6 minutes.
#   bash scripts/wp_ab_test.sh <site> --set key=value [--set key=value ...] [--rounds N] [--requests N] [--out FILE] [--host NAME] [--yes]
# Example (is the 16 MB interned-strings buffer better than the old 8?):  bash scripts/wp_ab_test.sh envo-ecommerce --set opcache.interned_strings_buffer=8
# Everything it prints is also saved to FILE (default ~/.local/state/wp-perf/wp_ab_<site>_<UTC stamp>.txt). --yes skips the one question (for a script).
set -euo pipefail
HOST=dellwp; OUT=""; SITE=""; YES=0; ROUNDS=2; REQUESTS=12; sets=()
while [ $# -gt 0 ]; do
  case $1 in
    --set) [[ ${2:-} =~ ^[a-z][a-z0-9_.]{1,60}=[A-Za-z0-9_.:/,+-]{1,60}$ ]] || { echo "--set wants key=value with a plain php.ini key and value, not '${2:-}'" >&2; exit 2; }
           sets+=("$2"); shift ;;
    --rounds) [[ ${2:-} =~ ^[1-4]$ ]] || { echo "--rounds is 1 to 4" >&2; exit 2; }; ROUNDS=$2; shift ;;
    --requests) [[ ${2:-} =~ ^[0-9]{1,2}$ ]] && [ "$2" -ge 3 ] && [ "$2" -le 60 ] || { echo "--requests is 3 to 60" >&2; exit 2; }; REQUESTS=$2; shift ;;
    --out) OUT=${2:?--out needs a file}; shift ;;
    --host) HOST=${2:?--host needs a name}; shift ;;
    --yes) YES=1 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) [ -z "$SITE" ] || { echo "one site at a time" >&2; exit 2; }; SITE=$1 ;;
  esac
  shift
done
[[ $SITE =~ ^[a-z]([a-z0-9]|-[a-z0-9]){0,22}$ ]] || { echo "name the site: bash scripts/wp_ab_test.sh <site> --set key=value" >&2; exit 2; }
[ "${#sets[@]}" -ge 1 ] || { echo "nothing to test: give at least one --set key=value" >&2; exit 2; }
[[ $HOST =~ ^[A-Za-z0-9.-]+$ ]] || { echo "not a host name: $HOST" >&2; exit 2; }
command -v ssh >/dev/null || { echo "missing command: ssh" >&2; exit 2; }
py="$(dirname "${BASH_SOURCE[0]}")/wp_ab_test.py"
[ -f "$py" ] || { echo "missing $py" >&2; exit 2; }
umask 077
if [ -z "$OUT" ]; then
  mkdir -p "${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf"
  OUT="${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf/wp_ab_${SITE}_$(date -u +%Y%m%dT%H%M%SZ).txt"
fi
: > "$OUT"

echo "A/B test on $SITE ($HOST): B is php.ini with ${sets[*]}"
echo "It restarts $SITE $((ROUNDS * 2 + 1)) times (about ten seconds offline each), takes about $((ROUNDS * 2 + 2)) minutes, and puts php.ini back as it was."
if [ "$YES" != 1 ]; then
  [ -t 0 ] || { echo "no terminal to ask on: pass --yes to run without the question" >&2; exit 2; }
  read -r -p "Type the site name to start (anything else stops): " answer
  [ "$answer" = "$SITE" ] || { echo "not started"; exit 1; }
fi

args=(--rounds "$ROUNDS" --requests "$REQUESTS" --lock-file "/var/lib/wp-ctl/locks/$SITE.lock")
for s in "${sets[@]}"; do args+=(--set "$s"); done
echo "writing to $OUT" >&2
# every argument was checked above (a-z, digits and a few punctuation marks only), so the remote command line holds nothing else
ssh -o BatchMode=yes -o ServerAliveInterval=30 "$HOST" "sudo python3 -u - $SITE ${args[*]}" < "$py" 2>&1 | tee -a "$OUT"
status=${PIPESTATUS[0]}
echo "saved: $OUT" >&2
exit "$status"
