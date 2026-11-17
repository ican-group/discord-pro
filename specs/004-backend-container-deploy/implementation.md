# Spec 004 — Implementation Notes and Runbook

Companion to `spec.md`. Status as of 2026-11-17 (updated with M11/M12, S1/S2):

- **Phase 2a:** code done.
- **Phase 2b:** files prepared. **Nothing is built, started, generated or committed.**

Every step marked **[GATE]** needs explicit approval before it runs.

## What changed

### Application code (`backend/`)

| File | Change |
|---|---|
| `server.py` | **C1:** lazy `emergentintegrations` import. |
| | **C2:** `APP_MODE`, with the Discord choke point in `start_shared_discord_bot()`, plus startup, OAuth-callback and restart guards. |
| | **C3:** Emergent storage is initialised only when an Emergent key is set. |
| | **C4:** `GET /api/health`. |
| | **C7:** startup does no seeding and runs no migrations; a read-only notice is logged when no superadmin exists. |
| | **C8:** strict CORS. |
| | **M1:** Knowledge Base 503 middleware and the `search_knowledge` guard. |
| | **M5:** email flows fail safe. |
| | **M6:** log redaction is installed. |
| `runtime_config.py` (new) | Validation for `APP_MODE`, `APP_ENV`, CORS and KB paths (stdlib only). |
| `password_policy.py` (new) | Bootstrap password policy (C6). |
| `manage.py` (new) | `bootstrap-superadmin` (one-time, `--confirm`, password file only, audit log) and `legacy-migrations` (dry run by default). |
| `email_delivery.py` (new) | Fail-safe email sending. Development-only console fallback. |
| `log_redaction.py` (new) | Redacting log formatter (M6). |
| `docker-entrypoint.sh` (new) | `*_FILE` secret loader (C9/M2). |
| `telegram_routes.py` | The webhook URL (which contains the secret) is no longer logged. |
| `requirements.txt` | `emergentintegrations` removed; `motor==3.7.1`, `pymongo==4.10.1`. |
| `Dockerfile`, `.dockerignore` (new) | Multi-stage, non-root, allowlist context (M7, M8). |
| `.env.example` | Rewritten for local development. |
| `tests/unit/` (new) | Offline tests (see below). |

### Deployment (`deploy/`)

| File | Purpose |
|---|---|
| `docker-compose.api.yml` (new) | Project `bitbotpro-api`. **Only** the `api` service: networks `bitbotpro_db` + `bitbotpro_egress`, no ports. Never references `admin_password`. |
| `docker-compose.admin.yml` (new) | Override for one-off admin commands: `admin-cli` (profile `admin`, `bitbotpro_db` only, `run --rm`). The only file that declares `admin_password`. |
| `docker-compose.resend.yml` (new) | Optional override: mounts the `resend_api_key` secret and sets `RESEND_API_KEY_FILE`. |
| `.env.example` | Adds `BITBOTPRO_API_TAG`. |
| `scripts/generate-api-secrets.sh` (new) | Creates `backend_mongo_url`, `jwt_secret`, `encryption_key` and `admin_password` in `$BITBOTPRO_SECRETS_DIR`. Never overwrites a file and never prints a value. |
| `scripts/scan-image-secrets.sh` (new) | R8 scans of the build log, image history and inspect output, `docker save` layers, `compose config`, and the container's inspect output and logs. Reports counts only. |

`docker-compose.db.yml`, the root `docker-compose.yml`, `frontend/` and all
database volumes are **unchanged**.

## Offline tests

Run from `backend/`:

```
python3 -m unittest discover -s tests/unit -t . -v
```

