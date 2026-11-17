# BitBotPro: Backend Self-Hosting Assessment and Migration Plan

- **Repository:** `ican-group/discord-pro`
- **Branch:** `bitbotpro-v1.0.0`
- **HEAD:** `5509ca6`, clean working tree (verified)
- **Assessment date:** 2026-11-17
- **Type:** read-only. No code, Git, infrastructure, DNS or database was changed.

**Legend**
- **[V]** I verified this in the source code at the cited `file:line`.
- **[I]** This is an inference or assumption that still needs checking.

All paths are relative to the repo root.

---

## A. Executive summary

**The backend can be moved off Emergent, but it is not "~100 lines away" as `docs/self-hosting-readiness/` claims.** Removing Emergent itself is small. Most of the work comes from splitting the frontend and backend onto different hosts: the code assumes they share one origin in more than 15 places, in both frontend and backend.

The main findings:

1. **The Emergent dependencies are few and easy to replace.**
   - The `emergentintegrations` LLM path, used by the default `"emergent"` provider.
   - Emergent object storage, which holds **bot logos only**. Knowledge base uploads are converted to text and stored in MongoDB.
   - The `CLAUDE_API_KEY` / `EMERGENT_LLM_KEY` Universal Key.
   - A same-container assumption: the backend reads `../frontend/.env`.
2. **Splitting the hosts is the real work, and several things will break if the backend just moves:**
   - **Frontend calls:** they use a relative `/api`.
   - **The embed widget:** its iframe would point at `api.bitbotpro.io/widget`, which doesn't exist.
   - **Invite emails:** they link to the API host.
   - **Discord and Zendesk OAuth:** after login they send users to the API host.
   - **Telegram and Zendesk webhooks:** they stay registered against the old Emergent URL.
3. **The "API" channel does not exist in this export.**
   - `bbk_` keys can be created, but **no endpoint checks them**.
   - `/api/v1/chat`, which the UI documents and the tests call, is not implemented.
   - The public `/api/chat/*` endpoints are unauthenticated and default to the first bot in the database.
4. **The readiness docs are wrong in several places that matter:**
   - There is no vector search or embeddings. RAG is MongoDB `$text` search, so Atlas is **not** required.
   - The "API key auth" they describe does not exist.
   - `_delete_object` does not exist.
   - Object storage holds logos only.
   - Webhook URLs need re-registering.
5. **Earlier self-hosting work on this branch belongs to a different version of the code.** ParadeDB in `docker-compose.yml`, `APP_MODE`, `ANTHROPIC_API_KEY` and `PARADEDB_URL` in `backend/.env.example`, specs 002 and 003, and `BUILD.txt` are not referenced by the current source. Reuse the patterns, not the files.
6. **There are security gaps to close before or during the move:**
   - CORS defaults to `*` with credentials.
   - There is no rate limiting.
   - The Zendesk HMAC check is never called.
   - Telegram's `secret_token` is not used.
   - The Discord OAuth `state` is unsigned.
   - Several endpoints are unauthenticated.
   - BYOK LLM keys and all channel tokens are stored in plaintext.
   - A committed database backup contains user emails and password hashes.
7. **Running two backends at once is unsafe for Discord, Telegram and Zendesk.** The cutover must be a single switch with a write freeze. You cannot run both backends against live channels in parallel.

**Readiness:** **amber.** The application is portable. I expect roughly 3–6 focused engineering days of code changes, plus infrastructure and per-tenant channel re-registration, before cutover. This is an estimate.

---

## B. Current architecture (as verified)

### Backend
- **Framework:** FastAPI monolith.
  - `backend/server.py` is **7,006 lines**. `CLAUDE.md` says ~2,086, which is out of date.
  - 11 router modules hold 226 routes under `/api`.
  - Routers are mounted at `server.py:405-453`, and `app.include_router` is at `server.py:6999`.
- **Startup** (`server.py:2373-2544`) runs on every process, every time the server starts [V]:
  - creates indexes
  - seeds the superadmin from `ADMIN_EMAIL` / `ADMIN_PASSWORD`
  - runs the single-to-multi-tenant `update_many`
  - runs the V2 migration (`2441-2450`)
  - runs the workspace migration (`2453-2458`)
  - starts the Discord gateway client in-process (`2491-2493`)
- **Authentication:**
  - HS256 JWT signed with `JWT_SECRET`, valid for 7 days (`server.py:296-321`).
  - The frontend sends `Authorization: Bearer` from localStorage `bf_token`.
  - There is also an httpOnly cookie, `bf_access_token`, set with `samesite=lax` and `secure` (`server.py:2686-2694`). The frontend does not rely on it.
  - The V2 routes and the integration routes accept the header only (`v2_helpers.py:32-41`, `server.py:390-403`).
  - Two JWT libraries are in use: PyJWT and python-jose.
  - Tokens cannot be revoked; logout only clears the cookie.
- **Tenancy:**
  - The model is Workspace → Organisation → Bot.
  - Old V1 `bot_instances` records still work, linked through `bots.legacy_instance_id`.
  - `_verify_instance_access` (`server.py:337-379`) checks the `X-Instance-ID` header against `assigned_user_ids`, org `member_ids`, or workspace admin/owner. Superadmins bypass the check.
  - All database queries are scoped by `instance_id`.
