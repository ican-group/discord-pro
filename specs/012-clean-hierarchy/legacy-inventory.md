# Spec 012 — Legacy cleanup inventory (Phase 6B)

Inventory of every pre-Spec 012 backend module, frontend page and test suite, taken on 2026-11-19
before anything was removed. Source of truth for the code: commit `1c20d2a` (production image
`bitbotpro-api:spec011-f245d7ed3bdb`), which keeps every removed file for rollback and reference.
Nothing in production changes: the release candidate is local, uncommitted and undeployed.

**Rule applied (spec §14):** a component is removed only if (a) its feature is explicitly retired
or its Spec 012 replacement has a verified parity row, and (b) no Spec 012 module imports it
(checked: `grep` of `api/ core/ runtime/ persistence/ tests/{e2e,contract,knowledge_eval,integration}`
for every root module, 2026-11-19), and (c) no supported workflow of the release candidate depends
on it. Anything that fails a check is **kept** and listed with the reason.

Dispositions: **REMOVED** · **REPLACED** (by the named Spec 012 module; parity row verified) ·
**KEPT** (spec §2 kept module, or still needed) · **RETAINED, NOT MOUNTED** (unported approved
feature: source kept for the port, release blocker) · **DECISION NEEDED** (unlisted feature with
no replacement; proposed disposition stated, not executed silently).

## 1. Backend modules

| Module | Lines | Spec 012 imports | Disposition | Replacement / reason |
|---|---|---|---|---|
| `server.py` (legacy monolith) | 7676 | 0 | **REPLACED** by wiring | Spec §2 "server.py becomes app wiring only": now `api/production.py` + `server.py` (≈20 lines) + `worker.py`. Every legacy route group is accounted for in §2 below |
| `workspace_routes.py` | 524 | 0 | **REMOVED** | Workspace terminology/routes (spec §1.1, §14) → `api/hierarchy.py` (Enterprise/Organisation) |
| `org_routes.py` | 351 | 0 | **REMOVED** | → `api/hierarchy.py`, `api/members.py` |
| `bot_routes.py` | 517 | 0 | **REMOVED** | → `api/hierarchy.py`, `api/config.py` |
| `account_routes.py` | 318 | 0 | **REMOVED** | V2 connected accounts → credentials (`api/credentials.py`) |
| `connection_routes.py` | 379 | 0 | **REMOVED** | V2 connections → Bot Instances / Bot Integrations |
| `webhook_router.py` | 292 | 0 | **REMOVED** | V2 routing + Discord/Telegram suggestions (**D-Q7 retired**) → `runtime/routing.py` |
| `platform_routes.py` | 216 | 0 | **REMOVED** | → `api/hierarchy.py` (platform), `api/users.py`, `api/credentials.py` (platform credentials) |
| `telegram_routes.py` | 592 | 0 | **REMOVED** | → `api/telegram.py`, `runtime/telegram*.py` (T1–T9 verified) |
| `zendesk_routes.py` | 1322 | 0 | **REMOVED** | → `api/integrations.py`, `runtime/zendesk*.py` (Z1–Z18; Z14 analytics deferred, labelled) |
| `api_diagnostics.py`, `api_diagnostics_routes.py` | 1440 | 0 | **REMOVED** | **D-Q12 retired** |
| `tenant_scope.py` | 50 | 0 | **REMOVED** | Legacy `X-Instance-ID` scoping → ancestry from the database (`core/authz.py`) |
| `v2_helpers.py` | 266 | 0 | **REMOVED** | V2 layer |
| `migrate_v2.py`, `migrate_workspaces.py` | 626 | 0 | **REMOVED** | Legacy migrations (spec §14; Phase 7 resets instead of migrating) |
| `manage.py` | 388 | 1 (integration test) | **KEPT**, `legacy-migrations` command **REMOVED** | Spec §14 removes only that command; §15 makes manage.py the home of `reset-application-data` (Phase 7) |
| `api_integrations.py` | 478 | 0 | **RETAINED, NOT MOUNTED** | Spec 011 API integrations: approved to be **ported** (D-Q12 note, spec §11 "not yet ported"). Guard logic + its module-level SSRF tests kept. **Release blocker B-API** |
| `api_integrations_routes.py` | 841 | 0 | **RETAINED, NOT MOUNTED** | Same; depends on the legacy auth/`X-Instance-ID` layer, so it cannot be mounted; kept as the port source |
| `auth_security`, `password_policy`, `rate_limit`, `client_ip`, `security_headers`, `log_redaction`, `email_delivery`, `runtime_config`, `oauth_state`, `webhook_auth`, `outbound_guard`, `security_utils`, `chat_sessions` | — | used | **KEPT** | Spec §2 kept modules (Specs 004–011 protections; unit suites unchanged) |
| `zendesk_sidebar_app/assets` | — | — | **KEPT** | Read by the Spec 012 sidebar ZIP route |
| `docker-entrypoint.sh` | — | — | **KEPT**, extended | `ENCRYPTION_KEYS` added to the `*_FILE` allowlist (key ring, Phase 6B) |

