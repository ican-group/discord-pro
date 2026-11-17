#!/usr/bin/env bash
# Post-deploy verification for the API container: spec 009 (tenant isolation),
# spec 010 (Ollama SSRF guard, Zendesk host validation) and spec 011 (API
# integration SSRF hardening).
#
#   bash deploy/scripts/verify-api-deploy.sh <expected-tag> [superadmin-email]
#
# Read-only. Docker calls: inspect, port, exec (a Python check that imports the
# app modules; no network, no database). HTTP: public GETs. With an email, it
# also logs in ONCE (prompts for the password without echo; a successful login
# writes nothing to the database), runs three GETs, and logs out. The token and
# password stay in shell variables and are never printed or written to disk.
#
# Compares the database containers with ~/.bitbotpro/predeploy-api.txt if
# present (written by the pre-deploy step in specs/009-.../deploy-runbook.md).
# Exit 0 = all checks passed, 1 = at least one failed.
# Testing the script itself: BBP_API=<base url> ONLY_HTTP=1 skips sections 1-3.
set -uo pipefail
TAG="${1:?usage: $0 <expected-tag> [superadmin-email]}"
EMAIL="${2:-}"
API="${BBP_API:-https://api.bitbotpro.io}"
ONLY_HTTP="${ONLY_HTTP:-0}"
NIL_ORG="00000000-0000-0000-0000-000000000000"
BASELINE="$HOME/.bitbotpro/predeploy-api.txt"
FAIL=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAIL=1; }
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"

if [ "$ONLY_HTTP" != 1 ]; then
echo "== 1. API container"
img="$($DOCKER inspect bitbotpro-api --format '{{.Config.Image}}' 2>/dev/null)"
[ "$img" = "bitbotpro-api:$TAG" ] && pass "image $img" || fail "image is '$img', expected bitbotpro-api:$TAG"
health="$($DOCKER inspect bitbotpro-api --format '{{.State.Health.Status}}' 2>/dev/null)"
[ "$health" = "healthy" ] && pass "health $health" || fail "health is '$health'"
ports="$($DOCKER port bitbotpro-api 2>/dev/null | tr '\n' ' ' | sed 's/ $//')"
[ "$ports" = "8000/tcp -> 10.50.0.20:8000" ] && pass "published only on 10.50.0.20:8000" || fail "ports: '$ports'"
envs="$($DOCKER inspect bitbotpro-api --format '{{range .Config.Env}}{{println .}}{{end}}')"
grep -qx 'TRUSTED_PROXY_IPS=10.50.0.1' <<<"$envs" && pass "edge-caddy override (TRUSTED_PROXY_IPS)" || fail "TRUSTED_PROXY_IPS missing"
grep -qx 'RESEND_API_KEY_FILE=/run/secrets/resend_api_key' <<<"$envs" && pass "resend override (RESEND_API_KEY_FILE)" || fail "RESEND_API_KEY_FILE missing"
files="$($DOCKER inspect bitbotpro-api --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
case "$files" in *docker-compose.edge-caddy.yml*docker-compose.resend.yml*) pass "compose files: api + edge-caddy + resend";; *) fail "compose files: $files";; esac

echo "== 2. Database containers untouched"
now="$($DOCKER inspect bitbotpro-mongo bitbotpro-postgres --format '{{.Name}} {{.Id}} {{.State.StartedAt}} {{.State.Health.Status}}')"
echo "$now" | sed 's/^/      /'
if [ -f "$BASELINE" ]; then
  [ "$now" = "$(cat "$BASELINE")" ] && pass "mongo/postgres: same container IDs and start times as before the deploy" \
    || fail "mongo/postgres differ from $BASELINE"
else
  echo "SKIP  no baseline file $BASELINE"
fi

echo "== 3. Security code inside the running container"
if $DOCKER exec -i bitbotpro-api python - <<'PY'
import sys
sys.path.insert(0, "/app")
import v2_helpers as h, bot_routes as b
from fastapi import HTTPException
class R:
    def __init__(s, p, hd): s.path_params, s.headers = p, hd
