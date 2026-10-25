#!/bin/bash
# Rehearses ONE WHOLE LIFE of a WordPress site on dellwp against the real thing, from the ThinkPad:   bash scripts/wp_site_lifecycle_rehearsal_dellwp.sh [options]
#   1. create    an empty site on hold (wp_sites.py create --no-install): containers up, no WordPress, hold answers a local request and refuses a visitor
#   2. import    the practice .wpress (wp_sites.py import): the preflight, the copy and its hash, then on the VM the load, the options that name code reset, the copy-in, the reinstall from
#                wordpress.org, WP-CLI, the scan, the report and the first backup. Everything is checked: tables, URLs, no PHP under uploads, nothing of the friend's code that is not from
#                wordpress.org on the site, ownership, a snapshot, the report, the phone notification (you say whether it came)
#   3. deny      the import is turned down (your phone's Deny button, or wp_sites.py reset with --no-phone) and the site must be exactly as it was before the import, tables and files
#   4. import    again, which also proves the file left by the first try is replaced
#   5. approve   with the phone's Approve button (or wp_sites.py approve with --no-phone): held code copied in root-owned, the intake file and the work directory gone, still on hold
#   5b. replace  (only with --with-replace) a second import onto the site that now has data, with --replace: the old site is snapshotted, wiped and the archive imported; you type the site name when
#                the VM asks; a Deny must put the old site back exactly (tables, files, snapshots); then replace again and approve, and the old site's snapshot must be re-tagged pre-update
#   5c. harden   the hosting service's must-use plugins on the real site: our three are loaded and no other, the mount is read-only, ?author and the REST users listing are closed, a wrong user name
#                and a wrong password read the same, an administrator without a second factor is sent to the profile page (the real Two Factor plugin), the password-reset mail goes through the mail
#                plugin (log mode), and the eleventh wrong password for one user name is refused. Uses a throw-away administrator it removes again
#   6. operate   the verifier (wp_site_verify_dellwp.sh), wp-site-update --check, a backup, a restore drill, the checksums
#   7. live      (only with --with-live, and it asks its own question) public through the tunnel, answers, and back to hold
#   8. stop      a final backup, containers down, the paused page; then start: back on hold with the data
#   9. delete    wp_sites.py delete (you type the name twice): the site is gone, its archive is on the VM, the repo says closed
# It makes real commits to vars.yml (create, stop, start, delete: --commit) on your current branch, which must not be main, and leaves the site's tombstone there for good (a name is never reused).
# The archive of the deleted site stays on the VM for 14 days. Options:  --site NAME (default rehearsal)  --existing (the site is already in wp_sites, empty and on hold: skip the create, e.g. after
# a reset)  --file PATH (default wpress/*.wpress)  --no-phone  --with-live  --with-replace  --until PHASE
# (create, import, deny, approve, replace, harden, operate, live, stop, delete: stop after that phase, leaving the site as it is)  --yes (do not ask before starting).
# It stops at the first phase that fails badly and says how to clean up. What it does not prove: that the real Elementor or WooCommerce commands behave like the discovered names (the report says
# what ran), that a friend's own plugin is safe, anything about the public path unless --with-live, or the 02:00 update timer.
set -u
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SITE=rehearsal; FILE=""; PHONE=yes; LIVE=no; REPLACE=no; UNTIL=delete; YES=no; EXISTING=no
usage() { sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case $1 in
    --site) SITE=${2:-}; shift 2 ;;
    --file) FILE=${2:-}; shift 2 ;;
    --no-phone) PHONE=no; shift ;;
    --with-live) LIVE=yes; shift ;;
    --with-replace) REPLACE=yes; shift ;;
    --until) UNTIL=${2:-}; shift 2 ;;
    --yes) YES=yes; shift ;;
    --existing) EXISTING=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ $SITE =~ ^[a-z][a-z0-9-]{1,23}$ ]] || { echo "--site must be a site name: lowercase letters, digits and hyphens, 2 to 24 characters" >&2; exit 2; }
