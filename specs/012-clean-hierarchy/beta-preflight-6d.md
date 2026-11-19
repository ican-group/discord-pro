# Phase 6D — private beta activation preflight (2026-11-19)

**Status: PASS, conditional on four approval-time settings (§2: P-1 and P-4 are handled in the
proposed manifest; P-2/P-3 by the beta model allowlist policy). No code change was made.**
Nothing was started on BBP1; production is unchanged.

## 1. Release candidate (verified)

| Item | Value |
|---|---|
| Image | `bitbotpro-api:spec012-beta1-4219354d1469`, id `sha256:181265070b34d076111edd4338d7f91c823ddad6bb63e9fbc6bd47b203abb0f4`, label `io.bitbotpro.source-tree=4219354d14693023ccf8baa9d2dc2c53f8f1427e`, user `10001:10001` |
| Evidence | `evidence/phase-6c/final-run-4219354d/` — `SHA256SUMS` verified; gate summary matches the recorded trees, locks and base digest |
| Beta frontend build | `frontend/build-beta` from frontend tree `1a54c99f4c56` with `REACT_APP_API_BASE_URL=https://beta-api.bitbotpro.io` (CI=true, 0 warnings); rebuilt from the committed tree at activation |

## 2. Findings

| # | Severity | Finding | Handling |
|---|---|---|---|
| P-1 | Medium (configuration) | The Spec 012 wiring reads `TRUSTED_PROXIES`; the existing edge override (`docker-compose.edge-caddy.yml`) sets the legacy name `TRUSTED_PROXY_IPS`. Reusing that override would make every visitor appear as `10.50.0.1` (shared per-IP rate limits). | **Handled**: the beta manifest sets `TRUSTED_PROXIES=10.50.0.1` explicitly and does not use the legacy override. Smallest code fix (later): accept `TRUSTED_PROXY_IPS` as an alias, refuse both at once |
| P-2 | **High for Claude 5.x models** | The Anthropic adapter sends no `thinking`/`effort` and `max_tokens` 1024 (512 for K-H). Claude Opus 5.5 / Sonnet 5.5 / Haiku 5.5 think by default and thinking counts against `max_tokens` → likely truncated or empty answers; an empty completion is delivered as empty text. | **Avoided for the beta** by the model allowlist (§5): Claude Sonnet 4.6 and Haiku 4.5 run without thinking when it is omitted, which matches the adapter. Smallest fix before enabling Claude 5.x: send `output_config.effort` for 5.x models, raise the cap to ≥ 4096, and treat empty/`refusal` completions as a provider failure (fallback) |
| P-3 | Medium | The platform `MODEL_ALLOWLIST` lists several OpenAI models whose dated snapshots are reported (secondary sources) as shut down on 2026-12-11, and lacks the Claude 5.x IDs; OpenAI reasoning models have the same token-cap risk as P-2. | **Avoided for the beta**: Enterprise policy restricts providers to Anthropic and models to the two in §5. Allowlist cleanup after the beta |
| P-4 | Low | `bitbotpro-mongo` allows ≈ 409 connections at the 1024 file limit (rehearsed); Motor's default pool is 100 per client. | **Handled**: `maxPoolSize=20` in the beta connection string (beta worst case ≈ 50 connections) |
| P-5 | Info | The official `caddy` binary carries a file capability; with `cap_drop: ALL` it cannot start. | Manifest keeps only `NET_BIND_SERVICE` for `bitbotpro-beta-web` (tested) |

## 3. Isolation assessment (rehearsed locally with disposable names)

| Requirement | Result | Evidence |
|---|---|---|
| Cannot write the legacy database | **Yes** — MongoDB user `bitbotpro_v12_app` has readWrite + dbAdmin on `bitbotpro_v12` only; writes and reads of `bitbotpro` refused; `listDatabases` shows only `bitbotpro_v12` | `deploy/rehearsal/beta-db-isolation-rehearsal.sh` |
| Cannot access Gridscan databases | **Yes** — no Gridscan service runs on BBP1 (`docker ps`: only `bitbotpro-api`, `-mongo`, `-postgres`); any other MongoDB database is refused to the beta user; the beta holds no PostgreSQL credential | rehearsal + host inspection |
| Separate API and worker containers | **Yes** — `bitbotpro-beta-api`, `bitbotpro-beta-worker`, `bitbotpro-beta-web`, project `bitbotpro-beta` | `deploy/proposed/beta/docker-compose.beta.yml` |
| Separate environment and credentials | **Yes** — own secret directory and files (Mongo user, JWT secret, key ring, Resend key) | manifest |
| Independent logging | **Yes** — json-file per container, 10 MB × 5 | manifest |
| Conservative CPU/memory | API 1 CPU / 768 MiB, worker 1 CPU / 768 MiB, web 0.25 CPU / 64 MiB; measured idle 58 / 50 / 12 MiB | local run |
| Conservative AI quotas | Enterprise policy 2 000 AI messages / month, 10 end-user messages / minute, webchat 500 / instance / day, provider-side spend limit | checklist §2 |
| MongoDB not public | **Yes** — no host port; reached only over the internal `bitbotpro_db` network | `ss -ltn` shows no 27017 listener |
| No production port conflict | **Yes** — beta binds `10.50.0.20:8001` and `:8002`; production uses `10.50.0.20:8000`; both free | `ss -ltn` |
| No change to production Docker services | **Yes** — separate project; joins the existing `bitbotpro_db` network as a member (network unchanged); own `bitbotpro_beta_egress` network | manifest |
| Existing Caddy routes untouched | **Yes** — one extra `import` line; sandbox test showed Gridscan + `api.bitbotpro.io` + 2 beta hosts after install, byte-identical Caddyfile after removal | `deploy/proposed/beta/vps-caddy-beta-site.sh` |
| WireGuard unchanged | **Yes** — upstreams use the existing `wg-bbp` address `10.50.0.20` | — |
| Stoppable independently | **Yes** — `docker compose -p bitbotpro-beta … stop` | local run |