## 2. Legacy `server.py` route groups (120 routes)

| Group (routes) | Disposition | Spec 012 replacement / decision |
|---|---|---|
| `auth/login, logout, me, forgot-password, reset-password, change-password, profile` | REPLACED | `api/auth.py`, `api/users.py` (`/users/me`) |
| `auth/register, verify, resend-code, registration-mode` | **NOT REPLACED — release blocker B-ACCT (decision 6B-C)** | Production runs `REGISTRATION_MODE=invite_only`, so legacy sign-up is invitation-based. **Spec 012 has no account-creation path at all:** `create_user` is called only by the superadmin bootstrap, and `POST /invitations/accept` requires an existing, signed-in, e-mail-verified account. A new person therefore cannot join (the e2e stack seeds its users directly). Proposed design in `DECISIONS.md` 6B-C; not implemented without approval because it adds an unauthenticated account-creation endpoint. The legacy routes are not served by the release candidate either way; the public `/register` page leads to sign-in |
| `knowledge/*` (25 Mongo KB routes, incl. share/import/export/categories) | **REMOVED** | Spec §14 "Mongo KB routes"; → `api/knowledge.py` (K1/K2). Share/import/export/categories are not ported (KB disabled in production since spec 004) |
| `discord/scripted-*` (9) | **REMOVED** | **D-Q6 retired** |
| `discord/app-config`, `oauth-url`, `share-oauth-url`, `send-invite-email`, `callback`, `channels`, `disconnect`, `status`, `sync-name`, `rename-global`, `restart`, `handoff-*`, `config`, `purge-all-connections` | REPLACED | `api/discord.py` (D-Q14; D1–D18). `debug-roles` retired (D17) |
| `admin/api-keys`, `admin/api-key-analytics` (`bbk_` keys) | **REMOVED** | Spec §14 `api_keys`; public chat is per-instance (`api/webchat.py`) |
| `admin/users*` | REPLACED | `api/users.py` (platform users, F2), `api/members.py` |
| `admin/bot-logo*` | REPLACED | Bot logo in MongoDB (D-Q8); Discord avatar superadmin-only (D15) |
| `admin/embed-config`, `embed/config`, `embed.js`, `bot-info`, `bot-logo/{id}` | REPLACED | W1/W3: `api/webchat.py`, `api/webchat_assets.py` |
| `chat/send`, `chat/stream`, `chat/history` | REPLACED | W5/W6: `/api/public/instances/{public_id}/…` |
| `conversations*` | REPLACED | `GET /api/instances/{id}/conversations`, approve (W8) |
| `analytics/*`, `admin/integration-usage-analytics`, `analytics/llm-*` | REPLACED | Usage (F1, `api/usage.py`); Zendesk analytics deferred (Z14, labelled) |
| `onboarding/*` | **REMOVED** | Spec §14 OnboardingWizard; §15 step 9 empty-state onboarding |
| `invite/{token}` | REPLACED | `POST /api/invitations/{lookup,accept}` (tokens in bodies, X9) |
| `admin/changelog`, `changelog` | **DECISION NEEDED (6B-D)** | Not listed in §14 and not ported. Proposed: retire (an admin "what's new" list with no tenant function). Not served by the release candidate; restoring it needs a small Spec 012 route + page. Release blocker until decided |
| `health` | REPLACED | `GET /api/health` (liveness) + `GET /api/ready` (database) in `api/production.py`; Caddy's `health_uri /api/health` keeps working |
| `/` | REMOVED | No root route on the API |

## 3. Frontend (`frontend/src`)