- **Database:**
  - MongoDB through Motor. About 35 collections; see `ENVIRONMENT_VARIABLES.md`, which is accurate on this point. Add `v2_suggestions`, `platform_config`, `zendesk_oauth_client` and `api_keys`.
  - TTL indexes: `processed_discord_messages` (300 s) and `zendesk_oauth_states` (600 s).
- **RAG:**
  - `search_knowledge` (`server.py:490-556`) runs a MongoDB `$text` search, scoped by instance. It then expands matching group chunks, and if there are no matches it falls back to all active sources ordered by priority.
  - The context is capped at 60 KB.
  - **There are no embeddings and no vector index** [V].
- **Knowledge base ingestion:**
  - Files: PDF, DOCX and text are converted in-process and stored in `knowledge_sources.content` (`server.py:5261+`).
  - Websites: requests/BeautifulSoup with a cloudscraper fallback (`server.py:4743`), crawl over SSE.
  - A GitHub importer.
- **LLM:**
  - Entry points are `call_claude` (`server.py:1284`) and `stream_chat_response` (`server.py:1664`). Providers are `emergent` (the default), `anthropic`, `openai` and `ollama`.
  - Configuration is per bot in `bots.llm_config` or `bot_instances.llm_config` (`_resolve_llm_config`, `server.py:1152-1171`).
  - BYOK keys (`anthropic_api_key`, `openai_api_key`) are stored **in plaintext** inside `llm_config` (`bot_routes.py:393-394`).
  - Anthropic, OpenAI and Ollama stream natively. The Emergent provider fetches the full reply and then simulates streaming.
- **Conversation memory:** kept in process memory (`llm_sessions`, `llm_history`, `server.py:121-125`). It is lost on restart and not shared between workers. The full history is also persisted to `conversations`.
- **Unified reasoning:** `_try_api_query` combines live API-integration results with knowledge base context in `build_system_prompt`.
- **Background work:** there is no queue, scheduler or cron.
  - Work runs as `asyncio.create_task` (Telegram) and FastAPI `BackgroundTasks` (Zendesk).
  - The Discord gateway runs as a long-lived task.
  - All of it is lost when the process restarts.
- **Email:** Resend. If `RESEND_API_KEY` is unset, codes are printed to stdout (`server.py:162-200`).
- **Encryption:** Fernet with `ENCRYPTION_KEY`, used **only** for API-integration auth values (`api_integrations.py:19-45`).
- **Rate limiting:** none. The only limit is a per-account outbound semaphore in `webhook_router.py:32-41`.

### Frontend
- **Stack:** React 19 with CRA/craco.
- **How it reaches the backend: a relative `/api`** [V]:
  - `frontend/src/utils/api.js:4` uses `baseURL: "/api"` with `withCredentials: true`.
  - Other relative uses: `utils/v2api.js:3`, `context/AuthContext.js:4`, `utils/streamChat.js:18`, and raw axios in `Login`, `Register`, `VerifyEmail`, `ForgotPassword`, `ResetPassword`, `DiscordInvited`, `ChatPage` and `ChatWidget`.
  - Only `Users.js`, `Profile.js` and `KnowledgeBase.js:506` use `process.env.REACT_APP_BACKEND_URL`.
  - **So today, Emergent's ingress (or `setupProxy.js` in local dev) routes `/api/*` on bitbotpro.io to the backend.**
- **Streaming:** `fetch` plus a manual SSE parser. There is no EventSource and no browser WebSocket.
- **Embedded widget:**
  - The backend serves `GET /api/embed.js` (`server.py:4173-4349`).
  - It injects an iframe pointing at `ORIGIN + "/widget"`, where `ORIGIN = request.base_url` (`server.py:4187`, `4211`).
  - `/widget` is a frontend route (`App.js:142`).
  - The widget calls the public `/api/chat/stream`.
- **Dev-only Emergent tooling:** `@emergentbase/visual-edits`, pulled from `assets.emergent.sh` (`package.json:81`, `craco.config.js:79-93`). It does not affect production.

---

## C. Emergent dependency inventory

