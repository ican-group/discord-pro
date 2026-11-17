#!/usr/bin/env bash
# Caddy VPS, rollback of Approval 2 — spec 008 Stage 10.
#   sudo bash vps-remove-peer.sh '<BBP1 PUBLIC key>'
# Removes ONLY the peer added by vps-add-peer.sh (live, no interface restart) and
# its exact [Peer] block from /etc/wireguard/wg0.conf. Cannot remove a Gridscan peer:
# - the live peer (if present) must have AllowedIPs exactly 10.50.0.20/32;
# - the file block must be exactly the 5 lines vps-add-peer.sh appended (blank,
#   [Peer], comment, PublicKey, AllowedIPs), followed by EOF, a blank line or the
#   next section; otherwise nothing is changed;
# - the new file is checked with wg-quick on a copy INSIDE /etc/wireguard, replaces
#   the old one atomically after a backup and is re-checked at its real path;
# - other peers' live allowed-ips are compared with the snapshot taken before.
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

[ $# -eq 1 ] || die "usage: $0 '<BBP1 public key>'"
PUB=$1
[[ $PUB =~ $KEY_RE ]] || die "malformed public key"
[ -f "$CONF" ] || die "$CONF not found"
! grep -qiE '^[[:space:]]*SaveConfig[[:space:]]*=[[:space:]]*true' "$CONF" || die "SaveConfig = true in $CONF; this procedure does not apply"
[ "$PUB" != "$(wg show "$IFACE" public-key)" ] || die "that is wg0's own public key"

BEFORE=$(wg show "$IFACE" allowed-ips | sort)
LIVE=0
if awk -v k="$PUB" '$1 == k { f = 1 } END { exit !f }' <<<"$BEFORE"; then
  LIVE=1
  [ "$(awk -v k="$PUB" '$1 == k { $1 = ""; sub(/^ /, ""); print }' <<<"$BEFORE")" = "$ADDR" ] \
    || die "live peer does not have AllowedIPs exactly $ADDR — not the BBP1 peer; nothing changed"
fi
INFILE=0
grep -qF "$PUB" "$CONF" && INFILE=1
if [ "$LIVE" -eq 0 ] && [ "$INFILE" -eq 0 ]; then
  echo "nothing to do: key is neither a live peer nor in $CONF"
  exit 0
fi

NEW=$(mktemp "$WG_DIR/.$IFACE.conf.XXXXXX")
CHECK=$(mktemp --suffix=.conf "$WG_DIR/wgchk.XXXXXX")     # name must be <iface>.conf for wg-quick
trap 'rm -f "$NEW" "$CHECK"' EXIT
trap '' HUP INT TERM     # an SSH disconnect cannot stop the run half-way
if [ "$INFILE" -eq 1 ]; then
  cp -p "$CONF" "$NEW"
  awk -v k="$PUB" -v c="$COMMENT" -v a="AllowedIPs = $ADDR" '
    { l[NR] = $0 }
    END {
      for (i = 1; i <= NR; i++) if (index(l[i], k)) { n++; s = i }
      if (n != 1 || s < 4 || l[s] != "PublicKey = " k || l[s-1] != c || l[s-2] != "[Peer]" || l[s-3] != "" || l[s+1] != a) exit 3
      if (s + 1 < NR && l[s+2] != "" && l[s+2] !~ /^\[/) exit 3
      for (i = 1; i <= NR; i++) if (i < s - 3 || i > s + 1) print l[i]
    }' "$CONF" > "$NEW" || die "the BBP1 block in $CONF is not exactly as vps-add-peer.sh wrote it; nothing changed (edit manually)"
  [ $(( $(wc -l < "$CONF") - $(wc -l < "$NEW") )) -eq 5 ] || die "internal: expected to remove exactly 5 lines"
  cp -p "$NEW" "$CHECK"
  strip_or_die "$CHECK" "Nothing changed"
  BACKUP="$CONF.bak-$(date +%Y%m%d%H%M%S)"
  cp -a "$CONF" "$BACKUP"
  mv -f "$NEW" "$CONF"
  rc=0; strip_check "$CONF" || rc=$?
  if [ "$rc" -ne 0 ]; then
    cp -p "$BACKUP" "$NEW" && mv -f "$NEW" "$CONF" || die "COULD NOT RESTORE $CONF — copy $BACKUP back manually"
    die "installed $CONF failed wg-quick processing (rc=$rc; 2 = read denied, not a config error); restored from $BACKUP; live peer untouched"
  fi
  echo "removed the BBP1 block from $CONF (backup: $BACKUP)"
fi
if [ "$LIVE" -eq 1 ]; then
  wg set "$IFACE" peer "$PUB" remove
  AFTER=$(wg show "$IFACE" allowed-ips | sort)
  [ "$AFTER" = "$(awk -v k="$PUB" '$1 != k' <<<"$BEFORE")" ] || die "remaining peers changed! Inspect 'wg show $IFACE allowed-ips' now"
  echo "removed the live BBP1 peer. Remaining peers unchanged:"
  awk '{ $1 = ""; sub(/^ /, ""); print }' <<<"$AFTER"
fi
