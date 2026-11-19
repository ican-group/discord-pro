#!/bin/sh
# BitBotPro backend container entrypoint (spec 004, C9).
#
# For an explicit allowlist of variables (MONGO_URL, JWT_SECRET, ENCRYPTION_KEY,
# ENCRYPTION_KEYS (Phase 6B key ring), CODE_HMAC_KEY and the optional RESEND_API_KEY), if <NAME>_FILE is set, read that file
# into <NAME> for this process only, then exec the command. Values never appear
# in image config, `docker inspect` (which only shows the *_FILE paths), or logs.
# Never add `set -x` here.
set -eu

for name in MONGO_URL JWT_SECRET ENCRYPTION_KEY ENCRYPTION_KEYS CODE_HMAC_KEY RESEND_API_KEY; do
  file_var="${name}_FILE"
  eval "file_path=\${$file_var:-}"
  if [ -n "$file_path" ]; then
    eval "current=\${$name:-}"
    if [ -n "$current" ]; then
      echo "entrypoint: both $name and $file_var are set; use only one" >&2
      exit 64
    fi
    if [ ! -r "$file_path" ]; then
      echo "entrypoint: $file_var points to an unreadable file" >&2
      exit 66
    fi
    # Command substitution strips trailing newlines only.
    value="$(cat "$file_path")"
    if [ -z "$value" ]; then
      echo "entrypoint: $file_var points to an empty file" >&2
      exit 65
    fi
    export "$name=$value"
    unset "$file_var"
  fi
done
unset name file_var file_path current value

exec "$@"