| # | Location | Purpose | Blocks self-hosting? | Replacement | Complexity | Risk |
|---|---|---|---|---|---|---|
| 1 | `backend/server.py:28` `from emergentintegrations.llm.chat import LlmChat, UserMessage`; `backend/requirements.txt:27` | LLM client for the default provider | **Yes.** It is imported at module load, and the package comes from Emergent's private index, so `pip install` from PyPI will likely fail [I, needs checking] | Import it lazily or remove it; see #2 | Low | Low |
| 2 | `server.py:1172-1201` `_call_emergent`; `server.py:1345-1395` default branch of `call_claude`; `server.py:1732-1736` streaming branch | `"emergent"` provider (the default for every bot with no provider set) | **Yes.** Every default bot fails without it | Keep the provider value `"emergent"` so no data migration is needed, but run it through the existing `_call_anthropic` / `_stream_anthropic` with a platform-level `ANTHROPIC_API_KEY`. Keep the Opus→Sonnet fallback. Optionally show it as "Platform default" in the UI | Low–Medium | Medium: changes the default model path, cost and billing |
| 3 | `server.py:76-114` `_init_storage`, `_put_object`, `_get_object` (there is no `_delete_object`) and the startup `_init_storage()` call at `server.py:2405-2409` | Bot logo storage only (`server.py:4080-4143`) | **Yes** for logos. Chat still works without it | An S3-compatible client (boto3 is already in requirements) with the same function signatures. Copy existing logos out of Emergent before cutover | Low–Medium | Low |
| 4 | `server.py:78`, `1176`, `1346` `CLAUDE_API_KEY` / `EMERGENT_LLM_KEY` | Universal Key for the LLM and storage | Yes | `ANTHROPIC_API_KEY` (platform key). Optionally an `OPENAI_API_KEY` platform default | Low | Low |
| 5 | `telegram_routes.py:75-90`, `zendesk_routes.py:105-118`, `server.py:2993-3001` read `REACT_APP_BACKEND_URL` with a fallback to `../frontend/.env` | One "public base URL" assumed to be both the frontend and the backend | **Yes, once the hosts split** | Two backend settings: `PUBLIC_API_URL` (webhooks, OAuth callbacks, embed.js) and `FRONTEND_URL` (invite links, post-OAuth redirects, widget iframe). Drop the `../frontend/.env` file read | Low–Medium | Medium: touches every channel's URL generation |
| 6 | `frontend/src/utils/api.js:4` and about 12 other relative `/api` uses (listed in §E) | Same-origin API through Emergent ingress | **Yes** for an external API | One `API_ORIGIN` constant taken from `REACT_APP_BACKEND_URL` | Low | Low–Medium |
| 7 | `frontend/src/components/AdminLayout.js:258-268` | PRODUCTION/PREVIEW banner based on hostname (`preview.emergentagent.com`) | No | Optional: `REACT_APP_ENV` | Trivial | None |
| 8 | `frontend/package.json:81`, `craco.config.js:79-93`, `setupProxy.js` | Emergent visual-edits dev plugin | No (dev only, and a missing module is tolerated) | Keep, since the frontend stays on Emergent | None | None |
| 9 | UI text: `BotSettings.js:363,372,418,423`, `LandingPage.js:1150`, `Docs.js:623` | Labels such as "Emergent Universal Key" | No | Optional relabel | Trivial | None |
| 10 | `bot_routes.py:89,119,300,354,378`, `migrate_v2.py:121`, `api_diagnostics_routes.py:723` | `"emergent"` as the default provider value | No, if #2 keeps `"emergent"` as an alias | Keep the value | None | None |
| 11 | `.emergent/emergent.yml`, `.gitconfig` (Emergent agent identity), `deployer-agent-docs/` | Emergent workspace metadata | No | Leave them | None | None |

There is no Emergent authentication, proxy-header trust or Emergent-managed background service in the backend code [V]. The implicit dependencies are **Emergent's ingress `/api` routing** and **Emergent-hosted MongoDB**, whose access for export is unknown; see §J.

---

## D. Target architecture

```
                 ┌──────────────── Emergent ────────────────┐
 Browser ───────▶│ https://bitbotpro.io  (React build)      │
                 │  REACT_APP_BACKEND_URL=https://api.bitbotpro.io
                 └──────────────────────────────────────────┘
     │ XHR/fetch (CORS, Bearer JWT)          ▲ iframe /widget (customer sites)
     ▼                                       │
 ┌─────────────────────── Our VM / cloud host ───────────────────────────┐
 │ Caddy or Traefik (TLS, Let's Encrypt) :443  api.bitbotpro.io          │
 │   ├─ /api/*  ──▶ backend-api  (uvicorn, APP_ROLE=api, N workers OK*)  │
 │   └─ /api/telegram|zendesk/webhook ──▶ same                           │
 │ backend-discord (same image, APP_ROLE=discord, exactly 1 replica)     │
 │ MongoDB 7 (replica set of 1+, auth on, not exposed publicly)          │
 │ MinIO (or AWS S3 / R2) for logos                                      │
 │ Backups: mongodump nightly → off-host object storage                  │
 │ Logs: docker json-file w/ rotation → optional Loki/Grafana            │
 │ Uptime: external probe of /api/ (health)                              │
 └───────────────────────────────────────────────────────────────────────┘
 Outbound: Anthropic / OpenAI / Ollama, Discord gateway, Telegram API, Zendesk API, Resend
```

`*` Several API workers are only safe after in-memory LLM history is persisted, or with sticky sessions. **For the first cutover, run 1 API process with 1 worker plus the Discord bot in the same process.** That is how it runs on Emergent today [I]. Split the API and Discord into separate processes later.

### Essential components
- Docker Compose, with the backend built from a new `backend/Dockerfile`. **There is no Dockerfile in the repo today** [V].
- A reverse proxy with automatic TLS. Caddy is the simplest choice. It must:
  - pass `X-Forwarded-Proto`, and uvicorn must run with `--proxy-headers --forwarded-allow-ips`, so `request.base_url` is `https`
  - disable buffering for SSE
  - allow a 120 s or longer read timeout for crawls
- MongoDB 7 with authentication and a persistent volume. Atlas is fine but not required, because no vector search is used.
- S3-compatible storage for logos. MinIO on the same host is acceptable; managed S3 or R2 is simpler to operate.
- Secrets in a root-only `.env` file on the host, or a secrets manager. Never in Git.
- Nightly `mongodump` to off-host storage, with a tested restore.
- Health check and uptime monitoring, plus log rotation.

### Optional or later
- A separate Discord worker and several API replicas. This first needs Mongo-backed LLM history, a leader lock for startup migrations, and fixes to restart and status so they reach the worker.
- A job queue (arq or RQ with Redis) for crawls and Zendesk tasks so they survive restarts.
- Prometheus or Grafana, Sentry, and a WAF or Cloudflare in front of `api.`.
- Spec 002 (ParadeDB) is **not needed** for this migration.

