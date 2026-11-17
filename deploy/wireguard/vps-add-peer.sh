#!/usr/bin/env bash
# Caddy VPS, Approval 2 — spec 008 Stage 10.
#   sudo bash vps-add-peer.sh '<BBP1 PUBLIC key>'
# Adds BBP1 as a wg0 peer with AllowedIPs 10.50.0.20/32: persisted by appending one
# [Peer] block to /etc/wireguard/wg0.conf, then applied live with `wg set` (no
# interface restart). Existing peers (Gridscan 10.50.0.11, 10.50.0.12) cannot change:
# - refuses a key that is malformed, wg0's own key, already a live peer or already
#   in the file (`wg set` on an existing key would REPLACE that peer's allowed-ips);
# - refuses if 10.50.0.20 is inside ANY live or persisted AllowedIPs entry
#   (WireGuard would move an identical prefix away from its current peer);
# - refuses if SaveConfig = true (the file would be rewritten on the next stop);
# - the new file must equal the old one byte-for-byte plus the block, must parse
#   (`wg-quick strip` on a copy INSIDE /etc/wireguard — wg-quick may be confined to
#   that directory), replaces the old one atomically after a backup, and is
#   checked again at its real path (restored on failure);
# - after `wg set`, the live allowed-ips of every other peer are compared with the
#   snapshot taken before;
# - if `wg set` fails, wg0.conf is restored from the backup; if the post-check
#   fails, the new peer is removed again and wg0.conf is restored;
# - from the backup onwards HUP/INT/TERM are ignored, so an SSH disconnect cannot
#   leave the peer persisted but not live (only SIGKILL or power loss can; see
#   vps-check-peer.sh, which reports such an INCOMPLETE state).
set -euo pipefail
umask 077
export LC_ALL=C

WG_DIR=${WG_DIR:-/etc/wireguard}
IFACE=wg0
CONF="$WG_DIR/$IFACE.conf"
ADDR=10.50.0.20/32
COMMENT='# BitBotPro Backend 1 (PVE1 VM BBP1)'
KEY_RE='^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$'
die() { echo "STOP: $*" >&2; exit 1; }

# wg-quick may be confined (e.g. AppArmor) to /etc/wireguard: validation copies are
# therefore made INSIDE $WG_DIR (root-only 0700; files 0600), never in /tmp.
# strip_check <file>: 0 = wg-quick read and processed it; 2 = wg-quick was DENIED read
# access (permission/LSM — not a configuration problem); 1 = any other wg-quick failure.
# wg-quick's stderr is shown with every WireGuard key redacted.
strip_check() {
  local err rc=0
  err=$(wg-quick strip "$1" 2>&1 >/dev/null) || rc=$?
  [ "$rc" -eq 0 ] && return 0
  printf '%s\n' "$err" | sed -E 's#[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=#<key redacted>#g' >&2
  if grep -qiE 'permission denied|operation not permitted' <<<"$err"; then return 2; fi
  return 1
}
strip_or_die() {   # strip_or_die <file> <what changed so far>
  local rc=0
  strip_check "$1" || rc=$?
  case "$rc" in
    0) ;;
    2) die "wg-quick was DENIED READ access to $1 (file permission or AppArmor/LSM policy) — this is NOT a WireGuard configuration error. $2. Diagnose (read-only): aa-status 2>/dev/null | grep -i wg; journalctl -k --since '-10 min' | grep -i denied" ;;
    *) die "wg-quick could not process $1 (configuration problem, see message above). $2" ;;
  esac
}

ip2int() { local IFS=.; local a b c d; read -r a b c d <<<"$1"; echo $(( (a << 24) | (b << 16) | (c << 8) | d )); }
contains() {   # contains <IPv4 CIDR> <IPv4 address>
  local net=${1%/*} len=32 mask
  [[ $1 == */* ]] && len=${1#*/}
  mask=$(( len == 0 ? 0 : (0xFFFFFFFF << (32 - len)) & 0xFFFFFFFF ))
  (( ($(ip2int "$net") & mask) == ($(ip2int "$2") & mask) ))
}

