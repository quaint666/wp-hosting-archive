#!/bin/bash
# An end-to-end checklist for the WordPress hosting on dellwp, run against ONE throwaway site. You do every change in the panel (and the browser); this script tells you what to do, asks
# whether it worked, and after each step reads the state from dellwp over ssh to see whether the machine agrees. Neither side is trusted alone: a step passes only when you say it worked AND
# the checks hold. It never changes anything on dellwp itself: it only calls read-only commands (wp-ctl show / list / import-list, wp-site-cli reads, grep, test).
#   bash scripts/wp_checklist_dellwp.sh [--name SITE] [--out FILE] [--host NAME] [--loopback-http]
# Steps: 0 pre-flight, 1 create, 2 install WordPress, 3 move to live (and, with --loopback-http, the page cache: MISS, HIT, BYPASS), 4 the 2FA page and its QR code, 5 authenticator set-up, 6 mail log/send (with or without the Resend key), 7 the three
# switches (XML-RPC, application passwords, user enumeration) each on and off, 7b the mini-cart refresh cache, 7c plugin requests (Install in wp-admin becomes a request you approve), 8 import a .wpress (optional), 9 stop and delete.
# The results (one PASS / FAIL / SKIP line per check, and what the machine said) go to FILE (default ~/.local/state/wp-perf/wp_checklist_<UTC stamp>.txt, mode 0600). They hold no secret: wp-config.php
# is only counted for lines, never printed; the password you type into the panel never touches this script. Exit status is 1 when any check failed.
# --loopback-http adds curl requests to nginx on dellwp's loopback (the page cache answers MISS, then HIT, and BYPASS for login and search; the QR script comes back 200; /xmlrpc.php and ?author=1 answer as the switches say), inside the ssh session. Without it the script
# is ssh-only and those behaviours rest on your own y/N and on the rendered nginx file, which proves the config, not the answer.
# COST: a closed site's name, port and quota id are never reused (docs/wordpress_hosting_explained.md), so every full run, delete included, uses up one of each for good. Skip step 9 to keep the site.
# Needs a terminal. Run it after the playbooks that carry the fixes (dellwp/wp_sites_dellwp.yml, and wp_tools if the renderer changed).
set -euo pipefail

HOST=dellwp; NAME="chk-$(date +%m%d%H%M)"; OUT=""; LOOP=0
SITES=/srv/wp/sites; MU=/srv/wp/mu; VHOSTS=/etc/nginx/conf.d; SECRETS=/etc/wp-tools
while [ $# -gt 0 ]; do
  case $1 in
    --name) NAME=${2:?--name needs a site name}; shift ;;
    --out) OUT=${2:?--out needs a file}; shift ;;
    --host) HOST=${2:?--host needs a name}; shift ;;
    --loopback-http) LOOP=1 ;;
    -h|--help) sed -n '2,3p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ $NAME =~ ^[a-z]([a-z0-9]|-[a-z0-9]){0,22}$ ]] || { echo "not a valid site name: $NAME" >&2; exit 2; }
[[ $HOST =~ ^[A-Za-z0-9.-]+$ ]] || { echo "not a host name: $HOST" >&2; exit 2; }
[ -t 0 ] || { echo "this needs a terminal: it asks you at each step" >&2; exit 2; }
command -v ssh >/dev/null && command -v python3 >/dev/null || { echo "needs ssh and python3" >&2; exit 2; }
umask 077
if [ -z "$OUT" ]; then
  mkdir -p "${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf"
  OUT="${XDG_STATE_HOME:-$HOME/.local/state}/wp-perf/wp_checklist_$(date -u +%Y%m%dT%H%M%SZ).txt"
fi
: > "$OUT"
pass=0; fail=0; skip=0

