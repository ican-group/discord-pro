#!/usr/bin/env bash
# The ONLY supported way to run docker compose for the production API (spec 008).
# Always includes BOTH required overrides:
#   - docker-compose.edge-caddy.yml : publish on 10.50.0.20:8000 only, trust 10.50.0.1
#   - docker-compose.resend.yml     : Resend key as a Docker secret file
# Examples (run as your user; sudo is used for docker):
#   bash deploy/scripts/api-compose.sh config --quiet
#   bash deploy/scripts/api-compose.sh up -d --no-deps --no-build api
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
SECRETS_DIR="$(grep -E '^BITBOTPRO_SECRETS_DIR=' deploy/.env | tail -n1 | cut -d= -f2-)"
[ -s "$SECRETS_DIR/resend_api_key" ] || { echo "STOP: $SECRETS_DIR/resend_api_key missing (Resend override required)" >&2; exit 1; }
[ $# -gt 0 ] || { echo "usage: $0 <docker compose arguments>" >&2; exit 2; }
exec sudo docker compose \
  -f deploy/docker-compose.api.yml \
  -f deploy/docker-compose.edge-caddy.yml \
  -f deploy/docker-compose.resend.yml \
  --env-file deploy/.env "$@"
