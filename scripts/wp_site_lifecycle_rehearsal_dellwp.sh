#!/bin/bash
# Rehearses ONE WHOLE LIFE of a WordPress site on dellwp against the real thing, from the ThinkPad:   bash scripts/wp_site_lifecycle_rehearsal_dellwp.sh [options]
#   1. create    an empty site on hold (wp_sites.py create --no-install): containers up, no WordPress, hold answers a local request and refuses a visitor
#   2. import    the practice .wpress (wp_sites.py import): the preflight, the copy and its hash, then on the VM the load, the options that name code reset, the copy-in, the reinstall from
#                wordpress.org, WP-CLI, the scan, the report and the first backup. Everything is checked: tables, URLs, no PHP under uploads, nothing of the friend's code that is not from
#                wordpress.org on the site, ownership, a snapshot, the report, the phone notification (you say whether it came)
#   3. deny      the import is turned down (your phone's Deny button, or wp_sites.py reset with --no-phone) and the site must be exactly as it was before the import, tables and files
#   4. import    again, which also proves the file left by the first try is replaced
#   5. approve   with the phone's Approve button (or wp_sites.py approve with --no-phone): held code copied in root-owned, the intake file and the work directory gone, still on hold
#   6. operate   the verifier (wp_site_verify_dellwp.sh), wp-site-update --check, a backup, a restore drill, the checksums
#   7. live      (only with --with-live, and it asks its own question) public through the tunnel, answers, and back to hold
#   8. stop      a final backup, containers down, the paused page; then start: back on hold with the data
#   9. delete    wp_sites.py delete (you type the name twice): the site is gone, its archive is on the VM, the repo says closed
# It makes real commits to vars.yml (create, stop, start, delete: --commit) on your current branch, which must not be main, and leaves the site's tombstone there for good (a name is never reused).
# The archive of the deleted site stays on the VM for 14 days. Options:  --site NAME (default rehearsal)  --file PATH (default wpress/*.wpress)  --no-phone  --with-live  --until PHASE
# (create, import, deny, approve, operate, live, stop, delete: stop after that phase, leaving the site as it is)  --yes (do not ask before starting).
# It stops at the first phase that fails badly and says how to clean up. What it does not prove: that the real Elementor or WooCommerce commands behave like the discovered names (the report says
# what ran), that a friend's own plugin is safe, anything about the public path unless --with-live, or the 02:00 update timer.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SITE=rehearsal; FILE=""; PHONE=yes; LIVE=no; UNTIL=delete; YES=no
usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case $1 in
    --site) SITE=${2:-}; shift 2 ;;
    --file) FILE=${2:-}; shift 2 ;;
    --no-phone) PHONE=no; shift ;;
    --with-live) LIVE=yes; shift ;;
    --until) UNTIL=${2:-}; shift 2 ;;
    --yes) YES=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ $SITE =~ ^[a-z][a-z0-9-]{1,23}$ ]] || { echo "--site must be a site name: lowercase letters, digits and hyphens, 2 to 24 characters" >&2; exit 2; }