say() { printf '%s\n' "$*" | tee -a "$OUT"; }
note() { printf '%s\n' "$*" >> "$OUT"; }
remote() { ssh -o BatchMode=yes "$HOST" "$1" </dev/null 2>&1 || true; }            # always </dev/null: a command that reads stdin must not eat anything else
jget() { printf '%s' "$1" | python3 -I -c 'import json, sys
d = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d.get(k) if isinstance(d, dict) else None
print("" if d is None else d if isinstance(d, str) else json.dumps(d))' "$2" 2>/dev/null || echo "?"; }
show() { remote "sudo /usr/local/sbin/wp-ctl show site=$NAME"; }
verdict() { # PASS|FAIL|SKIP, label, detail
  case $1 in PASS) pass=$((pass + 1)) ;; FAIL) fail=$((fail + 1)) ;; *) skip=$((skip + 1)) ;; esac
  say "  $1  $2${3:+  ($3)}"
}
expect() { # label, actual, expected
  if [ "$2" = "$3" ]; then verdict PASS "$1" "$2"; else verdict FAIL "$1" "wanted '$3', got '$2'"; fi
}
did() { # asks whether the manual part worked; sets $ans to y, n or s
  local a; read -r -p "  $1 [y = yes, n = no, s = skip] " a; a=${a:-n}
  case $a in y|Y) ans=y ;; s|S) ans=s ;; *) ans=n ;; esac
}
manual() { # label, what to do, question
  say ""; say "- $1"; printf '    %s\n' "$2" | tee -a "$OUT"
  did "$3"
  case $ans in y) verdict PASS "you: $1" ;; s) verdict SKIP "you: $1" ;; n) verdict FAIL "you: $1" "you said it did not work" ;; esac
}
wpc() { remote "sudo wp-site-cli $NAME $1"; }
conf="$VHOSTS/wp_site_$NAME.conf"; cfg="$SITES/$NAME/wp-config.php"
count() { local p f; p=$(printf '%q' "$2"); f=$(printf '%q' "$1"); remote "sudo grep -c -- $p $f || true" | tail -n1; }   # a count of matching lines, never the lines (wp-config.php holds a key)
set_switch() { # setting name (as the panel shows it), constant, want (true|false)
  manual "$1 = $3" "Panel > $NAME > Settings > $1: choose $3, Set. Wait for the job to say Finished." "Did it finish without an error?"
  expect "registry allow flag is $3" "$(jget "$(show)" "registry.$4")" "$3"
  expect "wp-config.php constant is $3" "$(count "$cfg" "'$2', $3")" 1
}

say "WordPress hosting checklist on $HOST, site '$NAME', $(date -u +%FT%TZ), loopback http: $LOOP"
say "Results go to $OUT"

say ""; say "== 0 pre-flight"
expect "ssh and sudo work" "$(remote 'sudo -n true && echo yes')" yes
expect "wp-ctl is there" "$(remote 'sudo test -x /usr/local/sbin/wp-ctl && echo yes')" yes
expect "the site name is unused" "$(remote "sudo test ! -e $SITES/$NAME && echo yes")" yes
KEY=$(remote "sudo test -f $SECRETS/resend_key && echo present || echo absent")
say "  INFO  Resend key on dellwp: $KEY (step 6 expects the matching behaviour)"
say "  INFO  the 2FA plugin directories: $(remote "ls -d $MU/two-factor-* 2>/dev/null | tr '\n' ' '")"

say ""; say "== 1 create"
manual "create the site" "Panel > New site > name: $NAME > Create the site. Wait for the job to say Finished." "Did the job finish without an error?"
J=$(show)
expect "registry state is hold" "$(jget "$J" registry.state)" hold
expect "the vhost exists" "$(remote "test -f $conf && echo yes")" yes
expect "the vhost serves /wp-mu/" "$(count "$conf" 'location .~ /wp-mu/ {')" 1
expect "the vhost blocks ?author=N (the default)" "$(count "$conf" 'author=\[0-9\]')" 1
expect "both containers are up" "$(jget "$J" status.containers)" 2/2