[ $# -eq 1 ] || die "usage: $0 '<BBP1 public key>'"
PUB=$1
[[ $PUB =~ $KEY_RE ]] || die "malformed public key"
[ -f "$CONF" ] || die "$CONF not found"
! grep -qiE '^[[:space:]]*SaveConfig[[:space:]]*=[[:space:]]*true' "$CONF" || die "SaveConfig = true in $CONF; this procedure does not apply"
[ "$PUB" != "$(wg show "$IFACE" public-key)" ] || die "that is wg0's own public key"

BEFORE=$(wg show "$IFACE" allowed-ips | sort)
! awk -v k="$PUB" '$1 == k { f = 1 } END { exit !f }' <<<"$BEFORE" || die "key is already a live wg0 peer"
! grep -qF "$PUB" "$CONF" || die "key already present in $CONF"
CIDRS=$( { awk '{ for (i = 2; i <= NF; i++) print $i }' <<<"$BEFORE";
           grep -iE '^[[:space:]]*AllowedIPs[[:space:]]*=' "$CONF" | cut -d= -f2- || true; } | tr ', ' '\n\n' | sed '/^$/d')
for cidr in $CIDRS; do
  [[ $cidr =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$ ]] || continue   # IPv6, (none)
  if contains "$cidr" "${ADDR%/*}"; then die "${ADDR%/*} is already covered by AllowedIPs $cidr"; fi
done

NEW=$(mktemp "$WG_DIR/.$IFACE.conf.XXXXXX")
CHECK=$(mktemp --suffix=.conf "$WG_DIR/wgchk.XXXXXX")     # name must be <iface>.conf for wg-quick
trap 'rm -f "$NEW" "$CHECK"' EXIT
cp -p "$CONF" "$NEW"
[ -z "$(tail -c1 "$NEW")" ] || echo >> "$NEW"      # terminate a last line without newline
printf '\n[Peer]\n%s\nPublicKey = %s\nAllowedIPs = %s\n' "$COMMENT" "$PUB" "$ADDR" >> "$NEW"
head -c "$(stat -c %s "$CONF")" "$NEW" | cmp -s - "$CONF" || die "internal: new file does not start with the old file"
cp -p "$NEW" "$CHECK"
strip_or_die "$CHECK" "Nothing changed"

# From here on, hang-ups (SSH disconnect), Ctrl-C and TERM are ignored so the run
# cannot stop between replacing wg0.conf and applying/verifying the live peer.
trap '' HUP INT TERM
BACKUP="$CONF.bak-$(date +%Y%m%d%H%M%S)"
cp -a "$CONF" "$BACKUP"
restore_file() {   # atomically put the pre-change wg0.conf back
  local t
  t=$(mktemp "$WG_DIR/.$IFACE.conf.restore.XXXXXX") && cp -p "$BACKUP" "$t" && mv -f "$t" "$CONF" \
    || die "COULD NOT RESTORE $CONF — copy $BACKUP back manually"
}
mv -f "$NEW" "$CONF"
# Re-check the installed file at its real path (the path wg-quick@wg0 uses at boot).
rc=0; strip_check "$CONF" || rc=$?
if [ "$rc" -ne 0 ]; then
  restore_file
  if [ "$rc" -eq 2 ]; then die "wg-quick was DENIED READ access to the installed $CONF (permission/LSM, not a configuration error); $CONF restored from $BACKUP; no live peer was added"; fi
  die "installed $CONF failed wg-quick processing; restored from $BACKUP; no live peer was added"
fi

# Failure after this point is rolled back automatically: the file is restored from
# the backup and, if the live peer was added, it is removed again.
if ! wg set "$IFACE" peer "$PUB" allowed-ips "$ADDR"; then
  restore_file
  die "live 'wg set' failed; $CONF restored from $BACKUP; no live peer was added"
fi
AFTER=$(wg show "$IFACE" allowed-ips | sort)
# Strict: every other peer's line is unchanged and nothing else appeared, and the
# new peer is exactly one line "<key><TAB>10.50.0.20/32" (no extra allowed-ips).
if [ "$(awk -v k="$PUB" '$1 != k' <<<"$AFTER")" != "$BEFORE" ] \
   || [ "$(awk -v k="$PUB" '$1 == k' <<<"$AFTER")" != "$PUB"$'\t'"$ADDR" ]; then
  wg set "$IFACE" peer "$PUB" remove || echo "WARNING: could not remove the BBP1 peer again" >&2
  restore_file
  {
    echo "Allowed-ips BEFORE this run (public keys only). Restore any peer that differs with:"
    echo "  wg set $IFACE peer <public key> allowed-ips <ip1,ip2>"
    printf '%s\n' "$BEFORE"
    echo "Allowed-ips NOW:"
    wg show "$IFACE" allowed-ips || true
  } >&2
  die "unexpected live state after 'wg set'; BBP1 peer removed again and $CONF restored from $BACKUP"
fi
echo "OK: BBP1 peer added live and persisted (backup: $BACKUP). Existing peers unchanged."
awk '{ $1 = ""; sub(/^ /, ""); print }' <<<"$AFTER"
