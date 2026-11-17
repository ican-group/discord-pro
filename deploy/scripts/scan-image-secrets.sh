#!/usr/bin/env bash
# R8 secret scan (spec 004), hardened version of deploy/scripts/scan-image-secrets.sh.
#   sudo bash scan-image-secrets-v2.sh <image:tag> <secrets-dir> [build-log ...] > report.txt 2>&1
#
# Read-only: Docker calls are limited to `image inspect`, `history`, `save` (export),
# `inspect`/`logs` of an EXISTING api container (skipped if absent) and
# `compose config` (local rendering). Never runs, creates, stops or removes anything.
# Secret values are streamed through a pipe (process substitution) and are never
# written to disk or printed; only match COUNTS are reported.
# Result: PASS (exit 0) | FAIL (exit 1, a secret was found) | INCOMPLETE (exit 2).
set -uo pipefail
umask 077
export LC_ALL=C
IMAGE="${1:?usage: scan-image-secrets.sh <image:tag> <secrets-dir> [build-log ...]}"
SECRETS_DIR="${2:?usage: scan-image-secrets.sh <image:tag> <secrets-dir> [build-log ...]}"
shift 2
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

FOUND=0; INCOMPLETE=0
incomplete() { INCOMPLETE=1; echo "!! INCOMPLETE: $*"; }
check() { printf '%-46s matches=%s\n' "$1" "$2"; [ "$2" = 0 ] || FOUND=1; }
finish() {
  echo "----"
  if   [ "$FOUND" = 1 ];      then echo "RESULT: FAIL (secret value found; see counts above)"; exit 1
  elif [ "$INCOMPLETE" = 1 ]; then echo "RESULT: INCOMPLETE (see !! lines above)"; exit 2
  else echo "RESULT: PASS (no secret values found)"; exit 0; fi
}

W="$(mktemp -d)" || { echo "!! cannot create temp dir"; echo "RESULT: INCOMPLETE"; exit 2; }
[ -n "$W" ] && [ -d "$W" ] || { echo "!! bad temp dir"; echo "RESULT: INCOMPLETE"; exit 2; }
trap 'rm -rf -- "$W"' EXIT
trap 'echo "!! interrupted"; echo "RESULT: INCOMPLETE"; exit 130' INT TERM HUP

