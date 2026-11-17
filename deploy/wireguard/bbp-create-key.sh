#!/usr/bin/env bash
# BBP1 (VM), Approval 1 — spec 008 Stage 10.
#   sudo bash deploy/wireguard/bbp-create-key.sh
# Installs wireguard-tools if missing and creates the BBP1 WireGuard key pair.
# - Never overwrites an existing private key (checked, then created with noclobber).
# - Never prints the private key; prints only the public key.
# - Creates no interface, starts no service, changes no firewall rule.
set -euo pipefail
umask 077

WG_DIR=${WG_DIR:-/etc/wireguard}
KEY="$WG_DIR/wg-bbp.key"
PUB="$WG_DIR/wg-bbp.pub"
KEY_RE='^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$'
die() { echo "STOP: $*" >&2; exit 1; }

if dpkg-query -W -f='${Status}' wireguard-tools 2>/dev/null | grep -q 'install ok installed'; then
  echo "wireguard-tools already installed"
else
  apt-get install -y --no-install-recommends wireguard-tools
fi

install -d -m 0700 "$WG_DIR"
if [ -e "$KEY" ]; then
  echo "EXISTS: $KEY left unchanged" >&2
else
  (set -C; wg genkey > "$KEY")
fi
chmod 0600 "$KEY"
if [ "$(id -u)" -eq 0 ]; then chown root:root "$WG_DIR" "$KEY"; fi

PUB_VALUE=$(wg pubkey < "$KEY") || die "$KEY is not a valid WireGuard private key (left unchanged)"
[[ $PUB_VALUE =~ $KEY_RE ]] || die "derived public key is malformed ($KEY left unchanged)"
TMP=$(mktemp "$WG_DIR/.wg-bbp.pub.XXXXXX")
trap 'rm -f "$TMP"' EXIT
printf '%s\n' "$PUB_VALUE" > "$TMP"
chmod 0644 "$TMP"
mv -f "$TMP" "$PUB"            # derived from the key: safe to (re)write

echo "BBP1 WireGuard PUBLIC key: $PUB_VALUE"