case $UNTIL in create|import|deny|approve|replace|harden|operate|live|stop|delete) ;; *) echo "--until must be one of: create import deny approve replace harden operate live stop delete" >&2; exit 2 ;; esac
cd "$REPO" || exit 2
if [ -z "$FILE" ]; then FILE=$(ls "$REPO"/wpress/*.wpress 2>/dev/null | head -n 1); fi
[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "no .wpress to import: give one with --file (wpress/*.wpress is where the practice file lives)" >&2; exit 2; }
FILENAME=$(basename "$FILE")
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" != main ] || { echo "you are on main: the rehearsal commits to vars.yml, do it on a branch" >&2; exit 2; }
git diff --quiet HEAD -- host_vars/dellwp/vars.yml || { echo "host_vars/dellwp/vars.yml has uncommitted changes: commit or revert them first" >&2; exit 2; }

# everything the VM side needs comes from the repo's own variables, nothing is typed here twice
read -r DOMAIN BIN SITES INTAKE LISTEN PROBE BDIR ARCH ADIR < <(python3 -I - "$REPO" "$SITE" "$EXISTING" <<'PY'
import sys, yaml
repo, site, existing = sys.argv[1:4]
h = yaml.safe_load(open(repo + "/host_vars/dellwp/vars.yml"))
entry = next((s for s in h["wp_sites"] if s["name"] == site), None)
if existing == "yes":
    state = {**h["wp_site_defaults"], **(entry or {})}["state"] if entry else None
    if state != "hold":
        sys.exit("--existing needs '%s' to be in wp_sites and on hold (it is %s)" % (site, state or "not there"))
elif entry:
    sys.exit("'%s' is already in wp_sites (a closed site's name is never reused): choose another with --site, or use --existing for an empty site on hold" % site)
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
# a read that must not load any plugin: Elementor prints PHP 8.5 deprecation notices into the output when it is loaded (seen 2026-10-25), which is not what a check should compare
wpr() { "$BIN/wp-site-cli" "$SITE" "$@" --skip-plugins --skip-themes </dev/null; }
db() { docker exec -e SQL="$1" "$DB" sh -c 'MYSQL_PWD="$MARIADB_PASSWORD" exec mariadb -u"$MARIADB_USER" -N -B -e "$SQL" "$MARIADB_DATABASE"' </dev/null; }
tables() { db "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()"; }
PREFIX=$(sed -n "s/.*\$table_prefix *= *'\([A-Za-z0-9_]*\)'.*/\1/p" "$DIR/wp-config.php" 2>/dev/null | head -n 1)
stored() { db "SELECT option_value FROM ${PREFIX:-wp_}options WHERE option_name='$1'"; }
tree() { find "$DIR/html/wp-content" -printf '%P %u %m\n' 2>/dev/null | sort; }
# the same, without what a snapshot leaves out on purpose (wp-site-backup: cache/, upgrade/, debug.log): a site put back from a snapshot does not have them, and WordPress makes them again when it needs them
tree_cmp() { tree | grep -vE '^(cache|upgrade)( |/)|^debug\.log '; }
snaps() { find "$BDIR/$SITE" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex ".*/[0-9]{8}T[0-9]{6}Z" 2>/dev/null | wc -l; }
ledger() { grep -P "\t$SITE\t" "$INTAKE/ledger.log" 2>/dev/null | cut -f3 | tr '\n' ' '; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null </dev/null)" = true ]; }
finish() { [ "$FAIL" = 0 ] && echo "   (all ok)"; [ "$FAIL" -gt 100 ] && FAIL=100; exit "$FAIL"; }

# Hidden files under uploads: the importer never copies one (they are counted as "not copied"), so what is there was made by a plugin or WordPress itself. They are listed, and each must be a small
# file (1 KB at most), which is what a plugin's own .htaccess is; a large one would be something else.
hidden_check() {
  local list; list=$(find "$DIR/html/wp-content/uploads" -mindepth 1 -name '.*' -type f -printf '%s %u %p\n' 2>/dev/null)
  [ -z "$list" ] || echo "$list" | sed 's/^/   (info) hidden file: /'
  check "no hidden file under uploads is larger than 1 KB" "$(find "$DIR/html/wp-content/uploads" -mindepth 1 -name '.*' -type f -size +1k 2>/dev/null | wc -l)" 0
  check "no hidden directory or link under uploads" "$(find "$DIR/html/wp-content/uploads" -mindepth 1 -name '.*' ! -type f 2>/dev/null | wc -l)" 0
}

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
  check "the stored siteurl is this site's name (read from the database: wp-config.php's WP_SITEURL would hide it from wp option)" "$(stored siteurl)" "https://$HOST"
  check "the stored home is this site's name" "$(stored home)" "https://$HOST"
  check "search engines are discouraged (blog_public)" "$(wpr option get blog_public 2>&1)" 0
  AP=$(wpr option get active_plugins --format=json 2>&1); case $AP in *litespeed*|*all-in-one-wp-migration*) bad "active_plugins still names a dropped plugin: $AP" ;; *) ok "active_plugins names none of the dropped plugins" ;; esac
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
  check "no PHP file over 64 bytes under uploads (a plugin's own 'silence' stub is not the archive's)" "$(find "$DIR/html/wp-content/uploads" -type f -iname '*.php*' -size +64c 2>/dev/null | wc -l)" 0
  hidden_check
  check "uploads belong to the php user" "$(find "$DIR/html/wp-content/uploads" ! -user 33 2>/dev/null | wc -l)" 0
  check "the plugins directory is root's (php cannot write code)" "$(stat -c %u "$DIR/html/wp-content/plugins")" 0
  has "the first backup exists (snapshots)" "$(snaps)"
  grep -E '^  (ClamAV|known vulnerabilities):' "$WORK/REPORT.txt" 2>/dev/null | sed 's/^ */   (info) /'
  check "ClamAV did not fail to run (NOT RUN is allowed until wp_scans_dellwp.yml has run)" "$(grep -c '^  ClamAV: .*FAILED' "$WORK/REPORT.txt" 2>/dev/null || true)" 0
  check "the vulnerability check did not fail to run" "$(grep -c '^  known vulnerabilities: FAILED' "$WORK/REPORT.txt" 2>/dev/null || true)" 0
  check "the ledger shows the unpack, the load and the report" "$(ledger | grep -c 'unpacked .*loaded .*reported')" 1
  has "tokens were issued for the phone (1 or 2)" "$(python3 -I -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("tokens", {})))' "$WORK/PLAN.json" 2>/dev/null)"
  hold_checks
}
LIB
vmtest() {      # the snippet's exit status is the answer
  { printf '%s\n' "$REMOTE_LIB"; cat; } | ssh dellwp "sudo -n env SITE=$SITE DOMAIN=$DOMAIN BIN=$BIN SITES=$SITES INTAKE=$INTAKE LISTEN=$LISTEN PROBE=$PROBE BDIR=$BDIR ARCH=$ARCH ADIR=$ADIR FILENAME=$FILENAME bash -s"
}
has_vulns() {    # does the waiting import's report list known vulnerabilities? Then the phone gets no Approve button, by design (unclean_reasons)
  vmtest <<'EOF' >/dev/null 2>&1
grep -q '^  known vulnerabilities: FOUND' "$WORK/REPORT.txt"
EOF
}
vmrun() {       # the snippet's failures are added to the count
  vmtest
  local rc=$?; FAIL=$((FAIL + rc)); return 0
}

