#!/bin/bash
# Rehearses the WordPress control app on its VM against the real thing, from the ThinkPad:   bash scripts/wp_panel_rehearsal_aopenpanel.sh [options]
# Run it after aopenvms/wp_panel_aopenvms.yml has run and the app's public ssh key is in wp_ctl_panel_keys on dellwp (dellwp/wp_sites_dellwp.yml --tags wp_ctl). Steps:
#   0. preflight   ssh and sudo -n work on the VM, Docker answers
#   1. the VM      wp-panel and nginx are active and start at boot; the container is running and healthy, read-only, as 10001:10001, without capabilities or new privileges, with the memory cap equal to its swap
#                  cap, on the host network, with ONE read-only mount (the secrets) and no secret in its environment; the secret files are the app's uid and mode 400 and nothing else is in that directory; dellwp's
#                  host key is pinned; the app listens on loopback only, nginx on 443 on the overlay address only and nothing on port 80; gunicorn runs 2 x the CPUs + 1 workers; the certificate has 14 days or more
#                  and belongs to its key; and, INSIDE the container with the real key, the app asks dellwp for the site list over ssh (the forced command) and its own client refuses a verb that changes something
#   2. the name    from here: the name resolves to the VM, 443 answers, ports 80 and 8000 do not; TLS is checked against the name (scripts/wp_panel_rehearsal_driver.py): a handshake for another name is
#                  refused, the security headers are there, /sites without a login goes to /login
#   3. the login   (skip with --no-login) you type the password and a fresh authenticator code, both hidden and passed on standard input only; the driver logs in, opens Sites, a site, Jobs, a job, Audit,
#                  and logs out. One wrong login is made first on purpose (it counts toward the lockout, and the right one clears it)
#   4. the cost    (only with --upload-mb) the upload went through real nginx, the app, ssh and wp-ctl: the container's memory peak stays under 90% of its cap and nginx kept no copy of the file on the VM's disk
# It changes nothing on the VM or on dellwp and makes no commit, EXCEPT with --upload-mb N: then a generated file of N MB goes into dellwp's intake (checked by count and hash) and is removed again. Options:  --yes (do not ask before starting)  --no-login  --ip ADDRESS (the VM's address; default from the inventory)
# --port N (default 443)  --cacert FILE (trust this CA instead of the system's, for a certificate that is not yet public)  --closed-ports "80 8000" (ports that must NOT answer from here)
# What it does not prove: load, the certificate renewal hook (it runs when the certificate renews), a browser, a second operator, or that dellwp's firewall layers are tight (the network test reads the files).
# Paste the whole output, including any FAIL lines.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
YES=no; LOGIN=yes; UPLOAD=0; IP=""; PORT=443; CACERT=""; CLOSED="80 8000"
usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case $1 in --ip|--port|--cacert|--closed-ports|--upload-mb) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; } ;; esac     # without this, `shift 2` with one argument left shifts nothing and the loop never ends
  case $1 in
    --yes) YES=yes; shift ;;
    --no-login) LOGIN=no; shift ;;
    --upload-mb) UPLOAD=${2:-}; shift 2 ;;
    --ip) IP=${2:-}; shift 2 ;;
    --port) PORT=${2:-}; shift 2 ;;
    --cacert) CACERT=${2:-}; shift 2 ;;
    --closed-ports) CLOSED=${2:-}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ $UPLOAD =~ ^[0-9]{1,4}$ ]] && [ "$UPLOAD" -le 3000 ] || { echo "--upload-mb is a number of MB, 0 to 3000" >&2; exit 2; }
