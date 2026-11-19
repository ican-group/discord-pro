# Private beta — activation plan (PREPARED IN PHASE 6C; NOT EXECUTED)

> Phase 6D: the concrete manifests are in `deploy/proposed/beta/` and the operator checklist is
> `beta-activation-checklist.md` (it supersedes the A5/A6 details below where they differ: separate
> beta compose project and egress network, `TRUSTED_PROXIES`, `maxPoolSize=20`, static web container).

Shortest safe path from the Phase 6C release candidate to the first invited tester, **without
interrupting the existing production API** (`api.bitbotpro.io`, image
`bitbotpro-api:spec011-f245d7ed3bdb`, database `bitbotpro`). The beta runs **beside** it on new
hostnames and the new isolated database (decision 6C-D). The later switch of `api.bitbotpro.io`
itself is `cutover-plan.md` (Phase 7) and is not needed for the beta.

## What requires downtime

| Change | Downtime |
|---|---|
| Beta API + worker containers, new database, new hostnames | **None** for the legacy service (separate containers, ports, database and site blocks) |
| MongoDB open-file limit (`operations.md` §2) | A MongoDB restart (5–30 s) for **both** legacy and beta. **Deferred** for the private beta: the rehearsal shows a 409-connection cap at 1024, far above beta load (API + worker pools). Schedule before growth or at the Phase 7 cutover |
| Caddy reload for the two new site blocks | None (graceful reload); existing sites unchanged |

## Approvals needed (each explicit)

1. Commit/tag the beta release. 2. DNS records `beta-api.bitbotpro.io` and `beta.bitbotpro.io`.
3. Two new Caddy site blocks (existing Gridscan and API sites untouched). 4. Starting the beta
containers on BBP1 and creating database `bitbotpro_v12`. 5. Each real-service validation
(`real-service-validation.md`). 6. Inviting the first testers.

## Steps

| # | Step | Detail |
|---|---|---|
| A1 | **Build** | From the committed tree: `git archive <commit> \| docker build --target runtime --build-arg SOURCE_TREE=<tree> -t bitbotpro-api:spec012-beta1-<tree12> -`; rerun the gate; Trivy report kept. Never pushed |
| A2 | **Secrets** (0600, `$BITBOTPRO_SECRETS_DIR`, never in the repo or a terminal log) | `beta_mongo_url` (the existing `bitbotpro_app` user if it may use `bitbotpro_v12`, otherwise a new MongoDB user restricted to `bitbotpro_v12` — preferred), `beta_jwt_secret` (new), `beta_encryption_keys` (`k1=<new Fernet key>`; offline copy), `resend_api_key` |
| A3 | **Isolated database** | `DB_NAME=bitbotpro_v12`. The API's `prepare_database` creates schema, validators and indexes on first start and refuses any legacy or foreign database. Nothing touches `bitbotpro`, PostgreSQL or Gridscan |
| A4 | **Superadmin bootstrap** | `docker compose -p bitbotpro-beta -f deploy/proposed/docker-compose.spec012.yml run --rm api python -m api.bootstrap --email admin@bitbotpro.io --name "Platform Admin"` — the password is typed interactively (policy-checked), never copied from the legacy database; refuses if a superadmin exists |
| A5 | **API and worker** | The proposed compose (`deploy/proposed/docker-compose.spec012.yml`) as a **separate project** `bitbotpro-beta` with `container_name`s `bitbotpro-beta-api` / `bitbotpro-beta-worker`, the API published only on the WireGuard address (`10.50.0.20:8001`), env: `PUBLIC_API_URL=https://beta-api.bitbotpro.io`, `PUBLIC_APP_URL=https://beta.bitbotpro.io`, `CORS_ORIGINS=https://beta.bitbotpro.io`, `ALLOWED_HOSTS=beta-api.bitbotpro.io,127.0.0.1,localhost`, `TRUSTED_PROXIES=10.50.0.1`, `FROM_EMAIL=noreply@bitbotpro.io`. Health: both containers `healthy`; `https://beta-api.bitbotpro.io/api/ready` → `ready` |
| A6 | **DNS and Caddy** | `beta-api.bitbotpro.io` → same VPS; site block identical to `api.bitbotpro.io.caddy` but upstream `10.50.0.20:8001` (`/api/*` only, `CF-Connecting-IP` overwrite, no access log). `beta.bitbotpro.io` → static admin-app build (`REACT_APP_API_BASE_URL=https://beta-api.bitbotpro.io`) served by Caddy `file_server` with SPA fallback to `index.html`, or an Emergent preview deployment. Both hostnames are the same site (`bitbotpro.io`), so the `SameSite=Strict` session cookie works |
| A7 | **AI credentials** | Per Enterprise/Organisation in the admin app (credential **Test** must pass); the platform holds no AI key |
| A8 | **Discord** | Superadmin creates the beta Enterprise's Discord application config (one per Enterprise, D-Q14), redirect `https://beta-api.bitbotpro.io/api/hooks/discord/oauth/callback`, starts the gateway (worker) |
| A9 | **Telegram** | Organisation credential + instance **Connect** (webhook registered on `beta-api`) |
| A10 | **Webchat** | Instance allowed origins = the tester's website; embed snippet from the Install tab |
| A11 | **Zendesk** | Superadmin: platform OAuth client (redirect `https://beta-api.bitbotpro.io/api/hooks/zendesk/oauth/callback`); Organisation: OAuth or API-token connection, signed webhook provisioned |
| A12 | **Knowledge** | Enterprise/Organisation sources, documents assigned to bots; strict mode only where approved; K-H stays **off** (experimental, `knowledge-eval-protocol.md`) |
| A13 | **Usage limits** | Enterprise policy `limits.max_monthly_ai_messages` (e.g. 2 000 per beta Enterprise), `WEBCHAT_DAILY_LIMIT_PER_INSTANCE` (2 000), provider-side spend limits on every AI key |
| A14 | **Real-service validation** | V1–V5 (`real-service-validation.md`) on the test Enterprise — **all must pass** |
| A15 | **Backup** | Daily `mongodump --db bitbotpro_v12 --gzip --archive` (0600, SHA-256, off-host copy) with `deploy/scripts/backup-restore-check.sh` adapted to `DB=bitbotpro_v12`; the key ring backed up offline with it; restore check on day 1 |
| A16 | **Monitoring** | `docker inspect` health of both beta containers; logs for `worker error`, 5xx, gateway reconnect loops; `/api/platform/workers`; usage per Enterprise; Caddy health check on `/api/health` |
| A17 | **Final smoke tests** | Superadmin login → create the tester's Enterprise + owner invitation → **the tester receives the e-mail, creates an account (B-ACCT), signs in** → Organisation → Bot → web instance preview answer; foreign Host → 400; `/docs` → 404 |
| A18 | **First testers** | Owner invites testers by e-mail (accounts are created only through invitations; no public sign-up) |

## Rollback

The beta is additive: stop `bitbotpro-beta-api`/`-worker` (`docker compose -p bitbotpro-beta stop`),
remove the two Caddy site blocks (graceful reload) and the DNS records. The legacy production API,
its image and the `bitbotpro` database were never touched, so nothing needs restoring. The
`bitbotpro_v12` database is kept (backed up) until a separately approved, allowlisted removal.

## Minimum to first tester

A1 → A2 → A3 → A4 → A5 → A6 → A7 → V1–V5 (A8–A11 happen inside them) → A12–A13 → A15 → A17 → A18.
Estimated: ½ day of setup and approvals, ½ day of real-service validation, then invitations.
