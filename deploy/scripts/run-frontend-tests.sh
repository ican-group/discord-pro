#!/usr/bin/env bash
# Spec 008 Stage 9a frontend gate: Jest tests + production build in a DISPOSABLE
# Node container. Needs docker (sudo) and internet access for the yarn registry.
# Never deploys anything; mounts frontend/ READ-ONLY and works on a copy inside
# the container, so no node_modules or build output is written to the repo.
#
#   sudo bash deploy/scripts/run-frontend-tests.sh > ~/.bitbotpro/frontend-9a.log 2>&1; echo "exit=$?"
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NODE_IMAGE="node:20.18.1-bookworm-slim"
API_ORIGIN="https://api.bitbotpro.io"

docker run --rm --name "bbp-frontend-test-$$" \
  -v "$REPO_ROOT/frontend:/src:ro" \
  -e CI=true -e API_ORIGIN="$API_ORIGIN" \
  "$NODE_IMAGE" bash -euo pipefail -c '
    mkdir /app && cd /src && tar --exclude=./node_modules --exclude=./build -cf - . | tar -xf - -C /app
    cd /app
    echo "== node $(node --version), yarn $(yarn --version)"
    echo "== [1/3] yarn install --frozen-lockfile"
    yarn install --frozen-lockfile --non-interactive --network-timeout 600000 >/tmp/install.log 2>&1 \
      || { tail -n 60 /tmp/install.log; exit 1; }
    echo "== [2/3] jest (CRA/craco test runner)"
    yarn test --watchAll=false --ci --verbose
    echo "== [3/3] production build with REACT_APP_API_BASE_URL=$API_ORIGIN"
    CI=false REACT_APP_API_BASE_URL="$API_ORIGIN" GENERATE_SOURCEMAP=false yarn build
    echo "== build checks"
    hits=$(grep -l "$API_ORIGIN" build/static/js/*.js | wc -l)
    echo "bundles containing $API_ORIGIN: $hits"
    [ "$hits" -gt 0 ] || { echo "FAIL: API origin not embedded in the build"; exit 1; }
    echo "== RESULT: PASS"
  '