# ---- the ThinkPad side
TIMES=(); T0=""; CUR=""
tick() { [ -z "$T0" ] || { echo "   ($CUR took $((SECONDS - T0)) s, your waiting for taps and typing included)"; TIMES+=("$CUR: $((SECONDS - T0)) s"); T0=""; }; }
say() { tick; echo; echo "== $*"; CUR=$PHASE; T0=$SECONDS; }
bump() { local rc=$1; shift; if [ "$rc" != 0 ]; then echo "FAIL: $* (exit $rc)"; FAIL=$((FAIL+1)); return 1; fi; echo "ok: $*"; return 0; }
yn() { local a; read -r -p "$1 [y/N] " a; [ "$a" = y ]; }
confirmed() { if yn "$1"; then MANUAL+=("yes: $1"); echo "ok: $1"; else MANUAL+=("NO: $1"); echo "FAIL: $1"; FAIL=$((FAIL+1)); fi; }
stop_here() { [ "$UNTIL" = "$1" ] && { echo; echo "Stopped after '$1' as asked (--until). The site $SITE is left as it is."; summary; exit "$FAIL"; }; }
summary() {
  tick
  echo; echo "== summary"; [ "${#TIMES[@]}" = 0 ] || { printf '   took %s\n' "${TIMES[@]}"; echo "   total: $SECONDS s"; }; printf '   %s\n' "${MANUAL[@]:-nothing was asked of you}"
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
if [ "$EXISTING" = yes ]; then echo "(--existing: the site is already declared; no create)"
else python3 scripts/wp_sites.py create --name "$SITE" --no-install --policy notify --yes --commit; bump $? "wp_sites.py create --no-install" || fatal "create failed"; fi
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
snaps > "$BASE.snaps"; ok "the site has $(cat "$BASE.snaps") snapshot(s) before the import"
finish
EOF
stop_here create

PHASE=import1; say "2. import the practice file"
python3 scripts/wp_sites.py import "$SITE" "$FILE" --yes; RC=$?
[ "$RC" = 0 ] && bump 0 "wp_sites.py import finished with the report" || fatal "import exited $RC (1 = a step failed, 2 = refused): read the report above"
[ "$PHONE" = no ] || echo "   (the notification is already on your phone: do NOT tap it yet; the checks read the import and a tap removes it. The script says when to tap.)"
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
check "the snapshot the import took is gone again (the site's backups are as before)" "$(snaps)" "$(cat "$BASE.snaps")"
check "the site's file tree is exactly what it was before the import" "$(tree | diff - "$BASE" >/dev/null && echo same || echo different)" same
tree | diff - "$BASE" | head -n 20 | sed 's/^/   (diff, < now, > before) /' || true
[ -f "$INTAKE/$FILENAME" ] && ok "the .wpress is still in the intake directory" || bad "the .wpress is gone from the intake directory"
check "the ledger records the decision" "$(ledger | grep -c 'reset')" 1
check "no partial file in the intake directory" "$(ls -A "$INTAKE" | grep -c '^\.recv-')" 0
finish
EOF
stop_here deny

PHASE=import2; say "4. import again (the file left by the first try is replaced)"
python3 scripts/wp_sites.py import "$SITE" "$FILE" --yes; RC=$?
[ "$RC" = 0 ] && bump 0 "the second import finished with the report" || fatal "second import exited $RC"
[ "$PHONE" = no ] || echo "   (the notification is already on your phone: do NOT tap it yet; the checks read the import and a tap removes it. The script says when to tap.)"
vmrun <<'EOF'
imported_checks
finish
EOF

PHASE=approve; say "5. approve"
if has_vulns; then
  echo "   The report lists known vulnerabilities (the practice archive carries old plugin versions), so the notification has Deny only: that is the rule, not a fault. Approving in the terminal."
  echo "   (The phone's Approve path needs an archive without findings; this one cannot exercise it.)"
  MANUAL+=("n/a: the report has known vulnerabilities, so no Approve button was expected; approved in the terminal")
  python3 scripts/wp_sites.py approve "$SITE" --yes; bump $? "wp_sites.py approve" || fatal "approve failed"
elif [ "$PHONE" = yes ] && yn "Did the second notification arrive with an Approve button?"; then
  MANUAL+=("yes: the second notification had an Approve button")
  echo "   Tap APPROVE now (waiting up to 10 minutes)."
  waitgone 600; bump $? "the work directory is gone after the tap" || fatal "no decision arrived"
  confirmed "A message 'Phone decision done: ...' arrived on the phone after Approve"
else
  if [ "$PHONE" = yes ]; then MANUAL+=("NO: the second notification had no Approve button"); echo "FAIL: no Approve button on a clean import"; FAIL=$((FAIL+1)); echo "   (approving in the terminal instead, so the rest of the lifecycle can be checked)"; fi
  python3 scripts/wp_sites.py approve "$SITE" --yes; bump $? "wp_sites.py approve" || fatal "approve failed"
fi
vmtest <<'EOF'
[ "$(tables)" -gt 0 ] 2>/dev/null
EOF
bump $? "the site has data after the decision" || fatal "the site is empty after this phase: it was denied or undone, so the checks that follow would prove nothing"
vmrun <<'EOF'
check "no work directory" "$([ -e "$WORK" ] && echo yes || echo no)" no
check "the .wpress is gone from the intake directory" "$([ -e "$INTAKE/$FILENAME" ] && echo yes || echo no)" no
check "the ledger records the approval" "$(ledger | grep -c 'approved')" 1
has "the database is still loaded (tables)" "$(tables)"
check "the stored siteurl is still this site" "$(stored siteurl)" "https://$HOST"
check "the plugins directory is root's" "$(stat -c %u "$DIR/html/wp-content/plugins")" 0
check "nothing under plugins, themes or mu-plugins is writable by the php user" "$(find "$DIR/html/wp-content/plugins" "$DIR/html/wp-content/themes" "$DIR/html/wp-content/mu-plugins" -user 33 2>/dev/null | wc -l)" 0
check "no PHP file over 64 bytes under uploads" "$(find "$DIR/html/wp-content/uploads" -type f -iname '*.php*' -size +64c 2>/dev/null | wc -l)" 0
hidden_check
hold_checks
finish
EOF
stop_here approve

if [ "$REPLACE" = yes ]; then
  PHASE=replace; say "5b. replace: a second import onto the site that has data"
  vmrun <<'EOF'
tree_cmp > "$BASE.replace"; tables > "$BASE.replace.tables"; snaps > "$BASE.replace.snaps"
ok "the site as it is now was recorded ($(wc -l < "$BASE.replace") paths, $(cat "$BASE.replace.tables") tables, $(cat "$BASE.replace.snaps") snapshot(s)) to compare after a deny"
finish
EOF
  for ROUND in deny approve; do
    echo "   Round: $ROUND. The VM will ask you to type the site name ($SITE) before it wipes anything."
    python3 scripts/wp_sites.py import "$SITE" "$FILE" --replace --yes; RC=$?
    [ "$RC" = 0 ] && bump 0 "the replace import ($ROUND round) finished with the report" || fatal "the replace import exited $RC: read the report above"
    [ "$PHONE" = no ] || echo "   (the notification is already on your phone: do NOT tap it yet; the checks read the import and a tap removes it. The script says when to tap.)"
    vmrun <<'EOF'
check "the work directory exists" "$([ -e "$WORK" ] && echo yes || echo no)" yes
check "the old site's snapshot is tagged pre-import (exactly one)" "$(grep -lx 'tag=pre-import' "$BDIR/$SITE"/*/MANIFEST 2>/dev/null | wc -l)" 1
has "the archive's data is loaded (tables)" "$(tables)"
check "the old site's files are gone from wp-content (the replaced tree differs)" "$(tree_cmp | diff - "$BASE.replace" >/dev/null && echo same || echo different)" different
check "the record says the old site was wiped and names its snapshot" "$(python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1]))["replace"]; print(r["wiped"] is True and bool(r["snapshot"]))' "$WORK/PLAN.json" 2>/dev/null)" True
hold_checks
finish
EOF
    if [ "$ROUND" = deny ]; then
      if [ "$PHONE" = yes ]; then
        echo "   Tap DENY on the notification now (waiting up to 10 minutes)."
        waitgone 600; bump $? "the work directory is gone after the tap" || fatal "no decision arrived"
        confirmed "A message 'Phone decision done: ...' arrived on the phone after the replace Deny"
      else
        python3 scripts/wp_sites.py reset "$SITE"; bump $? "wp_sites.py reset (type the name when asked)" || fatal "reset failed"
      fi
      vmrun <<'EOF'
check "no work directory" "$([ -e "$WORK" ] && echo yes || echo no)" no
check "the old site's tables are back" "$(tables)" "$(cat "$BASE.replace.tables")"
check "the old site's files are back (paths, owners, modes; cache/ and upgrade/ are not in a snapshot)" "$(tree_cmp | diff - "$BASE.replace" >/dev/null && echo same || echo different)" same
tree_cmp | diff - "$BASE.replace" | head -n 20 | sed 's/^/   (diff, < now, > before) /' || true
check "the site's snapshots are as before (nothing left over)" "$(snaps)" "$(cat "$BASE.replace.snaps")"
check "no pre-import snapshot is left" "$(grep -lx 'tag=pre-import' "$BDIR/$SITE"/*/MANIFEST 2>/dev/null | wc -l)" 0
check "the stored siteurl is this site's" "$(stored siteurl)" "https://$HOST"
hold_checks
finish
EOF
    else
      if [ "$PHONE" = yes ] && ! has_vulns; then
        echo "   Tap APPROVE now (waiting up to 10 minutes)."
        waitgone 600; bump $? "the work directory is gone after the tap" || fatal "no decision arrived"
        confirmed "A message 'Phone decision done: ...' arrived on the phone after the replace Approve"
      else
        has_vulns && echo "   (known vulnerabilities in the report: Deny only on the phone, by design; approving in the terminal)"
        python3 scripts/wp_sites.py approve "$SITE" --yes; bump $? "wp_sites.py approve" || fatal "approve failed"
      fi
      vmrun <<'EOF'
check "no work directory" "$([ -e "$WORK" ] && echo yes || echo no)" no
has "the new data is loaded (tables)" "$(tables)"
check "no pre-import snapshot is left (the old site's is a pre-update one now)" "$(grep -lx 'tag=pre-import' "$BDIR/$SITE"/*/MANIFEST 2>/dev/null | wc -l)" 0
has "the old site's snapshot is kept, tagged pre-update" "$(grep -lx 'tag=pre-update' "$BDIR/$SITE"/*/MANIFEST 2>/dev/null | wc -l)"
check "the plugins directory is root's" "$(stat -c %u "$DIR/html/wp-content/plugins")" 0
check "no PHP file over 64 bytes under uploads" "$(find "$DIR/html/wp-content/uploads" -type f -iname '*.php*' -size +64c 2>/dev/null | wc -l)" 0
hold_checks
finish
EOF
    fi
  done
  stop_here replace
fi

PHASE=harden; say "5c. the hosting service's must-use plugins on the real site (login, two-factor, mail)"
vmrun <<'EOF'
check "our three must-use plugin files are in the php container" "$(docker exec "$UP" sh -c 'ls /var/www/wp-mu/*.php 2>/dev/null | wc -l' </dev/null)" 3
check "PHP may read that directory (open_basedir lets it; without that WordPress loads none of them and says nothing)" "$(docker exec "$UP" php -r 'echo is_dir("/var/www/wp-mu") ? "yes" : "no";' </dev/null)" yes
check "wp-config.php points WordPress at that directory, not at a friend's mu-plugins folder" "$(grep -c "define( 'WPMU_PLUGIN_DIR', '/var/www/wp-mu' )" "$DIR/wp-config.php")" 1
check "the php container mounts the must-use directory read-only" "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/www/wp-mu"}}{{.RW}}{{end}}{{end}}' "$UP" 2>/dev/null </dev/null)" false
check "the login page answers" "$(code /wp-login.php)" 200
author_code=$(code '/?author=1')
case $author_code in 403|404) ok "?author=1 does not reveal a user name (nginx says 403, the plugin 404: $author_code)" ;; *) bad "?author=1 answered '$author_code'" ;; esac
check "the users listing of the REST API is gone" "$(code /wp-json/wp/v2/users)" 404
check "xmlrpc.php is refused" "$(code /xmlrpc.php)" 403