say ""; say "== 2 install WordPress"
manual "install WordPress" "Panel > $NAME > Install WordPress. Use the admin user chkadmin and an address you own; pick a password you will not reuse." "Did the page report success?"
expect "WordPress is installed" "$(wpc 'core is-installed && echo yes')" yes
ADMIN=$(wpc 'user list --role=administrator --field=user_login' | head -n1)
say "  INFO  the administrator login: ${ADMIN:-none found}"
[ -n "$ADMIN" ] || verdict FAIL "an administrator exists"

say ""; say "== 3 move to live"
manual "move to live" "Panel > $NAME > State > Move to live (type the name to confirm)." "Did it finish without an error?"
J=$(show)
expect "registry state is live" "$(jget "$J" registry.state)" live
expect "nginx answers as live" "$(jget "$J" status.actual)" live
expect "the vhost no longer denies visitors" "$(count "$conf" 'deny all')" 0
if [ "$LOOP" = 1 ]; then
  port=$(remote "awk '\$1==\"listen\"{sub(\".*:\",\"\",\$2); gsub(\";\",\"\",\$2); print \$2; exit}' $conf")
  host=$(remote "awk '\$1==\"server_name\"{gsub(\";\",\"\",\$2); print \$2; exit}' $conf")
  xcache() { remote "curl -s -o /dev/null -D - -H 'Host: $host' -H 'X-Forwarded-Proto: https' 'http://127.0.0.1:$port$1' | tr -d '\r' | awk 'tolower(\$1)==\"x-cache-status:\"{print \$2}'"; }
  remote "sudo find /var/cache/nginx/wp_pages -type f -delete" >/dev/null       # an empty cache, so the first answer is a MISS (this deletes only cached copies)
  expect "the home page's first answer is a cache MISS" "$(xcache /)" MISS
  expect "its second answer is a cache HIT" "$(xcache /)" HIT
  expect "wp-login.php is never cached" "$(xcache /wp-login.php)" BYPASS
  expect "a search is never cached" "$(xcache '/?s=test')" BYPASS
fi

say ""; say "== 4 the 2FA page and its QR code"
manual "log in and land on the profile page" "Open https://$NAME.<the wp domain>/wp-admin/ in a browser and log in as $ADMIN. wp-admin must send you to your profile with a notice that two-factor login is required." "Did it?"
if [ "$LOOP" = 1 ]; then
  js=$(remote "ls -d $MU/two-factor-*/includes/qrcode-generator/qrcode.js 2>/dev/null | head -n1")
  expect "the QR script is served (loopback)" "$(remote "curl -s -o /dev/null -w '%{http_code}' -H 'Host: $host' -H 'X-Forwarded-Proto: https' http://127.0.0.1:$port/wp-mu/${js#$MU/}")" 200
fi
manual "the QR code draws" "On that profile page, tick Enable Authenticator App. The square QR code must appear (not 'Loading…')." "Did the QR code appear?"

say ""; say "== 5 authenticator set-up"
manual "finish the authenticator set-up" "Scan the QR code, type the six digits, press Verify, then Update Profile." "Did Verify accept the code and the page reload without the notice?"
expect "the plugin recorded the authenticator app" "$(wpc "user meta get $ADMIN _two_factor_enabled_providers --format=json" | grep -c Totp || true)" 1

say ""; say "== 6 mail: log and send (Resend key on dellwp: $KEY)"
manual "mail_mode = send" "Panel > $NAME > Settings > Mail: choose send, Set. Wait for the job to finish." "Did the panel give an answer (a finished job, or a refusal)?"
J=$(show)
if [ "$KEY" = present ]; then
  expect "registry mail_mode is send" "$(jget "$J" registry.mail_mode)" send
  expect "wp-config.php says send" "$(count "$cfg" "WP_HOSTING_MAIL_MODE', 'send'")" 1
  expect "wp-config.php carries the key (counted, not shown)" "$(count "$cfg" 'WP_HOSTING_MAIL_KEY')" 1
else
  expect "registry mail_mode is still log (refused)" "$(jget "$J" registry.mail_mode)" log
  manual "the refusal names the missing key" "The panel's message should name dellwp_wp_resend_key and say to run scripts/wp_provider_key.sh resend." "Did it?"
