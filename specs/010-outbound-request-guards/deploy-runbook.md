# Specs 009 + 010 — Deployment Runbook (API first, then frontend)

Supersedes `specs/009-tenant-isolation-onboarding/deploy-runbook.md`. The image
contains spec 009 (tenant isolation, onboarding, AI defaults) **and** spec 010
(Ollama SSRF guard, Zendesk host validation).

**SUPERSEDED (2026-11-17) by `specs/011-api-integration-ssrf/deploy-runbook.md`** — do not deploy `spec010-5e9a61bf49ff`.

**Status: prepared 2026-11-17, NOT executed.** Every `[GATE]` needs explicit approval.

| Item | Value |
|---|---|
| New API image | `bitbotpro-api:spec010-5e9a61bf49ff` (`sha256:ad31b56ec029…`) |
| Tag meaning | first 12 chars of the git tree hash of `backend/` (`5e9a61bf49ffc27005eff0f611545ecaa48db321`) |
| Current / rollback image | `bitbotpro-api:launch-20261117` (`sha256:ba5e81587947…`), kept unchanged |
| Also kept (not deployed) | `bitbotpro-api:spec009-75737f5c636c` (`sha256:6d7cb6f0624c…`), spec 009 only |
| Ollama in production | disabled (no `OLLAMA_ALLOWED_BASE_URLS`; nothing to configure) |
| Compose project touched | `bitbotpro-api` only (service `api`) |
| Not touched | project `bitbotpro`: `bitbotpro-mongo`, `bitbotpro-postgres`, volumes `bitbotpro_mongo_*`, `bitbotpro_postgres_data` |

## Order and compatibility

| API | Frontend | Works? |
|---|---|---|
| new | old | Yes while only **one** organisation exists (true today: "Ican Group"). With two or more orgs, the old Team/Organisations pages send a mismatched `X-Org-ID` for non-selected orgs and get 400. |
| old | new | Yes (header always equals URL), but without the security fix. |

**Deploy the API now; deploy the frontend before a second organisation is created or more users are invited.**

## A. [GATE] API deployment

Run from `~/projects/discord-pro` as `ntfreem`. `api-compose.sh` always applies
`docker-compose.api.yml` + `docker-compose.edge-caddy.yml` + `docker-compose.resend.yml`.

### A0. Pre-flight (read-only, plus two local backup files)
```bash
cd ~/projects/discord-pro
TAG=spec010-5e9a61bf49ff
docker image inspect bitbotpro-api:$TAG --format '{{.Id}}'                  # sha256:ad31b56ec029...
docker image inspect bitbotpro-api:launch-20261117 --format '{{.Id}}'       # sha256:ba5e81587947...
grep '^BITBOTPRO_API_TAG=' deploy/.env                                      # BITBOTPRO_API_TAG=launch-20261117
docker inspect bitbotpro-mongo bitbotpro-postgres \
  --format '{{.Name}} {{.Id}} {{.State.StartedAt}} {{.State.Health.Status}}' > ~/.bitbotpro/predeploy-api.txt
cp -p deploy/.env ~/.bitbotpro/deploy.env.pre-spec010
```

### A1. Point the deployment at the new tag
```bash
sed -i 's/^BITBOTPRO_API_TAG=launch-20261117$/BITBOTPRO_API_TAG=spec010-5e9a61bf49ff/' deploy/.env
grep '^BITBOTPRO_API_TAG=' deploy/.env                                      # ...=spec010-5e9a61bf49ff
bash deploy/scripts/api-compose.sh config --quiet && echo CONFIG-OK
bash deploy/scripts/api-compose.sh config --services                        # api   (only)
bash deploy/scripts/api-compose.sh config | grep -E 'image:|host_ip|TRUSTED_PROXY_IPS|RESEND_API_KEY_FILE'
#   RESEND_API_KEY_FILE: /run/secrets/resend_api_key
#   TRUSTED_PROXY_IPS: 10.50.0.1
#   image: bitbotpro-api:spec010-5e9a61bf49ff
#   host_ip: 10.50.0.20
```

### A2. Recreate the API container only
```bash
bash deploy/scripts/api-compose.sh up -d --no-deps --no-build api
```
Expected: `Container bitbotpro-api  Recreate` → `Recreated` → `Starting` → `Started`.
No line mentions mongo or postgres. Expect a short API outage (seconds; healthcheck start
period up to 60 s). In-memory chat sessions are reset (by design, spec 004 F3).

### A3. Wait for health and read the startup log
```bash
for i in $(seq 1 40); do s=$(docker inspect bitbotpro-api --format '{{.State.Health.Status}}'); echo "$i $s"; [ "$s" = healthy ] && break; sleep 3; done
docker logs --since 5m bitbotpro-api 2>&1 | grep -v '/api/health' | tail -20
```
Expected: `healthy`; log shows `Startup mode: APP_MODE=api APP_ENV=production …` and
`Application startup complete.`; no `Traceback`.