[[ $PORT =~ ^[0-9]{1,5}$ ]] || { echo "--port is a number" >&2; exit 2; }
[[ $CLOSED =~ ^[0-9\ ]*$ ]] || { echo "--closed-ports is a list of port numbers" >&2; exit 2; }
[ -z "$IP" ] || [[ $IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "--ip is one IPv4 address" >&2; exit 2; }
[ -z "$CACERT" ] || [ -f "$CACERT" ] || { echo "--cacert: $CACERT is not a file" >&2; exit 2; }
cd "$REPO" || exit 2

# everything comes from the repo's own variables, nothing is typed here twice
read -r DOMAIN VMIP PUID CONTAINER MEMB BIND SECDIR CERTDIR WPIP CTLUSER < <(python3 -I - "$REPO" <<'PY'
import re, sys, yaml
repo = sys.argv[1]
inv = yaml.safe_load(open(repo + "/inventory.yml"))
def host(name):
    def walk(n):
        if name in (n.get("hosts") or {}):
            return n["hosts"][name]
        for k, c in n.items():
            if isinstance(c, dict) and k not in ("hosts", "vars"):
                r = walk(c)
                if r:
                    return r
        for c in (n.get("children") or {}).values():
            r = walk(c or {})
            if r:
                return r
    return walk(inv)
v = yaml.safe_load(open(repo + "/host_vars/aopenpanel/vars.yml"))
svc = yaml.safe_load(open(repo + "/group_vars/all/services.yml"))["services"]["wp_panel"]
dom = yaml.safe_load(open(repo + "/group_vars/all/domain.yml"))
le = "/etc/ssl/le/lab." + dom["base_domain"] if "base_domain" in dom else sys.exit("no base_domain")
m = re.fullmatch(r"([0-9]+)m", str(v["panel_memory"])) or sys.exit("panel_memory is not NNNm")
print(svc["lab_domain"], host("aopenpanel")["overlay_ip"], v["panel_uid"], v["panel_container"], int(m[1]) * 1048576, v["panel_bind"], v["panel_secret_dir"], le, host("dellwp")["overlay_ip"],
      yaml.safe_load(open(repo + "/host_vars/dellwp/vars.yml"))["wp_ctl_user"])
PY
) || exit 2
[ -n "${CTLUSER:-}" ] || exit 2
[ -n "$IP" ] || IP=$VMIP
FAIL=0

# ---- the VM side: a small library of checks, and a helper that runs a snippet as root on the VM with it
read -r -d '' REMOTE_LIB <<'LIB'
set -u
FAIL=0
ok() { echo "ok: $*"; }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
check() { if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1 (wanted '$3', got '$2')"; fi; }
finish() { [ "$FAIL" = 0 ] && echo "   (all ok)"; [ "$FAIL" -gt 100 ] && FAIL=100; exit "$FAIL"; }
insp() { docker inspect -f "$1" "$C" 2>/dev/null </dev/null; }
LIB
vmtest() { { printf '%s\n' "$REMOTE_LIB"; cat; } | ssh -o BatchMode=yes "admin@$VMIP" "sudo -n env C=$CONTAINER PUID=$PUID MEMB=$MEMB BIND=$BIND SECDIR=$SECDIR CERTDIR=$CERTDIR VMIP=$VMIP WPIP=$WPIP DOMAIN=$DOMAIN bash -s"; }
vmrun() { vmtest; local rc=$?; FAIL=$((FAIL + rc)); return 0; }

# ---- this side
say() { echo; echo "== $*"; }
bump() { local rc=$1; shift; if [ "$rc" != 0 ]; then echo "FAIL: $* (exit $rc)"; FAIL=$((FAIL+1)); return 1; fi; echo "ok: $*"; return 0; }
yn() { local a; read -r -p "$1 [y/N] " a; [ "$a" = y ]; }
summary() { echo; echo "== summary"; [ "$FAIL" = 0 ] && echo "All checks passed." || echo "$FAIL check(s) failed."; }
fatal() { echo "FAIL: $*"; FAIL=$((FAIL+1)); summary; exit "$FAIL"; }
probe() { timeout 4 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

echo "Control app rehearsal: https://$DOMAIN/ on $IP:$PORT (the VM aopenpanel), dellwp $WPIP behind it. Changes nothing."
[ "$YES" = yes ] || yn "Go on?" || { echo "Nothing was done."; exit 1; }

say "0. preflight"
ssh -o BatchMode=yes "admin@$VMIP" true </dev/null 2>/dev/null; bump $? "ssh to the VM ($VMIP) works without a password" || fatal "cannot ssh to admin@$VMIP"
ssh -o BatchMode=yes "admin@$VMIP" 'sudo -n true' </dev/null 2>/dev/null; bump $? "sudo -n works on the VM" || fatal "sudo -n does not work on the VM"

say "1. the VM"
vmrun <<'EOF'
for u in wp-panel nginx; do
  check "$u is active" "$(systemctl is-active "$u" 2>&1)" active
  check "$u starts at boot" "$(systemctl is-enabled "$u" 2>&1)" enabled
done
check "the container is running" "$(insp '{{.State.Running}}')" true
check "its health check says healthy" "$(insp '{{.State.Health.Status}}')" healthy
check "its image is wp-panel:<a 12 character hash>" "$(insp '{{.Config.Image}}' | grep -Ec '^wp-panel:[0-9a-f]{12}$')" 1
check "the root file system is read-only" "$(insp '{{.HostConfig.ReadonlyRootfs}}')" true
check "it runs as $PUID:$PUID" "$(insp '{{.Config.User}}')" "$PUID:$PUID"
check "every capability is dropped" "$(insp '{{.HostConfig.CapDrop}}' | grep -c ALL)" 1
check "no new privileges" "$(insp '{{.HostConfig.SecurityOpt}}' | grep -c no-new-privileges)" 1
check "it is not privileged" "$(insp '{{.HostConfig.Privileged}}')" false
check "the memory cap" "$(insp '{{.HostConfig.Memory}}')" "$MEMB"
check "the memory plus swap cap is the same" "$(insp '{{.HostConfig.MemorySwap}}')" "$MEMB"
check "it uses the host network, like the watcher's containers" "$(insp '{{.HostConfig.NetworkMode}}')" host
check "its only mount is the secret directory, read-only" "$(insp '{{range .Mounts}}{{.Source}}:{{.Destination}}:{{.RW}} {{end}}')" "$SECDIR:/run/secrets:false "
check "no secret is in its environment" "$(insp '{{range .Config.Env}}{{println .}}{{end}}' | grep -Eci 'BEGIN|scrypt|pbkdf2|totp|session_key|password')" 0
for f in session_key password_hash totp_secret panel_ssh_key known_hosts; do
  check "$f belongs to uid $PUID, mode 400" "$(stat -c '%u %a' "$SECDIR/$f" 2>&1)" "$PUID 400"
done
check "nothing else is in the secret directory" "$(ls -A "$SECDIR" | sort | tr '\n' ' ')" "known_hosts panel_ssh_key password_hash session_key totp_secret "
check "the secret directory belongs to root and is not writable by others" "$(stat -c '%u %a' "$SECDIR")" "0 755"
check "dellwp's host key is pinned by its address" "$(cut -d' ' -f1,2 "$SECDIR/known_hosts")" "$WPIP ssh-ed25519"
listen=$(ss -ltnH)
check "the app listens on loopback only" "$(echo "$listen" | awk '$4 ~ /:8000$/ {print $4}' | sort -u | tr '\n' ' ')" "$BIND "
check "nginx listens on 443 on the overlay address only" "$(echo "$listen" | awk '$4 ~ /:443$/ {print $4}' | sort -u | tr '\n' ' ')" "$VMIP:443 "
check "nothing listens on port 80" "$(echo "$listen" | awk '$4 ~ /:80$/' | wc -l)" 0
cpus=$(nproc)
# no ps options on purpose: `docker top -eo args` without pid fails ("Couldn't find PID field in ps output", seen on the VM 2026-10-29); the default columns carry it
check "gunicorn runs 2 x $cpus CPUs + 1 workers and a master" "$(docker top "$C" </dev/null | grep -c 'gunicorn')" "$((2 * cpus + 2))"
echo "   memory: $(docker stats --no-stream --format '{{.MemUsage}} ({{.MemPerc}})' "$C" </dev/null)"
end=$(openssl x509 -in "$CERTDIR/fullchain.pem" -noout -enddate | cut -d= -f2)
days=$(( ($(date -d "$end" +%s) - $(date +%s)) / 86400 ))
check "the certificate has 14 days or more left" "$([ "$days" -ge 14 ] && echo yes || echo "no ($days days)")" yes
check "the certificate and its key belong together" "$(openssl x509 -in "$CERTDIR/fullchain.pem" -pubkey -noout | sha256sum | cut -c1-16)" "$(openssl pkey -in "$CERTDIR/privkey.pem" -pubout | sha256sum | cut -c1-16)"
check "the private key is not readable by others" "$(stat -c '%a' "$CERTDIR/privkey.pem")" 640
PYCODE='import os
from wp_panel.ctl import Ctl, CtlError
c = Ctl(os.environ["PANEL_CTL_HOST"], os.environ["PANEL_CTL_USER"], "/run/secrets/panel_ssh_key", "/run/secrets/known_hosts")
print("sites:", len(c.call("list")["sites"]))
try:
    c.call("delete", site="x")
except CtlError as e:
    print("refused:", e.kind)'
out=$(docker exec "$C" python -c "$PYCODE" </dev/null 2>&1)
echo "$out" | sed 's/^/   | /'
check "inside the container the app asks dellwp for the sites over ssh (the forced command)" "$(echo "$out" | grep -c '^sites: [0-9]')" 1
check "and its own client refuses a verb that changes something" "$(echo "$out" | grep -c '^refused: not_allowed$')" 1
finish
EOF

say "2. the name, from here"
R=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -n 1)
[ "$R" = "$IP" ]; bump $? "$DOMAIN resolves to $IP (it says: ${R:-nothing})"
probe "$IP" "$PORT"; bump $? "port $PORT answers on $IP"
for p in $CLOSED; do
  if probe "$IP" "$p"; then bump 1 "port $p must not answer from here, and it does"; else bump 0 "port $p does not answer from here"; fi
done
DRV=(python3 -I scripts/wp_panel_rehearsal_driver.py --host "$DOMAIN" --ip "$IP" --port "$PORT")
[ -z "$CACERT" ] || DRV+=(--cacert "$CACERT")
"${DRV[@]}" </dev/null | sed 's/^/   /'; RC=${PIPESTATUS[0]}
FAIL=$((FAIL + RC))

if [ "$LOGIN" = yes ]; then
  say "3. the login (hidden input; nothing is shown or stored)"
  echo "   Type the control app's password, then a code from your authenticator app. The code is read last, so it is fresh."
  read -r -s -p "   Password: " PW; echo
  read -r -s -p "   Authenticator code: " CODE; echo
  if [ -z "$PW" ] || [ -z "$CODE" ]; then bump 1 "a password and a code are needed (or run with --no-login)"; else
    UP=(); [ "$UPLOAD" -eq 0 ] || UP=(--upload-mb "$UPLOAD")
    printf '%s\n%s\n' "$PW" "$CODE" | "${DRV[@]}" --login-only "${UP[@]}" | sed 's/^/   /'; RC=${PIPESTATUS[1]}
    FAIL=$((FAIL + RC))
  fi
  PW=""; CODE=""
else
  echo; echo "(3. the login: skipped with --no-login)"
fi

if [ "$LOGIN" = yes ] && [ "$UPLOAD" -gt 0 ]; then
  say "4. what the upload cost the VM"
  vmrun <<'EOF'
id=$(docker inspect -f '{{.Id}}' "$C" </dev/null)
peak_file=$(ls /sys/fs/cgroup/system.slice/docker-"$id".scope/memory.peak 2>/dev/null | head -n 1)
if [ -n "$peak_file" ]; then
  peak=$(cat "$peak_file")
  echo "   memory peak of the container since it started: $((peak / 1048576)) MB of $((MEMB / 1048576)) MB"
  [ "$peak" -lt $((MEMB * 9 / 10)) ]; check "the memory peak is under 90% of the cap" "$?" 0
else
  echo "   (no memory.peak file here: another cgroup layout; look at docker stats while uploading)"
fi
body=$(du -sk /var/lib/nginx/body 2>/dev/null | cut -f1)
check "nginx kept no copy of the upload on this disk (/var/lib/nginx/body is under 1 MB)" "$([ "${body:-0}" -lt 1024 ] && echo small || echo "${body} KB")" small
finish
EOF
fi

summary
if [ "$UPLOAD" -gt 0 ] && [ "$LOGIN" = yes ]; then
  echo "   Changed: one generated file of $UPLOAD MB was put into dellwp's intake and removed again. Nothing was committed or edited in the repo."
else
  echo "   Nothing was changed on the VM or on dellwp, and nothing was committed or edited in the repo."
fi
exit "$FAIL"
