#!/bin/bash
# Simulate visitors on one WordPress site and see what they cost the origin, from the thinkpad but WITHOUT any HTTP leaving dellwp: this starts scripts/wp_traffic_sim.py on dellwp over ssh, and
# that program sends its requests to nginx on dellwp's own loopback port with the site's Host header (the same path scripts/wp_perf_measure.sh uses). Cloudflare and your own network are not in it.
#   bash scripts/wp_traffic_sim.sh <site> [--minutes M] [--concurrency C] [--think S] [--pages N] [--assets N] [--returning F] [--seed N] [--out FILE] [--host NAME]
# Defaults: 2 minutes, 3 visitors at a time, 4 pages each. Run 7 minutes or more to see pages expire from the page cache (it keeps a page 300 s). Everything it prints is also saved to FILE
# (default ~/.local/state/wp-perf/wp_sim_<site>_<UTC stamp>.txt). The requests are anonymous reads plus WooCommerce's own "refresh the mini cart" POST; no login, no cart, no admin. Read the model
# and what it does not show at the top of scripts/wp_traffic_sim.py. The site gets real (small) load and its access log gets real lines, tagged with the User-Agent wp-traffic-sim/1.
set -euo pipefail
HOST=dellwp; OUT=""; SITE=""; pass=()
while [ $# -gt 0 ]; do
  case $1 in
    --out) OUT=${2:?--out needs a file}; shift ;;
    --host) HOST=${2:?--host needs a name}; shift ;;
    --minutes|--concurrency|--think|--pages|--assets|--returning|--seed)
      [[ ${2:-} =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "$1 needs a number" >&2; exit 2; }
      pass+=("$1" "$2"); shift ;;
    -h|--help) sed -n '2,3p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) [ -z "$SITE" ] || { echo "one site at a time" >&2; exit 2; }; SITE=$1 ;;
  esac
  shift
done
[[ $SITE =~ ^[a-z]([a-z0-9]|-[a-z0-9]){0,22}$ ]] || { echo "name the site: bash scripts/wp_traffic_sim.sh <site>" >&2; exit 2; }
[[ $HOST =~ ^[A-Za-z0-9.-]+$ ]] || { echo "not a host name: $HOST" >&2; exit 2; }
command -v ssh >/dev/null || { echo "missing command: ssh" >&2; exit 2; }
py="$(dirname "${BASH_SOURCE[0]}")/wp_traffic_sim.py"
[ -f "$py" ] || { echo "missing $py" >&2; exit 2; }
umask 077
if [ -z "$OUT" ]; then
  mkdir -p "${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf"
  OUT="${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf/wp_sim_${SITE}_$(date -u +%Y%m%dT%H%M%SZ).txt"
fi
: > "$OUT"
echo "writing to $OUT" >&2
# the arguments were checked above (numbers and flag names only), so the remote command line holds nothing else
ssh -o BatchMode=yes "$HOST" "sudo python3 -u - $SITE ${pass[*]:-}" < "$py" 2>&1 | tee -a "$OUT"
echo "saved: $OUT" >&2