### Host requirements to confirm (I have not assumed the dev VM is suitable)
- Public IPv4, ports 80 and 443, a static IP, and a provider SLA.
- Starting point [I]: 2–4 vCPU, 4–8 GB RAM, and 40 GB+ SSD for MongoDB.
- OS patching, SSH hardening and a firewall.
- Backup target, monitoring owner and on-call.
- Data residency (GDPR, if EU customers).

---

## E. Required code changes (minimal, targeted)

The changes are grouped by phase. Line numbers are from `5509ca6`.

### E1. Remove Emergent from the backend
1. `server.py:28`: lazy or optional import of `emergentintegrations`. Remove it from `requirements.txt:27`.
2. `server.py:1345-1395` and `1732-1736`: the `"emergent"` provider calls `_call_anthropic` / `_stream_anthropic` with `os.environ["ANTHROPIC_API_KEY"]`. Keep the retry and fallback, and map the model through `ALLOWED_ANTHROPIC_MODELS`.
3. `server.py:75-114`: replace with S3 using boto3 and the env vars `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION`. Keep the `_put_object` / `_get_object` signatures and the `{"path": ...}` return shape (`server.py:4100`).
4. A new one-off script, `backend/scripts/copy_logos_from_emergent.py`. Run it while still on Emergent. It reads every `bot_config.logo_path`, downloads through the current `_get_object`, and uploads to S3 under the same key.

### E2. Split the hosts (backend)
5. Add `PUBLIC_API_URL` and `FRONTEND_URL` settings, and remove the `../frontend/.env` reads:
   - `telegram_routes.py:75-90` (webhook URL) → `PUBLIC_API_URL`
   - `zendesk_routes.py:105-118` `_get_base_url` → `PUBLIC_API_URL` for the callback, webhook URL and sidebar `bitbot_url`
   - `zendesk_routes.py:325-326`, `462` post-OAuth redirect → `FRONTEND_URL`
   - `server.py:2993-3003` invite link → `FRONTEND_URL`
   - `server.py:6208` Discord `frontend_base` derived from `redirect_uri` → `FRONTEND_URL`
   - `server.py:4187` / `4211` embed.js: the API origin is `PUBLIC_API_URL`, and the iframe origin is `FRONTEND_URL` (two variables in the script)
6. `server.py:7000-7006` CORS: require an explicit `CORS_ORIGINS`, e.g. `https://bitbotpro.io,https://www.bitbotpro.io`. Fail or warn at startup if it is `*`.
7. A one-off admin script to call `setWebhook` again for every active `telegram_config`. It reuses `_set_webhook`. No bulk path exists today (`telegram_routes.py:131-178`).

### E3. Split the hosts (frontend, built on Emergent)
8. Add `src/utils/apiBase.js`: `export const API_ORIGIN = (process.env.REACT_APP_BACKEND_URL || "").replace(/\/$/, "")`. An empty value keeps today's same-origin behaviour, which makes rollback safe.
9. Use `${API_ORIGIN}/api` in:
   - `utils/api.js:4`, `utils/v2api.js:3`, `context/AuthContext.js:4`, `utils/streamChat.js:18`
   - `Login.js:9`, `Register.js:8`, `VerifyEmail.js:8`, `ForgotPassword.js:22,35,46`, `ResetPassword.js:24`, `DiscordInvited.js:19`, `ChatPage.js:10`, `ChatWidget.js:9`
10. Logo `<img>` URLs: `BotSettings.js:144,146,208`, `ChatWidget.js:120,122`.
11. Embed snippet: `EmbedCode.js:165,523` `scriptSrc` → `API_ORIGIN`. Keep `:117` `/widget` on `window.location.origin`.
12. Display-only URLs computed from `window.location.origin`:
    - `DiscordAppSetup.js:113`, `DiscordSettings.js:156,158`
    - `ConnectedAccounts.js:110,124,334`. Line 124 is saved as `credentials.redirect_uri`, so it has a functional effect.
    - `BotSettings.js:674,680`
    - Change these to `API_ORIGIN`, or better, fetch them from the backend.

### E4. Restore the API channel (needed to "preserve 5 channels")
13. Add an `X-API-Key` / `Bearer bbk_…` dependency that looks up `api_keys.key_hash` (sha256), checks it is enabled, sets `_current_api_key_id`, and pins `instance_id` to the key's instance.
14. Add `POST /api/v1/chat` and `/api/v1/chat/stream` that reuse `send_chat_message` / `stream_chat_response`. Tag them with `platform="api"`.
15. Reference: the previous implementation in commit `3ea2ad1` (`external_api_keys` collection). Adapt it to the current `api_keys` schema; don't restore it wholesale.
16. Decide whether the public `/api/chat/send`, `/api/chat/stream` and `/api/chat/history/{session_id}` stay public for the widget. At minimum, stop `_resolve_chat_instance` (`server.py:3380-3393`) falling back to "first bot in DB".

