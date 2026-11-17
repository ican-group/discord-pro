# Spec 004 — Backend Container Deployment (API-only, MongoDB)

## Status

**Approved in principle (2026-11-17). Decisions D1–D8 were selected, with eight
additional mandatory requirements (R1–R8).** Each implementation step below is
gated by your approval.

**Progress (2026-11-17):** Phase 2a code is implemented. Phase 2b files are
prepared; the image isn't built and nothing is deployed. See
[Amendments](#amendments-during-implementation) and `implementation.md`.

| Decision | Selection |
|---|---|
| D1 | Code changes C1–C6 approved, including strong-password validation (C6). The mandatory requirements add C7–C9. |
| D2 | Intended administrator: `admin@bitbotpro.io`. **The account is not created in this spec's first rollout.** It's created only by the controlled bootstrap (R2), with separate explicit approval. |
| D3 | Docker secret files plus a startup script. |
| D4 | Python 3.11. |
| D5 | Keep the existing dependencies; remove only Emergent-only packages that block installation (`emergentintegrations`). |
| D6 | `/api/health` with a MongoDB connectivity check. |
| D7 | Separate API compose file and project. |
| D8 | `motor==3.7.1`, `pymongo==4.10.1`, with compatibility testing. |

### Mandatory requirements

| # | Requirement | Where it's handled |
|---|---|---|
| R1 | Legacy account/data migrations don't run on startup; they require an explicit admin action. | C7, [Admin CLI](#admin-cli-backendmanagepy) |
| R2 | No automatic superadmin creation on startup; a controlled one-time bootstrap with explicit approval. | C7, C6, [Admin CLI](#admin-cli-backendmanagepy) |
| R3 | Explicit production CORS for `https://bitbotpro.io`; no wildcard; dev origins configured separately. | C8 |
| R4 | `APP_MODE=api` blocks Discord bot processes, including via admin configuration changes. | C2 |
| R5 | MongoDB holds application config, auth, workspaces and integrations only; no new MongoDB KB functionality. | [Non-Goals](#non-goals), D9 |
| R6 | No API or database ports on the host. | [Compose service](#compose-service-deploydocker-composeapiyml), [Validation](#validation) |
| R7 | PostgreSQL and the existing database volumes stay untouched. | [Architecture](#architecture), [Rollback](#rollback) |
| R8 | No credentials or secret values in build logs, image layers or `docker inspect` output. | C9, [Secret hygiene](#secret-hygiene-r8) |

## Amendments during implementation

These amendments were approved or required during Phases 2a and 2b. Where they
conflict with the sections below, the amendments win.

| # | Amendment | Reason |
|---|---|---|
| M1 | **MongoDB Knowledge Base disabled in `APP_MODE=api`:** a pure-ASGI middleware returns `503` for `/api/knowledge` and everything beneath it, before any handler or authentication runs. `search_knowledge()` returns the empty-KB signal. The `knowledge_sources` indexes aren't created. `legacy-migrations instance-backfill` skips `knowledge_sources`. | Your safeguard (Phase 2a), R5 |
| M2 | **Entrypoint allowlist** is `MONGO_URL`, `JWT_SECRET`, `ENCRYPTION_KEY` only. `ADMIN_PASSWORD` is never an env var: the bootstrap reads `--password-file` directly. | Stricter R8 |
| M3 | **`manage.py` uses `allow_abbrev=False`.** Otherwise `--password <value>` was silently accepted as `--password-file`, which could put a password on the command line. | Found by tests |
| M4 | **An unavailable Emergent provider returns `503`.** | Clearer than the unspecified "clear error" |
| M5 | **Email credential safety** (`email_delivery.py`). In production: | Your security fix (Phase 2b) |
| | • no verification code, reset code or invitation link is ever printed or logged; | |
| | • missing `RESEND_API_KEY` or a provider failure raises `EmailDeliveryError`, which becomes a generic `503` (`"Email delivery is temporarily unavailable…"`), and nothing claims an email was sent; | |
| | • `register`, `resend-code`, `forgot-password` and `invite` check deliverability before any state change; | |
| | • on a send failure, `register` deletes the new unverified user, `invite` deletes the pending invitation, and `forgot-password` clears the unsent reset code. | |
| | The labelled console fallback exists only with `APP_ENV=development`. | |
| M6 | **Log redaction** (`log_redaction.py`) wraps every handler's formatter, including uvicorn's access log. It scrubs: | Credential-logging review |
| | • webhook secrets in `/telegram\|zendesk/webhook/{id}/{secret}`; | |
| | • `/invite/{token}`; | |
| | • `code`/`state`/`token`/`key`/`secret`/`password` query parameters; | |
| | • Telegram bot tokens (including in `api.telegram.org/bot<token>` URLs); | |
| | • Bearer/Basic credentials, JWTs, `user:pass@` in URIs, and common provider key prefixes. | |
| | Separately, `telegram_routes.py` no longer logs the webhook URL, which contains the secret. | |
| M7 | **The image includes `zendesk_sidebar_app/assets/`.** `zendesk_routes.py` reads them at runtime to build the sidebar-app download. | Correction to the `.dockerignore` list |
| M8 | **The Dockerfile has a `test` stage** (`--target test`) that runs `tests/unit` against the real dependencies. It's never shipped. `.dockerignore` is an allowlist. The runtime sets `HOME=/tmp` (caches go to the tmpfs) and passes uvicorn `--no-server-header`. | Testing and hardening |
| M9 | **Compose additions:** `pids_limit: 256`, `/tmp` tmpfs capped at 64 MB, `admin-cli` sets `APP_MODE=api`, and `pull_policy: never` for `admin-cli`. | Hardening |
| M10 | **New scripts:** `deploy/scripts/generate-api-secrets.sh` (never overwrites, never prints values) and `deploy/scripts/scan-image-secrets.sh` (the R8 scans, which report counts only). | Phase 2b preparation |
| M11 | **The admin bootstrap secret moves to its own override file.** `deploy/docker-compose.api.yml` defines only `api` and never references `admin_password`. `admin-cli` and the `admin_password` secret live only in `deploy/docker-compose.admin.yml` (profile `admin`, `run --rm` only). Deleting `admin_password` after the bootstrap therefore can't affect API build, start, recreation or health checks. | Your Phase 2b decision |
| M12 | **Optional Resend secret.** `deploy/docker-compose.resend.yml` mounts `resend_api_key` and sets `RESEND_API_KEY_FILE`, and the entrypoint allowlist now includes `RESEND_API_KEY`. The key is never in a compose environment value. Without the override, the API starts normally and email endpoints return a generic 503 (M5). | Your Phase 2b decision |

## Mandatory security fixes before public exposure

These were identified during Phase 2b and are **not implemented**. Each one must
be fixed and tested before the Cloudflare Tunnel or any public route is enabled.

### S1. Account enumeration in the email and authentication flows

Responses differ depending on whether an account exists or is verified:

| Endpoint | Observable difference |
|---|---|
| `POST /api/auth/register` | `400 "Email already registered"` and `400 "Username already taken"` reveal existing emails and usernames. A new account gets `200` (or `503` when email is unavailable). |
| `POST /api/auth/resend-code` | Unknown or already-verified email → `200 "If this email is registered and unverified, a new code has been sent"`. Existing unverified account → `200 "New verification code sent"`, a different message. During a provider outage, only existing unverified accounts get `503`. |
| `POST /api/auth/forgot-password` | The success message is generic. But during a provider outage, **only existing verified accounts** get `503`; everyone else gets `200`. |
| `POST /api/auth/verify` | `404 "User not found"` versus `200 "Email already verified"` versus `400`. |
| All of the above | **Timing.** Existing accounts trigger database writes, bcrypt hashing (register) and a provider call, so response times differ measurably. |

Required fix:
- Identical status codes and bodies for existing and non-existing accounts.
- Registration that doesn't reveal existing emails, e.g. "check your email" for every request, with an "already registered" notice sent by email to the existing address.
- Provider failures for existing accounts handled asynchronously (queued or retried), so they don't change the synchronous response.
- Response times equalised (constant-time path or minimum response time).
- Tests for each endpoint asserting identical responses.

### S2. Brute force of 6-digit verification and reset codes

Verification and password-reset codes are 6 digits (900,000 values), valid for 15
minutes, and **I found no attempt limit or rate limit** on `/api/auth/verify` or
`/api/auth/reset-password`. Guessing a reset code within its lifetime is feasible,
which means **account takeover**.

Required fix:
- Per-account attempt counters that invalidate the code after N failures.
- Rate limiting per IP and per account.
- Optionally, longer codes or link tokens.
- Constant-time comparison.
- Tests.

## Problem

The FastAPI backend currently runs only on Emergent.

## Problem

The FastAPI backend currently runs only on Emergent. Phase 1 created self-hosted
MongoDB 8.0 and PostgreSQL 18 + pgvector (`deploy/docker-compose.db.yml`, private
network `bitbotpro_db`, no host ports).

The backend:
- has no Dockerfile;
- can't be installed outside Emergent;
- creates a superadmin and runs legacy migrations on every boot, including
  hard-coded accounts;
- defaults CORS to `*` with credentials;
- starts Discord processes whenever credentials appear.

## Goal

Run the existing backend as a hardened, API-only container connected to the
fresh MongoDB:

- no host ports;
- no implicit account creation or legacy migration at startup;
- explicit CORS;
- no secret leakage;
- verified by internal-only smoke tests;
- with a rollback that never touches database volumes.

## Non-Goals

- PostgreSQL Knowledge Base migration or schema (spec 005). The API gets no
  PostgreSQL credentials in this phase.
- New Knowledge Base functionality in MongoDB (R5). Existing KB code and its
  existing `knowledge_sources` index creation are left unchanged. KB endpoints
  aren't used in this phase's tests (see D9).
- React frontend changes.
- Cloudflare Tunnel, DNS, TLS or any public exposure.
- Discord bot processes.
- Replacing Emergent logo storage or the default `emergent` LLM provider
  (spec 006).
- Migrating from Motor to PyMongo's async API (spec 007).
- Creating the administrator account. The bootstrap is built and tested here,
  but it runs only with separate approval.

---

## Current-State Findings

Verified against the `bitbotpro-v1.0.0` source on 2026-11-17.

### Emergent dependencies

| # | Location | Dependency | Impact |
|---|---|---|---|
| E1 | `backend/server.py:28`, `:121`; `requirements.txt` (`emergentintegrations==0.1.0`) | Imported at module load; `llm_sessions: Dict[str, LlmChat]` | **Install/startup blocker.** Not on PyPI (`pypi.org/pypi/emergentintegrations/json` → 404). |
| E2 | `server.py:76-114`; startup `:2405-2409` | Emergent object storage (bot logos) | Outbound call to `integrations.emergentagent.com` with an empty key on every boot. Logos are broken off Emergent. |
| E3 | `server.py:1162-1170`, `:1290`, `:1345`; `bot_routes.py:89,119,300` | Default LLM provider `"emergent"` | Chat fails on the default provider. Deferred to spec 006. |
| E4 | `server.py:2993-2998`, `telegram_routes.py:77-82`, `zendesk_routes.py:107-113` | `REACT_APP_BACKEND_URL` env var, with a fallback that reads a frontend `.env` file | Set explicitly. |
| E5 | `server.py:4187-4211` | The embed script builds the widget iframe URL from `request.base_url`, i.e. the **API** origin, then `/widget`. | Off Emergent, `/widget` lives on the frontend origin, so embeds break once the API and frontend are split. **Pre-public blocker** (Tunnel/frontend phase), not this phase. |

### Startup logic that creates or changes accounts and data

All of this is in `startup_event`, `server.py:2373-2544`. It runs on every boot.

| # | Location | Behaviour | Disposition |
|---|---|---|---|
| A1 | `server.py:53-54` (required env), `:2412-2430` | Seeds a verified `superadmin` from `ADMIN_EMAIL`/`ADMIN_PASSWORD` if missing. No strength check. Also patches `username` on the existing admin. | **Removed from startup (R2).** Replaced by the CLI bootstrap. `ADMIN_EMAIL`/`ADMIN_PASSWORD` no longer read by the API. |
| A2 | `server.py:2453-2458` → `migrate_workspaces.py:23-232` | Hard-coded accounts: if `admin@bridgebot.tech` exists, it creates `admin@bitbotpro.io` as superadmin with a **copied password hash** and demotes the original. It demotes two hard-coded test accounts. It copies `DISCORD_*` env vars into `platform_config`. | **Removed from startup (R1).** Explicit CLI only. |
| A3 | `server.py:2441-2450` → `migrate_v2.py:34+` | V1→V2 data migration; updates `users.org_ids`. | **Removed from startup (R1).** Explicit CLI only. |
| A4 | `server.py:2432-2438` | Backfills `instance_id` with `update_many` on 4 collections. | **Removed from startup (R1).** Explicit CLI only. |
| A5 | `server.py:2465-2479` | Backfills `metadata.source` on Discord `connected_accounts`. | **Removed from startup (R1).** Explicit CLI only. |
| A6 | `server.py:2566+` | Open self-registration (role `user`, minimum password length 6). | Not reachable this phase. **Pre-public decision.** |
| A7 | `backend/tests/` (62 files) | Live-server tests assume seeded `admin@bridgebot.tech` / `admin@bitbotpro.io`. | Must **never** run against the production database. They need a separate dev stack with its own bootstrap. |

### Discord start paths (R4)

- `start_shared_discord_bot()`: `server.py:2059`. This is the single function that
  creates `discord.Client` and calls `.start(token)` (`:2068`, `:2364`).
- Its callers are:
  - startup (`:2492`);
  - `GET /api/discord/callback`, the OAuth callback (`:6280`);
  - `POST /api/discord/restart` (`:6680-6700`).

### Other

- **F1.** `APP_MODE` isn't implemented anywhere in the current source.
- **F2.** There's no health endpoint with a database check. `GET /api/`
  (`server.py:2559`) is static.
- **F3.** Chat sessions live in process memory (`server.py:121-125`), so there
  must be **one worker**.
- **F4.** CORS uses `os.environ.get('CORS_ORIGINS','*')` with
  `allow_credentials=True` (`server.py:7000-7006`).
- **F5.** `ENCRYPTION_KEY` (Fernet) is read lazily (`api_integrations.py:26-33`).
- **F6.** Drivers are `motor==3.3.1`/`pymongo==4.5.0`. MongoDB 8.0 needs
  PyMongo ≥4.9. Motor 3.7.1 requires `pymongo>=4.9,<5`. Motor is deprecated
  upstream.
- **F7.** There's no Dockerfile or `.dockerignore`. `load_dotenv(ROOT_DIR/'.env')`
  is harmless when no `.env` is present.
- **F8.** uvicorn 0.25.0 supports `--timeout-graceful-shutdown` (verified in its
  source).

---

## Architecture

```
                        VM (Ubuntu 24.04, Docker 29)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  project "bitbotpro-api"   (deploy/docker-compose.api.yml)           │
 │   ┌───────────────────────────┐   ┌─────────────────────────────┐    │
 │   │ api  (long-running)       │   │ admin-cli (profile "admin", │    │
 │   │ uvicorn :8000, 1 worker   │   │ one-off `run --rm`; only    │    │
 │   │ APP_MODE=api, non-root    │   │ service given admin_password)│   │
 │   └───────┬──────────┬────────┘   └──────────────┬──────────────┘    │
 │   bitbotpro_egress   │ bitbotpro_db (external)   │ bitbotpro_db only │──▶ Internet
 │   (outbound only)    │                           │                   │    (from api only)
 │  project "bitbotpro" (deploy/docker-compose.db.yml — unchanged)      │
 │   ┌──────────────────▼─┐   ┌──────────────────────┐                  │
 │   │ bitbotpro-mongo    │   │ bitbotpro-postgres   │ untouched (R7)   │
 │   └────────────────────┘   └──────────────────────┘                  │
 └──────────────────────────────────────────────────────────────────────┘
   No `ports:` in either project (R6). Tests run via `docker exec` inside the container.
```

- **A separate compose project** (D7) means API `up`/`down`/rebuild/rollback can't
  affect the DB project's containers or volumes (R7). `bitbotpro_db` is referenced
  as `external: true`. `deploy/docker-compose.db.yml` isn't modified.
- **`bitbotpro_egress`** is a normal user-defined bridge network for outbound
  calls only. Nothing is published.
- **`admin-cli` uses the same image and isn't started by `up`.** It joins only
  `bitbotpro_db`, with no internet access.

## Application code changes

All of these require the approval already given (D1 + R1–R8). Each change keeps
today's Emergent behaviour unless noted.

| # | File | Change |
|---|---|---|
| C1 | `server.py:28`, `:121`; `requirements.txt` | Lazy-import `emergentintegrations` inside `_call_emergent` (`:1172`), and change the annotation to `Dict[str, Any]`. If the package is missing, it raises a clear "Emergent provider unavailable" error. Remove the package from `requirements.txt` (D5). |
| C2 | `server.py` | **`APP_MODE` (R4).** `APP_MODE = os.environ.get("APP_MODE", "all")`; anything other than `all`/`api` exits at import. In `api` mode: |
| | | (a) **guard inside `start_shared_discord_bot()`**: it logs and returns without creating a client. This is the single choke point, so any current or future caller is covered; |
| | | (b) startup skips the Discord section (credential lookup, guild map, task creation); |
| | | (c) `GET /api/discord/callback` still saves the OAuth result but doesn't start the bot; |
| | | (d) `POST /api/discord/restart` returns `409 {"detail":"Discord bot disabled (APP_MODE=api)"}`. |
| C3 | `server.py:2405-2409` | Call `_init_storage()` at startup only if `CLAUDE_API_KEY` or `EMERGENT_LLM_KEY` is set. |
| C4 | `server.py` | **`GET /api/health` (D6).** Unauthenticated. Runs `await db.command("ping")` with a 2 s timeout. Returns `200 {"status":"ok"}` or `503 {"status":"unavailable"}`, with no versions, hostnames or error text. |
| C5 | `requirements.txt` | `motor==3.7.1`, `pymongo==4.10.1` (D8). |
| C6 | `backend/manage.py` (new) | **Strong-password policy** for the bootstrap: |
| | | ≥ 16 characters; |
| | | not in a denylist (`ChangeMe123!`, `password`, `admin`, `bitbotpro`, …), case-insensitive; |
| | | doesn't contain the email's local part or domain label; |
| | | at least 3 of 4 character classes, or ≥ 24 characters. |
| | | Failures are rejected with a reason, and the password is never echoed. |
| C7 | `server.py` | **Startup reduced to non-mutating initialisation (R1, R2):** |
| | | *Remove* from `startup_event`: the admin seed and username patch (A1); the `instance_id` backfill (A4); the V2 migration (A3); the workspace migration (A2); the Discord metadata backfill (A5). |
| | | *Remove* the import-time `os.environ["ADMIN_EMAIL"]`/`["ADMIN_PASSWORD"]` (`:53-54`). |
| | | *Keep:* index creation (idempotent schema, unchanged list), `_init_diagnostics`/`_init_telegram`/`_init_zendesk`, `bot_routes.init_llm_config`. |
| | | *Add:* one startup log line when the database has **zero superadmins** ("run the bootstrap CLI"). Nothing is created. |
| C8 | `server.py:7000-7006` | **CORS (R3).** |
| | | `CORS_ORIGINS` is **required**, with no default. It's a comma-separated list of exact origins. |
| | | Each origin must match `^https://[a-z0-9.-]+(:\d+)?$`. `*`, paths, trailing slashes, `null` and empty entries are rejected, and the process exits at startup. |
| | | `http://localhost[:port]` / `http://127.0.0.1[:port]` are accepted **only** when `APP_ENV=development`. `APP_ENV` defaults to `production`, and any other value exits. |
| | | No `allow_origin_regex`. `allow_credentials=True` is kept (cookies). |
| | | Production value: `CORS_ORIGINS=https://bitbotpro.io`. Development origins live only in the git-ignored dev `backend/.env` (`APP_ENV=development`, `CORS_ORIGINS=http://localhost:3000`), never in the production compose. |
| C9 | `backend/docker-entrypoint.sh` (new) | **Secret loader (D3, R8).** For an explicit allowlist (`MONGO_URL`, `JWT_SECRET`, `ENCRYPTION_KEY`, `ADMIN_PASSWORD`), if `<NAME>_FILE` is set it reads the file into `<NAME>` in the process environment, then `exec "$@"`. |
| | | No `set -x`, no echo of values, and it fails with the variable *name* only. |
| | | Values exist only in the container process memory, not in image config or `docker inspect`. |

## Admin CLI (`backend/manage.py`)

This is a new, standalone module. It's never imported by `server.py` and never
run at startup. It reuses `MONGO_URL`/`DB_NAME` and the same bcrypt hashing as
`server.py`.

```
python manage.py bootstrap-superadmin --email <addr> --password-file <path> --confirm
python manage.py legacy-migrations [--only instance-backfill|v2|workspaces|discord-metadata] [--apply --confirm]
```

### `bootstrap-superadmin` (R2, C6)

- **One-time:** it refuses if *any* user has `role == "superadmin"` or
  `platform_role == "superadmin"`, or if the email already exists. Running it
  twice can never create a second superadmin or overwrite a password.
- **Explicit:** it requires `--confirm`. Without it, it prints a dry run (target
  email, "would create superadmin", password-policy result) and exits non-zero.
- **Validation:** the email is validated, and the password policy is C6.
- **The password is read only from `--password-file`.** No CLI argument or env
  var holds it, so it's never in the process list or shell history.
- **Creates exactly one user,** with the same document shape as the old seed
  (`role` and `platform_role` both `superadmin`, `is_verified: True`).
- **Audit:** writes `{action:"bootstrap_superadmin", email, at, via:"cli"}` to an
  `audit_log` collection (application/auth data, R5).
- **Output:** the email and the outcome only.
- **Run** (separate approval required, **not part of this rollout**):
  ```
  docker compose -p bitbotpro-api -f deploy/docker-compose.api.yml --env-file deploy/.env \
    --profile admin run --rm admin-cli \
    bootstrap-superadmin --email admin@bitbotpro.io --password-file /run/secrets/admin_password --confirm
  ```
- **Afterwards:** log in and change the password in the UI. Then delete
  `~/.bitbotpro/secrets/admin_password`, which only the `admin-cli` service
  mounts.

### `legacy-migrations` (R1)

- **Dry run by default:** reports the counts each migration would touch.
- **`--apply --confirm`** runs A4/A3/A2/A5 using the existing functions unchanged.
- **`workspaces`** prints a warning about its hard-coded accounts and needs an
  extra `--allow-hardcoded-accounts` flag.
- **Not needed on the fresh database.** It exists so these migrations are only
  ever an explicit admin action.

## Dockerfile (`backend/Dockerfile`)

- **Base image:** `python:3.11-slim-bookworm`, pinned to an exact patch tag at
  implementation time (D4).
- **Multi-stage build:**
  - *Builder:* venv at `/opt/venv`, `pip install --no-cache-dir -r requirements.txt`,
    and `pip check`.
  - *Runtime:* copies only the venv plus application source.
- **Non-root user** `app` (10001:10001). Source is owned by root and read-only to
  the app.
- **`python -m compileall`** at build time.
- **Environment:** `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`.
- **No secrets in the image (R8):**
  - no `ARG`/`ENV` holding secrets;
  - no `COPY` of `.env` or secrets;
  - the build context is `backend/` only, and the secrets live outside the repo.
- **Entrypoint:** `ENTRYPOINT ["/app/docker-entrypoint.sh"]`.
- **`CMD`:** `["uvicorn","server:app","--host","0.0.0.0","--port","8000","--workers","1","--timeout-graceful-shutdown","25"]`.
- **`EXPOSE 8000`** (documentation only). `--proxy-headers` is added in the
  Tunnel phase.
- **`backend/.dockerignore`:** `.env`, `.env.*`, `*.env`, `tests/`,
  `__pycache__/`, `*.pyc`, `.pytest_cache/`, `zendesk_sidebar_app/`, venvs,
  `*.pem`, `*.key`.

## Compose service (`deploy/docker-compose.api.yml`)

```yaml
name: bitbotpro-api
networks:
  db:     { name: bitbotpro_db, external: true }
  egress: { name: bitbotpro_egress, driver: bridge }
secrets:   # files in $BITBOTPRO_SECRETS_DIR (Phase 1 dir, 0700)
  backend_mongo_url: { file: ${BITBOTPRO_SECRETS_DIR:?}/backend_mongo_url }
  jwt_secret:        { file: ${BITBOTPRO_SECRETS_DIR:?}/jwt_secret }
  encryption_key:    { file: ${BITBOTPRO_SECRETS_DIR:?}/encryption_key }
  admin_password:    { file: ${BITBOTPRO_SECRETS_DIR:?}/admin_password }
x-hardening: &hardening
  user: "10001:10001"
  read_only: true
  tmpfs: [ /tmp ]
  cap_drop: [ ALL ]
  security_opt: [ "no-new-privileges:true" ]
  init: true
  logging: { driver: json-file, options: { max-size: "10m", max-file: "5" } }
services:
  api:
    <<: *hardening
    build: { context: ../backend }
    image: bitbotpro-api:${BITBOTPRO_API_TAG:?}      # git short SHA
    container_name: bitbotpro-api
    restart: unless-stopped
    networks: [ db, egress ]
    # NO `ports:` (R6)
    environment:
      APP_MODE: api
      APP_ENV: production
      DB_NAME: bitbotpro
      CORS_ORIGINS: https://bitbotpro.io
      REACT_APP_BACKEND_URL: https://api.bitbotpro.io
      FROM_EMAIL: noreply@bitbotpro.io
      MONGO_URL_FILE: /run/secrets/backend_mongo_url
      JWT_SECRET_FILE: /run/secrets/jwt_secret
      ENCRYPTION_KEY_FILE: /run/secrets/encryption_key
    secrets: [ backend_mongo_url, jwt_secret, encryption_key ]   # NOT admin_password
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 60s
    stop_grace_period: 30s
    mem_limit: 2g
  admin-cli:
    <<: *hardening
    image: bitbotpro-api:${BITBOTPRO_API_TAG:?}
    profiles: [ admin ]                   # never started by `up`
    networks: [ db ]                      # no internet
    entrypoint: ["/app/docker-entrypoint.sh", "python", "manage.py"]
    environment:
      APP_ENV: production
      DB_NAME: bitbotpro
      MONGO_URL_FILE: /run/secrets/backend_mongo_url
    secrets: [ backend_mongo_url, admin_password ]
```

The following env vars are **deliberately not set**: `ADMIN_EMAIL`,
`ADMIN_PASSWORD`, `DISCORD_*`, `CLAUDE_API_KEY`, `EMERGENT_LLM_KEY`,
`INTEGRATION_PROXY_URL`, `KB_*`, and any PostgreSQL URL.

`read_only: true` is kept if the tests pass. If a write path turns up, it gets a
dedicated, documented `tmpfs`.

## Configuration and secrets

- **New files** in `~/.bitbotpro/secrets/` are generated by a script that:
  - never overwrites an existing file;
  - never prints values;
  - creates no file inside the repo.

  Modes match Phase 1: directory 0700, files 0444, because uid 10001 must read
  the bind mounts. The host is still protected by the 0700 directory and the 0750
  home directory.

  | File | Contents |
  |---|---|
  | `backend_mongo_url` | The `bitbotpro_app` URI, the same as `MONGO_URL` in `~/.bitbotpro/backend.db.env`. |
  | `jwt_secret` | `openssl rand -hex 32` |
  | `encryption_key` | A Fernet key. |
  | `admin_password` | Generated (≥ 32 characters, passes C6). Used only by the bootstrap, and deleted after the first password change. |

- **`deploy/.env`** (git-ignored, no secrets) gets `BITBOTPRO_API_TAG`.
  `deploy/.env.example` gets placeholders.
- **`backend/.env.example`** is rewritten for development: `APP_ENV=development`,
  a localhost `CORS_ORIGINS`, and the bootstrap CLI instead of
  `ADMIN_EMAIL`/`ADMIN_PASSWORD`.

## Secret hygiene (R8)

| Surface | Control | Verification (counts only; values never printed) |
|---|---|---|
| Build context and logs | `.dockerignore`; secrets live outside the build context; no secret `ARG`/`ENV`; `--progress=plain` build log saved | `grep -cF -f <secret files>` on the build log = 0 |
| Image layers and config | Nothing secret is copied or set | `docker history --no-trunc` and `docker image inspect`, grepped for secret values = 0; `docker save` + grep over the layer tarballs = 0 |
| `docker inspect` (container) | Env holds only `*_FILE` paths; secrets are file mounts (paths only) | `docker inspect bitbotpro-api` grepped for secret values = 0 |
| `docker compose config` | Resolves to file paths only | Grep = 0 |
| Application and container logs | No URI or secret logging; entrypoint is silent; the CLI prints email and outcome only | `docker logs` grepped for secret values = 0 |

Not in scope: anyone who can run `docker exec` (effectively root) can read
`/proc/1/environ` inside the container. That's inherent and acceptable.

## Health, startup and shutdown

- **Pre-flight:** the runbook checks both DB containers are `healthy` before `up`
  (there's no cross-project `depends_on`).
- **Startup:**
  - Validate `APP_MODE`, `APP_ENV` and `CORS_ORIGINS`; exit fast on error.
  - Motor connects lazily, then indexes are created.
  - If Mongo authentication fails, `/api/health` returns 503, the container is
    marked `unhealthy`, and the logs say so.
  - No accounts or data are mutated (C7).
- **Healthcheck:** `/api/health` uses the app's own Mongo client, so "healthy"
  means serving *and* authenticated to the database.
- **Shutdown:** SIGTERM goes through `init` to uvicorn, with a graceful timeout of
  25 s, inside the 30 s `stop_grace_period`. The existing `shutdown_event` closes
  the Mongo client. No Discord client exists.
- **One worker** (F3).

## Validation

These are read-only checks, packaged as a script you review before running.

1. **Container:** `docker inspect bitbotpro-api` shows:
   - `healthy`, `RestartCount=0`;
   - `PortBindings={}` and `PublishAllPorts=false`;
   - networks `bitbotpro_db` and `bitbotpro_egress`;
   - user `10001:10001`, `ReadonlyRootfs=true`, `CapDrop=[ALL]`.
2. **Host:** `ss -ltn` shows nothing on 8000, 27017, 5432 or 5433 (R6).
3. **Logs:**
   - no tracebacks;
   - **no** Discord start line;
   - no `[WS-MIGRATE]`, V2-migration or `Seeded admin` lines (R1, R2);
   - one "no superadmin — run bootstrap" notice.
4. **MongoDB** (`mongosh` as root inside `bitbotpro-mongo`, read-only):
   - `bitbotpro` now exists with indexes;
   - **`users` count = 0**, so no account was created (D2, R2);
   - `platform_config`, `discord_app_config` and `organisations` are empty;
   - the client connected as `bitbotpro_app@bitbotpro`.
5. **PostgreSQL (R7):** volume `bitbotpro_postgres_data` unchanged, the
   container's `StartedAt` unchanged, and no new connections from the API
   network.
6. **Secret hygiene:** every check in the R8 table = 0.

## Internal-only API smoke tests

These run via `docker exec bitbotpro-api python …`, never from the host network.
Tokens and passwords are never printed.

### Phase 2a: this rollout, no administrator exists

| Test | Expected |
|---|---|
| `GET /api/` | 200 |
| `GET /api/health` | 200 `{"status":"ok"}` |
| `GET /api/auth/me` with no token | 401 |
| `POST /api/auth/login`, non-existent user | 401 |
| CORS preflight, `Origin: https://bitbotpro.io` | `access-control-allow-origin: https://bitbotpro.io`, credentials true |
| CORS preflight, `Origin: https://evil.example` / `http://bitbotpro.io` / `https://www.bitbotpro.io` | No allow-origin header |
| `POST /api/discord/restart` (an admin route) | 401 without auth. The 409 path is unit-tested via C2. |
| Negative config: start a throwaway container with `CORS_ORIGINS=*` / `APP_MODE=bogus` / `APP_ENV=production` + an `http://localhost` origin | Each one exits non-zero with a clear message. Runs with `--rm`, on the `bitbotpro_db` network only. |
| Admin CLI: `bootstrap-superadmin` **without** `--confirm`, plus a weak-password dry run | Dry-run output; policy failure reported; `users` stays 0 |
| Egress: DNS and TLS connection to `api.anthropic.com:443` from `api` | Succeeds (no API call) |

### Phase 2b: only after separate bootstrap approval

| Test | Expected |
|---|---|
| Bootstrap with `--confirm` | Exactly 1 superadmin, `admin@bitbotpro.io`, plus an `audit_log` entry |
| A second bootstrap | Refused; `users` still 1 |
| `POST /api/auth/login` (admin) → `GET /api/auth/me` | 200, role `superadmin` |
| Wrong password | 401 |

**Not tested:** registration, KB endpoints, chat/LLM, Discord, Telegram, Zendesk,
logos. The existing `backend/tests/` suite is never run against this database
(A7).

## Rollback

Rollback never deletes or modifies database volumes (R7).

1. **Stop the API:**
   `docker compose -p bitbotpro-api -f deploy/docker-compose.api.yml --env-file deploy/.env down`.
   - No `-v`. The project has no volumes anyway.
   - Never use `--remove-orphans` with the `bitbotpro` project.
   - The DB containers keep running.
2. **Optional:** `docker image rm bitbotpro-api:<tag>`, and
   `docker network rm bitbotpro_egress`.
3. **Code:** revert the uncommitted Phase 2 changes on `bitbotpro-v1.0.0` only.
4. **MongoDB data:** Phase 2a leaves only indexes and empty collections in
   `bitbotpro`. These are left in place by default. Removing them is a separate,
   explicitly approved `dropDatabase()` on `bitbotpro`, never a volume deletion.
   PostgreSQL is never touched.
5. **Secrets:** the new files stay put. They're inert without the container.

## Implementation Steps (each gated by your approval)

1. **Code:**
   - C1–C9 plus `manage.py`;
   - `py_compile`;
   - offline unit checks for the CORS validator, `APP_MODE` gating and the
     password policy (pure functions, no database needed).
2. **Container files:**
   - add `backend/Dockerfile`, `.dockerignore`, `docker-entrypoint.sh`;
   - add `deploy/docker-compose.api.yml`;
   - update `deploy/.env.example` and `backend/.env.example`.
3. **Config:**
   - generate secrets (no values printed);
   - set `BITBOTPRO_API_TAG`;
   - run `docker compose config` validation.
4. **Build** (you run it with sudo): save the plain build log. Then check the
   image: non-root user, `pip check`, an import smoke test, and the R8 scans.
5. **Approval gate:** start `api`.
6. **Validation and Phase 2a tests** (script shown before running).
7. Record results in `test_result.md` (format unchanged, per CLAUDE.md).
8. **Separate approval gate:** bootstrap `admin@bitbotpro.io`, then Phase 2b.
9. Commit only if you ask.

## Risks

| Risk | Mitigation |
|---|---|
| Dependency resolution fails on 3.11 (about 140 pins) | `pip check` at build time. If it fails, report back; no silent pin changes. |
| Motor 3.7.1 / PyMongo 4.10.1 behaviour changes (e.g. `bool(Database)` raises, changed defaults) | Grep for truthiness checks on db/collection objects; health check plus smoke tests. |
| Removing startup migrations changes Emergent behaviour if this code is redeployed there | Intended (R1). Emergent runs the CLI explicitly if it ever needs them. |
| Removing the seed breaks dev/test setups that rely on it (A7) | `backend/.env.example` plus docs point to the bootstrap CLI. Tests run only against dev stacks. |
| The `api` container could start other in-process background work later | C2 covers Discord at the single choke point. Telegram and Zendesk are webhook-driven only (no polling). |
| `read_only` rootfs breaks a write path | Found in the tests; targeted `tmpfs`. |
| Motor is deprecated | Spec 007. |
| Secrets readable by uid 10001 in the container | Expected. Only the needed secrets are mounted per service; `admin_password` is mounted only in `admin-cli`. |

## Decisions Still Open

- **D9. Existing MongoDB KB endpoints (R5).** They still write to
  `knowledge_sources` if called. They're unreachable this phase (no public
  access).
  - Recommendation: before the Tunnel phase, either switch them off in
    production (`503` while `KB_BACKEND` isn't `postgres`), or land spec 005
    first.
- **D10. `https://www.bitbotpro.io`.** It's excluded per R3 as written. Add it
  only if the frontend is actually served there.

## Pre-Public Blockers (not this phase)

- **S1 (mandatory security fix):** account enumeration in the email and auth
  flows. See [Mandatory security fixes](#mandatory-security-fixes-before-public-exposure).
- **S2 (mandatory security fix):** brute force of 6-digit verification and reset
  codes.
- **E5:** the embed widget iframe URL is derived from the API origin.
- **E3:** the default LLM provider is Emergent, so chat fails without it.
- **E2:** logo storage depends on Emergent.
- **A6:** open self-registration policy.
- **D9:** the KB endpoints are disabled in `APP_MODE=api` (M1). They need the
  PostgreSQL implementation (spec 005) before they can be re-enabled.
- Proxy headers and the Cloudflare Tunnel.
- Zendesk sidebar app origin: whether it calls the API from the browser (CORS)
  or through Zendesk's proxy. Needs checking.

## Files Changed

As implemented: see the list in `implementation.md`. The original plan follows.

- `backend/server.py` (C1–C4, C7, C8)
- `backend/manage.py` (new: C6, the bootstrap, legacy migrations)
- `backend/requirements.txt` (C1, C5)
- `backend/Dockerfile`, `backend/.dockerignore`, `backend/docker-entrypoint.sh`
  (new: C9)
- `backend/.env.example`
- `deploy/docker-compose.api.yml` (new)
- `deploy/.env.example`, `deploy/.env` (git-ignored)
- `~/.bitbotpro/secrets/*` (outside the repo)
- `test_result.md` (results only)
- Unchanged: `deploy/docker-compose.db.yml`, `docker-compose.yml`, `frontend/`,
  the PostgreSQL configuration, and all database volumes.

## Follow-up Specs

- **005:** PostgreSQL/pgvector Knowledge Base with row-level security per tenant.
  This resolves D9.
- **006:** Replace the Emergent LLM default and logo storage (E2, E3).
- **007:** Move from Motor to PyMongo's async API, and trim the dependencies.
- **Pre-Tunnel:** E5, A6, proxy headers, Cloudflare Tunnel.
