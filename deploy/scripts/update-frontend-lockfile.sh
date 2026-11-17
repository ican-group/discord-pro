#!/usr/bin/env bash
# Add the missing entries to frontend/yarn.lock with Yarn 1.22.22 (the version
# pinned in package.json "packageManager"), preserving every existing entry.
# Needs docker (sudo) and internet access for the npm registry.
#
#   sudo bash deploy/scripts/update-frontend-lockfile.sh > ~/.bitbotpro/frontend-lockfile.log 2>&1; echo "exit=$?"
#
# Steps (all in a disposable Node container; the repo is mounted read-only):
#  1. `yarn install --ignore-scripts` on a copy: Yarn 1 keeps existing lockfile
#     resolutions and only adds what is missing. No dependency install scripts run.
#  2. Verify the new lockfile with `yarn install --frozen-lockfile --ignore-scripts`.
#  3. On the host: compare old vs new (lockfile-diff.py). The repo file is
#     replaced ONLY if no existing entry changes its resolved version (additions
#     and pruning of unused entries are allowed); otherwise nothing changes.
#  A backup of the old lockfile is kept next to the log.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NODE_IMAGE="node:20.18.1-bookworm-slim"
OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT
chmod 0777 "$OUT"

docker run --rm --name "bbp-lockfile-$$" \
  -v "$REPO_ROOT/frontend:/src:ro" -v "$OUT:/out" \
  "$NODE_IMAGE" bash -euo pipefail -c '
    mkdir /app && cd /src && tar --exclude=./node_modules --exclude=./build -cf - . | tar -xf - -C /app
    cd /app
    echo "== node $(node --version), yarn $(yarn --version)"
    [ "$(yarn --version)" = "1.22.22" ] || { echo "FAIL: expected yarn 1.22.22"; exit 1; }
    echo "== [1/2] yarn install (adds missing lockfile entries; no scripts)"
    yarn install --non-interactive --ignore-scripts --network-timeout 600000 >/tmp/install.log 2>&1 \
      || { tail -n 60 /tmp/install.log; exit 1; }
    grep -E "warning|error" /tmp/install.log | grep -viE "peer dependency|unmet peer|incorrect peer" | head -40 || true
    echo "== [2/2] verify: yarn install --frozen-lockfile"
    rm -rf node_modules
    yarn install --frozen-lockfile --non-interactive --ignore-scripts --network-timeout 600000 >/tmp/verify.log 2>&1 \
      || { tail -n 40 /tmp/verify.log; exit 1; }
    cp yarn.lock /out/yarn.lock
    echo "frozen-lockfile verification: OK"
  '

echo "== lockfile diff (old -> new)"
BACKUP_DIR="$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)/.bitbotpro"
cp "$REPO_ROOT/frontend/yarn.lock" "$BACKUP_DIR/yarn.lock.before-9a-fix"
if python3 "$REPO_ROOT/deploy/scripts/lockfile-diff.py" "$REPO_ROOT/frontend/yarn.lock" "$OUT/yarn.lock"; then
  owner="$(stat -c %u:%g "$REPO_ROOT/frontend/yarn.lock")"
  install -m 0644 "$OUT/yarn.lock" "$REPO_ROOT/frontend/yarn.lock"
  chown "$owner" "$REPO_ROOT/frontend/yarn.lock"
  echo "== RESULT: frontend/yarn.lock updated (existing versions preserved). Backup: $BACKUP_DIR/yarn.lock.before-9a-fix"
else
  cp "$OUT/yarn.lock" "$BACKUP_DIR/yarn.lock.proposed"
  echo "== RESULT: NOT APPLIED - existing entries would change. Proposed file: $BACKUP_DIR/yarn.lock.proposed"
  exit 1
fi