### E5. Hardening, strongly recommended before public exposure on our own infrastructure
17. Call `_verify_zendesk_webhook` (`zendesk_routes.py:251`) in the webhook handler (`zendesk_routes.py:945-954`).
18. Pass Telegram's `secret_token` in `setWebhook` (`telegram_routes.py:91`) and check `X-Telegram-Bot-Api-Secret-Token`.
19. Use `hmac.compare_digest` for path secrets (`telegram_routes.py:268`, `zendesk_routes.py:954`).
20. Sign or store the Discord OAuth `state` (`server.py:6127,6148,6217-6222`).
21. Authenticate or remove the legacy `GET /zendesk/suggestion/{instance_id}/{ticket_id}` (`zendesk_routes.py:918-924`).
22. Rate-limit login, register, forgot/reset password, verify and chat, using slowapi or proxy-level limits. Add an attempt cap on 6-digit codes (`server.py:2707-2736`).
23. Escape `instance` and `mode` in embed.js (`server.py:4197-4198`).
24. Later: encrypt BYOK keys and channel tokens with the existing Fernet helper.

### E6. Ops scaffolding (new files only)
25. `backend/Dockerfile`, `deploy/docker-compose.prod.yml`, `deploy/Caddyfile`.
26. Rewrite `backend/.env.example` to match the real code: remove `PARADEDB_URL` and `APP_MODE`; add `ENCRYPTION_KEY`, `CORS_ORIGINS`, `FROM_EMAIL`, `PUBLIC_API_URL`, `FRONTEND_URL`, `S3_*` and `ANTHROPIC_API_KEY`.
27. Drop ParadeDB from `docker-compose.yml`.
28. Update `CLAUDE.md`, `BUILD.txt` and `README.md`.

---

## F. Infrastructure requirements

- **Networking:**
  - DNS `A`/`AAAA` record for `api.bitbotpro.io` pointing to the host. This is a later step.
  - Only 80 and 443 are public. MongoDB and MinIO are bound to the internal network.
  - Outbound HTTPS is needed to Anthropic, OpenAI, Discord (including the gateway WebSocket), Telegram, Zendesk, Resend, and the sites crawled for the knowledge base.
- **TLS:** Let's Encrypt through Caddy or Traefik. HSTS on `api.`.
- **Process model for the first cutover:** one backend container running `uvicorn server:app --workers 1 --proxy-headers --forwarded-allow-ips='*'`, with `restart: unless-stopped`.
- **MongoDB:**
  - Version 7.x with a dedicated application user that only has rights on `DB_NAME`.
  - Daily `mongodump --gzip` with 14–30 days' retention off-host, and a monthly restore drill.
  - A replica set of 1 is enough to start, and lets you add transactions and change streams later.
- **Object storage:** a bucket for logos with private ACL. Logos are served through `/api/bot-logo/{id}`, so the bucket does not need public read.
- **Secrets:** `.env` with mode 600 owned by the deploy user, or SOPS/age, Doppler, or AWS SSM. Never in the image.
- **Logging and monitoring:**
  - stdout JSON logs with rotation (`max-size`)
  - an uptime probe on `GET /api/`
  - alerts on 5xx rate, Discord disconnects and disk usage
- **Deployment:** from GitHub, with an image tag per commit. Manual `docker compose pull && up -d` is acceptable at first. Keep the previous image tag for rollback.

---

## G. Integration compatibility