case $UNTIL in create|import|deny|approve|operate|live|stop|delete) ;; *) echo "--until must be one of: create import deny approve operate live stop delete" >&2; exit 2 ;; esac
cd "$REPO" || exit 2
if [ -z "$FILE" ]; then FILE=$(ls "$REPO"/wpress/*.wpress 2>/dev/null | head -n 1); fi
[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "no .wpress to import: give one with --file (wpress/*.wpress is where the practice file lives)" >&2; exit 2; }
FILENAME=$(basename "$FILE")
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" != main ] || { echo "you are on main: the rehearsal commits to vars.yml, do it on a branch" >&2; exit 2; }
git diff --quiet HEAD -- host_vars/dellwp/vars.yml || { echo "host_vars/dellwp/vars.yml has uncommitted changes: commit or revert them first" >&2; exit 2; }

# everything the VM side needs comes from the repo's own variables, nothing is typed here twice
read -r DOMAIN BIN SITES INTAKE LISTEN PROBE BDIR ARCH ADIR < <(python3 -I - "$REPO" "$SITE" <<'PY'
import sys, yaml
repo, site = sys.argv[1:3]
h = yaml.safe_load(open(repo + "/host_vars/dellwp/vars.yml"))
if any(s["name"] == site for s in h["wp_sites"]):
    sys.exit("'%s' is already in wp_sites (a closed site's name is never reused): choose another with --site" % site)
m = h["wp_data_mount"]
def r(v):
    out = v.replace("{{ wp_data_mount }}", m)
    if "{{" in out:
        sys.exit("cannot resolve %r" % v)
    return out
print(h["wp_domain"], h["wp_tools_bin"], m + "/sites", r(h["wp_intake_dir"]), h["wp_nginx_listen_port"], h["wp_nginx_probe_port"], r(h["wp_backup_dir"]), r(h["wp_archive_dir"]), h["wp_approvals_dir"])
PY
) || exit 2
[ -n "${ADIR:-}" ] || exit 2
HOST=$SITE.$DOMAIN
FAIL=0; PHASE=""; MANUAL=()

# ---- the VM side: a library of checks, and a helper that runs a snippet as root on dellwp with it
read -r -d '' REMOTE_LIB <<'LIB'
set -u
FAIL=0; DIR=$SITES/$SITE; HOST=$SITE.$DOMAIN; WORK=$INTAKE/work-$SITE; UP=wp_${SITE//-/_}_php; DB=wp_${SITE//-/_}_db; BASE=/root/.lifecycle-$SITE.base
ok() { echo "ok: $*"; }
bad() { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
check() { if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1 (wanted '$3', got '$2')"; fi; }
has() { if [ "$2" -gt 0 ] 2>/dev/null; then ok "$1 ($2)"; else bad "$1 (got '$2')"; fi; }
code() { local path=$1; shift; curl -s -o /dev/null -w '%{http_code}' --max-time 30 -H "Host: $HOST" -H 'X-Forwarded-Proto: https' "$@" "http://127.0.0.1:$LISTEN$path" </dev/null; }
wpc() { "$BIN/wp-site-cli" "$SITE" "$@" </dev/null; }
db() { docker exec -e SQL="$1" "$DB" sh -c 'MYSQL_PWD="$MARIADB_PASSWORD" exec mariadb -u"$MARIADB_USER" -N -B -e "$SQL" "$MARIADB_DATABASE"' </dev/null; }
tables() { db "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()"; }
tree() { find "$DIR/html/wp-content" -printf '%P %u %m\n' 2>/dev/null | sort; }
ledger() { grep -P "\t$SITE\t" "$INTAKE/ledger.log" 2>/dev/null | cut -f3 | tr '\n' ' '; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null </dev/null)" = true ]; }
finish() { [ "$FAIL" = 0 ] && echo "   (all ok)"; [ "$FAIL" -gt 100 ] && FAIL=100; exit "$FAIL"; }

hold_checks() {
  check "hold: a request from the VM itself is answered" "$(code / | grep -E -c '^(200|301|302)$')" 1
  check "hold: a request that carries a visitor address is refused" "$(code / -H 'CF-Connecting-IP: 203.0.113.9')" 403
}

imported_checks() {
  [ -d "$WORK" ] && ok "the work directory exists" || bad "no work directory"
  check "the import is at stage reported" "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["stage"])' "$WORK/PLAN.json" 2>&1)" reported
  check "the report is private (600)" "$(stat -c %a "$WORK/REPORT.txt" 2>&1)" 600
  has "the database was loaded (tables)" "$(tables)"
  check "WordPress is installed" "$(wpc core is-installed >/dev/null 2>&1 && echo yes || echo no)" yes
  check "siteurl is this site's name" "$(wpc option get siteurl 2>&1)" "https://$HOST"
  check "home is this site's name" "$(wpc option get home 2>&1)" "https://$HOST"
  check "search engines are discouraged (blog_public)" "$(wpc option get blog_public 2>&1)" 0
  AP=$(wpc option get active_plugins --format=json 2>&1); case $AP in *litespeed*|*all-in-one-wp-migration*) bad "active_plugins still names a dropped plugin: $AP" ;; *) ok "active_plugins names none of the dropped plugins" ;; esac
  python3 -I - "$WORK/PLAN.json" "$DIR/html/wp-content" "$WORK/wp-content" <<'PY'
import json, os, sys
rec = json.load(open(sys.argv[1])); site, work = sys.argv[2], sys.argv[3]
bad = 0
for kind, sub in (("plugins", "plugins"), ("themes", "themes")):
    for slug, d in sorted(rec[kind].items()):
        on_site, held = os.path.isdir(os.path.join(site, sub, slug)), os.path.isdir(os.path.join(work, sub, slug))
        want = {"reinstall": (True, False), "approve": (False, True), "dropped": (False, False)}[d]
        if (on_site, held) == want:
            print("ok: %s %s is %s (%s)" % (kind[:-1], slug, "on the site" if on_site else "held back" if held else "not carried over", d))
        else:
            print("FAIL: %s %s decided '%s' but on_site=%s held=%s" % (kind[:-1], slug, d, on_site, held)); bad += 1
sys.exit(bad)
PY
  FAIL=$((FAIL + $?))
  check "no PHP or hidden file under uploads" "$(find "$DIR/html/wp-content/uploads" -mindepth 1 \( -iname '*.php*' -o -name '.*' \) 2>/dev/null | wc -l)" 0
  check "uploads belong to the php user" "$(find "$DIR/html/wp-content/uploads" ! -user 33 2>/dev/null | wc -l)" 0
  check "the plugins directory is root's (php cannot write code)" "$(stat -c %u "$DIR/html/wp-content/plugins")" 0
  has "the first backup exists (snapshots)" "$(ls "$BDIR/$SITE" 2>/dev/null | grep -c '^[0-9]\{8\}T')"
  check "the ledger shows the unpack, the load and the report" "$(ledger | grep -c 'unpacked .*loaded .*reported')" 1
  has "tokens were issued for the phone (1 or 2)" "$(python3 -I -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("tokens", {})))' "$WORK/PLAN.json" 2>/dev/null)"
  hold_checks
}
LIB
vmrun() {
  { printf '%s\n' "$REMOTE_LIB"; cat; } | ssh dellwp "sudo -n env SITE=$SITE DOMAIN=$DOMAIN BIN=$BIN SITES=$SITES INTAKE=$INTAKE LISTEN=$LISTEN PROBE=$PROBE BDIR=$BDIR ARCH=$ARCH ADIR=$ADIR FILENAME=$FILENAME bash -s"
  local rc=$?; FAIL=$((FAIL + rc)); return 0
}

# ---- the ThinkPad side
say() { echo; echo "== $*"; }
bump() { local rc=$1; shift; if [ "$rc" != 0 ]; then echo "FAIL: $* (exit $rc)"; FAIL=$((FAIL+1)); return 1; fi; echo "ok: $*"; return 0; }
yn() { local a; read -r -p "$1 [y/N] " a; [ "$a" = y ]; }
confirmed() { if yn "$1"; then MANUAL+=("yes: $1"); echo "ok: $1"; else MANUAL+=("NO: $1"); echo "FAIL: $1"; FAIL=$((FAIL+1)); fi; }
stop_here() { [ "$UNTIL" = "$1" ] && { echo; echo "Stopped after '$1' as asked (--until). The site $SITE is left as it is."; summary; exit "$FAIL"; }; }
summary() {
  echo; echo "== summary"; printf '   %s\n' "${MANUAL[@]:-nothing was asked of you}"
  [ "$FAIL" = 0 ] && echo "All checks passed ($PHASE)." || echo "$FAIL check(s) failed (last phase: $PHASE)."
}
cleanup_hint() {
  echo; echo "The rehearsal stopped at '$PHASE'. The site '$SITE' may exist. To clean up:"
  echo "  python3 scripts/wp_sites.py reset $SITE        (if an import is waiting; it asks you to type the name)"
  echo "  python3 scripts/wp_sites.py stop $SITE --commit && python3 scripts/wp_sites.py delete $SITE --commit"
}
fatal() { echo "FAIL: $*"; FAIL=$((FAIL+1)); summary; cleanup_hint; exit "$FAIL"; }
waitgone() {   # until the work directory is gone on the VM; $1 = seconds
  local t=0
  while [ "$t" -lt "$1" ]; do ssh dellwp "sudo -n test ! -e '$INTAKE/work-$SITE'" </dev/null 2>/dev/null && return 0; sleep 5; t=$((t+5)); done
  return 1
}

echo "Lifecycle rehearsal of '$SITE' ($HOST) on dellwp, branch $BRANCH, file $FILENAME, phone $PHONE, live $LIVE, until $UNTIL."
echo "It creates, imports, denies, imports again, approves, operates, stops, starts and deletes the site, with real commits to host_vars/dellwp/vars.yml."
[ "$YES" = yes ] || yn "Go on?" || { echo "Nothing was done."; exit 1; }

PHASE=preflight; say "0. preflight"
ssh -o BatchMode=yes dellwp true </dev/null 2>/dev/null; bump $? "ssh to dellwp works without a password" || fatal "cannot ssh to dellwp"
ssh dellwp 'sudo -n true' </dev/null 2>/dev/null; bump $? "sudo -n works on dellwp" || fatal "sudo -n does not work: the tools are called with it"
vmrun <<'EOF'
for x in wp-site-import wp-site-cli wp-site-ext wp-site-backup wp-site-restore wp-site-update wp-site-delete; do [ -x "$BIN/$x" ] && ok "$x is installed" || bad "$BIN/$x is missing: run dellwp/wp_sites_dellwp.yml"; done
check "wp-site-import prints its usage" "$("$BIN/wp-site-import" </dev/null 2>&1 | head -n 1 | cut -c1-6)" usage:
[ -d "$INTAKE" ] && ok "the intake directory exists" || bad "no $INTAKE"
for f in key approver listener; do [ -s "$ADIR/$f" ] && ok "approvals: $ADIR/$f is there" || bad "approvals: $ADIR/$f is missing (dellwp/wp_approvals_dellwp.yml)"; done
systemctl is-active --quiet wp-approval-listener && ok "the approval listener is running" || bad "wp-approval-listener is not running"
[ -r /etc/ntfy-notify.conf ] && ok "ntfy-notify.conf is readable" || bad "no /etc/ntfy-notify.conf"
finish
EOF
python3 -I scripts/wp_wpress.py preflight "$FILE" | tail -n 1 | grep -q '^GO' ; bump $? "the archive's preflight says GO" || fatal "the preflight does not say GO for $FILENAME"
[ "$FAIL" = 0 ] || fatal "the preflight found problems; fix them first"

PHASE=create; say "1. create an empty site on hold"
python3 scripts/wp_sites.py create --name "$SITE" --no-install --policy notify --yes --commit; bump $? "wp_sites.py create --no-install" || fatal "create failed"
vmrun <<'EOF'
running "$UP" && ok "the php container is running" || bad "$UP is not running"
running "$DB" && ok "the database container is running" || bad "$DB is not running"
check "the database is empty" "$(tables)" 0
check "WordPress is not installed" "$(wpc core is-installed >/dev/null 2>&1 && echo yes || echo no)" no
[ ! -e "$DIR/stopped.flag" ] && ok "not stopped" || bad "stopped.flag"
[ ! -e "$WORK" ] && ok "no work directory" || bad "a work directory already exists"
check "hold: the installer answers a request from the VM" "$(code /wp-admin/install.php)" 200
check "hold: a request that carries a visitor address is refused" "$(code / -H 'CF-Connecting-IP: 203.0.113.9')" 403
tree > "$BASE"; ok "the site's file tree was recorded ($(wc -l < "$BASE") entries) to compare after a deny"
finish
EOF
stop_here create

PHASE=import1; say "2. import the practice file"
python3 scripts/wp_sites.py import "$SITE" "$FILE" --yes; RC=$?
[ "$RC" = 0 ] && bump 0 "wp_sites.py import finished with the report" || fatal "import exited $RC (1 = a step failed, 2 = refused): read the report above"
vmrun <<'EOF'
imported_checks
finish
EOF
if [ "$PHONE" = yes ]; then confirmed "A notification 'Import ready: $SITE' arrived on the phone with Approve and Deny buttons"; else echo "(--no-phone: the notification is not asked about)"; fi
stop_here import

PHASE=deny; say "3. turn the import down"
if [ "$PHONE" = yes ]; then
  echo "   Tap DENY on the notification now (waiting up to 10 minutes)."
  waitgone 600; bump $? "the work directory is gone after the tap" || fatal "no decision arrived: is the listener running (journalctl -u wp-approval-listener)?"
  confirmed "A message 'Phone decision done: ...' arrived on the phone after Deny"
else
  python3 scripts/wp_sites.py reset "$SITE"; bump $? "wp_sites.py reset (type the name when asked)" || fatal "reset failed"
fi
vmrun <<'EOF'
check "no work directory" "$([ -e "$WORK" ] && echo yes || echo no)" no
check "the database is empty again" "$(tables)" 0
check "the site's file tree is exactly what it was before the import" "$(tree | diff - "$BASE" >/dev/null && echo same || echo different)" same
[ -f "$INTAKE/$FILENAME" ] && ok "the .wpress is still in the intake directory" || bad "the .wpress is gone from the intake directory"
check "the ledger records the decision" "$(ledger | grep -c 'reset')" 1
check "no partial file in the intake directory" "$(ls -A "$INTAKE" | grep -c '^\.recv-')" 0
finish
EOF
stop_here deny

PHASE=import2; say "4. import again (the file left by the first try is replaced)"
python3 scripts/wp_sites.py import "$SITE" "$FILE" --yes; RC=$?
[ "$RC" = 0 ] && bump 0 "the second import finished with the report" || fatal "second import exited $RC"
vmrun <<'EOF'
imported_checks
finish
EOF

PHASE=approve; say "5. approve"
if [ "$PHONE" = yes ]; then
  confirmed "The second notification arrived with an Approve button (the import was clean), or you know why it did not"
  echo "   Tap APPROVE now (waiting up to 10 minutes). If there is no Approve button, press Ctrl-C and use: python3 scripts/wp_sites.py approve $SITE"
  waitgone 600; bump $? "the work directory is gone after the tap" || fatal "no decision arrived"
  confirmed "A message 'Phone decision done: ...' arrived on the phone after Approve"
else
  python3 scripts/wp_sites.py approve "$SITE" --yes; bump $? "wp_sites.py approve" || fatal "approve failed"
fi
vmrun <<'EOF'
check "no work directory" "$([ -e "$WORK" ] && echo yes || echo no)" no
check "the .wpress is gone from the intake directory" "$([ -e "$INTAKE/$FILENAME" ] && echo yes || echo no)" no
check "the ledger records the approval" "$(ledger | grep -c 'approved')" 1
has "the database is still loaded (tables)" "$(tables)"
check "siteurl is still this site" "$(wpc option get siteurl 2>&1)" "https://$HOST"
check "the plugins directory is root's" "$(stat -c %u "$DIR/html/wp-content/plugins")" 0
check "nothing under plugins, themes or mu-plugins is writable by the php user" "$(find "$DIR/html/wp-content/plugins" "$DIR/html/wp-content/themes" "$DIR/html/wp-content/mu-plugins" -user 33 2>/dev/null | wc -l)" 0
check "no PHP or hidden file under uploads" "$(find "$DIR/html/wp-content/uploads" -mindepth 1 \( -iname '*.php*' -o -name '.*' \) 2>/dev/null | wc -l)" 0
hold_checks
finish
EOF
stop_here approve

PHASE=operate; say "6. operate the site"
bash scripts/wp_site_verify_dellwp.sh "$SITE" | tail -n 4; bump "${PIPESTATUS[0]}" "the verifier ends with 0 failed checks" || true
vmrun <<'EOF'
"$BIN/wp-site-update" "$SITE" --check </dev/null | sed 's/^/   | /'; check "wp-site-update --check ran" "${PIPESTATUS[0]}" 0
"$BIN/wp-site-backup" "$SITE" --force </dev/null >/dev/null 2>&1; check "a backup now" "$?" 0
"$BIN/wp-site-restore" "$SITE" latest </dev/null 2>&1 | tail -n 3 | sed 's/^/   | /'; check "the restore drill into scratch" "${PIPESTATUS[0]}" 0
CS=$(wpc core verify-checksums 2>&1); echo "$CS" | grep -q '^Success' && ok "core checksums match wordpress.org" || bad "core checksums: $CS"
PC=$(wpc plugin verify-checksums --all 2>&1); echo "$PC" | grep -q -i 'error' && bad "plugin checksums: $(echo "$PC" | tail -n 3)" || ok "plugin checksums: no error"
finish
EOF
stop_here operate

if [ "$LIVE" = yes ]; then
  PHASE=live; say "7. go live (public) and back to hold"
  python3 scripts/wp_sites.py modify "$SITE" --set state=live --commit; bump $? "wp_sites.py modify state=live (it asked its own question)" || fatal "going live failed"
  sleep 5
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$HOST/" </dev/null)
  case $C in 200|301|302) bump 0 "https://$HOST/ answers $C through the tunnel" ;; *) bump 1 "https://$HOST/ answered '$C'" ;; esac
  python3 scripts/wp_sites.py modify "$SITE" --set state=hold --yes --commit; bump $? "back on hold" || fatal "could not put it back on hold"
  stop_here live
else
  echo; echo "(7. live: skipped; use --with-live to go public through the tunnel and back)"
fi

PHASE=stop; say "8. stop, then start"
python3 scripts/wp_sites.py stop "$SITE" --yes --commit; bump $? "wp_sites.py stop (final backup first)" || fatal "stop failed"
vmrun <<'EOF'
check "no container of the site is running" "$(docker ps -q --filter "name=^wp_${SITE//-/_}_" </dev/null | wc -l)" 0
[ -e "$DIR/stopped.flag" ] && ok "stopped.flag" || bad "no stopped.flag"
check "visitors get the paused page (503)" "$(code / -H 'CF-Connecting-IP: 198.51.100.9')" 503
[ -d "$DIR/db" ] && [ -d "$DIR/html/wp-content" ] && ok "the data is intact" || bad "the data is not intact"
finish
EOF
python3 scripts/wp_sites.py start "$SITE" --yes --commit; bump $? "wp_sites.py start (back on hold)" || fatal "start failed"
vmrun <<'EOF'
running "$UP" && running "$DB" && ok "both containers are running again" || bad "containers did not come back"
has "the data survived (tables)" "$(tables)"
check "siteurl survived" "$(wpc option get siteurl 2>&1)" "https://$HOST"
hold_checks
finish
EOF
stop_here stop
python3 scripts/wp_sites.py stop "$SITE" --yes --commit; bump $? "stopped again before the delete (a delete needs a stopped site)" || fatal "second stop failed"

PHASE=delete; say "9. delete (you will be asked to type the name twice)"
python3 scripts/wp_sites.py delete "$SITE" --commit; bump $? "wp_sites.py delete" || fatal "delete failed"
vmrun <<'EOF'
check "the site directory is gone" "$([ -e "$DIR" ] && echo yes || echo no)" no
check "its containers are gone" "$(docker ps -aq --filter "name=^wp_${SITE//-/_}_" </dev/null | wc -l)" 0
has "its archive is on the VM (kept 14 days)" "$(ls -d "$ARCH/$SITE"-* 2>/dev/null | wc -l)"
check "the intake directory has nothing of it" "$(ls "$INTAKE" | grep -c "$SITE")" 0
rm -f "$BASE"
finish
EOF
python3 -I -c 'import sys, yaml; d = yaml.safe_load(open("host_vars/dellwp/vars.yml")); s = next(x for x in d["wp_sites"] if x["name"] == sys.argv[1]); sys.exit(0 if s.get("state") == "closed" else 1)' "$SITE"
bump $? "the repo says '$SITE' is closed (a tombstone: the name is never reused)"
summary
echo "   Commits made: $(git log --oneline "$BRANCH" | head -n 6 | grep -c "$SITE") for $SITE (git log --oneline)."
exit "$FAIL"
