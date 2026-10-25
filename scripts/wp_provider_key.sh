#!/bin/bash
# Put an API key of an outside provider into host_vars/dellwp/vault_wp.yml, for a service the sites use:
#   bash scripts/wp_provider_key.sh wordfence   dellwp_wp_wordfence_key  the Wordfence Intelligence vulnerability feed (dellwp/wp_scans_dellwp.yml writes it to the VM)
#   bash scripts/wp_provider_key.sh resend      dellwp_wp_resend_key     the mail provider (a site with mail_mode: send gets it in its wp-config.php; a domain-restricted key)
# wordfence: free, log in at wordfence.com, Integrations, create an API key (shown ONCE; you can make a new one any time). resend: the key made for the sender domain in the Resend dashboard.
# You type the key at a hidden prompt: it is never an argument, never in the environment of another command, never printed (only its length is), and the only place it is written is the encrypted vault.
# If the name is already in the vault it asks before replacing it. Needs ansible-vault and its password file (ansible.cfg names it); refuses if the vault file has uncommitted changes, so the commit
# afterwards is only this change.
# Afterwards:  git add host_vars/dellwp/vault_wp.yml && git commit -m "ops(wp): <provider> key"   then run the play that uses it (dellwp/wp_scans_dellwp.yml, or dellwp/wp_sites_dellwp.yml for resend).
set -euo pipefail
cd "$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
readonly VAULT=host_vars/dellwp/vault_wp.yml
case "${1:-}" in
  wordfence) readonly NAME=dellwp_wp_wordfence_key LABEL="Wordfence Intelligence API key" ;;
  resend) readonly NAME=dellwp_wp_resend_key LABEL="Resend API key" ;;
  *) echo "usage: bash scripts/wp_provider_key.sh wordfence|resend" >&2; exit 2 ;;
esac
tmp=""; new=""
trap 'rm -f "$tmp" "$new"; unset key' EXIT

for c in ansible-vault python3 git; do command -v "$c" >/dev/null || { echo "missing command: $c" >&2; exit 2; }; done
[ -f "$VAULT" ] || { echo "$VAULT does not exist" >&2; exit 2; }
git diff --quiet HEAD -- "$VAULT" || { echo "$VAULT has uncommitted changes: commit or revert them first" >&2; exit 2; }
head -c 14 "$VAULT" | grep -q '^\$ANSIBLE_VAULT' || { echo "$VAULT is not an encrypted vault file" >&2; exit 2; }
[ -t 0 ] || { echo "this needs a terminal: the key is typed at a hidden prompt" >&2; exit 2; }

tmp=$(mktemp); new=$(mktemp)      # mktemp makes them 0600
ansible-vault view "$VAULT" > "$tmp"
if grep -q "^${NAME}:" "$tmp"; then
  read -r -p "$NAME is already in the vault. Replace it? [y/N] " ans
  [ "$ans" = y ] || { echo "left alone"; exit 0; }
  grep -v "^${NAME}:" "$tmp" > "$new"; cp "$new" "$tmp"; : > "$new"
fi
read -r -s -p "$LABEL (hidden): " key; echo
# the same rule as scripts/wp_vulnfeed.py (KEY_RE): 16 to 512 of letters, digits and . _ ~ + / = -
[[ $key =~ ^[A-Za-z0-9._~+/=-]{16,512}$ ]] || { echo "that does not look like an API key (16 to 512 characters: letters, digits and . _ ~ + / = -). Nothing was changed." >&2; exit 2; }
printf '%s: "%s"\n' "$NAME" "$key" >> "$tmp"
len=${#key}; unset key

# the plaintext must still be valid YAML with the name in it (checked without printing a value) before it replaces the vault
python3 -I - "$tmp" "$NAME" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
if not isinstance(d.get(sys.argv[2]), str) or len(d[sys.argv[2]]) < 16:
    sys.exit("not valid after the edit: " + sys.argv[2])
PY
ansible-vault encrypt --output "$new" "$tmp" >/dev/null
head -c 14 "$new" | grep -q '^\$ANSIBLE_VAULT' || { echo "encryption failed; the vault file was not touched" >&2; exit 2; }
cp "$new" "$VAULT"
echo "$VAULT updated: $NAME is $len characters. The value is not shown. Now: git add $VAULT && git commit"
