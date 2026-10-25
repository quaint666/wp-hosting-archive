#!/bin/bash
# Add the two ntfy logins that approvals by phone need to host_vars/centermc92/vault_ntfy.yml: centermc92_ntfy_approver_password (the user whose login is inside the Approve and Deny buttons,
# write-only on the approvals topic) and centermc92_ntfy_wpvm_password (the listener on dellwp, read-only on it). Both are generated here at random and never printed, never put in an argument,
# never written anywhere except the encrypted vault: only their lengths are shown. A name that is already in the vault is left alone (run it again to be sure; it changes nothing).
#   bash scripts/wp_approvals_secrets.sh
# Needs ansible-vault and its password file (ansible.cfg names it) and openssl; run it from anywhere in the repo. It refuses if the vault file has uncommitted changes, so the commit afterwards is only this.
# Afterwards:  git add host_vars/centermc92/vault_ntfy.yml && git commit -m "ops(wp): ntfy logins for phone approvals"   then run centermc92/ntfy_centermc92.yml and dellwp/wp_approvals_dellwp.yml.
set -euo pipefail
cd "$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
readonly VAULT=host_vars/centermc92/vault_ntfy.yml
readonly NAMES=(centermc92_ntfy_approver_password centermc92_ntfy_wpvm_password)
tmp=""; new=""
trap 'rm -f "$tmp" "$new"' EXIT

for c in ansible-vault openssl python3 git; do command -v "$c" >/dev/null || { echo "missing command: $c" >&2; exit 2; }; done
[ -f "$VAULT" ] || { echo "$VAULT does not exist" >&2; exit 2; }
git diff --quiet HEAD -- "$VAULT" || { echo "$VAULT has uncommitted changes: commit or revert them first" >&2; exit 2; }
head -c 14 "$VAULT" | grep -q '^\$ANSIBLE_VAULT' || { echo "$VAULT is not an encrypted vault file" >&2; exit 2; }

tmp=$(mktemp); new=$(mktemp)      # mktemp makes them 0600
ansible-vault view "$VAULT" > "$tmp"
added=0
for name in "${NAMES[@]}"; do
  if grep -q "^${name}:" "$tmp"; then
    echo "$name: already in the vault, left alone"
  else
    pw=$(openssl rand -hex 24)
    [ "${#pw}" = 48 ] || { echo "could not generate a password" >&2; exit 2; }
    printf '%s: "%s"\n' "$name" "$pw" >> "$tmp"
    echo "$name: generated, ${#pw} characters"
    added=$((added + 1))
  fi
done
unset pw
if [ "$added" = 0 ]; then echo "nothing to add"; exit 0; fi

# the plaintext must still be valid YAML with every name in it (checked without printing a value) before it replaces the vault
python3 -I - "$tmp" "${NAMES[@]}" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
missing = [n for n in sys.argv[2:] if not isinstance(d.get(n), str) or len(d[n]) < 32]
if missing:
    sys.exit("not valid after the edit: " + ", ".join(missing))
PY
ansible-vault encrypt --output "$new" "$tmp" >/dev/null
head -c 14 "$new" | grep -q '^\$ANSIBLE_VAULT' || { echo "encryption failed; the vault file was not touched" >&2; exit 2; }
cp "$new" "$VAULT"
echo "$VAULT updated ($added added). Review nothing: the values are not shown. Now: git add $VAULT && git commit"