fi
manual "mail_mode back to log" "Panel > $NAME > Settings > Mail: choose log, Set (skip if it never left log)." "Is it log again?"
expect "registry mail_mode is log" "$(jget "$(show)" registry.mail_mode)" log

say ""; say "== 7 the switches, each on then off"
for pair in "Allow XML-RPC|WP_HOSTING_ALLOW_XMLRPC|allow_xmlrpc|location = /xmlrpc.php" "Allow application passwords|WP_HOSTING_ALLOW_APP_PASSWORDS|allow_app_passwords|" "Allow user enumeration|WP_HOSTING_ALLOW_USER_ENUM|allow_user_enum|author=\[0-9\]"; do
  IFS='|' read -r title const key block <<< "$pair"
  set_switch "$title" "$const" true "$key"
  [ -z "$block" ] || expect "$title on: the nginx rule is gone" "$(count "$conf" "$block")" 0
  set_switch "$title" "$const" false "$key"
  [ -z "$block" ] || expect "$title off: the nginx rule is back" "$(count "$conf" "$block")" 1
done
if [ "$LOOP" = 1 ]; then
  say "  INFO  with every switch off, ?author=1 answers $(remote "curl -s -o /dev/null -w '%{http_code}' -H 'Host: $host' -H 'X-Forwarded-Proto: https' 'http://127.0.0.1:$port/?author=1'") (403 expected) and /xmlrpc.php $(remote "curl -s -o /dev/null -w '%{http_code}' -H 'Host: $host' -H 'X-Forwarded-Proto: https' http://127.0.0.1:$port/xmlrpc.php") (403 expected)"
fi

say ""; say "== 7b the mini-cart refresh cache (cart_fragments)"
manual "cart_fragments = cache" "Panel > $NAME > Settings > Mini-cart refresh: choose cache, Set. Wait for the job to say Finished." "Did it finish without an error?"
expect "registry cart_fragments is cache" "$(jget "$(show)" registry.cart_fragments)" cache
expect "the vhost has the mini-cart location" "$(count "$conf" 'location = /__wph_fragments')" 1
if [ "$LOOP" = 1 ]; then
  frag() { remote "curl -s -o /dev/null -D - -X POST -H 'Host: $host' -H 'X-Forwarded-Proto: https' -H 'X-Requested-With: XMLHttpRequest' $1 --data 'time=1' 'http://127.0.0.1:$port/?wc-ajax=get_refreshed_fragments' | tr -d '\r' | awk 'tolower(\$1)==\"x-cache-status:\"{print \$2}'"; }
  remote "sudo find /var/cache/nginx/wp_pages -type f -delete" >/dev/null
  expect "the first mini-cart refresh is a MISS" "$(frag '')" MISS
  expect "the second is a HIT" "$(frag '')" HIT
  expect "with a cart cookie it is not shared (BYPASS)" "$(frag "-H 'Cookie: woocommerce_items_in_cart=1'")" BYPASS
  expect "with a language cookie it is not shared (BYPASS)" "$(frag "-H 'Cookie: pll_language=en'")" BYPASS
fi
manual "the mini-cart still works" "On a site WITH WooCommerce (not this throwaway one: press s here): open the shop in a browser, add a product to the cart; the header cart must show it, and show it empty again after you remove it." "Did the mini-cart update?"
manual "cart_fragments = normal" "Panel > $NAME > Settings > Mini-cart refresh: choose normal, Set." "Did it finish without an error?"
expect "the mini-cart location is gone again" "$(count "$conf" 'location = /__wph_fragments')" 0

