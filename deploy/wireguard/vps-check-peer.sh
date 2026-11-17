#!/usr/bin/env bash
# Caddy VPS, READ-ONLY verification for Approval 2 and its rollback — spec 008 Stage 10.
#   sudo bash vps-check-peer.sh snapshot > before-wg0.txt          (before vps-add-peer.sh)
#   sudo bash vps-check-peer.sh added   '<BBP1 public key>' before-wg0.txt
#   sudo bash vps-check-peer.sh removed '<BBP1 public key>' before-wg0.txt
#   sudo bash vps-check-peer.sh status  '<BBP1 public key>'
# Changes nothing. The snapshot holds public keys and allowed-ips only.
# `added` exits 0 only if ALL hold:
#   - every peer line of the snapshot (key + complete allowed-ips) is still present,
#     unchanged (so both Gridscan peers are exactly as before);
#   - exactly one new line exists, and it is "<BBP1 key><TAB>10.50.0.20/32";
#   - wg0.conf has the BBP1 key exactly once, in the exact block vps-add-peer.sh
#     writes, one more [Peer] section than in the snapshot, and still parses.
# `removed` exits 0 only if the live peers equal the snapshot exactly, the key is not
# in wg0.conf, the [Peer] count equals the snapshot and the file parses.
# Every mode prints the BBP1 peer state: COMPLETE, INCOMPLETE (persisted-only or
# live-only, e.g. after an interrupted run) or ABSENT.
set -euo pipefail
export LC_ALL=C

WG_DIR=${WG_DIR:-/etc/wireguard}
IFACE=wg0
CONF="$WG_DIR/$IFACE.conf"
ADDR=10.50.0.20/32
COMMENT='# BitBotPro Backend 1 (PVE1 VM BBP1)'
KEY_RE='^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$'
FAILED=0
die() { echo "STOP: $*" >&2; exit 2; }

# wg-quick may be confined (e.g. AppArmor) to /etc/wireguard: this read-only check
# runs it on the real $CONF only and never copies the file anywhere.
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
fail() { echo "FAIL: $*" >&2; FAILED=1; }

live() { wg show "$IFACE" allowed-ips | sort; }
file_peers() { grep -cE '^[[:space:]]*\[Peer\][[:space:]]*$' "$CONF" || true; }
parse_check() {   # read-only: the real file, at the path wg-quick@wg0 uses
  local rc=0
  strip_check "$CONF" || rc=$?
  case "$rc" in
    0) ;;
    2) fail "wg-quick was DENIED READ access to $CONF (permission/LSM) — cannot confirm it parses; this is not a configuration error" ;;
    *) fail "$CONF could not be processed by wg-quick (configuration problem)" ;;
  esac
}
exact_block() {   # the BBP1 key occurs once, inside exactly the appended block
  awk -v k="$PUB" -v c="$COMMENT" -v a="AllowedIPs = $ADDR" '
    { l[NR] = $0 }
    END {
      for (i = 1; i <= NR; i++) if (index(l[i], k)) { n++; s = i }
      if (n != 1 || s < 4 || l[s] != "PublicKey = " k || l[s-1] != c || l[s-2] != "[Peer]" || l[s-3] != "" || l[s+1] != a) exit 1
      if (s + 1 < NR && l[s+2] != "" && l[s+2] !~ /^\[/) exit 1
    }' "$CONF"
}
state() {
  local in_live in_file
  in_live=$(awk -v k="$PUB" '$1 == k' <<<"$NOW" | wc -l)
  in_file=$(grep -cF "$PUB" "$CONF" || true)
  if [ "$in_live" -ge 1 ] && [ "$in_file" -ge 1 ]; then
    echo "BBP1 peer state: COMPLETE (live and persisted)"
  elif [ "$in_file" -ge 1 ]; then
    echo "BBP1 peer state: INCOMPLETE — persisted in $CONF but NOT live (interrupted run?). Recover: vps-remove-peer.sh, then vps-add-peer.sh"
  elif [ "$in_live" -ge 1 ]; then
    echo "BBP1 peer state: INCOMPLETE — live but NOT persisted (lost on next wg0 restart). Recover: vps-remove-peer.sh, then vps-add-peer.sh"
  else
    echo "BBP1 peer state: ABSENT (neither live nor in $CONF)"
  fi
}
load_snapshot() {
  [ -f "$1" ] || die "snapshot file not found: $1"
  [ "$(head -n1 "$1" | cut -f1)" = "peers-in-file" ] || die "not a vps-check-peer.sh snapshot: $1"
  SNAP_N=$(head -n1 "$1" | cut -f2)
  SNAP_LIVE=$(tail -n +2 "$1")
}

[ -f "$CONF" ] || die "$CONF not found"
MODE=${1:-}
case "$MODE" in
snapshot)
  [ $# -eq 1 ] || die "usage: $0 snapshot > before-wg0.txt"
  printf 'peers-in-file\t%s\n' "$(file_peers)"
  live
  exit 0 ;;
added|removed)
  [ $# -eq 3 ] || die "usage: $0 $MODE '<BBP1 public key>' <snapshot file>"
  PUB=$2; [[ $PUB =~ $KEY_RE ]] || die "malformed public key"
  load_snapshot "$3" ;;
status)
  [ $# -eq 2 ] || die "usage: $0 status '<BBP1 public key>'"
  PUB=$2; [[ $PUB =~ $KEY_RE ]] || die "malformed public key"
  NOW=$(live); state; exit 0 ;;
*) die "usage: $0 snapshot | added <key> <snapshot> | removed <key> <snapshot> | status <key>" ;;
esac

! awk -v k="$PUB" '$1 == k { f = 1 } END { exit !f }' <<<"$SNAP_LIVE" || die "the snapshot already contains the BBP1 key; take it BEFORE adding"
NOW=$(live)
state
MISSING=$(comm -23 <(printf '%s\n' "$SNAP_LIVE") <(printf '%s\n' "$NOW") | sed '/^$/d')
EXTRA=$(comm -13 <(printf '%s\n' "$SNAP_LIVE") <(printf '%s\n' "$NOW") | sed '/^$/d')
[ -z "$MISSING" ] || fail "peers changed or removed since the snapshot (snapshot lines not found live):"$'\n'"$MISSING"
parse_check
N=$(file_peers)

if [ "$MODE" = added ]; then
  [ "$EXTRA" = "$PUB"$'\t'"$ADDR" ] || fail "expected exactly one new live line '<BBP1 key><TAB>$ADDR'; new/changed lines:"$'\n'"${EXTRA:-(none)}"
  exact_block || fail "$CONF does not contain the BBP1 key exactly once in the expected [Peer] block"
  [ "$N" -eq $((SNAP_N + 1)) ] || fail "$CONF has $N [Peer] sections; expected $((SNAP_N + 1))"
  [ "$FAILED" -eq 0 ] && echo "PASS: Gridscan/existing peers unchanged; exactly one new peer (BBP1, $ADDR), live and persisted"
else
  [ -z "$EXTRA" ] || fail "live peers not identical to the snapshot; extra/changed lines:"$'\n'"$EXTRA"
  ! grep -qF "$PUB" "$CONF" || fail "BBP1 key still present in $CONF"
  [ "$N" -eq "$SNAP_N" ] || fail "$CONF has $N [Peer] sections; expected $SNAP_N"
  [ "$FAILED" -eq 0 ] && echo "PASS: wg0 is exactly as in the snapshot; BBP1 peer fully removed"
fi
exit "$FAILED"