### A4. Verify (read-only; one login, no database writes)
```bash
bash deploy/scripts/verify-api-deploy.sh spec010-5e9a61bf49ff admin@bitbotpro.io
```
Expected: every line `PASS`, ending `RESULT: PASS`, including
`mongo/postgres: same container IDs and start times as before the deploy`,
`spec 009: header/URL mismatch rejected…`, `spec 010: Ollama SSRF guard and Zendesk
host validation active`, `mismatched X-Org-ID -> 400`, `matching X-Org-ID -> 200`,
`bot list has no raw API key fields`, `unknown org -> 404`,
`llm-config reports ollama_enabled=false`.
(Section 5 tests the first organisation the superadmin sees: "Ican Group".)

### A5. Browser smoke test
Sign in at https://bitbotpro.io → Bots: "Cardano AI Assistant" is listed. With the old
frontend no "AI key needed" badge is shown yet (that UI ships with the frontend).

### A6. Rollback (API only; no data migration either way)
```bash
cd ~/projects/discord-pro
sed -i 's/^BITBOTPRO_API_TAG=spec010-5e9a61bf49ff$/BITBOTPRO_API_TAG=launch-20261117/' deploy/.env
grep '^BITBOTPRO_API_TAG=' deploy/.env                                      # ...=launch-20261117
bash deploy/scripts/api-compose.sh up -d --no-deps --no-build api
for i in $(seq 1 40); do s=$(docker inspect bitbotpro-api --format '{{.State.Health.Status}}'); echo "$i $s"; [ "$s" = healthy ] && break; sleep 3; done
bash deploy/scripts/verify-api-deploy.sh launch-20261117
```
Expected after rollback: sections 1, 2, 4 `PASS`; both section 3 checks `FAIL` (the old
code lacks the fixes — this confirms the old image is running). Data written by the new API (bots with
`provider: anthropic`) is fully readable by the old API. If `deploy/.env` is damaged:
`cp -p ~/.bitbotpro/deploy.env.pre-spec010 deploy/.env`.

Never run `down`, `down -v`, `rm` or `up` without `--no-deps` for this change.

## B. [GATE] GitHub

```bash
cd ~/projects/discord-pro
git switch bitbotpro-v1.0.0
git add backend/v2_helpers.py backend/bot_routes.py backend/org_routes.py backend/account_routes.py \
        backend/connection_routes.py backend/server.py \
        backend/tests/unit/_support.py backend/tests/unit/_fake_motor.py backend/tests/unit/test_v2_tenant_isolation.py \
        frontend/src/utils/v2api.js frontend/src/utils/__tests__/v2api.test.js \
        frontend/src/pages/BotList.js frontend/src/pages/BotSettings.js frontend/src/pages/Dashboard.js \
        frontend/src/pages/Organisations.js \
        backend/outbound_guard.py backend/zendesk_routes.py \
        backend/tests/unit/test_outbound_guard.py backend/tests/unit/test_outbound_routes.py \
        deploy/scripts/verify-api-deploy.sh \
        specs/009-tenant-isolation-onboarding/ specs/010-outbound-request-guards/
git status --short                                   # nothing else staged or modified
git commit                                           # "Specs 009+010: tenant isolation, onboarding, AI defaults, outbound guards"
git rev-parse HEAD:backend                           # MUST print 5e9a61bf49ffc27005eff0f611545ecaa48db321
git push origin bitbotpro-v1.0.0
```
The `HEAD:backend` check proves the committed backend is byte-identical to the image.

## C. [GATE] Emergent frontend

1. In Emergent, update the frontend to the commit pushed in B (branch `bitbotpro-v1.0.0`).
2. Keep the build variables unchanged: `REACT_APP_API_BASE_URL=https://api.bitbotpro.io`
   (and `REACT_APP_BACKEND_URL=https://api.bitbotpro.io` if set).
3. Build and deploy. The local gate already passed (67 Jest tests, production build with the
   API origin embedded): `bash deploy/scripts/run-frontend-tests.sh` → `RESULT: PASS`.
4. Verify in a private browser window at https://bitbotpro.io:
   - DevTools → Network: every `/api/v2/orgs/<id>/…` request has `X-Org-ID: <same id>`.
   - Bots: "Cardano AI Assistant" shows **AI key needed**; its Behaviour page shows the
     "AI chat is not set up yet" banner; the provider list shows Anthropic and OpenAI, plus
     "Emergent Universal Key (legacy, unavailable here)" (that bot still uses `emergent`);
     there is no Ollama option (disabled on this server).
   - Team and Organisations pages load without errors.
5. Frontend rollback: redeploy the previous Emergent build (commit `1b8e0a3`). Safe while only
   one organisation exists (see the compatibility table).

## D. Follow-up (owner action, not part of this deployment)

Set the existing bot to Anthropic or OpenAI with your own key (Bot → Behaviour → LLM Provider);
it currently uses `emergent` and cannot answer chat on this deployment.
