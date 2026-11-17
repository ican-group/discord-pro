#!/usr/bin/env bash
# Generate the API secrets for spec 004 Phase 2b. Run as the normal user (no sudo).
#
#   bash deploy/scripts/generate-api-secrets.sh            # uses BITBOTPRO_SECRETS_DIR from deploy/.env
#
# - Never overwrites an existing file; never prints a secret value.
# - Reuses the Phase 1 MongoDB application password to build backend_mongo_url.
# - Files are 0444 inside the 0700 secrets directory (the container's non-root
#   uid 10001 must read the bind-mounted files; host access is limited by the
#   0700 directory and the 0750 home directory).
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/deploy/.env"
DIR="${BITBOTPRO_SECRETS_DIR:-$(grep -E '^BITBOTPRO_SECRETS_DIR=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)}"
[ -n "$DIR" ] || { echo "BITBOTPRO_SECRETS_DIR not set" >&2; exit 1; }
[ -d "$DIR" ] || { echo "secrets directory does not exist (run Phase 1 first)" >&2; exit 1; }
[ "$(stat -c %a "$DIR")" = "700" ] || { echo "secrets directory must be mode 0700" >&2; exit 1; }
[ -r "$DIR/mongo_app_password" ] || { echo "Phase 1 secret mongo_app_password missing" >&2; exit 1; }

write_new() {  # name, generator command...
  local name="$1"; shift
  local f="$DIR/$name"
  if [ -e "$f" ]; then echo "exists, kept: $name"; return; fi
  local tmp; tmp="$(mktemp "$DIR/.tmp.XXXXXX")"
  "$@" > "$tmp"
  [ -s "$tmp" ] || { rm -f "$tmp"; echo "generation failed: $name" >&2; exit 1; }
  chmod 0444 "$tmp"
  mv -n "$tmp" "$f"
  rm -f "$tmp"
  echo "generated: $name"
}

gen_mongo_url() {
  local pw; pw="$(head -n1 "$DIR/mongo_app_password")"
  printf 'mongodb://bitbotpro_app:%s@bitbotpro-mongo:27017/bitbotpro?authSource=bitbotpro\n' "$pw"
}
gen_hex32() { openssl rand -hex 32; }
gen_fernet() { python3 -I -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'; }
gen_admin_password() {
  # 32 chars from all four classes, validated against the bootstrap policy (C6).
  python3 -I - "$REPO_ROOT/backend" <<'PY'
import secrets, string, sys
sys.path.insert(0, sys.argv[1])
from password_policy import check_password
alphabet = string.ascii_letters + string.digits + "!#%+-=?@^_~"
for _ in range(100):
    pw = "".join(secrets.choice(alphabet) for _ in range(32))
    if not check_password(pw, "admin@bitbotpro.io"):
        print(pw)
        break
else:
    sys.exit("could not generate a policy-compliant password")
PY
}

write_new backend_mongo_url gen_mongo_url
write_new jwt_secret gen_hex32
write_new encryption_key gen_fernet
write_new code_hmac_key gen_hex32        # spec 008 Stage 3 (independent of jwt_secret)
write_new admin_password gen_admin_password

echo "--- lengths only (values are never printed):"
for n in backend_mongo_url jwt_secret encryption_key code_hmac_key admin_password; do
  printf '%s len=%s mode=%s\n' "$n" "$(tr -d '\n' < "$DIR/$n" | wc -c)" "$(stat -c %a "$DIR/$n")"
done