| File | Covers |
|---|---|
| `test_runtime_config.py` | `APP_MODE`/`APP_ENV`, more than 30 CORS cases, KB path matching. |
| `test_password_policy.py` | C6. |
| `test_manage.py` | Bootstrap: dry run, one-time, refusals, no password or hash in output, no `--password` abbreviation. Legacy migrations: dry run by default, `--confirm`, the hard-coded-accounts flag, and no KB writes in `api` mode. |
| `test_entrypoint.py` | Secret loading and error handling; values are never echoed. |
| `test_email_delivery.py` | Production: missing key, blank key and provider failure all raise a generic error, nothing is printed, and logs contain no credential and only the exception type. Development: labelled fallback. |
| `test_log_redaction.py` | Every redaction pattern, including uvicorn-style access records and tracebacks. |
| `test_server_static.py` | AST guards: no Emergent import at module level; startup is free of seeds and migrations; the Discord choke point and its three known callers; no wildcard CORS; every MongoDB KB access is behind the guard; no `print()`; no secret variable is interpolated into a log call; email flows check deliverability before writing and roll back on failure. |
| `test_compose_files.py` | Parses the compose files: the base file has only `api` and no `admin_password`; no ports anywhere; CORS is exactly `https://bitbotpro.io`; no raw secret env vars (only `/run/secrets` paths); no PostgreSQL settings for the API; `admin-cli` has the same hardening as `api`; the Resend override is minimal. Runs on the host only; skips in the Docker test stage. |
| `test_server_app.py` | In-process FastAPI tests: 503 on 23 KB requests, CORS headers on that 503, health, runtime CORS, the Discord 409, production email 503 with no writes, register rollback, and invalid config failing at import. These **skip** when FastAPI and Motor aren't installed; they run in the Docker `test` stage. |

## Runbook (Phase 2b onward)

> For the first public launch use `specs/008-pre-public-hardening/launch-runbook.md`,
> which supersedes the steps below where they differ.

All commands run from the repo root. Docker commands need `sudo`, because your
user isn't in the `docker` group.

1. **[GATE] Generate secrets** (as your normal user, no sudo). Since spec 008
   Stage 3 this also creates `code_hmac_key`; existing files are never
   overwritten, so re-running it only adds missing secrets:
   ```
   bash deploy/scripts/generate-api-secrets.sh
   ```
   Then set a tag in `deploy/.env`, for example `BITBOTPRO_API_TAG=<git short SHA or phase2b-YYYYMMDD>`.
2. **Validate the compose file:**
   ```
   docker compose -f deploy/docker-compose.api.yml --env-file deploy/.env config --quiet
   ```
3. **[GATE] Build and test** (saves plain build logs for the R8 scan):
   ```
   sudo docker build --progress=plain --target test -t bitbotpro-api-test backend > ~/.bitbotpro/build-test.log 2>&1
   sudo docker compose -f deploy/docker-compose.api.yml --env-file deploy/.env build --progress=plain api > ~/.bitbotpro/build-runtime.log 2>&1
   ```
   Output goes only to the log file (nothing is printed); a non-zero exit status
   means a step failed, and the log shows which. The `test` stage must finish with
   **0 failures and 0 errors**. Only host-only tests may be skipped (85 since
   spec 008 A29: 12 compose, 7 Caddy/WireGuard edge, 5 tunnel, 2 Dockerfile tests in
   `test_compose_files.py`, 57 script tests in `test_wireguard_scripts.py`, 1 frontend-parity test), because `deploy/`,
   `frontend/` and the Dockerfile are outside
   the build context. Every other test, including all FastAPI/Motor tests in
   `test_server_app.py`, must run. Also confirm in the log that `pip check`
   reported "No broken requirements found."
4. **Check the image:**
   ```
   sudo bash deploy/scripts/scan-image-secrets.sh bitbotpro-api:<tag> /home/ntfreem/.bitbotpro/secrets ~/.bitbotpro/build-runtime.log
   ```
   Also confirm the image runs as user `10001` and that `pip check` passed in the
   build log.
5. **[GATE] Start the API.** First confirm both DB containers are `healthy`, then:
   ```
   sudo docker compose -f deploy/docker-compose.api.yml --env-file deploy/.env up -d api
   ```
6. **Validation and Phase 2a smoke tests:** a read-only script, shown to you
   before it runs. See `spec.md` "Validation" and "Phase 2a". MongoDB must show
   `users = 0` and no `knowledge_sources` collection.
7. **[GATE, separate approval] Bootstrap the superadmin.** This is the only
   command that uses the admin override:
   ```
   sudo docker compose -f deploy/docker-compose.api.yml -f deploy/docker-compose.admin.yml \
     --env-file deploy/.env --profile admin run --rm admin-cli \
     bootstrap-superadmin --email admin@bitbotpro.io --password-file /run/secrets/admin_password            # dry run
   # add --confirm only after review
   ```
   Then:
   - run the Phase 2b login tests;
   - change the password in the UI;
   - delete `~/.bitbotpro/secrets/admin_password`.

   Normal API commands never include `docker-compose.admin.yml`, so deleting the
   file doesn't affect them.