# ── secret patterns (streamed, never stored) ────────────────────────────────
[ -d "$SECRETS_DIR" ] || { echo "!! secrets dir not found"; echo "RESULT: INCOMPLETE"; exit 2; }
NFILES="$(find "$SECRETS_DIR" -maxdepth 1 -type f ! -name '.*' | wc -l)"
# One pattern per secret file (first line); for JSON secrets (cloudflared credentials)
# every string value of >= 16 characters. Short fragments are ignored (false positives).
patterns() {
  for f in "$SECRETS_DIR"/*; do
    [ -f "$f" ] || continue
    case "$f" in
      *.json) grep -oE '"[^"]{16,}"' -- "$f" | tr -d '"' ;;
      *) head -n1 -- "$f" ;;
    esac
  done | awk 'length($0) >= 16'
}
NPAT="$(patterns | wc -l)"
echo "image: $IMAGE"
echo "secret files: $NFILES   non-empty patterns loaded: $NPAT"
echo "secret file names: $(find "$SECRETS_DIR" -maxdepth 1 -type f ! -name '.*' -printf '%f ' | tr ' ' '\n' | sort | tr '\n' ' ')"
[ "$NPAT" -gt 0 ] || { echo "!! no patterns"; echo "RESULT: INCOMPLETE"; exit 2; }
[ "$NPAT" -ge "$NFILES" ] || incomplete "fewer usable patterns ($NPAT) than secret files ($NFILES)"
count() { grep -caF -f <(patterns) || true; }   # stdin -> number of matching lines

# ── build logs ──────────────────────────────────────────────────────────────
for log in "$@"; do
  if [ -r "$log" ]; then check "build log $(basename "$log")" "$(count < "$log")"
  else incomplete "build log not readable: $log"; fi
done

# ── image metadata and history ──────────────────────────────────────────────
docker image inspect "$IMAGE" >/dev/null 2>&1 || { incomplete "image not found or docker not accessible"; finish; }
OUT="$(docker image inspect "$IMAGE")"            && check "docker image inspect" "$(printf '%s\n' "$OUT" | count)" || incomplete "image inspect"
OUT="$(docker history --no-trunc "$IMAGE")"       && check "docker history --no-trunc" "$(printf '%s\n' "$OUT" | count)" || incomplete "history"
unset OUT

# ── every blob of the exported image (layers, config, manifests) ────────────
if docker save "$IMAGE" -o "$W/image.tar" && mkdir "$W/x" && tar -xf "$W/image.tar" -C "$W/x"; then
  rm -f -- "$W/image.tar"
  layers=0; layer_hits=0; meta_hits=0; files=0
  while IFS= read -r -d '' b; do
    files=$((files + 1))
    if tar -tf "$b" >/dev/null 2>&1; then
      layers=$((layers + 1))
      n="$(tar -xOf "$b" 2>/dev/null | count)" || incomplete "reading layer $(basename "$b")"
      layer_hits=$((layer_hits + ${n:-0}))
    else
      n="$(count < "$b")"; meta_hits=$((meta_hits + ${n:-0}))
    fi
  done < <(find "$W/x" -type f -print0)
  echo "exported files: $files   layers scanned: $layers"
  [ "$layers" -gt 0 ] || incomplete "no layers scanned"
  check "image layers (file contents)" "$layer_hits"
  check "image config/manifests (non-layer blobs)" "$meta_hits"
else
  incomplete "docker save / extract failed"
fi

# ── compose configs (rendered locally; nothing started) ─────────────────────
C=(docker compose --env-file "$REPO/deploy/.env" -f "$REPO/deploy/docker-compose.api.yml")
if OUT="$("${C[@]}" config 2>/dev/null)"; then check "compose config (api)" "$(printf '%s\n' "$OUT" | count)"
else incomplete "compose config (api) failed"; fi
if [ -f "$SECRETS_DIR/admin_password" ]; then
  if OUT="$("${C[@]}" -f "$REPO/deploy/docker-compose.admin.yml" --profile admin config 2>/dev/null)"; then
    check "compose config (api + admin profile)" "$(printf '%s\n' "$OUT" | count)"
  else incomplete "compose config (admin) failed"; fi
fi
if OUT="$("${C[@]}" -f "$REPO/deploy/docker-compose.edge-caddy.yml" config 2>/dev/null)"; then
  check "compose config (api + edge-caddy)" "$(printf '%s\n' "$OUT" | count)"
else incomplete "compose config (edge-caddy) failed"; fi
if [ -f "$SECRETS_DIR/resend_api_key" ]; then
  if OUT="$("${C[@]}" -f "$REPO/deploy/docker-compose.resend.yml" config 2>/dev/null)"; then
    check "compose config (api + resend)" "$(printf '%s\n' "$OUT" | count)"
  else incomplete "compose config (resend) failed"; fi
else
  echo "compose config (api + resend)                  skipped (no resend_api_key yet)"
fi
if [ -f "$SECRETS_DIR/cloudflared_credentials.json" ] && grep -qE '^CLOUDFLARE_TUNNEL_ID=.+' "$REPO/deploy/.env"; then
  if OUT="$(docker compose --env-file "$REPO/deploy/.env" -f "$REPO/deploy/docker-compose.tunnel.yml" --profile tunnel config 2>/dev/null)"; then
    check "compose config (tunnel)" "$(printf '%s\n' "$OUT" | count)"
  else incomplete "compose config (tunnel) failed"; fi
fi
if docker inspect bitbotpro-cloudflared >/dev/null 2>&1; then
  check "docker inspect bitbotpro-cloudflared" "$(docker inspect bitbotpro-cloudflared | count)"
  check "docker logs bitbotpro-cloudflared" "$(docker logs bitbotpro-cloudflared 2>&1 | count)"
fi
unset OUT

# ── running API container (only if it already exists; never started here) ───
if docker inspect bitbotpro-api >/dev/null 2>&1; then
  check "docker inspect bitbotpro-api" "$(docker inspect bitbotpro-api | count)"
  check "docker logs bitbotpro-api" "$(docker logs bitbotpro-api 2>&1 | count)"
else
  echo "container bitbotpro-api                        not present (skipped; not started yet)"
fi
finish
