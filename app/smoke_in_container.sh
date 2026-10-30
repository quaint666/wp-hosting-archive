# Run INSIDE the built image, as the script of `docker run -i ... sh -s` (scripts/wp_panel_image_rehearsal.sh does it). It makes throwaway secrets, starts the real gunicorn on 127.0.0.1 and asks
# it two questions. It needs no mounted file, so it works wherever Docker does. Run with --read-only, --tmpfs /tmp, /dev/shm and /run/panel: that is how the real container runs.
set -eu
fail() { echo "FAIL: $*"; exit 1; }
d=$(mktemp -d)
diagnose() {      # on any failure: what gunicorn said and whether the kernel killed anything for memory
  rc=$?
  if [ "$rc" != 0 ]; then
    echo "--- gunicorn log, last 40 lines"; tail -n 40 "$d/gunicorn.log" 2>/dev/null || true
    echo "--- memory kills in this container: $(grep -s '^oom_kill ' /sys/fs/cgroup/memory.events || echo unknown)"
  fi
}
trap diagnose EXIT
[ "$(id -u)" = 10001 ] || fail "the container does not run as uid 10001 (it is $(id -u))"
ssh -V 2>&1 | grep -q OpenSSH || fail "there is no ssh in the image"
python -c 'import flask, pyotp, gunicorn, werkzeug' || fail "a package is missing"
if touch /app/should-not-be-writable 2>/dev/null; then fail "the root file system is writable (run with --read-only)"; fi
python -c 'import os, sys, pyotp; from werkzeug.security import generate_password_hash as g; d = sys.argv[1]; open(d + "/session_key", "wb").write(os.urandom(48)); open(d + "/password_hash", "w").write(g("smoke-test-password", method="pbkdf2:sha256:1000")); open(d + "/totp_secret", "w").write(pyotp.random_base32())' "$d"
touch /run/panel/x && rm /run/panel/x || fail "the state directory /run/panel is not writable"
ask() { python -c 'import http.client, sys; c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=5); c.request("GET", sys.argv[1]); r = c.getresponse(); print(r.status, r.getheader("Content-Security-Policy") or "-", r.getheader("Location") or "-")' "$1"; }
PANEL_CTL_HOST=dellwp PANEL_SITE_DOMAIN=example.test PANEL_SECRET_DIR="$d" PANEL_STATE_DIR=/run/panel PANEL_BIND=127.0.0.1:8000 gunicorn -c /app/gunicorn.conf.py wp_panel.wsgi:app >"$d/gunicorn.log" 2>&1 &
pid=$!
n=0
until login=$(ask /login 2>/dev/null); do
  n=$((n + 1)); [ "$n" -lt 100 ] || { cat "$d/gunicorn.log"; fail "the app did not answer in 20 seconds"; }
  kill -0 "$pid" 2>/dev/null || { cat "$d/gunicorn.log"; fail "gunicorn stopped"; }
  sleep 0.2
done
case $login in "200 default-src 'none'"*) echo "ok: /login answers 200 with the policy" ;; *) fail "/login answered: $login" ;; esac
threads=$(python -c 'import os; print(len(os.sched_getaffinity(0)))')
want=$((2 * threads + 1))
n=0
until [ "$(grep -c 'Booting worker' "$d/gunicorn.log")" -ge "$want" ]; do
  n=$((n + 1)); [ "$n" -lt 150 ] || fail "only $(grep -c 'Booting worker' "$d/gunicorn.log") of $want workers started in 30 seconds"
  sleep 0.2
done
echo "ok: $want workers started ($threads CPU threads usable: 2 x $threads + 1)"
sites=$(ask /sites)
case $sites in "302 "*"/login") echo "ok: /sites without a login is sent to /login" ;; *) fail "/sites answered: $sites" ;; esac
kill "$pid"; wait "$pid" 2>/dev/null || true
kills=$(grep -s '^oom_kill ' /sys/fs/cgroup/memory.events | cut -d' ' -f2 || true)
[ -z "$kills" ] || [ "$kills" = 0 ] || fail "the kernel killed $kills process(es) of this container for memory"
peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || true)
[ -z "$peak" ] || echo "memory peak of the container: $((peak / 1048576)) MB"
echo "smoke ok"
