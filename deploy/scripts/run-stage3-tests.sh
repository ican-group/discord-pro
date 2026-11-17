#!/usr/bin/env bash
# Spec 008 security test gate (Stages 1-4; originally Stage 3). Needs docker access (sudo). Touches NO production
# container, network, volume, database or secret.
#
#   sudo bash deploy/scripts/run-stage3-tests.sh > ~/.bitbotpro/test-stage3.log 2>&1; echo "exit=$?"
#
# 1. Builds the Docker `test` target (runs the full unit suite) as bitbotpro-api-test.
# 2. Starts a DISPOSABLE mongod: unique name, new --internal network (no egress,
#    no published ports), tmpfs storage only (no volumes), removed afterwards.
# 3. Runs tests/integration against it from the test image (same isolated network).
# 4. Always removes the disposable container and network.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
MONGO_IMAGE="mongo:8.0.38-noble"          # already present locally (Phase 1)
TEST_IMAGE="bitbotpro-api-test"
SUFFIX="$(date +%s)-$$"
NET="bbp-itest-net-${SUFFIX}"
MONGO="bbp-itest-mongo-${SUFFIX}"

cleanup() {
  docker rm -f "$MONGO" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  echo "== cleanup: removed $MONGO and $NET (if created)"
}
trap cleanup EXIT

case "$NET" in bitbotpro_*|*bitbotpro_db*|*bitbotpro_egress*) echo "refusing: unsafe network name"; exit 2;; esac

echo "== [1/3] docker build --target test (unit suite)"
docker build --progress=plain --target test -t "$TEST_IMAGE" backend

echo "== [2/3] disposable mongod on internal network $NET"
docker network create --internal "$NET" >/dev/null
docker run -d --rm --name "$MONGO" --network "$NET" \
  --tmpfs /data/db --tmpfs /data/configdb --tmpfs /tmp \
  --label bitbotpro.purpose=stage3-itest "$MONGO_IMAGE" >/dev/null
for i in $(seq 1 60); do
  if docker exec "$MONGO" mongosh --quiet --eval 'db.adminCommand({ping: 1}).ok' 2>/dev/null | grep -q 1; then
    echo "mongod ready after ${i}s"; break
  fi
  [ "$i" = 60 ] && { echo "mongod did not become ready"; exit 1; }
  sleep 1
done

echo "== [3/3] integration tests (real MongoDB, disposable database)"
docker run --rm --network "$NET" --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges \
  -e MONGO_TEST_URL="mongodb://${MONGO}:27017/?directConnection=true" \
  -e MONGO_TEST_DB="bbp_itest" -e PYTHONDONTWRITEBYTECODE=1 \
  "$TEST_IMAGE" python -m unittest discover -s tests/integration -t . -v

echo "== RESULT: PASS (spec 008 unit suite in build + integration tests)"