# one wrong user name and one wrong password for a real user read the same: the form does not say which user names exist
login() { curl -s --max-time 30 -H "Host: $HOST" -H 'X-Forwarded-Proto: https' -H 'Cookie: wordpress_test_cookie=WP%20Cookie%20check' --data-urlencode "log=$1" --data-urlencode "pwd=$2" -d 'wp-submit=Log+In' "http://127.0.0.1:$LISTEN/wp-login.php" </dev/null; }
real_admin=$(wpr user list --role=administrator --field=user_login 2>/dev/null | head -n 1)
if [ -n "$real_admin" ]; then
  msg_a=$(login "no-such-user-$$" 'wrong-password-1' | grep -c 'The user name or the password is not correct')
  msg_b=$(login "$real_admin" 'wrong-password-1' | grep -c 'The user name or the password is not correct')
  check "a wrong user name and a wrong password for a real user ($real_admin) give the same message" "$msg_a$msg_b" 11
else
  bad "no administrator on the site to compare against"
fi

# an administrator with no second factor is sent to the profile page, and the profile page stays open (the real Two Factor plugin is loaded on this site)
tmppw=$(openssl rand -hex 16); jar=$(mktemp); pwf=$(mktemp); chmod 600 "$jar" "$pwf"
wpr user create wph-check wph-check@example.invalid --role=administrator "--user_pass=$tmppw" >/dev/null 2>&1 && ok "a throw-away administrator was made" || bad "could not make the throw-away administrator"
printf 'log=wph-check&pwd=%s&wp-submit=Log+In&redirect_to=%%2Fwp-admin%%2F' "$tmppw" > "$pwf"
unset tmppw
curl -s -o /dev/null -c "$jar" -b "$jar" --max-time 30 -H "Host: $HOST" -H 'X-Forwarded-Proto: https' -H 'Cookie: wordpress_test_cookie=WP%20Cookie%20check' --data-binary "@$pwf" "http://127.0.0.1:$LISTEN/wp-login.php" </dev/null
admin_head=$(curl -s -o /dev/null -D - -b "$jar" --max-time 30 -H "Host: $HOST" -H 'X-Forwarded-Proto: https' "http://127.0.0.1:$LISTEN/wp-admin/plugins.php" </dev/null | tr -d '\r')
case "$admin_head" in *"profile.php?wph_2fa=required"*) ok "wp-admin sends an administrator without a second factor to the profile page" ;; *) bad "wp-admin did not send the administrator without a second factor to the profile page: $(printf '%s' "$admin_head" | head -n 3 | tr '\n' ' ')" ;; esac
profile=$(curl -s -b "$jar" --max-time 30 -H "Host: $HOST" -H 'X-Forwarded-Proto: https' "http://127.0.0.1:$LISTEN/wp-admin/profile.php" </dev/null)
case "$profile" in *"two-factor-options"*) ok "the profile page stays open and offers the two-factor setup" ;; *) bad "the profile page does not show the two-factor options (is the Two Factor plugin loaded?)" ;; esac
case "$profile" in *"Two-factor login is required for your role"*) ok "and says why" ;; *) bad "the profile page does not say that two-factor is required" ;; esac