| Channel | Entry point | Uses unified pipeline? | What changes at migration | Action |
|---|---|---|---|---|
| **Web Chat / widget** | `POST /api/chat/stream`, `/chat/send` (`server.py:3401-3442`), `GET /api/embed.js` (`server.py:4173`) | Yes (`stream_chat_response`, `server.py:1664`) [V] | The widget iframe origin and the embed.js host change. **Existing customer snippets point at `https://bitbotpro.io/api/embed.js`** | Fix embed.js (E2 #5). **Keep `bitbotpro.io/api/embed.js` working** through an Emergent rewrite or redirect to `api.`, or ask customers to update their snippets. See §J |
| **External API** | **Missing.** Keys exist but there is no endpoint [V] | Will be once restored | Customers who were promised `/api/v1/chat` currently get 404 | Implement it (E4) |
| **Discord** | In-process gateway (`server.py:2059-2365`); OAuth `/api/discord/callback` (`server.py:6196`) | Yes (V1 `2304-2329`; V2 via `webhook_router`, `2005-2027`) [V] | The gateway is outbound, so it is **host-independent**. The OAuth `redirect_uri` is stored in the DB (`discord_app_config`, `workspace_discord_apps`, `platform_config`) and the Discord portal | Update the redirect URI in the portal and DB to `https://api.bitbotpro.io/api/discord/callback`. **Only one backend may hold the bot token at a time.** |
| **Telegram** | `POST /api/telegram/webhook/{instance_id}/{secret}` (`telegram_routes.py:262`) | Yes (`telegram_routes.py:391-461`, `493-549`) [V] | Telegram keeps POSTing to the old Emergent URL | Bulk re-register `setWebhook` at cutover (E2 #7). This switches traffic immediately. |
| **Zendesk** | `POST /api/zendesk/webhook/{id}/{secret}` (`zendesk_routes.py:945`); OAuth callback (`316`); sidebar `/zendesk/sidebar/generate` (`726`) | Yes (`zendesk_routes.py:1046-1067`, `1135-1171`) [V] | The webhook URL, the OAuth app redirect URL and the sidebar app's `bitbot_url` are **configured by hand in each tenant's Zendesk** | Per-tenant runbook: update the webhook endpoint, reinstall or edit the sidebar app setting, and update the redirect URL on the OAuth client |

All five channels resolve to an `instance_id` and run the same knowledge-base, API-integration, prompt and LLM steps [V]. The old readiness doc is right on that point.

---

## H. Migration risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Two backends live at the same time** (Discord token in both, or webhooks split) | Duplicate Discord replies (dedup only works within one DB, `server.py:2101-2115`), and the two databases drift apart | A single cutover window. Disable Discord on Emergent before starting it on the new host (clear the token or stop the Emergent backend). Freeze writes during the export |
| **No access to Emergent's production MongoDB** for export | Blocks migration | Confirm first (§J). Fallbacks: an Emergent support export, or an admin-only export endpoint; the latter is a code change and needs approval |
| **`ENCRYPTION_KEY` not carried over exactly** | Every API-integration credential becomes undecryptable | Copy it verbatim. Rotate later with a re-encrypt script |
| **`JWT_SECRET` changed** | Every user is logged out | Acceptable at cutover, and required if the secret was exposed. Communicate it |
| **Startup migrations run against imported data** (`server.py:2430-2458`) | Unexpected writes on first boot | Rehearse on a copy and diff collection counts before and after |
| **Embed snippets already on customer sites** | The widget breaks on customer sites | Keep `bitbotpro.io/api/embed.js` resolving (needs Emergent support) or run a customer notice campaign |
| **Emergent cannot set the build env var, or keeps routing `/api` to its own backend** | The frontend keeps talking to the old backend | Verify before Phase 6 (§J). The `API_ORIGIN` fallback means a wrong value fails visibly rather than silently |
| **CORS `*` with `allow_credentials`** (`server.py:7003`) | Any site can make credentialed calls | Use an explicit origin list (E2 #6) |
| **No rate limiting and public unauthenticated endpoints** on a self-managed host | Abuse, LLM cost blow-up, code brute-force | E5 items and proxy-level limits |
| **In-memory state** (LLM history, handoff, guild map) | Context lost on restart; incorrect with more than one worker | 1 worker at first. Persist later |
| **Plaintext secrets in Mongo** (BYOK keys, bot tokens, Zendesk tokens) | A DB dump or backup leak exposes every tenant's credentials | Encrypt backups; restrict DB access; field encryption later |
| **PII committed to Git**: `memory/backup-20260526-153656.json` has user emails, bcrypt hashes and conversations [V] | Data exposure; GDPR | Remove it from the branch going forward (a commit). Whether to purge history is a decision for you (§J). Treat it as exposed |
| **Stale docs and config** (ParadeDB, `APP_MODE`, CLAUDE.md line counts, readiness docs) | Engineers follow the wrong instructions | Fix in Phase 2 |
| **`emergentintegrations` not installable off Emergent** | Local dev and CI break | E1 #1 [I, needs checking] |

---

## I. Phased implementation plan

### Phase 1: Verification and decisions (no code)
- **Objective:** close the open questions in §J.
- **Actions:**
  - Confirm Emergent supports a custom `REACT_APP_BACKEND_URL` at build time and can drop or keep the `/api` ingress.
  - Get MongoDB export access.
  - Get an inventory of current env values (names only) from Emergent.
  - List tenants that use Telegram, Zendesk, Discord and embeds.
- **Completion:** every §J item is answered and the hosting provider is chosen.
- **Rollback:** not applicable.

### Phase 2: Local development parity
- **Objective:** the backend boots and passes tests on a plain Ubuntu machine with no Emergent access.
- **Changes:**
  - Make the `emergentintegrations` import lazy (E1 #1).
  - `docker-compose.yml` runs Mongo only.
  - Correct `backend/.env.example`.
  - Fix `CLAUDE.md`, `BUILD.txt` and `README.md`.
  - Add `backend/Dockerfile`.
- **Tests:**
  - `pip install` from clean PyPI.
  - The server starts.
  - `BACKEND_URL=http://localhost:8000 pytest backend/tests -v`, recording the baseline pass/fail list (some tests call `/api/v1/chat` and will fail; that is expected).
- **Risk:** low.
- **Rollback:** revert the commits.
- **Completion:** a documented baseline test report.

### Phase 3: Remove Emergent backend dependencies
- **Changes:** E1 #2–4 (LLM default via Anthropic, S3 storage, logo copy script) and the API channel in E4.
- **Tests:**
  - `test_llm_provider.py`, `test_streaming_chat.py`, `test_bot_logo.py`, `test_external_api_and_gate_mode.py`, `test_api_key_analytics*.py`.
  - A manual chat on each provider: default, Anthropic BYOK, OpenAI BYOK, Ollama.
- **Risks:** model ID mismatches, and cost accounting (`llm_usage.model_used` names change from `claude-…` to `anthropic:…`, which affects the analytics UI).
- **Rollback:** feature-flag the `"emergent"` path with `PLATFORM_LLM=emergent|anthropic` until cutover.
- **Completion:** with no Emergent env vars set, all providers work locally.

### Phase 4: Split-host readiness
- **Changes:** E2 (#5–7), E3 (#8–12) and E5 hardening.
- **Tests:**
  - A local cross-origin rehearsal: frontend on `http://localhost:3000` with `REACT_APP_BACKEND_URL=http://localhost:8000` and `CORS_ORIGINS=http://localhost:3000`.
  - Login, workspace switching, SSE chat, knowledge base upload and crawl, logo, embed on a third test origin, invite email link, and Discord/Zendesk OAuth round-trips against test apps.
- **Rollback:** an empty `REACT_APP_BACKEND_URL` keeps the same-origin behaviour, so the frontend stays deployable on Emergent unchanged.
- **Completion:** all flows pass in the cross-origin rehearsal.

### Phase 5: Database and storage migration rehearsal
- **Actions:**
  - `mongodump` from Emergent (read-only) and `mongorestore` into a staging Mongo.
  - Run the logo copy script into staging S3.
  - Boot the staging backend and check:
    - collection counts and checksums match
    - startup migrations behave
    - Fernet decrypt works for API integrations
    - logins work
- **Do not** point any live channel at staging. Use test Discord, Telegram and Zendesk apps only.
- **Rollback:** discard staging.
- **Completion:** a repeatable, timed migration script and a parity report.

### Phase 6: Production backend deployment, dark
- **Actions:**
  - Provision the host, Caddy, Mongo, S3, backups and monitoring.
  - Deploy at `api.bitbotpro.io` (DNS created at this point, after your approval).
  - **Leave the Discord token empty and channels disabled.**
- **Tests:** TLS, health, CORS preflight from bitbotpro.io, a backup-restore drill.
- **Rollback:** tear down; production is untouched.

### Phase 7: Frontend connection (staged)
- **Actions:**
  - Build an Emergent **preview** with `REACT_APP_BACKEND_URL=https://api.bitbotpro.io` against the dark backend, which holds a staging data copy.
  - Run the full integration test matrix (Phase 8 list).
  - The production frontend stays on the old backend.
- **Rollback:** the preview is disposable.

### Phase 8: Integration testing (on the dark production stack with copied data)
- Auth: register, verify, login, reset, invite.
- Tenant isolation: the existing `test_multitenant_auth.py`, `test_iter76_instance_scoping.py` and `test_iter78`–`85` workspace tests, plus a manual cross-tenant `X-Instance-ID` probe.
- Knowledge base: CRUD, upload, crawl, GitHub import.
- API integrations: decryption works.
- Each channel via test bots and test Zendesk.
- Embed on an external test origin.
- Analytics.
- **Completion:** sign-off checklist complete.

### Phase 9: Production cutover (maintenance window)
1. Announce the window. Put the Emergent app into read-only or maintenance mode if possible.
2. Stop the Discord bot on Emergent: remove the token or stop the backend.
3. Final `mongodump`, then restore and verify counts. Run the final logo sync.
4. Start the new backend with the Discord token. Confirm the gateway is online.
5. Bulk re-register Telegram webhooks.
6. Update the Discord OAuth redirect in the portal and DB.
7. Deploy the production frontend build with `REACT_APP_BACKEND_URL`.
8. Run the Zendesk per-tenant runbook (webhook URL, sidebar setting, OAuth redirect).
9. Smoke-test every channel. Watch errors for 24–72 hours.

**Rollback (within the window or within 72 hours):**
- Redeploy the frontend with an empty `REACT_APP_BACKEND_URL`.
- Stop the Discord bot on the new host and restore the token on Emergent.
- Re-register Telegram webhooks to the Emergent URL.
- Revert the Zendesk URLs.
- **Data written on the new backend after cutover must be exported back, or accepted as lost.** That is why the window should be short and the go/no-go decision fast.

**Completion:** 7 days stable. Then decommission the Emergent backend and **rotate every credential** (§K-2).

---

## J. Open questions for the project owner

1. **Emergent frontend:**
   - Can the production build set `REACT_APP_BACKEND_URL` (it is a build-time variable in CRA)?
   - Can Emergent's `/api` ingress be disabled, or rewritten so that `bitbotpro.io/api/embed.js` keeps working?
   - Does deploying a frontend on Emergent still deploy or bill a backend container?
2. **MongoDB:** how do we get a consistent export of Emergent's production DB (URI, mongodump access, size)?
3. **Hosting:** which provider and region? Is there a requirement for EU data residency? Who is on call?
4. **Platform LLM:** should the default provider be your own Anthropic key, an OpenAI key, or both? Who pays, and is per-tenant billing needed?
5. **API channel:** were external customers given `/api/v1/chat`? Should `/api/chat/*` stay unauthenticated for the widget?
6. **Embeds and Zendesk:** how many customer sites embed the widget? How many Zendesk tenants have the sidebar app or webhooks installed? Who will run the per-tenant updates?
7. **Discord:** one platform Discord app, or per-workspace apps (`workspace_discord_apps`)? Every app's redirect URI must be updated.
8. **Downtime:** what maintenance window is acceptable for the final sync (estimate: 15–60 minutes, depending on DB size)?
9. **History:** keep the leaked credentials and the PII backup in Git history (rotation only), or approve a separately planned history purge?
10. **Object storage:** self-hosted MinIO or managed (S3, R2, B2)?

---

## K. Secrets and credential rotation

### K-1. Required secrets (names only)
- **Required:** `MONGO_URL`, `DB_NAME`, `JWT_SECRET`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ENCRYPTION_KEY`, `CORS_ORIGINS`, `PUBLIC_API_URL`*, `FRONTEND_URL`*, `ANTHROPIC_API_KEY`*, `S3_*`*
- **Optional:** `RESEND_API_KEY`, `FROM_EMAIL`, `ZENDESK_OAUTH_CLIENT_ID` / `_SECRET` (the DB takes precedence), `OLLAMA_BASE_URL`, `DISCORD_*` (env is used only by the migration seed; the runtime reads the DB)

\* = new.

Most channel credentials live **in MongoDB**, not in env:
- `discord_app_config`, `workspace_discord_apps`, `platform_config`, `connected_accounts.credentials`
- `telegram_config.bot_token`
- `zendesk_config` tokens, `zendesk_oauth_client`
- `bots.llm_config` BYOK keys

They migrate with the database dump, so **the backups are as sensitive as the secrets.**

### K-2. Rotation checklist

Do this after cutover, not before, unless a secret is known to be actively abused. Do not print values. Rotate one item at a time and verify each.

| Credential | Where it lives | Interrupts service? | Procedure |
|---|---|---|---|
| `JWT_SECRET` | env | **Yes.** Every session is invalidated and all users must log in again | Rotate **at cutover** on the new host (it is a natural point) |
| `ENCRYPTION_KEY` | env | **Yes, if swapped naively.** API-integration credentials become undecryptable | Write a re-encrypt script (decrypt with the old key, encrypt with the new), run it, then swap. Or use `MultiFernet` |
| `ADMIN_PASSWORD` | env, used only to seed | No. Change it in the UI; the env value only seeds a missing admin | Change it after first login |
| MongoDB credentials | Emergent-managed today; new host after | Old: no effect after cutover. New: create a fresh user from the start | Issue new credentials on our Mongo; never reuse Emergent's |
| Emergent Universal Key (`CLAUDE_API_KEY` / `EMERGENT_LLM_KEY`) | Emergent env | **Yes, for Emergent production**, until cutover | Revoke after decommissioning Emergent |
| Platform Anthropic / OpenAI keys | env (new) | No, if replaced and restarted together | Create new keys; restart |
| Tenant BYOK keys | Mongo `llm_config` | Tenants must re-enter them | Notify tenants if a DB leak is suspected; otherwise leave them |
| Discord bot token(s) and client secret | Mongo + Discord portal | **Yes.** Resetting the token disconnects whichever backend uses it | Reset in the portal **during the cutover window** and put the new token only into the new backend. This also guarantees Emergent stops replying |
| Telegram bot tokens | Mongo `telegram_config` | **Yes.** Revoking via BotFather kills the old token, and the webhook must be re-set | Per tenant: `/revoke` in BotFather, then save the new token in the UI (the save runs `setWebhook` and rotates `webhook_secret`, `telegram_routes.py:131-157`) |
| Telegram / Zendesk `webhook_secret` | Mongo, embedded in the webhook URL | Yes, until the URL is re-registered | Rotate together with re-registration |
| Zendesk OAuth client secret / API tokens / `sidebar_api_key` | Mongo + Zendesk | Yes, per tenant | Rotate in Zendesk, re-run OAuth, regenerate the sidebar key and update the app setting |
| Resend API key | env | Briefly: emails fail until restart | Create new, deploy, then revoke the old |
| GitHub deploy keys / tokens (if any) | GitHub | — | Audit the repository's deploy keys and Actions secrets |

**Historical exposure:** treat every credential that ever appeared in Git history as compromised (`memory/test_credentials.md` is ignored now but may have been tracked earlier). Rotation is the remedy. Rewriting history is out of scope and was not done.

---

## L. Corrections to `docs/self-hosting-readiness/`

| Document claim | Actual |
|---|---|
| "Uses MongoDB Atlas `$vectorSearch` for RAG" (ENV_VARS §1, MIGRATION_READINESS blocker 3) | **False.** `$text` search only (`server.py:497`); no embeddings |
| External API via `X-API-Key: bbk_*` on `/chat/send` and `/chat/stream` (CHANNEL_INTEGRATIONS, API_INVENTORY) | **False.** No key validation exists; the endpoints are public |
| Object storage used for "bot logos and uploaded KB documents"; four functions including `_delete_object` | Logos only; three functions plus init |
| "No hardcoded backend URLs … uses `REACT_APP_BACKEND_URL`" | Most calls use a relative `/api`; only 3 files use the env var |
| "Total code changes ~100 lines" | Much more, mainly host-split work (§E) |
| Channels "self-hosting ready, no changes needed" | The Telegram, Zendesk, Discord-OAuth and embed URLs all need changes or re-registration |
| `REACT_APP_BACKEND_URL` listed only as a frontend var | The backend also reads it (and `../frontend/.env`) for webhooks and invite links |
| "No secrets are hardcoded" | True for code. But `memory/backup-*.json` is committed with PII and password hashes |
| `CLAUDE.md`: `server.py` ~2,086 lines, 5 collections, `specs/001-…` | 7,006 lines, ~35 collections, and spec 001 does not exist |
| Prior `.env.example`, `docker-compose.yml`, `BUILD.txt`, specs 002/003 | Target ParadeDB/`APP_MODE` code that is not in this export |

---

## M. Recommended first implementation step

**"Local parity" change set.** This is the smallest and safest step: it affects nothing in production and is fully reversible.

1. `backend/server.py:28`: make the `emergentintegrations` import lazy, i.e. move it inside `_call_emergent`. This is a one-line move and does not change behaviour on Emergent.
2. Rewrite `backend/.env.example` to list the variables the code actually reads.
3. Remove the ParadeDB service from `docker-compose.yml`.
4. Add `backend/Dockerfile`.
5. Run the backend test suite locally to record a **baseline** before any behaviour changes.

This unblocks every later phase. It needs no Emergent cooperation and no data access, and gives a reference point for detecting regressions.

In parallel, and without code: resolve §J items 1 and 2 (Emergent's frontend env and ingress behaviour, and MongoDB export access). These are the two potential hard blockers.

*No implementation will begin without your explicit approval.*