def out(p, hd):
    try: return h.resolve_target_org_id(R(p, hd))
    except HTTPException as e: return e.status_code
ok = (out({"org_id": "B"}, {"X-Org-ID": "A"}) == 400 and out({"org_id": "B"}, {}) == "B"
      and b.DEFAULT_NEW_BOT_LLM_CONFIG == {"provider": "anthropic", "model": None})
sys.exit(0 if ok else 1)
PY
then pass "spec 009: header/URL mismatch rejected; new-bot default is anthropic"; else fail "spec 009 code check"; fi
if $DOCKER exec -i bitbotpro-api python - <<'PY'
import asyncio, os, sys
sys.path.insert(0, "/app")
import outbound_guard as g
def blocked(fn, *a):
    try:
        r = fn(*a)
        if asyncio.iscoroutine(r):
            asyncio.run(r)
        return False
    except g.OutboundBlocked:
        return True
checks = {
    "ollama disabled (no allowlist in production)": not g.ollama_enabled(os.environ),
    "metadata/internal Ollama targets blocked": all(blocked(g.prepare_ollama_request, u, os.environ) for u in (
        "http://169.254.169.254", "http://127.0.0.1:11434", "http://bitbotpro-mongo:27017", "http://10.50.0.1")),
    "addresses: metadata/loopback/private rejected": not any(g.address_allowed(a) for a in (
        "169.254.169.254", "127.0.0.1", "10.0.0.1", "172.20.0.2", "::1", "fd00:ec2::254")),
    "zendesk host injection rejected": all(blocked(g.normalize_zendesk_subdomain, s) for s in (
        "evil.example#", "evil.example/", "acme.zendesk.com.evil.example", "user@evil.example", "acme:443")),
    "zendesk url fixed to *.zendesk.com": g.zendesk_url("acme", "/api/v2/users/me.json") == "https://acme.zendesk.com/api/v2/users/me.json",
}
for name, ok in checks.items():
    print(("      ok   " if ok else "      BAD  ") + name)
sys.exit(0 if all(checks.values()) else 1)
PY
then pass "spec 010: Ollama SSRF guard and Zendesk host validation active"; else fail "spec 010 code check"; fi
if $DOCKER exec -i bitbotpro-api python - <<'PY'
import asyncio, os, sys
sys.path.insert(0, "/app")
import outbound_guard as g, api_integrations as ai
def rejected(url):
    try:
        g.parse_integration_url(url, os.environ)
        return False
    except g.OutboundBlocked:
        return True
def blocked_request(url, headers=None):
    r = asyncio.run(ai.safe_api_request("GET", url, headers=headers))
    return r["success"] is False and r["status"] == 0 and r["error"].startswith("URL blocked")
src = open("/app/api_integrations.py").read() + open("/app/api_integrations_routes.py").read()
checks = {
    "integrations: alternative IP encodings rejected": all(rejected(f"https://{h}/") for h in (
        "2130706433", "0x7f.1", "0177.0.0.1", "127.1")),
    "integrations: internal names, http and credentials rejected": all(rejected(u) for u in (
        "https://bitbotpro-mongo:27017/", "https://localhost/", "https://metadata.google.internal/",
        "http://api.example.com/", "https://user:pw@api.example.com/")),
    "integrations: metadata/internal requests blocked before connecting": all(blocked_request(u) for u in (
        "https://169.254.169.254/latest/meta-data/", "https://[::ffff:127.0.0.1]/", "https://10.50.0.1/")),
    "integrations: Host header cannot be set": blocked_request("https://api.example.com/", {"Host": "bitbotpro-mongo"}),
    "integrations: no raw HTTP clients or redirect following": "httpx." not in src and "follow_redirects=True" not in src,
}
for name, ok in checks.items():
    print(("      ok   " if ok else "      BAD  ") + name)
sys.exit(0 if all(checks.values()) else 1)
PY
then pass "spec 011: API integration SSRF hardening active"; else fail "spec 011 code check"; fi
fi