say ""; say "== 7c plugin requests (Install in the site's wp-admin becomes a request)"
expect "the hosting plugin for requests is deployed" "$(remote "sudo test -f $MU/50-wp-hosting-plugin-requests.php && echo yes")" yes
expect "wp-config.php allows requests (a limit above 0)" "$(count "$cfg" "define( 'WP_HOSTING_PLUGIN_REQUESTS_MAX', [1-9]")" 1
expect "the tool is installed" "$(remote 'sudo test -x /usr/local/sbin/wp-plugin-requests && echo yes')" yes
expect "the collection timer is active" "$(remote 'systemctl is-active wp-plugin-requests.timer')" active
expect "the approval listener knows the tool" "$(remote "sudo grep -c 'wp-plugin-requests' /usr/local/sbin/wp-approval-listener")" 1
expect "the tool answers with its one JSON line" "$(jget "$(remote 'sudo /usr/local/sbin/wp-ctl plugin-requests')" ok)" true
manual "ask for a plugin" "Log in to $NAME's wp-admin as the administrator. Plugins now has Add New. Search for 'classic editor', press the button on Classic Editor: it should say Requested (not Installed), and the page should list the request as waiting. There must be no Upload Plugin button." "Did Install turn into a request?"
manual "refresh and read the request" "Panel > Requests > Refresh. The request should be listed as waiting for $NAME, with checks 'none known'. Your phone should get a notification with Approve and Deny buttons (it can take a moment)." "Is it listed, and did the phone get the notification?"
waiting=$(printf '%s' "$(remote 'sudo /usr/local/sbin/wp-ctl plugin-requests')" | python3 -I -c 'import json, sys
d = json.load(sys.stdin)
print(sum(1 for r in d.get("requests", []) if r.get("site") == sys.argv[1] and r.get("state") == "pending" and r.get("slug") == "classic-editor"))' "$NAME" 2>/dev/null || echo "?")
expect "dellwp remembers one waiting request for classic-editor" "$waiting" 1
expect "nothing was installed by the press" "$(remote "sudo test -d $SITES/$NAME/html/wp-content/plugins/classic-editor && echo yes || echo no")" no
manual "approve it" "Approve it on the phone (or Panel > Requests > Approve)." "Did the decision finish without an error?"
expect "the plugin is installed and inactive" "$(printf '%s' "$(remote "sudo /usr/local/sbin/wp-ctl plugins site=$NAME")" | python3 -I -c 'import json, sys
d = json.load(sys.stdin)
print(",".join(sorted(p["status"] for p in d.get("plugins", []) if p["name"] == "classic-editor")))' 2>/dev/null || echo "?")" inactive
manual "the administrator sees the answer" "Reload $NAME's wp-admin Plugins page: the notice should say classic-editor was installed and to activate it." "Did the notice say so?"
manual "clean up" "Panel > $NAME > Plugins > Delete classic-editor (it is inactive; type its name)." "Is it gone from the list?"

say ""; say "== 8 import a .wpress (optional: press s to skip)"
manual "import into $NAME" "Panel > Import > pick a .wpress (one is already on dellwp if you uploaded one) > 'Replace the data of a site that has some' (not 'Load it into an existing empty site': step 2 put WordPress in this one, and that mode refuses a site with tables) > type $NAME > run, then read the report page." "Did the report appear and read sensibly?"
say "  INFO  what dellwp says is waiting: $(remote 'sudo /usr/local/sbin/wp-ctl import-list' | cut -c1-400)"
manual "decide on the import" "Approve it if the report is clean, otherwise Reset (the site goes back to what it was)." "Did the decision finish without an error?"
say "  INFO  after the decision: $(remote 'sudo /usr/local/sbin/wp-ctl import-list' | cut -c1-400)"

say ""; say "== 9 stop and delete (optional: press s to keep the site; a delete uses up the name, port and quota id for good)"
manual "stop the site" "Panel > $NAME > State > Move to stopped (type the name)." "Did it stop?"
manual "delete the site" "Panel > $NAME > Delete (type the name twice)." "Did the job finish?"
expect "the site directory is gone" "$(remote "sudo test ! -e $SITES/$NAME && echo yes")" yes
expect "wp-ctl no longer lists it" "$(remote 'sudo /usr/local/sbin/wp-ctl list' | grep -c "\"$NAME\"" || true)" 0

say ""; say "== result: $pass passed, $fail failed, $skip skipped. Saved: $OUT"
[ "$fail" -eq 0 ]
