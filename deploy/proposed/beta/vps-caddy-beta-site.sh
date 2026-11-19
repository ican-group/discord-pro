#!/usr/bin/env bash
# PROPOSED (Spec 012 Phase 6D) — beta sites install/remove; adapted from deploy/caddy/vps-caddy-site.sh.
#   sudo bash vps-caddy-site.sh install ./api.bitbotpro.io.caddy
#   sudo bash vps-caddy-site.sh remove
# Adds/removes ONE line in the Caddyfile: `import /etc/caddy/sites/api.bitbotpro.io.caddy`.
# Every other line (Gridscan, global options) is preserved byte-for-byte.
# - The current config must validate before anything is changed.
# - The candidate Caddyfile is validated BEFORE it replaces the live one (atomic
#   rename after a backup); on failure nothing live is changed.
# - Caddy is reloaded gracefully (`systemctl reload caddy`), never restarted.
set -euo pipefail
umask 022

CADDY_DIR=${CADDY_DIR:-/etc/caddy}
CADDYFILE="$CADDY_DIR/Caddyfile"
SITE="$CADDY_DIR/sites/beta.bitbotpro.io.caddy"
IMPORT="import $SITE"
die() { echo "STOP: $*" >&2; exit 1; }
validate() { caddy validate --adapter caddyfile --config "$1" > /dev/null 2>&1; }

[ -f "$CADDYFILE" ] || die "$CADDYFILE not found"
validate "$CADDYFILE" || die "the CURRENT Caddy config does not validate; fix that first (nothing changed)"

CAND=$(mktemp "$CADDY_DIR/.Caddyfile.XXXXXX")
CREATED_SITE=0
cleanup() { rm -f "$CAND"; if [ "$CREATED_SITE" -eq 1 ]; then rm -f "$SITE"; fi; }
trap cleanup EXIT
cp -p "$CADDYFILE" "$CAND"

case "${1:-}" in
install)
  [ $# -eq 2 ] && [ -f "$2" ] || die "usage: $0 install <path to beta.bitbotpro.io.caddy>"
  ! grep -qxF "$IMPORT" "$CADDYFILE" || die "already imported in $CADDYFILE (nothing changed)"
  ! grep -qE '^[[:space:]]*import[[:space:]].*sites/?\*' "$CADDYFILE" || die "$CADDYFILE already imports a sites/* glob; installing would load the site twice"
  ! grep -rqsE --exclude="$(basename "$SITE")" --exclude='Caddyfile.bak-*' --exclude='.Caddyfile.*' '(^|[[:space:]])beta(-api)?\.bitbotpro\.io' "$CADDY_DIR"     || die "an existing Caddy file already defines beta.bitbotpro.io or beta-api.bitbotpro.io"
  if [ -e "$SITE" ]; then
    cmp -s "$2" "$SITE" || die "$SITE exists and differs (nothing changed)"
  else
    install -D -m 0644 "$2" "$SITE"
    CREATED_SITE=1
  fi
  [ -z "$(tail -c1 "$CAND")" ] || echo >> "$CAND"
  printf '%s\n' "$IMPORT" >> "$CAND"
  validate "$CAND" || die "Caddy config with the new site does not validate (nothing live changed)"
  BACKUP="$CADDYFILE.bak-$(date +%Y%m%d%H%M%S)"
  cp -a "$CADDYFILE" "$BACKUP"
  mv -f "$CAND" "$CADDYFILE"
  CREATED_SITE=0
  systemctl reload caddy
  echo "installed and reloaded (backup: $BACKUP)"
  ;;
remove)
  [ $# -eq 1 ] || die "usage: $0 remove"
  N=$(grep -cxF "$IMPORT" "$CADDYFILE" || true)
  if [ "$N" -eq 0 ]; then echo "nothing to do: not imported in $CADDYFILE"; exit 0; fi
  [ "$N" -eq 1 ] || die "import line present $N times; edit manually"
  grep -vxF "$IMPORT" "$CADDYFILE" > "$CAND"
  [ $(( $(wc -l < "$CADDYFILE") - $(wc -l < "$CAND") )) -eq 1 ] || die "internal: expected to remove exactly 1 line"
  validate "$CAND" || die "Caddy config without the site does not validate (nothing changed)"
  BACKUP="$CADDYFILE.bak-$(date +%Y%m%d%H%M%S)"
  cp -a "$CADDYFILE" "$BACKUP"
  mv -f "$CAND" "$CADDYFILE"
  systemctl reload caddy
  echo "removed the import and reloaded (backup: $BACKUP). $SITE is left in place, unused."
  ;;
*) die "usage: $0 install <site file> | remove" ;;
esac
