#!/usr/bin/env bash
# BBP1 (VM), Approval 3 — spec 008 Stage 10.
#   sudo bash deploy/wireguard/bbp-install-conf.sh '<VPS wg0 PUBLIC key>'
# Renders wg-bbp.conf.template with the VPS public key and installs it as
# /etc/wireguard/wg-bbp.conf (0600). The ONLY supported way to install that file.
# - Never overwrites: an identical existing file is accepted (idempotent); a
#   different one stops the script. The final step is an atomic hard link, which
#   fails if the destination appeared in the meantime.
# - Does not start the tunnel.
set -euo pipefail
umask 077

WG_DIR=${WG_DIR:-/etc/wireguard}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TEMPLATE="$HERE/wg-bbp.conf.template"
DEST="$WG_DIR/wg-bbp.conf"
PLACEHOLDER='<VPS_WG0_PUBLIC_KEY>'
KEY_RE='^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$'
die() { echo "STOP: $*" >&2; exit 1; }

[ $# -eq 1 ] || die "usage: $0 '<VPS wg0 public key>'"
VPS_PUB=$1
[[ $VPS_PUB =~ $KEY_RE ]] || die "malformed VPS public key"
[ -f "$TEMPLATE" ] || die "template not found: $TEMPLATE"
[ "$(grep -o "$PLACEHOLDER" "$TEMPLATE" | wc -l)" -eq 1 ] || die "template must contain the placeholder exactly once"
grep -qx "PublicKey = $PLACEHOLDER" "$TEMPLATE" || die "placeholder is not on the [Peer] PublicKey line"
[ -f "$WG_DIR/wg-bbp.key" ] || die "no private key: run bbp-create-key.sh first (Approval 1)"
[ "$(stat -c %a "$WG_DIR/wg-bbp.key")" = 600 ] || die "$WG_DIR/wg-bbp.key must be mode 0600"
if [ -f "$WG_DIR/wg-bbp.pub" ] && [ "$VPS_PUB" = "$(cat "$WG_DIR/wg-bbp.pub")" ]; then
  die "that is BBP1's own public key, not the VPS key"
fi

TMP=$(mktemp "$WG_DIR/.wg-bbp.conf.XXXXXX")
trap 'rm -f "$TMP"' EXIT
sed "s|$PLACEHOLDER|$VPS_PUB|" "$TEMPLATE" > "$TMP"
chmod 0600 "$TMP"
grep -qx "PublicKey = $VPS_PUB" "$TMP" || die "rendering failed"
! grep -q '<[A-Z0-9_]*>' "$TMP" || die "unreplaced placeholder in rendered config"
! grep -qiE '^[[:space:]]*PrivateKey[[:space:]]*=' "$TMP" || die "rendered config must not contain a PrivateKey line"

if [ -e "$DEST" ]; then
  if cmp -s "$TMP" "$DEST"; then
    echo "already installed (identical): $DEST — not changed"
    exit 0
  fi
  die "$DEST exists and differs from the rendered template; not overwritten (review it manually)"
fi
ln "$TMP" "$DEST"
echo "installed $DEST (0600). Tunnel NOT started."
