# Spec 008 — Pre-Public Security Hardening

## Status

**Approved as the working security specification (2026-11-17).** Every
implementation stage below needs separate approval, and each container rebuild
or restart is its own gate.

**Progress (2026-11-17):** Stage 1 (foundations) is implemented and tested
offline and in the Docker `test` target (201 tests: 190 passed, 11 compose
skips). It is not deployed: no route uses it, the running container is
unchanged, and the TTL index is not applied.

**Progress (2026-11-17):** Stage 2 (quick containment) is implemented and
tested offline (243 tests, 0 failures; the 27 FastAPI tests run in the Docker
`test` target). Not built into a runtime image and not deployed. Docker `test` gate passed
(245 tests: 232 passed, 13 host-only skips).

**Progress (2026-11-17):** Stage 3 (account-takeover protection) is implemented
and tested offline (295 unit tests, 0 failures; the FastAPI flow tests run in
the Docker `test` target) plus real-MongoDB integration tests prepared
(`deploy/scripts/run-stage3-tests.sh`). Not deployed: it needs the
`code_hmac_key` production secret (approval) first.

**Progress (2026-11-17):** Stage 3b (N8, authorization scoping) is implemented
and tested offline (320 unit tests, 0 failures). Combined Stage 3 + 3b Docker
gate passed (320 tests: 307 passed, 13 host-only skips; 5 real-MongoDB
integration tests passed).

**Progress (2026-11-17):** Stage 4 (public chat sessions) is implemented and
tested offline (352 unit tests, 0 failures). Docker gate passed (352 tests:
339 passed, 13 host-only skips; 6 integration tests passed).

**Progress (2026-11-17):** Stage 9a (frontend chat compatibility and API base
URL) is implemented; frontend gate passed (Jest + production build with the API
origin embedded).

**Progress (2026-11-17):** Stage 9b (embed widget, Zendesk sidebar, Discord
settings, password UX, public origins) is implemented; combined gate passed
(backend 368 tests: 353 passed, 15 host-only skips; frontend 63 Jest tests and
production build).

**Progress (2026-11-17):** Stages 5–8 (enumeration, OAuth state, webhooks and
public surface, invite-only registration) are implemented; combined gate passed
(backend 404 tests: 389 passed, 15 host-only skips; 8 MongoDB integration tests;
frontend 64 Jest tests and build).