# mail: the password-reset mail of that user goes through the mail plugin, in log mode nothing leaves the site
mailseen=$(curl -s --max-time 30 -H "Host: $HOST" -H 'X-Forwarded-Proto: https' -H 'Cookie: wordpress_test_cookie=WP%20Cookie%20check' --data-urlencode 'user_login=wph-check' -d 'wp-submit=Get+New+Password' "http://127.0.0.1:$LISTEN/wp-login.php?action=lostpassword" </dev/null | grep -c 'Check your email')
check "the password reset says the mail was sent (log mode reports success)" "$mailseen" 1
has "the mail plugin logged it instead of sending (container log)" "$(docker logs --since 3m "$UP" 2>&1 </dev/null | grep -c 'wp-hosting: mail (log mode, not sent)')"
check "and recorded its mode" "$(stored wph_mail_status | grep -c '"log"')" 1

# the throttle: ten wrong passwords for one user name, the eleventh is refused whatever it is (nginx lets six through at once and then one every six seconds)
throttled=0
for i in 1 2 3 4 5 6 7 8 9 10 11; do
  [ "$i" -gt 6 ] && sleep 6.5
  if login "throttle-test-user" "wrong-$i" | grep -q 'Too many failed logins'; then throttled=$i; break; fi
done
check "the eleventh try for one user name is refused as too many" "$throttled" 11