8. **[GATE] Security indexes (spec 008, R-RL.7).** Dry run, then apply:
   ```
   sudo docker compose -f deploy/docker-compose.api.yml -f deploy/docker-compose.admin.yml \
     --env-file deploy/.env --profile admin run --rm admin-cli security-indexes            # dry run
   # then: ... admin-cli security-indexes --apply --confirm
   ```
   Creates only the TTL indexes on `security_counters.expires_at` and
   `oauth_states.expires_at`.
9. **After deployment (spec 008 Stage 7):** re-save each Telegram bot once so its
   webhook is registered with `secret_token`; for each Zendesk webhook, copy the
   signing secret from Zendesk and store it with
   `PUT /api/admin/zendesk/webhook-signing-secret`.

### Resend API key (optional; configure when email is needed)

Until this is done, email-dependent endpoints (register, resend-code,
forgot-password, invite) return a generic 503. The API itself starts normally.

1. Create the secret file without the key ever appearing in shell history, the
   process list or the terminal. Run as your normal user:
   ```
   umask 077
   read -rs -p "Resend API key: " RESEND_KEY; echo
   printf '%s\n' "$RESEND_KEY" > ~/.bitbotpro/secrets/resend_api_key
   unset RESEND_KEY
   chmod 0444 ~/.bitbotpro/secrets/resend_api_key   # container uid 10001 must read it; the dir stays 0700
   ```
   `read -s` doesn't echo, and `printf` is a shell builtin, so the key never
   appears in a process's arguments.
2. Recreate the API with the override (also include it in later `up` commands):
   ```
   sudo docker compose -f deploy/docker-compose.api.yml -f deploy/docker-compose.edge-caddy.yml \
     -f deploy/docker-compose.resend.yml --env-file deploy/.env up -d --no-deps --no-build api
   ```
3. Verify: run the R8 scan (`scan-image-secrets.sh`, which also checks this
   override). Check `docker inspect bitbotpro-api` shows only
   `RESEND_API_KEY_FILE=/run/secrets/resend_api_key`.
4. Set `FROM_EMAIL` to an address on a domain verified in Resend (currently
   `noreply@bitbotpro.io`).
5. **Rotate** by overwriting the file (`chmod u+w` first) and recreating `api`.
   **Remove** by deleting the file and dropping the override from the `up`
   command.

### Rollback (never touches database volumes)

```
sudo docker compose -f deploy/docker-compose.api.yml --env-file deploy/.env down     # never -v, never --remove-orphans
sudo docker image rm bitbotpro-api:<tag>                                             # optional
sudo docker network rm bitbotpro_egress                                              # optional
```

Code changes are uncommitted on `bitbotpro-v1.0.0`, so they can be reverted
there. The database containers keep running throughout.

## Known issues and open items

- **Resolved (M11):** `admin_password` is declared only in
  `docker-compose.admin.yml`. The API's compose configuration validates and runs
  without that file.
- **Mandatory before public exposure (spec S1):** account enumeration through
  register, resend-code, forgot-password and verify messages, provider-outage
  503s and timing. Documented, not fixed in this phase.
- **Mandatory before public exposure (spec S2):** 6-digit verification and reset
  codes have no attempt or rate limit, which allows brute force and account
  takeover. Documented, not fixed in this phase.
- **Deferred (legacy CLI only): `migrate_v2.migrate()` client cleanup.** The
  early return at `migrate_v2.py:57` ("No superadmin user found") skips
  `client.close()`. It only affects a one-off `admin-cli` run of
  `legacy-migrations --only v2`, which exits immediately, so nothing stays open
  in production. Fix later with `try/finally`. (Recorded 2026-11-17 while fixing
  the test-suite `ResourceWarning`; that fix was test-only.)
- **Access-log redaction** covers known secret-bearing paths and parameters.
  New secret-in-URL routes must be added to `log_redaction.py`.
  `test_log_redaction.py` documents the current set.
- **The invite endpoint still returns the invitation `token` in its JSON
  response** to the authenticated admin, as before. That's by design (manual
  sharing), and it isn't logged.
- **Other pre-public blockers** (see `spec.md`): the embed widget's iframe URL
  (E5), the Emergent default LLM and logo storage (E2/E3), open registration
  (A6), and whether the Zendesk sidebar calls the API from the browser (CORS).