**Progress (2026-11-17):** Stage 10 (edge and deployment readiness) is prepared:
API security headers and trusted-proxy configuration implemented; Cloudflare
Tunnel files, edge rules (`cloudflare.md`) and the launch runbook
(`launch-runbook.md`) written. Backend 424 offline unit tests pass. **Nothing is
deployed; the tunnel is not started and DNS is unchanged.** See
[Amendments](#amendments-during-implementation).

**Public access stays disabled** (no Cloudflare Tunnel, no published ports, no DNS
change) until every **mandatory** requirement in this spec has passed its
acceptance criteria and the final verification gate ([G-final](#g-final-before-cloudflare-tunnel)).

### Decisions recorded

| ID | Decision (2026-11-17) |
|---|---|
| DD1 | **Registration: invite-only** for the initial public launch (`REGISTRATION_MODE=invite_only`). |
| DD2 | **Frontend changes** for security and API migration are approved in principle. Each implementation stage that touches `frontend/` needs separate approval. |
| DD3 | **Zendesk suggestion endpoint:** planned for removal, provided a final dependency check confirms no legitimate client uses it (see [P4](#p4-unauthenticated-zendesk-ai-suggestions)). |
| DD4 | **Zendesk sidebar:** move browser calls to Zendesk `client.request`, after verifying authentication and compatibility (see [P9](#p9-frontend-embed-widget-and-zendesk-sidebar-configuration)). |

### Relationship to other specs (preserved, not changed)

- **Spec 004** (`specs/004-backend-container-deploy/`): the private API deployment.
  This spec implements its pre-public blockers S1, S2, E5, A6, the proxy-header
  item and the Zendesk sidebar origin item. Spec 004 stays the source for
  R1–R8, which remain in force.
- **PostgreSQL/pgvector Knowledge Base** (spec 004's follow-up "005";
  `specs/002-pg-knowledge-primary/`): unchanged. The MongoDB KB stays disabled
  (503, spec 004 M1/D9) until that spec lands. This spec adds no KB functionality.
- **Emergent replacement** (spec 004's follow-up "006", E2/E3): unchanged. This
  spec only records E2/E3 as launch blockers or explicit deferrals at the final gate.
- **Admin config store** (`specs/003-admin-config-store/`): unchanged.

## Problem

The private deployment (spec 004) passed its tests, the R8 secret scan and the
private smoke tests. A route review of all 227 routes found that several public
(unauthenticated) endpoints and authentication flows are unsafe for internet
exposure. The current database is empty and the API is reachable only inside the
VM, so nothing is exploitable today. All of the findings below become exploitable
the moment a public route exists.

## Goals

1. Close every path to **account takeover**, **cross-tenant exposure** and
   **unauthorized conversation access** before any public route exists.
2. Make rate limiting and client identification impossible to bypass with
   spoofed headers.
3. Lock the public surface: every unauthenticated route is deliberate, listed and
   tested.
4. Point the frontend, embed widget and Zendesk sidebar at the self-hosted API
   correctly, without weakening CORS.
5. Define verification gates that must pass before the Cloudflare Tunnel.

## Non-Goals

- Re-enabling or migrating the Knowledge Base (PostgreSQL spec).
- Replacing Emergent LLM or logo storage (Emergent replacement spec), beyond
  recording them at the final gate.
- Open self-registration (DD1). Its requirements are documented only so that a
  later change of policy has a defined bar.
- Multi-worker or multi-replica scaling (spec 004 F3: one worker).
- Moving the Motor driver to PyMongo async (spec 004 follow-up 007).

---

## Amendments during implementation

- **A1 (Stage 1).** The `security_counters` TTL index is created only by
  `manage.py security-indexes --apply --confirm` (R-RL.7), never at startup.
  Applying it to the production database is pending separate approval.
- **A2 (Stage 1).** `sanitize_for_log()` lives in `backend/log_redaction.py`.
- **A3 (Stage 1).** Account keys (`rate_limit.account_key`) are an unkeyed,
  namespaced SHA-256 of the normalised identifier (no secret exists yet).
  Stage 3 switches them to an HMAC with the planned `code_hmac_key` before any
  account-keyed counter is written in production.
- **A4 (Stage 1).** R-IP.3 (`--no-proxy-headers`) is not yet in the Dockerfile;
  it is a prerequisite of the first stage that wires `client_ip()` into a route.
- **A5 (Stage 1).** Test-suite hygiene: `test_server_app.py` closes the Motor
  client created by `import server` (`tearDownModule`); no application change.
- **A6 (Stage 2).** Webhook authentication failures (unknown instance or wrong
  secret) now return `404 {"detail": "Not found"}` for both Telegram and Zendesk
  (previously 403 for a wrong secret). Both providers treat any non-2xx as a
  failed delivery; valid deliveries are unchanged.
- **A7 (Stage 2).** Sidebar keys are stored only as `sidebar_api_key_hash`
  (namespaced SHA-256; keys are 256-bit random, so no secret is needed) and
  `sidebar_api_key_hint`. The plaintext is returned only when a key is created
  or regenerated. `GET /admin/zendesk/sidebar-key` keeps the `sidebar_api_key`
  field for the current frontend but returns a masked, unusable value for an
  existing key (plus `key_hidden`, `key_hint`). A legacy plaintext key is never
  used for authentication; the next admin GET migrates it to a hash and shows
  it one last time. **Frontend follow-up (Stage 9b):** show the hidden state
  instead of a copyable masked value.
- **A8 (Stage 2).** The existing startup index on the legacy
  `zendesk_config.sidebar_api_key` field is left unchanged (removing it is an
  index change; deferred to an admin-command cleanup). No index was added for
  `sidebar_api_key_hash` (R-RL.7); lookups scan the small `zendesk_config`
  collection.
- **A9 (Stage 2).** Docker `test` target skips are now **13 host-only
  deployment-file tests**: the 11 compose-file tests plus 2 Dockerfile
  runtime-command tests (the Dockerfile is outside the build context). G-stage
  item 1 reads "only host-only deployment-file tests may be skipped".
- **A10 (Stage 2, new low findings recorded, not fixed):**
  `GET /api/admin/users` builds its bot-name map from all bots
  (`server.py`, `list_all_users`; admin-only, names only, for users already in
  scope); `zendesk_routes.py` `[ZENDESK_TEST]` logs every configured instance ID
  when a config is missing. Both are queued for Stage 7's public-surface review.
- **A12 (Stage 2).** In-process webhook tests must apply the route modules'
  dependency injection (`server._init_telegram()`, `server._init_zendesk()`),
  which production performs in the startup event; a static test guards that
  startup calls both unconditionally.
- **A13 (Stage 3, CRITICAL fixed).** `POST /api/admin/users/{id}/reset-password`
  was guarded only by `require_admin_or_ws_admin`, and every verified user owns
  an auto-created workspace, so **any user could reset any password, including
  the superadmin's**. Now: superadmins may reset any other account; workspace
  owners/admins only members of a workspace they administer (never the
  workspace owner, never a superadmin, admins only by the owner); never self.
- **A14 (Stage 3, CRITICAL; fixed in Stage 3b — see A20) — N8.**
  `require_admin_or_ws_admin` authorises every verified user (see A13). Other
  endpoints relying on it without further scoping:
  `POST /api/admin/users/invite` (invite into **any** workspace / org / bot →
  cross-tenant access), `GET/PUT /api/discord/app-config` (platform Discord app
  credentials), `POST /api/discord/purge-all-connections` (platform-wide
  destructive), the Discord connections listing, and the API-key endpoints
  (`create/revoke/toggle`). Proposed as **Stage 3b** (authorization scoping),
  mandatory before public exposure; G-final item 1 includes N8.
- **A15 (Stage 3).** R-P2.6 (password-changed notice) is deferred to Stage 5,
  which introduces asynchronous email delivery.
- **A16 (Stage 3).** `POST /api/auth/change-password` revokes all sessions and
  returns a fresh `token` (also set as the cookie). Frontend follow-up
  (Stage 9b): store the returned token; until then the user is logged out after
  changing the password.
- **A17 (Stage 3).** Codes are 6 digits from `000000`–`999999`. The
  resend-code response is now one generic message for every case. Login runs a
  dummy bcrypt check for unknown accounts (part of R-P1.3, ahead of Stage 5).
  Other S1 enumeration differences remain for Stage 5.
- **A18 (Stage 3).** `CODE_HMAC_KEY` (Docker secret `code_hmac_key`, ≥32 chars,
  must differ from `JWT_SECRET`) is required when `APP_ENV=production`; the API
  refuses to start without it. Development uses a random per-process key.
  `deploy/docker-compose.api.yml` now mounts it, so the next `up` fails until
  the secret exists (fail closed). Account rate-limit keys are now HMACs with
  this key (resolves A3).
- **A19 (Stage 3).** `get_current_user` now loads the user to compare
  `token_version` (one indexed `users.find_one` per authenticated request) and
  rejects tokens for deleted users.
- **A20 (Stage 3b, N8 fixed).** Every endpoint guarded by
  `require_admin_or_ws_admin` now applies explicit scoping (a static test
  enforces a scoping check in each one):
  - **Platform Discord app** (`PUT`/`DELETE /discord/app-config`,
    `GET /discord/app-config/connections`, `POST /discord/purge-all-connections`):
    superadmin only (`require_admin`). `GET /discord/app-config` returns the masked
    credential view only to superadmins; others get non-secret status
    (`client_id`, `redirect_uri`, `has_*` flags) with no secret fragments.
  - **Invitations:** non-superadmins must target a workspace they own/administer;
    only the owner may invite `admin`; role is `admin`/`member` only (default now
    `member`); every org must belong to the target workspace and every bot to one
    of its orgs (unknown or foreign IDs → 400); legacy `instance_id` grants are
    superadmin-only; the platform role is always `user`.
  - **Invitation list/revoke (new CRITICAL finding):** the list returned every
    tenant's pending invitations **including bearer tokens** to any user, and
    any user could revoke any invitation. Now scoped to the caller's
    administered workspaces (superadmin: all); out-of-scope revoke → 404.
  - **API keys (CRITICAL):** create/revoke/toggle let any user mint or control
    keys for any tenant's bot. Now use `_verify_instance_access` (the
    `X-Instance-ID` rule); out-of-scope keys → 404.
  - **Role changes (new CRITICAL finding, bypass of A13):** a workspace owner or
    admin could insert **any** user ID into their workspace as admin, then reset
    that user's password or delete the account. Non-superadmins can now only
    change roles of existing members; only the owner can create, change or
    remove admins. Platform role changes increment the target's
    `token_version` (no stale-role tokens).
  - **Delete user / admin password reset:** one rule (`_may_manage_user`): the
    target must belong *only* to workspaces the caller administers (never an
    owner, never a superadmin; admins only by the owner). Users with no
    membership or any outside membership (e.g. their own workspace) are out of
    scope.
  - **User listing (A10 fixed):** org/bot names resolved only within the
    caller's workspaces; V1 instance names only for superadmins.
  - Accepting a legacy invitation without a valid `workspace_role` grants
    `member`, never `admin`.
  - **Frontend follow-up (9b):** `DiscordSettings.js` lets workspace users edit
    the platform Discord credentials; those calls now return 403 for
    non-superadmins (intended). The page should be superadmin-only.
- **A21 (Stage 4).** Implemented P3, N1, N3 as specified, with these details:
  - The chat-session token key is **derived** from `CODE_HMAC_KEY`
    (HMAC with a fixed label) instead of a separate `chat_session_key` secret
    (R-P3.5 deviation): it gives the same key separation without another
    secret to provision.
  - Session IDs: `web_` + 192 random bits; tokens `v1.<expiry>.<HMAC>` bound to
    session and bot, 30-day lifetime. A presented session ID without a valid
    token for the same bot starts a new session; only `web_` IDs are ever
    accepted, so Discord/Telegram/Zendesk sessions can never be resumed or read.
    Legacy (tokenless) sessions therefore start fresh.
  - `GET /api/chat/history/{session_id}?instance_id=<bot>` with header
    `X-Chat-Session-Token`; every failure is `404 {"detail": "Not found"}`;
    returns only `{session_id, messages}` of `platform: web` conversations.
  - `POST /chat/send` returns `session_token`; `/chat/stream` includes it in the
    first `session` SSE event.
  - Preflight order (before any knowledge search or LLM call): per-IP limit
    (20/min; history 60/min) → message 1–4,000 characters (400/413) → explicit
    existing bot (V2 id or legacy instance id; otherwise 404, no first-bot
    fallback) → per-bot daily ceiling (`CHAT_DAILY_LIMIT_PER_BOT`, default 2,000;
    then 503 "temporarily unavailable") → session resume/new.
  - **New finding fixed:** `save_conversation` located conversations by
    `session_id` only, so a message sent to one bot with another bot's session
    ID was appended to the other tenant's conversation. Now one atomic upsert
    scoped by `(session_id, instance_id)`.
  - Admin conversation search treats input literally (`re.escape`, 200 chars);
    `DELETE /conversations/{id}` clears that bot's in-memory LLM context only
    after the ownership check (the previous pop never matched any key).
  - Two log lines no longer contain user text (API-query routing).
  - Optional hardening (admin-command gate, R-RL.7): a unique index on
    `conversations(session_id, instance_id)` would also prevent a duplicate
    document if the first two messages of one session race.
  - **Frontend (Stage 9a, required before public exposure):** store
    `session_token` with the session ID; send it in `/chat/send` and
    `/chat/stream`; read it from the send response and the stream `session`
    event; call history with `?instance_id=` and the `X-Chat-Session-Token`
    header (clear the stored session on 404); always pass a real bot ID (no
    `default`); handle 429/503/413. Until then the widget still chats but every
    message starts a new session and history is not restored; `ChatPage.js` and
    `ChatWidget.js` opened without `?instance=` now get `404 Bot not found`.
  - Audit note: no API-key-authenticated chat route exists (API keys are issued
    but no route accepts them); the public `/chat/*` routes are the complete
    public chat surface. `GET /bot-info/{id}` and `GET /embed/config/{id}` field
    review stays in Stage 7 (R-P7.6).
- **A22 (Stage 9a, frontend).**
  - `src/utils/chatSession.js` (new): per-bot, per-surface storage of the
    `{sessionId, sessionToken}` pair (`bbp.chat.<widget|page>.v2.<bot>`; widget →
    `sessionStorage`, full page → `localStorage`); a session ID is never stored
    without a valid token; corrupt entries and the old tokenless keys
    (`widget_session_*`, `bf_chat_*`, including `*_default`) are removed;
    storage failures (sandboxed iframes) never break chat. History uses
    `GET /api/chat/history/{id}?instance_id=<bot>` with `X-Chat-Session-Token`;
    a 404 clears the stored session and starts fresh. Friendly messages for
    429/503/413/404/400 and a client-side 4,000-character check.
  - `src/utils/streamChat.js`: sends `instance_id`, `session_id` and
    `session_token` as JSON body fields (the backend contract; no header);
    `onSession(id, token)` from the first SSE event; HTTP errors map to friendly
    text plus the status; SSE parsing now survives events split across network
    chunks (previously an event split between chunks was dropped).
  - `ChatWidget.js` / `ChatPage.js`: no `"default"` bot fallback (missing or
    invalid `?instance=` shows a clear message and disables sending); the
    server-sent pair is always saved (the old code kept only the first session
    ID ever received, so a server-started session was never adopted); bot
    switching aborts the in-flight stream, clears messages and restores only
    that bot's session; late callbacks from a previous bot are ignored.
  - **API base URL:** `src/utils/apiBase.js` resolves
    `REACT_APP_API_BASE_URL` → legacy `REACT_APP_BACKEND_URL` → same origin.
    All 12 files that called a relative `/api` (including login, registration,
    password reset, `AuthContext`, the shared axios client `utils/api.js` and
    `utils/v2api.js`) and the 3 that read `REACT_APP_BACKEND_URL` directly now
    use it. **Production builds must set
    `REACT_APP_API_BASE_URL=https://api.bitbotpro.io`.** A Jest source guard
    fails on any new relative `/api` request.
  - `BotSettings.js`: the "Usage Example" documented non-existent
    `/api/v1/chat*` API-key endpoints; it now shows the real public chat
    contract and states that API-key endpoints are not available.
  - Backend CORS already permits the `X-Chat-Session-Token` header
    (`allow_headers=["*"]`, exact origin `https://bitbotpro.io`).
  - Not covered by automated tests (no React Testing Library in the
    dependencies; adding it changes `yarn.lock`): component rendering. The
    session, history, streaming, storage and API-base logic is covered by pure
    Jest tests; the components are thin wrappers over it.
- **A23 (Stage 9a, build).** The frontend gate's `yarn install --frozen-lockfile`
  failed because three direct dependencies in `frontend/package.json` had no
  `yarn.lock` entry: `canvas-confetti@^1.9.4`, `react-markdown@^10.1.0` and
  `remark-gfm@^4.0.1` (added upstream in the Emergent import, commit 5509ca6,
  without regenerating the lockfile; all three are imported by
  `MarkdownMessage.js` / `SetupGuide.js`). Fix:
  `deploy/scripts/update-frontend-lockfile.sh` runs Yarn 1.22.22 (the pinned
  `packageManager`) with `--ignore-scripts` in a disposable container, verifies
  the result with `--frozen-lockfile`, and replaces `frontend/yarn.lock` only if
  `deploy/scripts/lockfile-diff.py` shows that no existing entry changes its
  resolved version. The test gate keeps `--frozen-lockfile`.
- **A24 (Stage 9b).**
  - **Public origins activated (R-P6.1–.3, part of Stage 6 done here):**
    `PUBLIC_APP_URL` and `PUBLIC_API_URL` are **required in production** (the API
    refuses to start without valid https origins) and set in
    `deploy/docker-compose.api.yml`; `REACT_APP_BACKEND_URL` removed from the
    API. Invitation links and Discord/Zendesk OAuth landing pages use
    `PUBLIC_APP_URL`; Telegram/Zendesk webhooks, the Zendesk OAuth callback and
    the sidebar app's `bitbot_url` use `PUBLIC_API_URL`. All `frontend/.env`
    fallbacks removed. Discord redirect query values are URL-encoded (the raw
    `reason`/`guild` values previously allowed parameter injection).
  - **Embed widget (E5):** `embed.js` loads the widget UI from
    `PUBLIC_APP_URL/widget` and calls `PUBLIC_API_URL` for config and logo.
    **Fixed:** query values `instance`/`mode` were spliced raw into the script
    (reflected script injection); now JSON string literals (`<` escaped) and
    `mode` is allowlisted. Widget iframes get
    `sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"`,
    `allow="clipboard-write"`, `referrerpolicy="strict-origin"`. **Fixed:**
    inline embeds never rendered (`bitbotpro-container` in the snippet vs
    `bridgebot-container` in the script; both accepted now). No `postMessage`
    is used; session tokens stay inside the widget frame (`sessionStorage` of
    the bitbotpro.io origin) and are never put in URLs.
  - **Embed config:** `GET /api/embed/config/{id}` returns only
    `instance_id, custom_css_url, theme, welcome_message, default_open`
    (previously the whole stored document) with
    `Access-Control-Allow-Origin: *` and no credentials, so `embed.js` on
    customer sites can read it (it could not before: CORS allowed only
    bitbotpro.io). Stylesheet URLs must be `https://` (rejected on save, dropped
    on read, ignored by the widget).
  - **Not done (needs header control on bitbotpro.io):** R-P9.2
    `frame-ancestors` per bot. The widget page is served by the frontend host
    (Emergent), where this API cannot set response headers; enforce with a
    Cloudflare response-header rule at Stage 10, or accept embedding by any site
    (abuse is bounded by the Stage 4 chat limits).
  - **Zendesk sidebar (DD4):** `main.js` calls the API through
    `client.request({secure: true, cors: false})` with header
    `X-Sidebar-Key: {{setting.sidebar_api_key}}`; the key is a secure setting
    that the app iframe can no longer read (the previous code read it from
    `metadata.settings`, which Zendesk does not expose for secure settings).
    The manifest adds `domainWhitelist: [<PUBLIC_API_URL host>]` (required for
    secure-setting substitution); version 1.1.0. **Verification still required
    in a Zendesk sandbox** before launch (secure-setting substitution, proxy
    timeout for long suggestions); fallback per DD4.
  - **Frontend:** `DiscordAppSetup` route is now `AdminOnlyRoute` (was any
    logged-in user) with an in-page superadmin guard; the (unused) inline
    credential panel renders only for superadmins; OAuth callback hints show the
    API origin (also fixed in `ConnectedAccounts.js`). `EmbedCode.js` builds the
    script URL from the API origin. `ZendeskSettings.js` shows the sidebar key
    only after creation/regeneration ("copy now, it will not be shown again"),
    otherwise a hint (`zsk_…abcd`). `Profile.js` stores the fresh token from
    change-password (`AuthContext.updateToken`) and signs out on 401. A shared
    `passwordPolicy.js` (exact mirror of `check_user_password`) and
    `PasswordRequirements` component are used by registration, reset (both
    pages), password change, invitation acceptance and admin reset; the old
    6-character checks are gone. `tests/unit/test_frontend_parity.py`
    (host-only) and frontend Jest vectors keep both policies identical.
  - Docker `test` target skips are now **15 host-only tests**: 12 compose-file
    tests (Stage 9b added `test_public_origins`), 2 Dockerfile tests and 1
    frontend-parity test.
  - **Fixed during the 9b gate (reflected script injection):** the `embed.js`
    template still interpolated the raw `instance` query value inside a
    JavaScript comment (`// … /api/bot-logo/{instance}`); a value containing a
    newline ended the comment and ran as code on the embedding page. The
    comment no longer interpolates anything, and a static test now requires
    every value interpolated into the template to go through `js(...)`.
- **A25 (Stages 5–8).**
  - **Stage 8 (P8):** `REGISTRATION_MODE` = `invite_only` (production default,
    set explicitly in compose; `open` refused in production) | `open`
    (development). In invite-only mode `POST /auth/register` returns one 403
    without reading or writing anything; `GET /auth/registration-mode` tells the
    frontend, whose Register page shows an invitation-only message (fail
    closed if the mode cannot be read). Invitation acceptance validates input
    first, then claims the invitation atomically (`pending → accepting`,
    exactly one winner, verified against real MongoDB), re-validates grants at
    acceptance (orgs/bots must belong to the invitation's workspace; a legacy
    instance only if the inviter is still a superadmin; platform role always
    `user`), and rolls the claim back if account creation fails. Unknown, used,
    revoked and expired invitations get one 404; lookup/accept are limited to
    20/hour per IP.
  - **Stage 5 (S1):** resend-code and forgot-password always return the same
    body; their emails are sent in FastAPI background tasks with 3 retries, so a
    provider failure for a real account never changes the response (an
    undeliverable reset code is removed). A global 503 remains only when no
    email provider is configured at all (uniform for every caller). Verify
    returns the generic invalid-code error for already-verified accounts. In
    `open` mode, registering an existing email returns the normal success body
    (username conflicts still return 400; `open` is development-only). All five
    account endpoints have a minimum response time (`AUTH_RESPONSE_FLOOR_MS`,
    default 400 ms, plus jitter). Login already equalised bcrypt work (A17).
  - **Stage 6 (P5):** Discord OAuth `state` is 256 random bits, stored only as a
    hash in `oauth_states` with provider, mode (`install` 10 min / `share`
    7 days), initiating user and instance; consumed with one atomic
    `find_one_and_delete` and an explicit expiry check before any token
    exchange (single-use verified against real MongoDB). Install-mode callbacks
    must carry the initiating user's session cookie (login-CSRF protection);
    share links are bound to the instance only, by design. The legacy
    `state=<instance_id>` / `share:<id>` forms are rejected. Redirects go only to
    `PUBLIC_APP_URL` with URL-encoded parameters. Zendesk OAuth state is now
    consumed atomically as well. `manage.py security-indexes` also creates the
    `oauth_states` TTL index (same approval gate).
  - **Stage 7 (P7):** Telegram webhooks are registered with `secret_token` and,
    once registered that way, require `X-Telegram-Bot-Api-Secret-Token`
    (constant-time); older registrations keep path-only auth until the bot is
    re-saved. Zendesk webhooks verify `X-Zendesk-Webhook-Signature` over
    timestamp + body with a ±5-minute replay window once the admin stores the
    signing secret (`PUT /api/admin/zendesk/webhook-signing-secret`, encrypted
    with `ENCRYPTION_KEY`; config shows `webhook_signature_configured`); event
    IDs are already de-duplicated. Webhook bodies are capped at 256 KiB and
    parsed strictly; 120 deliveries/minute per instance. Public reads
    (`bot-info`, `bot-logo`, `embed/config`) are limited to 300/minute per IP.
    **New findings fixed:** `GET /bot-info/{id}` published the bot `persona`,
    which is part of the system prompt (now name only);
    `POST /discord/send-invite-email` would email any caller-supplied URL from
    our domain (now only Discord authorize links with our client ID); a
    Zendesk test route logged every tenant's instance ID (A10, fixed).
    `tests/unit/test_public_routes.py` inventories all 23 unauthenticated
    routes with their required protections (R-P7.5).
  - **Frontend follow-up still open:** an admin UI field for the Zendesk
    signing secret (the API exists; until then it is set via the API).
  - Not implemented (documented): R-P2.6 password-changed notice (no async
    notification templates yet); per-username anti-enumeration in `open`
    registration (development only).
- **A26 (Stage 10).**
  - **Trusted proxy (R-IP.2–.4):** the API loads `TRUSTED_PROXY_IPS` at import
    (exact addresses only; invalid values stop the process) into
    `client_ip`. Compose: new internal network `bitbotpro_ingress`
    (10.250.250.0/29, no internet/LAN route), API at 10.250.250.3,
    `TRUSTED_PROXY_IPS=10.250.250.2` = cloudflared's fixed address; uvicorn keeps
    `--no-proxy-headers`. Verified end to end in-process: from 10.250.250.2,
    `CF-Connecting-IP` keys the rate limits; from any other peer, forged
    `CF-Connecting-IP`/`X-Forwarded-For` are ignored.
  - **Cloudflare Tunnel:** `deploy/docker-compose.tunnel.yml` (project
    `bitbotpro-tunnel`, profile `tunnel` — never started by a plain `up`;
    pinned image; non-root, read-only, no capabilities, no ports; credentials
    JSON as a Docker secret, tunnel ID on the command line, no token in env) and
    `deploy/cloudflared/config.yml` (only `api.bitbotpro.io` + `^/api/` →
    `http://10.250.250.3:8000`, everything else 404; metrics on loopback).
  - **API security headers** (`backend/security_headers.py`, outermost pure-ASGI
    middleware, no body buffering): `nosniff`, `X-Frame-Options: DENY`,
    `CSP default-src 'none'; frame-ancestors 'none'`, `Referrer-Policy:
    no-referrer`, `Permissions-Policy`, COOP, CORP `same-site` (`cross-origin`
    only for `embed.js`, bot logos and embed config), `Cache-Control: no-store`
    for auth/chat/invite/admin/v2, HSTS in production. Route headers are never
    overridden (SSE keeps `no-cache`); CORS preflights keep their CORS headers.
  - **Frontend headers, WAF and rate limits** (Cloudflare, not yet applied):
    `cloudflare.md` — skip rules so Telegram/Zendesk webhooks are never
    challenged, path/method allowlists, managed rules in log mode first,
    per-path rate limits, Transform Rules for `bitbotpro.io` (HSTS, nosniff,
    `frame-ancestors 'none'` for the app, `frame-ancestors https:` for
    `/widget`). Per-bot embedding allowlists (R-P9.2) remain an accepted,
    documented risk (bounded by Stage 4 chat limits).
  - **Operations:** `deploy/scripts/scan-image-secrets.sh` replaced by the
    hardened scanner (INCOMPLETE/FAIL/PASS; patterns never on disk; JSON
    secrets such as the tunnel credentials; tunnel config and container);
    `deploy/scripts/backup-restore-check.sh` (read-only dump with credentials
    via an in-container 0600 config file, restore into a disposable `mongod`,
    per-collection count comparison); `launch-runbook.md` (ordered, gated
    steps from backup to external verification, plus rollback).
  - Docker `test` target host-only skips are now **20** (15 + 5 tunnel/
    deployment tests).
- **A27 (Stage 10, edge selection — supersedes the A26 compose layout).** The
  public edge is the **existing Caddy VPS (37.27.153.47) over WireGuard**;
  Cloudflare Tunnel is kept only as a documented alternative.
  - The base `docker-compose.api.yml` now has **no** edge: no ports, no trusted
    proxy, no ingress network. One override adds the edge:
    `docker-compose.edge-caddy.yml` (selected: publishes the API only on the
    WireGuard address `10.50.0.20:8000`, `TRUSTED_PROXY_IPS=10.50.0.1`) or
    `docker-compose.edge-tunnel.yml` (alternative: ingress network,
    `TRUSTED_PROXY_IPS=10.250.250.2`).
  - `deploy/wireguard/wg-bbp.conf.template`: `Address 10.50.0.20/32`, single
    peer = VPS `wg0` (`37.27.153.47:51820`, `AllowedIPs 10.50.0.1/32` → no default
    route/DNS change; the Gridscan peers .11/.12 can neither be reached nor reach
    this VM), private key from a root-only file via `PostUp` (never in the config),
    `PersistentKeepalive 25` (VM behind NAT), and `DOCKER-USER` rules (added on up,
    removed on down) dropping any traffic to the published port that did not
    arrive on `wg-bbp` from `10.50.0.1`. `docker-after-wg-bbp.conf` orders Docker
    after the tunnel (fail closed: without the tunnel the API is not published).
  - `deploy/caddy/api.bitbotpro.io.caddy`: separate imported site, no global
    options, Gridscan untouched; only `/api/*` proxied to `10.50.0.20:8000`
    (other paths 404); `header_up CF-Connecting-IP {remote_host}` **overwrites**
    the client-IP header (never passed through); `flush_interval -1` for SSE; no
    compression; no access log (paths carry tokens/secrets); upstream health
    check on `/api/health`.
  - `deploy/wireguard/README.md`: gated operator steps (pre-checks, key
    generation without printing, live `wg set` + appended `[Peer]` on the VPS,
    tunnel up, publish, connectivity checks incl. negative tests from LAN and
    Gridscan peers, DNS + Caddy graceful reload, external checks) and per-step
    rollback. `launch-runbook.md` updated (steps 5b, 6, 12, rollback).
  - Tests: host-only edge tests verify the four files agree (bind address =
    WireGuard address = Caddy upstream; trusted proxy = the only allowed WireGuard
    source); the in-process proxy-trust test covers 10.50.0.1 (trusted) vs a
    Gridscan peer (untrusted). Docker `test` target host-only skips: **26**.
- **A28 (Stage 10, pre-activation review of the WireGuard/Caddy edge).**
  - **Fixed (critical):** the `DOCKER-USER` rules matched on the connection's
    original destination without `--ctdir ORIGINAL`, so they also matched the
    **reply** packets (container → Caddy, entering from the Docker bridge, source
    = container) and would have dropped every API response. Both rules now carry
    `--ctdir ORIGINAL`.
  - **Fixed:** rules moved from `PostUp` to `PreUp` (installed before the address
    exists: no exposure window when the tunnel is re-upped with the API already
    published) and made idempotent (`iptables -C … || iptables -I …`; `PostDown`
    deletes in a loop until no copy remains).
  - Verified on the VM (read-only): Docker 29.10.0 with its default iptables
    firewall backend (no `daemon.json`), host `iptables` = iptables-nft (same
    backend as Docker), WireGuard kernel module present; both rules parse
    (`iptables-translate`) to `ct direction original ct original daddr
    10.50.0.20 ct original proto-dst 8000`. A host-only test parses the actual
    rules from the template and evaluates them against packet scenarios (Caddy
    request allowed; container reply allowed; LAN host, LAN host spoofing
    10.50.0.1 and a Gridscan peer dropped; unrelated traffic untouched).
  - README: pre-check that `DOCKER-USER` exists and that no Caddy site already
    serves bitbotpro; VPS changes are live `wg set` only (never restart `wg0`);
    with `SaveConfig = true` persist via `wg-quick save wg0`; idempotency, DNS
    and LAN-route checks after tunnel-up; LAN negative test with an explicit
    route; rule-removal check on rollback. `deploy/wireguard/.gitignore` keeps
    real configs/keys out of git.
- **A29 (Stage 10, edge hardening and runbook correction).**
  - Template: added F3 (`DOCKER-USER`: drop anything forwarded from `wg-bbp` not
    addressed to the API — no pivot from the VPS into the LAN/containers) and I1
    (`INPUT`: drop new non-ICMP connections from `wg-bbp` to the VM). All four
    rules are tagged `bitbotpro-wg-bbp`; the template now contains the VPS-key
    placeholder exactly once.
  - Every privileged step is a reviewed script with guards (no hand edits):
    `bbp-create-key.sh` (Approval 1; never overwrites the key; prints only the
    public key), `vps-add-peer.sh` (Approval 2; refuses own/live/persisted keys,
    any AllowedIPs covering 10.50.0.20, `SaveConfig = true`, unparsable result;
    append-only, atomic, verifies other peers unchanged), `bbp-install-conf.sh`
    (Approval 3; single install path, never overwrites, identical rerun is a no-op,
    atomic hard link), `vps-caddy-site.sh install|remove` (Approval 5 / rollback;
    validates a candidate before replacing the live Caddyfile; one-line change;
    graceful reload), `vps-remove-peer.sh` (rollback; only a peer whose AllowedIPs
    are exactly 10.50.0.20/32 and only the exact appended block).
  - README corrected: the public-key command (redirection ran before sudo), the
    contradictory Step D install, the bracketed `[-f …]` pseudo-syntax in Step E,
    an unsafe Caddy rollback (restore of an old backup), manual `wg0.conf` editing
    on rollback, missing file transfer/checksum step to the VPS, missing gates on
    rollback and on the optional LAN-route test.
  - Tests: `test_wireguard_scripts.py` (32, host-only) runs every script against
    mocked `wg`/`wg-quick`/`caddy`/`systemctl`/`apt-get` in temporary directories
    and `bash -n` on every README block; mutation checks (removing each guard)
    are all caught. `vps-add-peer.sh` rolls back automatically: a failed `wg set`
    restores `wg0.conf` from the backup; a failed post-check removes the new peer
    again and restores the file. Its post-check is strict (other peers' complete
    allowed-ips lines unchanged, exactly one new line `<key>\t10.50.0.20/32`), and
    HUP/INT/TERM are ignored from the backup onwards (an SSH disconnect cannot
    leave a half-applied peer). `vps-check-peer.sh` (read-only) verifies
    `added`/`removed` against a pre-change snapshot and reports COMPLETE,
    INCOMPLETE (persisted-only / live-only) or ABSENT. Docker `test` target
    host-only skips: **85** (incl. the `api-compose.sh` wrapper test).
  - **Approval 2 first attempt stopped safely (nothing changed):** `wg-quick strip`
    was denied READ access to the validation copy in `/tmp`
    (`wg-quick: line 40: /tmp/tmp.…/wg0.conf: Permission denied`) while the real
    `/etc/wireguard/wg0.conf` parsed — consistent with `wg-quick` being confined
    to `/etc/wireguard` by a path-based LSM (AppArmor) on the VPS. Fix: validation
    copies are created INSIDE `/etc/wireguard` (`wgchk.XXXXXX.conf`, 0600, removed
    on exit), the installed file is re-checked at its real path (restored on
    failure), `vps-check-peer.sh` checks the real file in place, and wg-quick
    failures are classified: read denied (permission/LSM) vs other, with keys
    redacted from wg-quick's messages. Regression tests run the REAL
    `/usr/bin/wg-quick` parser (copy with only the root re-exec disabled) under a
    real kernel Landlock restriction (read only the WireGuard dir): the old `/tmp`
    copy reproduces the exact `line 40 … Permission denied` failure; the fixed
    scripts pass add → check → remove.
- **A11 (Stage 2).** Legacy live-server tests (`backend/tests/`, never run
  against production, spec 004 A7) updated for the removed suggestion route and
  hashed sidebar keys.

## Findings

Severity reflects impact once the API is public. Priority order: **account
takeover > cross-tenant exposure > unauthorized conversation access > abuse/cost >
information disclosure**.

### Summary

| ID | Severity | Class | Title | Stage |
|---|---|---|---|---|
| P2 (S2) | **Critical** | Account takeover | Brute force of 6-digit verification and reset codes | 3 |
| N5 | High | Account takeover (persistence) | Password reset does not revoke existing sessions | 3 |
| N6 | Medium | Account takeover (weak credentials) | 6-character passwords; plaintext codes at rest | 3 |
| N2 | **High** | Cross-tenant exposure | Login response returns every tenant's bots | 2 |
| P4 | **High** | Cross-tenant exposure | Unauthenticated Zendesk AI suggestions | 2 |
| P5 | Medium-High | Cross-tenant integrity | Discord OAuth `state` is a raw instance ID (no CSRF protection) | 6 |
| P3 | **High** | Unauthorized conversation access | Unauthenticated chat history with guessable Discord session IDs | 4 |
| N1 | **High** | Unauthorized conversation access | Client-chosen chat `session_id` allows session injection | 4 |
| N3 | High | Abuse / cost | Public chat has no rate limit; falls back to the first bot | 4 |
| P1 (S1) | Medium-High | Account enumeration | Enumeration via auth flows, provider outages and timing; no login rate limit | 5 |
| P7 | Medium | Public surface | Webhook secrets compared non-constant-time; no signature verification; no body limits | 2, 7 |
| N4 | Medium | Information disclosure | `/docs`, `/redoc`, `/openapi.json` publicly served | 2 |
| P6 | Medium | Integrity / phishing surface | Invitation links point at the API domain; `frontend/.env` fallbacks | 1, 6 |
| P8 (A6) | Medium | Policy | Open self-registration with 6-character passwords | 8 |
| N7 | High (functional + security) | Configuration | Frontend calls relative `/api`, inconsistent with `REACT_APP_BACKEND_URL` | 9 |
| P9 (E5) | Medium | Configuration | Embed iframe URL built from the API origin; sidebar blocked by CORS | 9 |
| L1 | Low | Logging | Raw `X-Instance-ID` header and raw OAuth `error_description` logged | 2, 6 |

### P1. Account enumeration and auth rate limiting (spec 004 S1)

- **Endpoints:** `POST /api/auth/register`, `/auth/resend-code`, `/auth/verify`,
  `/auth/login`, `/auth/forgot-password` (`backend/server.py` ~2599–2770).
- **Observed:**
  - Register: `400 "Email already registered"` / `"Username already taken"`.
  - Resend-code: different messages for unverified vs. unknown accounts; provider
    outage returns 503 only for existing unverified accounts.
  - Forgot-password: generic message, but 503 during an outage only for existing
    verified accounts.
  - Verify: `404 "User not found"` vs. `200 "Email already verified"` vs. `400`.
  - Login: bcrypt runs only when the user exists (timing difference). No attempt
    limit.
- **Requirements:**
  - **R-P1.1** Identical status code and body for existing, non-existing,
    unverified and verified accounts on register, resend-code, verify and
    forgot-password.
  - **R-P1.2** Per-message email delivery runs asynchronously (background task
    with bounded retry) so a provider failure for one account cannot change the
    synchronous response. A **global** 503 is allowed only when no provider is
    configured at all (uniform for every caller; compatible with spec 004 M5).
  - **R-P1.3** Login performs a bcrypt verification against a fixed dummy hash
    when the user does not exist. All five endpoints apply a minimum response
    time floor (e.g. 400 ms) with jitter.
  - **R-P1.4** Login rate limits per client IP and per target account (see
    [Rate limiting](#rate-limiting-requirements)), returning a uniform `429`
    with `Retry-After`.
  - **R-P1.5** Registration under invite-only (DD1) returns one uniform response
    for every non-invited request.
- **Acceptance:** an automated response matrix (account state × provider
  up/down) shows byte-identical bodies and identical status codes; the timing
  test shows no statistically significant difference (median difference below
  the jitter range over N≥200 requests per class, run offline against the app);
  the 6th failed login within the window returns 429.

### P2. Brute force of verification and reset codes (spec 004 S2) — Critical

- **Endpoints:** `POST /api/auth/verify`, `POST /api/auth/reset-password`
  (`server.py` ~2649, ~2770). Codes are 6 digits, valid 15 minutes, compared
  with `!=`, stored in plaintext, with **no attempt limit**.
- **Impact:** guessing a reset code within its lifetime is feasible → **account
  takeover**, including of a superadmin.
- **Requirements:**
  - **R-P2.1** Per-code attempt counter: the code is invalidated after **5**
    failed attempts; further attempts return the same generic error even with
    the correct code.
  - **R-P2.2** Per-IP and per-account rate limits on verify and reset.
  - **R-P2.3** Codes are stored only as a keyed hash (HMAC-SHA256 with a
    server-side key) and compared with `hmac.compare_digest`.
  - **R-P2.4** Issuing a new code replaces the old one and resets its counter;
    at most 3 codes per account per hour.
  - **R-P2.5** Successful reset increments a per-user `token_version`; JWTs carry
    it and `get_current_user` rejects stale versions (also fixes N5).
  - **R-P2.6** A successful reset sends a "your password was changed" notice
    (asynchronous, per R-P1.2).
- **Acceptance:** 6th attempt with the correct code fails; a fresh code works and
  resets the counter; no plaintext code exists in MongoDB after issuance; a JWT
  issued before reset returns 401 after reset; rate-limit responses are uniform.

### N5. Password reset does not revoke sessions

- **Where:** `reset_password` (`server.py` ~2770), `create_access_token`,
  `get_current_user` (`server.py` ~345). Tokens last 7 days.
- **Requirement:** covered by R-P2.5; also applied to admin-initiated password
  reset and password change.
- **Acceptance:** as R-P2.5, for all three password-changing paths.

### N6. Weak passwords and plaintext codes

- **Where:** `register` and `reset_password` accept ≥6 characters; codes stored
  in plaintext.
- **Requirements:**
  - **R-N6.1** A user password policy for register, reset, change-password and
    invite acceptance: minimum 12 characters, maximum 72 bytes (bcrypt), the
    `password_policy.py` denylist and email-part checks. (The 16-character
    bootstrap policy, spec 004 C6, stays for the superadmin.)
  - **R-N6.2** Plaintext codes removed (R-P2.3).
- **Acceptance:** policy unit tests; endpoint tests reject a weak password with a
  generic validation message.

### N2. Login returns every tenant's bots — Cross-tenant

- **Where:** `login` (`server.py` ~2737): `db.bots.find({})` is returned to every
  user who logs in.
- **Requirement R-N2.1:** the login response includes only bots in organisations
  the user belongs to (same scoping as `/api/v2/orgs`); superadmins get the same
  scoped result plus nothing extra at login.
- **Acceptance:** with two tenants, a user of tenant A receives no ID, name or
  org ID from tenant B; a regression test enforces it.

### P4. Unauthenticated Zendesk AI suggestions — Cross-tenant

- **Endpoint:** `GET /api/zendesk/suggestion/{instance_id}/{ticket_id}`
  (`zendesk_routes.py:919`). No authentication. Instance IDs are public (embed
  snippet); ticket IDs are sequential.
- **Dependency check so far (2026-11-17):** no caller in `frontend/src` or
  `backend/zendesk_sidebar_app` (the sidebar uses only
  `/api/zendesk/sidebar/generate`); git history has no other caller; this
  deployment has never had public traffic.
- **Requirements (DD3):**
  - **R-P4.1** Before removal, a final check: repository search, the sidebar
    app's shipped zip contents, and the API access log for any request to the
    route. If a legitimate client is found, the fallback is to require
    `X-Sidebar-Key` and verify that the key's instance equals `instance_id`.
  - **R-P4.2** Remove the route.
  - **R-P4.3** Sidebar keys are stored as a keyed hash and looked up by hash;
    comparison is constant-time.
- **Acceptance:** route returns 404; the public-route inventory test (R-P7.5)
  no longer lists it; sidebar generate still works with a valid key and rejects
  an invalid or other-instance key.

### P5. Discord OAuth state validation

- **Endpoints:** `GET /api/discord/oauth-url`, `GET /api/discord/share-oauth-url`
  (issue `state = instance_id` / `share:<instance_id>`),
  `GET /api/discord/callback` (public; saves `discord_config` for the `state`
  instance).
- **Impact:** an attacker can complete an OAuth flow with a forged `state` and
  bind a Discord guild to another tenant's instance (login CSRF / account
  linking).
- **Requirements:**
  - **R-P5.1** `state` is a 256-bit random value, stored server-side with the
    issuing user ID, instance ID, mode (normal/share) and a 10-minute expiry
    (MongoDB TTL collection).
  - **R-P5.2** The callback validates and **consumes** the state atomically
    (`find_one_and_delete`) before any write; missing, unknown, expired, reused
    or mismatched state → generic error redirect, no write.
  - **R-P5.3** Redirects go only to `PUBLIC_APP_URL` paths (P6).
  - **R-P5.4** `error` and `error_description` are not logged verbatim
    (length-capped, control characters stripped) (L1).
  - **R-P5.5** In `APP_MODE=api`, the existing no-bot-start guard stays.
- **Acceptance:** raw instance ID as state rejected; reused state rejected;
  expired state rejected; valid state writes exactly once; redirect host is
  always `PUBLIC_APP_URL`.

### P3 + N1. Chat history and chat session integrity

- **Endpoints:** `GET /api/chat/history/{session_id}` (public, returns the full
  conversation for any session ID); `POST /api/chat/send` and
  `/api/chat/stream` (public; accept a client-supplied `session_id`).
- **Impact:**
  - Discord session IDs are `discord_<discord_user_id>` or
    `discord_<user_id>_<conn8>` (`server.py` ~2018, ~2320); Discord user IDs are
    public → anyone can read Discord conversations (**P3**).
  - A client can send `session_id=discord_<id>` (or another web session's ID) to
    write into that conversation and the LLM session memory (**N1**).
- **Requirements:**
  - **R-P3.1** The server issues every web chat session: a random 128-bit ID plus
    a **session token** = HMAC-SHA256(server key, session_id ‖ instance_id ‖
    expiry), returned on the first message.
  - **R-P3.2** `chat/send` and `chat/stream` accept a `session_id` only with a
    valid, unexpired token for the same instance; otherwise they start a new
    session. Reserved prefixes (`discord_`, `telegram_`, `zendesk_`, and any
    non-web platform prefix) are always refused.
  - **R-P3.3** `chat/history` requires the token (header, not URL), filters by
    `session_id` **and** `instance_id`, and returns `404` for every failure
    (no distinction between missing and forbidden).
  - **R-P3.4** Conversations for non-web platforms are never returned by any
    public endpoint.
  - **R-P3.5** Token key is a dedicated secret (`chat_session_key`, Docker
    secret, `*_FILE` loader) — not reused from `JWT_SECRET`.
- **Coordinated deployment (mandatory):** the widget (`ChatWidget.js`) and
  `ChatPage.js` currently call `chat/history/{session_id}` without a token. The
  backend change (stage 4) and the frontend change (stage 9a) **must ship in the
  same release window**: frontend first with backward-compatible token handling
  (sends the token when present), then backend enforcement. No public route
  exists between them, so there is no exposure window. Acceptance includes an
  end-to-end widget check.
- **Acceptance:** a guessed `discord_*` ID returns 404; a forged, expired or
  other-instance token is rejected; an injected reserved-prefix `session_id`
  starts a new session and never touches the target conversation; the widget
  restores history after reload using its token.

### N3. Public chat abuse (cost)

- **Endpoints:** `chat/send`, `chat/stream`; `_resolve_chat_instance` falls back
  to the first bot when `instance_id` is missing or `"default"`.
- **Requirements:**
  - **R-N3.1** Rate limits per client IP and per instance (see below); uniform
    `429`.
  - **R-N3.2** Message length cap (e.g. 4,000 characters) and request body cap.
  - **R-N3.3** In production, `instance_id` is required and must reference an
    existing, enabled bot; no first-bot fallback.
  - **R-N3.4** Per-instance daily message ceiling (configurable), after which
    chat returns a generic "temporarily unavailable" message without calling the
    LLM.
- **Acceptance:** limits trigger at their thresholds; missing `instance_id`
  returns 400/404 without an LLM call; oversized messages return 413/422.

### P7. Webhooks and other public endpoints

- **Endpoints:**
  - `POST /api/telegram/webhook/{instance_id}/{webhook_secret}` — secret compared
    with `!=`.
  - `POST /api/zendesk/webhook/{instance_id}/{webhook_secret}` — secret compared
    with `!=`; no Zendesk signature check.
  - `POST /api/zendesk/sidebar/generate` — `X-Sidebar-Key` looked up in plaintext.
  - Public reads: `GET /api/bot-info/{bot_id}`, `/api/embed/config/{instance_id}`,
    `/api/bot-logo/{instance_id}`, `/api/embed.js`, `/api/invite/{token}`.
- **Requirements:**
  - **R-P7.1** All secret comparisons use `hmac.compare_digest`; unknown
    instance and wrong secret return the **same** status (404).
  - **R-P7.2** Telegram: register the webhook with `secret_token` and require the
    `X-Telegram-Bot-Api-Secret-Token` header (path secret kept only during
    migration, then removed).
  - **R-P7.3** Zendesk: verify `X-Zendesk-Webhook-Signature` (HMAC-SHA256 over
    timestamp + body with the webhook signing secret) and reject timestamps
    older than 5 minutes; path secret kept only during migration.
  - **R-P7.4** Request body size limits for all public POST routes (e.g. 256 KiB
    for webhooks).
  - **R-P7.5** **Public-route inventory test:** enumerate the FastAPI route table
    in-process; every route that is reachable without authentication must appear
    on an explicit allowlist in the test with a justification. Adding a public
    route without updating the allowlist fails CI.
  - **R-P7.6** Field review of public read responses: return only what the widget
    needs (no org IDs, no config secrets, no internal flags). Documented field
    lists, enforced by tests.
  - **R-P7.7** `GET /api/invite/{token}` is rate limited per IP; tokens are ≥128
    bits (verify); responses for unknown/used/expired tokens are uniform.
- **Acceptance:** wrong-secret and unknown-instance responses identical; Telegram
  and Zendesk requests without valid header/signature rejected; oversize body
  413; inventory test passes with the agreed allowlist.

### N4. Public API documentation

- **Where:** `app = FastAPI()` (`server.py:128`) serves `/docs`, `/redoc`,
  `/openapi.json` (outside `/api`).
- **Requirement R-N4.1:** `docs_url`, `redoc_url` and `openapi_url` are `None`
  when `APP_ENV=production`.
- **Acceptance:** all three return 404 in production; available in development.

### P6. Invitation links and public URLs

- **Where:** `send_invitation_email` (`server.py` ~3041),
  `zendesk_routes._get_base_url` (~105), `telegram_routes._set_webhook` (~77);
  all use `REACT_APP_BACKEND_URL` (= `https://api.bitbotpro.io`) with a fallback
  that reads `../frontend/.env`.
- **Requirements:**
  - **R-P6.1** `PUBLIC_APP_URL` (user-facing pages: invitations, email links,
    OAuth landing pages, widget iframe) and `PUBLIC_API_URL` (webhooks, OAuth
    callbacks), validated in `runtime_config.py`: HTTPS origin only in
    production, no path, no trailing slash; startup fails otherwise.
  - **R-P6.2** Remove all three `frontend/.env` fallback reads.
  - **R-P6.3** Backend stops using `REACT_APP_BACKEND_URL`.
- **Acceptance:** invitation links start with `https://bitbotpro.io/invite/`;
  webhook URLs start with `https://api.bitbotpro.io/api/`; an HTTP value fails at
  import in production; an AST test confirms no backend module reads
  `frontend/.env`.

### P8. Registration policy (spec 004 A6) — DD1 invite-only

- **Endpoints:** `POST /api/auth/register`; invite acceptance
  `POST /api/invite/{token}/accept`.
- **Requirements:**
  - **R-P8.1** `REGISTRATION_MODE` ∈ {`invite_only`, `open`}; production default
    and launch value `invite_only`; validated at startup.
  - **R-P8.2** In `invite_only`, `register` creates nothing and returns the
    uniform response of R-P1.5; account creation happens only through invite
    acceptance (password policy R-N6.1 applies).
  - **R-P8.3** Invite acceptance binds the account email to the invited email.
  - **R-P8.4** `open` mode is not allowed in production until S1 (P1), CAPTCHA and
    abuse controls are specified; startup refuses `open` in production in this
    spec.
- **Acceptance:** in `invite_only`, register writes nothing (DB unchanged) and
  returns the uniform body; invite acceptance works and enforces the policy and
  the invited email.

### N7 + P9. Frontend, embed widget and Zendesk sidebar configuration

#### N7. Frontend API base URL

- **Where:** `frontend/src/utils/api.js` uses `baseURL: "/api"` (relative to
  `bitbotpro.io`); several pages call `${REACT_APP_BACKEND_URL}/api/...` directly
  (`Users.js`, `Profile.js`, `KnowledgeBase.js`, `ChatWidget.js`, `ChatPage.js`).
- **Requirements:**
  - **R-N7.1** One API base (`REACT_APP_API_BASE_URL=https://api.bitbotpro.io`),
    used by the shared axios instance; direct `fetch`/`axios` calls go through it.
  - **R-N7.2** Bearer-token auth stays primary; cookie auth across
    `bitbotpro.io` → `api.bitbotpro.io` is same-site (`SameSite=Lax`,
    `Secure`) and requires `withCredentials` only where cookies are used.
    Any state-changing request authenticated by cookie alone must carry a
    custom header (CSRF defence), or cookie auth is restricted to GET.
  - **R-N7.3** CORS stays exactly `https://bitbotpro.io` (spec 004 R3).

#### P9. Embed widget (spec 004 E5) and Zendesk sidebar

- **Where:** `embed.js` (`server.py` ~4214) builds the iframe URL from
  `request.base_url` (the API origin, and `http` behind a proxy); `EmbedCode.js`;
  `zendesk_sidebar_app/assets/main.js` calls the API directly from the Zendesk
  iframe, which CORS blocks.
- **Requirements:**
  - **R-P9.1** Widget iframe URL = `PUBLIC_APP_URL/widget`.
  - **R-P9.2** Per-instance list of allowed embedding origins; the widget page is
    served with `Content-Security-Policy: frame-ancestors <list>`, and
    `embed/config` refuses unknown origins.
  - **R-P9.3** Widget stores and sends the chat session token (R-P3.1) — the
    coordinated stage 9a.
  - **R-P9.4** (DD4) The sidebar app uses `client.request` (Zendesk proxy) with the
    sidebar key as a secure setting (`{{setting.…}}` placeholder, never readable
    by agent-side JavaScript). Before implementation: verify secure-setting
    support for this app type, the `X-Sidebar-Key` header placement, request
    and response size limits, and streaming incompatibility. If verification
    fails, fallback: CORS allowance for the Zendesk origins **only** on
    `/api/zendesk/sidebar/*`, with no credentials.
- **Acceptance:** embed script output uses `https://bitbotpro.io/widget`;
  framing from an unlisted origin is blocked; sidebar generate works through
  `client.request` in a Zendesk sandbox; global CORS unchanged.

### L1. Log injection

- **Where:** `zendesk_routes.py:505` logs the raw `X-Instance-ID` header; the
  Discord callback logs `error_description` verbatim.
- **Requirement R-L1.1:** request-derived values are length-capped and stripped
  of control characters before logging; a test asserts a CR/LF payload produces a
  single log line.

---

## Cross-cutting requirements

### Client-IP handling (anti-spoofing)

Rate limits keyed by client IP are worthless if a client can choose its IP.

- **R-IP.1** The application never reads `X-Forwarded-For`, `X-Real-IP`,
  `Forwarded` or `CF-Connecting-IP` directly. A single helper,
  `client_ip(request)`, is the only source of a client IP.
- **R-IP.2** `client_ip` trusts `CF-Connecting-IP` **only** when the immediate
  TCP peer (`request.client.host`, as seen by uvicorn **without** proxy-header
  rewriting) is in `TRUSTED_PROXY_IPS` — the `cloudflared` container's fixed
  address on a dedicated internal network. Otherwise it returns the TCP peer.
- **R-IP.3** uvicorn runs with `--no-proxy-headers` (or
  `--forwarded-allow-ips` set to exactly the cloudflared address, never `*`),
  so `request.client` is the real peer. **This must be in the runtime image
  before any route uses `client_ip()`** (uvicorn's default rewrites
  `request.client` from `X-Forwarded-For` for loopback peers).
- **R-IP.4** `cloudflared` reaches the API over a dedicated network with a fixed
  subnet and static address; no other container joins it. The API's
  `bitbotpro_egress` network is not used for ingress.
- **R-IP.5** The header value is validated as a single IPv4/IPv6 address; IPv6
  clients are bucketed by /64.
- **R-IP.6** Without the Tunnel (today), every request's IP is the internal peer,
  which is correct: limits apply per peer.
- **Acceptance:** a request from an untrusted peer with forged
  `CF-Connecting-IP`/`X-Forwarded-For` is keyed by its real peer; a request from
  the trusted address uses the header; malformed headers fall back to the peer;
  an AST test forbids reading those headers outside `client_ip`.

### Rate limiting requirements

- **R-RL.1** Two layers:
  - **Per-account and per-code counters** in MongoDB (TTL-indexed collection
    `security_counters`), so lockouts survive restarts. Every update is a single
    atomic document update, and expiry is checked explicitly against
    `expires_at` on every read and update; correctness never depends on when
    the TTL monitor deletes documents.
  - **Per-IP sliding-window limiter** in process memory (valid because the API
    runs one worker, spec 004 F3). If workers or replicas are ever added, it
    moves to a shared store; the spec 004 F3 constraint is restated here.
- **R-RL.2** Initial limits (configurable, tuned in testing):

  | Route | Per IP | Per account / target |
  |---|---|---|
  | login | 10 / 5 min | 5 failures / 15 min, then 15-min lock |
  | verify, reset-password | 10 / 15 min | 5 failures per code (code invalidated) |
  | register, resend-code, forgot-password | 5 / hour | 3 / hour per email |
  | invite lookup/accept | 20 / hour | — |
  | chat send/stream | 20 / min | per-instance daily ceiling |
  | chat history | 60 / min | — |
  | webhooks | 120 / min per instance | — |

- **R-RL.3** Limit responses are uniform (`429`, generic body, `Retry-After`) and
  never reveal whether the account exists; account-keyed limits are keyed by a
  hash of the normalised email, including for non-existent accounts.
- **R-RL.4** Lockouts never block the legitimate path entirely: password reset
  remains possible after a login lock (subject to its own limits).
- **R-RL.5** No new third-party dependency without approval (spec 004 D5); an
  in-house limiter is preferred.
- **R-RL.6** Cloudflare WAF/rate-limit rules are added as defence in depth at the
  Tunnel stage, never as the only control.
- **R-RL.7** **Indexes are created only by an explicit administrative command,
  never automatically at application startup** (consistent with spec 004 R1/C7).
  This applies to the `security_counters` TTL index and to every other index or
  collection this spec introduces (e.g. `oauth_states`). The command
  (`manage.py security-indexes`) is a dry run by default, requires
  `--apply --confirm`, creates only the specified index, writes no documents,
  is idempotent, and refuses to alter an existing index that differs from the
  specification. Running it against the production database is a separate
  approval gate. Code that uses a counter collection must behave correctly
  (explicit expiry checks) even before the index exists.

### Secrets introduced by this spec

`code_hmac_key`, `chat_session_key`, `oauth_state` storage (no key; random
values), sidebar-key hashing key — delivered as Docker secrets via the existing
`*_FILE` loader allowlist, generated by an extension of
`deploy/scripts/generate-api-secrets.sh` (never overwrite, never print). R8
scans cover them. Generation and rollout are their own gates.

---

## Staged implementation plan

Each stage: spec-conformant change → offline unit tests → Docker `test` target
build (0 failures, 0 errors; only host-only deployment-file skips, see A9) → **separate approval** →
runtime image build → R8 scan → container recreate → private smoke tests.

| Stage | Scope | Findings | Depends on | Touches frontend |
|---|---|---|---|---|
| **0** | This spec | — | — | No |
| **1** | Foundations: `client_ip()` helper (R-IP.1–.6, without Tunnel config), rate-limit module, `security_counters` counters and the admin-only TTL index command (R-RL.1, .3, .5, .7), `PUBLIC_APP_URL`/`PUBLIC_API_URL` validation (R-P6.1), log-sanitising helper (R-L1.1). **No endpoint behaviour change.** | foundation, P6, L1 | 0 | No |
| **2** | Quick containment: remove Zendesk suggestion route after the final dependency check (P4), disable docs in production (N4), scope login bots (N2), constant-time webhook/sidebar secret checks + uniform 404 (R-P7.1), sidebar key hashing (R-P4.3), sanitise the two log sites (L1) | P4, N4, N2, P7 (part), L1 | 1 | No |
| **3** | Account takeover: hashed codes, attempt counters, verify/reset limits, `token_version` revocation, user password policy, password-changed notice | P2, N5, N6 | 1 | No |
| **3b** | Authorization scoping for every `require_admin_or_ws_admin` endpoint (N8: invitations, invitation list/revoke, Discord app config, purge, API keys, role changes, delete/reset users) — implemented | N8 | 3 | No (frontend follow-up in 9b) |
| **4** | Conversations: server-issued sessions + tokens, history protection, reserved prefixes, chat rate limits, size caps, no first-bot fallback | P3, N1, N3 | 1; **released with 9a** | No (backend) |
| **9a** | Widget + ChatPage token handling (backward compatible), released **before** stage 4 enforcement in the same window | P3, P9 (R-P9.3) | 4 (API contract) | **Yes** |
| **5** | Enumeration: uniform responses, async email with retry, dummy bcrypt, response floor, login limits | P1 | 3 | No |
| **6** | Discord OAuth state; invitation/email/OAuth URLs from `PUBLIC_APP_URL`; remove `frontend/.env` fallbacks | P5, P6 | 1 | No |
| **7** | Telegram secret header, Zendesk signature verification, body limits, public-response field review, public-route inventory test, invite lookup limits | P7 | 1, 2 | No |
| **8** | `REGISTRATION_MODE=invite_only`, invite email binding | P8 | 5 | Possibly (hide sign-up) |
| **9b** | Frontend API base URL migration, CSRF header rule, embed iframe from `PUBLIC_APP_URL`, `frame-ancestors`, Zendesk sidebar via `client.request` (after verification) | N7, P9 | 6, 7 | **Yes** |
| **10** | Pre-Tunnel: cloudflared network and trusted-proxy config, security headers, final gate, then Tunnel | proxy, G-final | all | No |

Priority rationale: stage 3 (account takeover) and stage 2 (cross-tenant leaks)
come first after the foundations; stage 4 + 9a (conversation access) next.
Stage 2 is placed before 3 only because it is small, independent and closes two
High cross-tenant leaks immediately; the two may be swapped on request.

---

## Regression tests (offline, `backend/tests/unit/`)

| Area | Test file (new) | Key assertions |
|---|---|---|
| Client IP | `test_client_ip.py` | Untrusted peer + forged headers → peer; trusted peer → header; malformed → peer; IPv6 /64; AST guard on header reads |
| Rate limiting | `test_rate_limit.py` | Windows, uniform 429, `Retry-After`, TTL documents, non-existent accounts keyed identically |
| Codes | `test_auth_codes.py` | 5-attempt invalidation, hash at rest, constant-time compare, rotation, per-hour cap |
| Sessions | `test_token_version.py` | Revocation after reset/change/admin reset |
| Password policy | `test_password_policy.py` (extend) | User policy vs. bootstrap policy |
| Enumeration | `test_enumeration.py` | Response matrix (state × provider), timing floor |
| Chat | `test_chat_sessions.py` | Token issue/verify, other-instance rejection, reserved prefixes, history 404 uniformity, caps, no fallback |
| Tenancy | `test_login_scoping.py` | Login bots scoped to the user's orgs |
| OAuth | `test_discord_oauth_state.py` | Forged/reused/expired state rejected before any write |
| URLs | `test_public_urls.py` | URL validation, invite/webhook URL composition, no `frontend/.env` reads |
| Webhooks | `test_webhooks.py` | Constant-time, uniform 404, Telegram header, Zendesk signature + timestamp, body limit |
| Public surface | `test_public_routes.py` | Route-table inventory vs. allowlist; docs disabled in production; public field lists |
| Registration | `test_registration_mode.py` | Invite-only: no writes, uniform body; startup refuses `open` in production |
| Logging | `test_log_redaction.py` (extend) | CR/LF injection, length caps |

Existing suites (spec 004: 123 tests) must keep passing. The FastAPI tests run
in the Docker `test` target. Frontend changes add Jest tests for the API base
URL and the widget token handling.

---

## Security verification gates

### G-stage (every stage)

1. Offline unit tests pass; Docker `test` target: 0 failures, 0 errors (only the
   host-only deployment-file tests may be skipped; see A9).
2. Static guards pass (AST tests, public-route inventory once it exists).
3. Runtime image built under a new tag; R8 scan PASS (image, logs, compose,
   running container).
4. Private smoke tests for the stage's endpoints, read-only where possible;
   any write tests use a disposable tenant created only with explicit approval.
5. Rollback path documented (previous image tag kept).

### G-final (before Cloudflare Tunnel)

All of the following must pass; any failure keeps public access disabled.

1. Every **mandatory** requirement in this spec has passed its acceptance
   criteria: P1, P2, N2, N3, N4, N5, N6, N8, N1/P3, P4, P5, P6, P7, P8, N7, P9 and the
   IP/rate-limit requirements. (L1 is mandatory too; all requirements here are
   mandatory unless marked optional.)
2. Spec 004 blockers resolved or explicitly deferred in writing: D9 (KB stays
   503 until the PostgreSQL spec), E2/E3 (Emergent replacement spec or a
   documented launch decision), D10 (`www`).
3. Superadmin bootstrapped via the controlled spec 004 process; Phase 2b login
   tests pass; `admin_password` file deleted.
4. Resend configured with SPF/DKIM on the sending domain, or email-dependent
   flows documented as unavailable (invite-only requires invitation email).
5. MongoDB backup taken and a test restore verified.
6. Security headers present (HSTS, `X-Content-Type-Options: nosniff`,
   `Referrer-Policy`, `frame-ancestors` per route).
7. Tunnel configuration reviewed: only `api.bitbotpro.io` → API port 8000 on the
   dedicated network; no host ports; `TRUSTED_PROXY_IPS` equals the cloudflared
   address; Cloudflare WAF/rate-limit rules in place.
8. External verification after Tunnel start (separate approval): only 443 via
   Cloudflare is reachable; `/docs` 404; CORS rejects foreign origins; forged
   `CF-Connecting-IP` from outside has no effect; R8 scan rerun on the running
   container.

## Rollback

Every stage ships as a new image tag. Rollback = recreate the API container with
the previous tag (`deploy/.env` `BITBOTPRO_API_TAG`). Database changes are
additive (new collections `security_counters`, `oauth_states`; new user fields
`token_version`, code hashes); none are destructive, and rollback does not need
data migration. Never `down -v`; database volumes are untouched (spec 004 R7).

## Risks

- **Coordinated chat release (stage 4 + 9a):** mitigated by backward-compatible
  frontend first and no public route between the two.
- **Invite-only and email:** invitation needs working email; G-final item 4.
- **Single-worker in-memory limiter:** restated constraint; counters that must
  survive restarts live in MongoDB.
- **Zendesk `client.request` limits:** verified before stage 9b; CORS fallback
  scoped to `/api/zendesk/sidebar/*`.
- **Behaviour changes for existing frontend flows** (uniform registration
  messages, session tokens): frontend copy and error handling updated in 9a/9b.

## Files expected to change (by later stages; none in stage 0)

Backend: `server.py`, `runtime_config.py`, `password_policy.py`,
`email_delivery.py`, `log_redaction.py`, `telegram_routes.py`,
`zendesk_routes.py`, `docker-entrypoint.sh` (secret allowlist), new
`client_ip.py`/`rate_limit.py`, `tests/unit/*`. Frontend: `src/utils/api.js`,
`ChatWidget.js`, `ChatPage.js`, `EmbedCode.js`, pages calling
`REACT_APP_BACKEND_URL`, `zendesk_sidebar_app/assets/main.js`. Deploy:
`docker-compose.api.yml`, `generate-api-secrets.sh`, later a cloudflared compose
file. Spec 004 `implementation.md` gets a pointer to this spec.