wpr user delete wph-check --yes >/dev/null 2>&1 && ok "the throw-away administrator was removed" || bad "could not remove the throw-away administrator"
rm -f "$jar" "$pwf"
wpr transient delete --all >/dev/null 2>&1; wpr option delete wph_mail_status >/dev/null 2>&1
ok "the throttle's counters and the mail status were cleared so the next steps start clean"
hold_checks
finish
EOF
stop_here harden

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
  echo "   This makes https://$HOST/ reachable by anyone for a minute or two, with the imported content. A hold site is refused to visitors; a live one is not."
  python3 scripts/wp_sites.py modify "$SITE" --set state=live --commit; bump $? "wp_sites.py modify state=live (it asked its own question)" || fatal "going live failed"
  sleep 5
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$HOST/" </dev/null)
  case $C in 200|301|302) bump 0 "https://$HOST/ answers $C through the tunnel" ;; *) bump 1 "https://$HOST/ answered '$C' (is there a DNS record and a tunnel route for $HOST?)" ;; esac
  BODY=$(curl -sL --max-time 30 "https://$HOST/" </dev/null)
  case $BODY in *wp-content*) bump 0 "the front page is WordPress's (it links to wp-content)" ;; *) bump 1 "the front page does not look like WordPress (no wp-content in it)" ;; esac
  case $BODY in *"Fatal error"*|*"Parse error"*|*"Uncaught"*) bump 1 "the front page shows a PHP error" ;; *) bump 0 "the front page shows no PHP error" ;; esac
  L=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$HOST/wp-login.php" </dev/null)
  [ "$L" = 200 ] && bump 0 "the login page answers 200" || bump 1 "the login page answered '$L'"
  vmrun <<'EOF'
check "the cron timer is on while the site is live" "$(systemctl is-enabled "wp-cron@$SITE.timer" 2>/dev/null || true)" enabled
check "the backup timer is on" "$(systemctl is-enabled "wp-backup@$SITE.timer" 2>/dev/null || true)" enabled
check "no maintenance flag is up" "$([ -e "$DIR/maintenance.flag" ] && echo yes || echo no)" no
finish
EOF
  python3 scripts/wp_sites.py modify "$SITE" --set state=hold --yes --commit; bump $? "back on hold" || fatal "could not put it back on hold"
  sleep 5
  H=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "https://$HOST/" </dev/null)
  [ "$H" != 200 ] && bump 0 "back on hold the public no longer gets the site (answer: '$H')" || bump 1 "back on hold, https://$HOST/ still answers 200: the site is still public"
  vmrun <<'EOF'
check "the cron timer is off again on hold" "$(systemctl is-enabled "wp-cron@$SITE.timer" 2>/dev/null || true)" disabled
hold_checks
finish
EOF
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
check "the stored siteurl survived" "$(stored siteurl)" "https://$HOST"
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