echo "== 4. Public endpoint"
code="$(curl -s -o /dev/null -w '%{http_code}' "$API/api/health")"
[ "$code" = 200 ] && pass "GET /api/health -> 200" || fail "GET /api/health -> $code"
code="$(curl -s -o /dev/null -w '%{http_code}' -H "X-Org-ID: $NIL_ORG" "$API/api/v2/orgs/$NIL_ORG/bots")"
[ "$code" = 401 ] && pass "unauthenticated org route -> 401" || fail "unauthenticated org route -> $code"

if [ -n "$EMAIL" ]; then
  echo "== 5. Authenticated tenant-isolation checks (read-only)"
  read -rsp "Password for $EMAIL (not echoed): " PW; echo
  BODY="$(EM="$EMAIL" PW="$PW" python3 -c 'import json,os; print(json.dumps({"email_or_username": os.environ["EM"], "password": os.environ["PW"]}))')"
  unset PW
  TOKEN="$(printf '%s' "$BODY" | curl -s -H 'Content-Type: application/json' --data-binary @- "$API/api/auth/login" \
           | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)"
  unset BODY
  if [ -z "$TOKEN" ]; then
    fail "login failed (no token); authenticated checks skipped"
  else
    auth() { printf 'Authorization: Bearer %s' "$TOKEN"; }
    ORG="$(curl -s -H @<(auth) "$API/api/v2/orgs" | python3 -c 'import json,sys; o=json.load(sys.stdin)["organisations"]; print(o[0]["id"] if o else "")')"
    if [ -z "$ORG" ]; then
      fail "no organisation visible; cannot run org checks"
    else
      echo "      org under test: $ORG"
      code="$(curl -s -o /dev/null -w '%{http_code}' -H @<(auth) -H "X-Org-ID: $NIL_ORG" "$API/api/v2/orgs/$ORG/bots")"
      [ "$code" = 400 ] && pass "mismatched X-Org-ID -> 400" || fail "mismatched X-Org-ID -> $code (expected 400)"
      body="$(curl -s -w '\n%{http_code}' -H @<(auth) -H "X-Org-ID: $ORG" "$API/api/v2/orgs/$ORG/bots")"
      code="${body##*$'\n'}"; body="${body%$'\n'*}"
      [ "$code" = 200 ] && pass "matching X-Org-ID -> 200" || fail "matching X-Org-ID -> $code"
      if grep -qE '"(anthropic|openai)_api_key":' <<<"$body"; then fail "bot list contains raw API key fields"; else pass "bot list has no raw API key fields"; fi
      grep -q '"llm_configured"' <<<"$body" && pass "bot list reports llm_configured" || fail "llm_configured missing"
      code="$(curl -s -o /dev/null -w '%{http_code}' -H @<(auth) "$API/api/v2/orgs/$NIL_ORG/bots")"
      [ "$code" = 404 ] && pass "unknown org -> 404" || fail "unknown org -> $code (expected 404)"
      BOT="$(python3 -c 'import json,sys; b=json.loads(sys.argv[1]).get("bots",[]); print(b[0]["id"] if b else "")' "$body" 2>/dev/null)"
      if [ -n "$BOT" ]; then
        cfg="$(curl -s -H @<(auth) -H "X-Org-ID: $ORG" "$API/api/v2/orgs/$ORG/bots/$BOT/llm-config")"
        python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1]).get("ollama_enabled") is False else 1)' "$cfg" 2>/dev/null \
          && pass "llm-config reports ollama_enabled=false" || fail "llm-config ollama_enabled is not false"
      else
        echo "SKIP  no bot in $ORG for the llm-config check"
      fi
    fi
    curl -s -o /dev/null -X POST -H @<(auth) "$API/api/auth/logout"
    unset TOKEN
  fi
fi

echo "----"
[ "$FAIL" = 0 ] && { echo "RESULT: PASS"; exit 0; } || { echo "RESULT: FAIL"; exit 1; }