**Shared-host headroom (BBP1, read-only, 2026-11-19):** 8 CPUs; RAM 19 GiB total, 16 GiB available
(production API 71 MiB, MongoDB 280 MiB, PostgreSQL 62 MiB); disk 898 GB free of 984 GB; MongoDB CPU
spikes to ≈ 125 % briefly between ≈ 2 % idle (legacy workload). Beta hard limits total 1.6 GiB and
2.25 CPUs, ≈ 10 % of RAM; database growth for a private beta is megabytes. The MongoDB file limit
stays at 1024 for the beta (decision 6C-D); with `maxPoolSize=20` the beta adds ≤ 50 connections.

## 4. Edge, URLs, cookies

Rehearsed on the real image with the proposed environment (local, internal network):
`/api/health` and `/api/ready` 200 for `Host: beta-api.bitbotpro.io`; `Host: api.bitbotpro.io` → 400;
CORS allows `https://beta.bitbotpro.io` only (production origin refused); session cookie
`Secure; HttpOnly; SameSite=Strict; Path=/api` (beta and beta-api are the same site); SPA served
with `no-store` shell and immutable hashed assets.

| Link / callback | Value |
|---|---|
| Invitation | `https://beta.bitbotpro.io/app/invitations#token=…` |
| Password reset | `https://beta.bitbotpro.io/app/reset-password#token=…` |
| OAuth result pages | `https://beta.bitbotpro.io/oauth/{discord,zendesk}/result` |
| Discord OAuth redirect | `https://beta-api.bitbotpro.io/api/hooks/discord/oauth/callback` |
| Zendesk OAuth redirect | `https://beta-api.bitbotpro.io/api/hooks/zendesk/oauth/callback` |
| Telegram webhook | `https://beta-api.bitbotpro.io/api/hooks/telegram/<instance>/<secret>` (set automatically) |
| Zendesk webhook | `https://beta-api.bitbotpro.io/api/hooks/zendesk/<integration>/<secret>` (provisioned) |
| Webchat embed | `https://beta-api.bitbotpro.io/api/embed.js`; allowed origins per instance |

TLS: Caddy obtains certificates for both hostnames automatically once DNS points at the VPS.
CSRF: unchanged (custom header on every unsafe cookie request; tested in the gate).

## 5. AI model allowlist (recommended for the beta; no code change)

| Provider | Models | Why |
|---|---|---|
| Anthropic | **`claude-sonnet-4-6`** (default), **`claude-haiku-4-5`** (low cost) | Current, served, already in `MODEL_ALLOWLIST`; without a `thinking` field they answer without thinking — exactly what the adapter sends (P-2 does not apply) |
| OpenAI | none for the first beta | Snapshot deprecations in flux (P-3) and reasoning-token risk; enable after a dedicated V1 run |

Applied as **Enterprise policy** on each beta Enterprise (superadmin, Policy tab):
`ai.allowed_providers = [anthropic]`, `ai.allowed_models = [claude-sonnet-4-6, claude-haiku-4-5]`,
`limits.max_monthly_ai_messages = 2000`, `limits.max_end_user_msgs_per_minute = 10`; each AI key gets a
provider-side monthly spend limit (USD 50 suggested). Claude 5.x models follow after the P-2 fix.

## 6. E-mail (Resend)

Requirements: a **new** Resend API key with *sending access* restricted to the verified `bitbotpro.io`
domain (production's key is not reused or changed); sender `beta@bitbotpro.io` (any address on the
verified domain). Behaviour rehearsed with a dummy key: invitations report `email_sent: false` and
return no token; password reset still answers 202 (sent in the background); logs record only the
error type. Rate limits: invitations 30 / inviter / hour and 5 / recipient / day; reset 3 / account /
hour; Resend's own API limit (≈ 2 requests/s) is far above beta volume. Test: the first real e-mail is
the A17 smoke-test invitation to an operator-owned address (needs approval).