| Component | Disposition | Replacement / reason |
|---|---|---|
| Pages listed in spec §14: InstanceSelect, Instances, Workspaces, OnboardingWizard, DiscordSettings, DiscordAppSetup, ProviderApps, TelegramSettings, ZendeskSettings, IntegrationsPage, ConnectedAccounts, Analytics, ApiKeyAnalytics, ApiDiagnostics, EmbedCode, KnowledgeBase, KnowledgeContentViewer, SetupGuide, Users, WorkspaceSettings, EnterpriseWorkspaces, Organisations, BotList, BotOverview, BotSettings, Dashboard, SuggestionsInbox | **REMOVED** | `src/app12/*` (spec §12) |
| ChatWidget, ChatPage, MarkdownMessage | **REMOVED** (W2 approved, P5-H) | API-hosted widget (W-A). `/widget` and `/chat` show a "this chat window has moved" notice instead of a blank page, so old legacy embeds on customer sites fail visibly (their bots are removed by the Phase 7 reset anyway) |
| Login, Register, VerifyEmail, ForgotPassword, ResetPassword, InviteAccept, Profile, DiscordInvited | **REPLACED** (spec §12 "retained/retargeted") | `/app/login`, `/app/forgot-password`, `/app/reset-password` (the `#token` fragment is preserved by the redirect), `/app/invitations`, `/app/account`, `/oauth/discord/result`. Old paths redirect |
| Conversations | REPLACED | Instance page, Conversations tab |
| ChangelogPage | **DECISION NEEDED (6B-D)** | See §2 |
| ApiIntegrations | **REMOVED from the tree; feature RETAINED (blocker B-API)** | Its data layer (`utils/api.js` with the `X-Instance-ID` interceptor, AuthContext, ApiDiagnostics) is removed by approved items; the port needs a new `app12` page on Bot Integrations `type: api`. The old page stays in git (`1c20d2a`) as the reference |
| AdminLayout, OnboardingWizard, PasswordRequirements, context/AuthContext, hooks used only by them | **REMOVED** | Legacy shell/session (localStorage token, `X-Instance-ID`) |
| `utils/api.js`, `v2api.js`, `chatSession.js`, `streamChat.js`, `roles.js`, `sidebarKey.js` (+ tests, `chatTestHelpers.js`) | **REMOVED** | Legacy API clients; app12 has its own client |
| `utils/apiBase.js`, `utils/passwordPolicy.js` (+ tests, vectors) | **KEPT** | `apiBase` is used by `app12/api/client.js`; `passwordPolicy` mirrors the backend policy (`test_frontend_parity.py`) |
| `components/ui/*`, `lib/utils.js`, `hooks/use-toast.js`, `theme.js`, `BridgeBotLogo` | **KEPT** | Shared primitives / branding |
| LandingPage | **KEPT**, corrected | Sign-up links → sign-in (invitation-only); removed the retired "API key management" and "Emergent Universal Key" claims |
| Docs | **REPLACED** | Rewritten for the Spec 012 hierarchy (no Workspace terminology, no customer-owned Discord application, no templates, no `bbk_` keys) |
| `utils/__tests__/securityGuards.test.js` | **REPLACED** | Spec 012 source guards (no tenant headers, no web storage, no 6-character rule, no legacy routes) |

## 4. Tests

| Suite | Tests | Disposition | Where the protection lives now |
|---|---|---|---|
| `backend/tests/test_*.py` (64 legacy live-server suites, `requests` against `BACKEND_URL`) | — (never in the gate) | **REMOVED** | They exercise only removed legacy routes |
| `unit/test_server_app.py`, `unit/test_server_static.py` | 109 + 66 | **REPLACED** | `unit/test_production_app.py` (wiring: config refusal, health/ready, Host header, CORS, client IP, security headers, no docs, log redaction, legacy DB refused, worker separation, no Emergent/discord.py) + the Spec 012 suites mapped in `security-traceability.md` |
| `unit/test_v2_tenant_isolation.py`, `unit/test_tenant_scope.py` | 32 + 11 | **REPLACED** | `test_api_matrix.py` (route × role × tenant, DB-unchanged), `test_api_security*.py`, `test_core_isolation.py` |
| `unit/test_outbound_routes.py` | 22 | **REPLACED** | Ollama is not offered (platform policy); Zendesk outbound → `test_api_zendesk.py::OutboundGuardTests`; kept guard → `test_outbound_guard.py` |
| `unit/test_public_routes.py` | 1 | **REPLACED** | `test_production_app.py::PublicRouteInventoryTests` (Spec 012 public routes) |
| `unit/test_api_integrations_ssrf.py` | 25 | **KEPT (module level)**; route-level class retired with the unmounted routes | Guarded request, TLS pinning and source guards still run against `api_integrations.py`; route tests must be rebuilt in the B-API port |
| `unit/test_manage.py`, `unit/test_rate_limit.py` | — | **KEPT**; legacy-migration tests removed | — |
| Frontend `v2api`, `chatSession`, `streamChat`, `roles` tests | — | **REMOVED** with their modules | — |

## 5. Explicitly preserved (checked after removal)

Discord staff handoff and passive mode (D11, D12) · Zendesk agent suggestions (Z10, Z14–Z16) · the
API-hosted Webchat widget (W-A) · every Specs 009–011 protection (traceability:
`security-traceability.md`) · audit and deletion behaviour · the D-Q14 invariant and its permanent
regression tests · no production data touched, no destructive migration.
