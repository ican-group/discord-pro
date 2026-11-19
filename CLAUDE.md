# CLAUDE.md

Guidance for Claude Code (claude.ai/code) and every other contributor working in
this repository. **Read this whole file before changing anything.**

## 0. Source of truth

BitBotPro (formerly BridgeBot / BotForge) is being rebuilt under **Spec 012 —
Clean Enterprise Hierarchy**. The repository files below are the durable,
authoritative reference. Do not rely on chat history or automatic conversation
memory; if they disagree with these files, these files win.

| File | Purpose |
|---|---|
| [`specs/012-clean-hierarchy/spec.md`](specs/012-clean-hierarchy/spec.md) | Approved architecture (Phase 1 design §1-18, consolidated approval §19, implementation maps §20+) |
| [`specs/012-clean-hierarchy/integration-parity.md`](specs/012-clean-hierarchy/integration-parity.md) | Discord / Telegram / Webchat / Zendesk feature-parity matrices — no feature may be removed silently |
| [`specs/012-clean-hierarchy/DECISIONS.md`](specs/012-clean-hierarchy/DECISIONS.md) | Approved decisions D-Q1…D-Q13 (+ Strict Knowledge Only clarification), rationale, modules, enforcing tests, open discrepancies |
| [`specs/012-clean-hierarchy/IMPLEMENTATION_STATUS.md`](specs/012-clean-hierarchy/IMPLEMENTATION_STATUS.md) | Which phases are done, accepted, authorised or NOT authorised; known limitations and risks |

**Every session must read all four files before any Spec 012 work.** Check
`IMPLEMENTATION_STATUS.md` for what is authorised and `DECISIONS.md` for
approved decisions and open discrepancies; never treat an open discrepancy as
approved. Check the code against the spec. Update `IMPLEMENTATION_STATUS.md`
when a phase starts, finishes or is reviewed.

## 1. Non-negotiable architectural rules

### 1.1 Definitive hierarchy
- **Platform → Enterprise → Organisation → Bot → Bot Instance.** Platform
  Superadmins exist above Enterprises.
- **Workspace is obsolete.** Do not introduce a parallel Workspace model,
  alias, adapter or compatibility layer.
- Every child has exactly one valid parent. Parent relationships and
  server-assigned ancestry ids are immutable. No orphaned records. No
  cross-tenant access.

### 1.2 Configuration inheritance
- One authoritative configuration registry (`backend/core/config_registry.py`)
  and one effective-configuration resolver (`backend/core/resolver.py`). No other
  code reads `overrides`/`policy` to compute behaviour.
- Sparse overrides only; parent configuration flows down automatically; children
  may override only permitted properties; parent-enforced policies can only
  become stricter; never duplicate effective configuration into child records.

### 1.3 AI knowledge restrictions
- Modes: `general`, `knowledge_preferred`, `strict_knowledge_only`.
- Strict Knowledge Only is a mandatory Enterprise capability. If approved
  evidence is absent, insufficient, contradictory or unavailable, the agent must
  refuse or use its configured fallback. **Never silently fall back to general
  AI knowledge in strict mode.**
- Enforce knowledge restrictions in the backend runtime (`backend/core/knowledge.py`
  contract), not merely through prompts. Require tenant-scoped retrieval and
  evidence validation.
- Never claim a mathematical guarantee of zero hallucinations.
- Phase 4K engine: `backend/runtime/knowledge_store.py` (ingestion, access-filtered retrieval) and
  `backend/core/knowledge.py` (gate, deterministic claim validation). Access filters belong in the
  retrieval query itself; never retrieve broadly and filter afterwards. Claim validation is lexical
  (documented limit), and every channel must go through the shared pipeline.
- PDFs are parsed only inside `backend/runtime/pdf_extract_worker.py`, an isolated, resource-limited child
  process with networking disabled; never parse untrusted documents in the API process.
- Every outbound Zendesk request for an integration must go through the fenced client (Z-G); only cleanup and
  recovery may use an unfenced client.

### 1.4 Essential integrations
- Discord, Telegram, Webchat and Zendesk must remain functional. Discord,
  Telegram and Webchat are **Bot Instance channels**; Zendesk is a **Bot
  Integration**.
- Preserve the approved parity matrix; do not silently remove supported features.
- Explicitly retired: Discord scripted mode; Discord/Telegram reply suggestions;
  legacy API diagnostics. Required: Discord staff handoff and passive mode;
  Zendesk suggestions. Zendesk ticket creation is outside Spec 012.

### 1.4a PERMANENT ARCHITECTURAL INVARIANT — Discord (D-Q14)

**ONE DISCORD APPLICATION PER ENTERPRISE. THE PLATFORM SUPERADMIN EXCLUSIVELY
CONFIGURES AND CONTROLS THAT APPLICATION. ALL ORGANISATIONS WITHIN THE
ENTERPRISE USE THAT APPLICATION TO CONNECT THEIR AUTHORISED DISCORD BOT
INSTANCES. ORGANISATION ADMINS CAN INVITE AND CONNECT THEIR BOTS BUT CANNOT
EDIT THE ENTERPRISE DISCORD APPLICATION.**

- This supersedes the old platform-global Discord application model and must
  never change without explicit authorisation. Never implement a global
  application or Organisation-/Bot-owned applications.
- Keep the permanent regression tests (`backend/tests/unit/test_api_discord.py`
  and the matrix rows): Enterprise-specific ownership, superadmin-only
  configuration and gateway controls, Organisation invite/connect, shared use
  across Organisations, channel-level ownership, shared-server safety,
  cross-Enterprise isolation, secure OAuth and share links, safe disconnect and
  deletion.
- Discord is **not operationally complete** until the real gateway connection
  adapter exists and passes its tests (Phase 4F release blocker, `DECISIONS.md`
  C1).

### 1.5 Credentials
- Platform credentials (Zendesk OAuth client) are separate from
  Enterprise/Organisation credentials; tenant users can never manage them.
- Zendesk is a **Bot Integration** (Enterprise → Organisation → Bot →
  Integration), never a Bot Instance. All Zendesk outbound calls go through the
  kept `outbound_guard`; webhook signatures are mandatory (D-Q10); no ticket
  creation (D-Q11).
- **Discord (D-Q14):** each Enterprise has its own Discord application,
  configured and controlled exclusively by the Platform Superadmin and shared by
  all Organisations in that Enterprise for their authorised Bot Instance
  connections. There is no global Discord application and none per
  Organisation or Bot. Organisation admins may connect and configure their
  instances; they never create, edit or administer the application (token,
  secret, gateway, avatar, OAuth permissions, intents).
- Never expose raw secrets (responses, logs, errors, effective config). Never
  inherit raw credentials — only references.
- Permission to *use* a credential is separate from permission to *manage* it.
  Credential access is tenant-scoped and fails closed.

### 1.6 Authentication and permissions
- One authentication model; one authoritative `is_platform_superadmin` field.
- Roles: Owner (Enterprise only), Admin, Editor, Viewer; permissions inherit
  downward; no privilege escalation.
- **No client-supplied header establishes authority** (no `X-Instance-ID`,
  `X-Org-ID`, `X-Workspace-ID`, `X-Enterprise-ID`). Authority comes from the
  session and the database only.
- Protect the last Platform Superadmin and the last Enterprise Owner.

### 1.7 Lifecycle
- States: Active, Suspended, Archived, Deleted. An object is available only if
  it and every ancestor are Active (computed, never copied to children).
- Parent suspension immediately disables descendants; resuming a parent never
  overrides a child's own suspension.

### 1.8 Permanent deletion
- Archive and permanent deletion for Enterprises, Organisations, Bots, Bot
  Instances and Users. Deletion is authorised, previewed, explicitly confirmed,
  tenant-scoped, auditable and idempotent; descendants are deleted safely
  (descendant-first); unrelated tenant data is never touched.

### 1.9 Unified runtime
- One runtime pipeline; Discord, Telegram and Webchat are channel adapters;
  Zendesk is an integration adapter. Every runtime path enforces tenant
  isolation, permissions, lifecycle availability, effective configuration,
  knowledge restrictions, credential scope and usage quotas.

### 1.10 Security
- Preserve every protection from Specs 009-011. Never weaken tenant isolation,
  SSRF protection (`outbound_guard`), Zendesk hostname validation, DNS-rebinding
  protection, redirect restrictions, credential security, webhook validation or
  platform-credential ownership.

### 1.11 Infrastructure
- Production runs on **BBP1**; the public API (`api.bitbotpro.io`) is exposed
  through the existing VPS + Caddy + WireGuard arrangement.
- **Gridscan is a separate service and must never be modified or disrupted.**
- Do not alter production infrastructure (VPS, Caddy, WireGuard, DNS, containers,
  volumes, databases) without explicit approval. Never delete Docker volumes. Do
  not touch PostgreSQL without separate approval.
- Do not reset application data without the separately approved
  destructive-operation procedure (spec §15).
- Never handle or print passwords, API keys or other secrets; the user runs
  authenticated production verification themselves.

### 1.12 Development discipline
- Never silently change approved architecture. Never invent legacy compatibility
  shortcuts. Never remove an approved feature merely to simplify code.
- Never treat passing unit tests as proof of production readiness.
- Never implement a later phase without approval (see `IMPLEMENTATION_STATUS.md`).
- Never deploy, commit, push, restart production containers or reset production
  data without explicit approval for that specific action.
- **If a task conflicts with the approved specification: STOP, explain the
  conflict, propose a specific amendment, and wait for approval.**

## 2. Code layout for Spec 012 (new code)

| Path | Role | Isolation |
|---|---|---|
| `backend/core/` | Pure domain: hierarchy, registry, resolver, lifecycle, authz, credentials, knowledge, memberships, identity, deletion, bindings | stdlib + `cryptography` only; never imports the legacy app |
| `backend/persistence/` | MongoDB store adapter, schemas, indexes | imports `core` + motor/pymongo |
| `backend/api/` | REST API (FastAPI routers, one auth dependency); `api/production.py` = composition root (environment → real adapters), `api/rotate_keys.py` = key rotation command | served by the release candidate; **not deployed** until the Phase 7 cutover |
| `backend/server.py`, `worker.py`, `healthcheck.py` | Wiring only (Phase 6B, spec §2): API process, worker process (D-Q4: Discord gateway, jobs, deliveries), container health check | no routes or logic here |
| `backend/runtime/` | The one runtime pipeline, routing, usage/quotas, workers (adapters and providers are injected) | imports `core` only; not used by the legacy app |
| `backend/tests/unit/test_core_*.py`, `test_api_*.py` | Isolated tests (in-memory store) | run in the Docker `test` target |
| `backend/tests/integration/test_spec012_*.py` | Real-MongoDB tests (disposable mongod only) | skipped unless `MONGO_TEST_URL` is set |

| `frontend/src/app12/` | Spec 012 admin app (Phase 5), mounted at `/app/*` and `/oauth/:provider/result` in `App.js` | own API client (`api/client.js`: cookie session + CSRF header, no web storage, no tenant headers); calls only routes in `contract/routes.json` |
| `backend/tests/e2e/`, `e2e/` | Phase 6A local end-to-end server, real-HTTP journeys, Playwright browser tests | test only; isolated network; never production |
| `backend/tests/knowledge_eval/harness.py` | Knowledge evaluation harness (scripted by default) | live providers gated by env + flag |
| `backend/tests/contract/export_frontend_contract.py` | Exports the route manifest and real response samples into `frontend/src/app12/contract/` | re-run after **any** API change; the frontend refuses routes not in the manifest |

The legacy application was removed in Phase 6B (`specs/012-clean-hierarchy/legacy-inventory.md`;
it stays in git at commit `1c20d2a` and in the production image `bitbotpro-api:spec011-f245d7ed3bdb`).
Root modules other than the three wiring files must not import `core`, `persistence`, `api` or
`runtime`. `api_integrations.py`/`api_integrations_routes.py` (Spec 011) are retained, **not
mounted and not shipped**, until they are ported (release blocker B-API). The runtime image ships
an explicit file list (`Dockerfile`). Tests enforce all of this (`test_core_architecture.py`).

## 3. Commands

### Backend (Python/FastAPI)

```bash
cd backend
# Offline unit tests (host; suites needing FastAPI are skipped if it is absent)
python3 -m unittest discover -s tests/unit -t .
# Full gate with real dependencies (no secrets, nothing deployed)
docker build --target test -t bitbotpro-api-test:<tag> .
```

Real-MongoDB integration tests run only against a disposable `mongod` on an
isolated Docker network (`MONGO_TEST_URL`, database name `bbp_itest*`); never
against `bitbotpro-mongo`.

Release candidate (Phase 6B; local only — never pushed or deployed without approval):

```bash
TREE=$(git write-tree --prefix=backend/)      # with backend/ staged in a temporary index
git archive $TREE | docker build --target runtime --build-arg SOURCE_TREE=$TREE -t bitbotpro-api:spec012-rc<N>-<tree12> -
# API: default command; worker: APP_MODE=worker, command ["python", "worker.py"]
python -m api.rotate_keys {status|verify|rotate}   # key rotation (operations.md §1)
bash deploy/rehearsal/mongo-nofile-rehearsal.sh <test-image> <dir>      # disposable only
bash deploy/rehearsal/backup-restore-rehearsal.sh <test-image> <dir>    # disposable only
```

Gate evidence (complete logs) goes to `specs/012-clean-hierarchy/evidence/`.

### Spec 012 frontend contract (after any API change)

```bash
docker run --rm -u $(id -u):$(id -g) -v "$PWD/backend":/src:ro \
  -v "$PWD/frontend/src/app12/contract":/out -w /src bitbotpro-api-test:<tag> \
  python -m tests.contract.export_frontend_contract /out
```

### Spec 012 local end-to-end stack (Phase 6A; test only)

Real frontend + real API + disposable MongoDB + fake external services, on an
`--internal` Docker network (no live service is reachable). See `spec.md` §31.

```bash
# same-origin frontend build, then (all on network bbp-e2e):
#   mongo:8.0.38-noble (tmpfs)  →  <test image> python -m tests.e2e.server
#     (MONGO_URL=…, DB_NAME=bbp_e2e*, E2E_ORIGIN=http://bbp-e2e-app:8080, FRONTEND_BUILD=/frontend)
#   <test image> python -m unittest tests.e2e.test_e2e_http   (E2E_BASE_URL=…)
#   mcr.microsoft.com/playwright:v1.49.1-noble  npx playwright test   (in e2e/, E2E_BASE_URL=…)
```

- The e2e server refuses any database not named `bbp_e2e*` and the production host.
- Knowledge evaluation harness: `python -m tests.knowledge_eval.harness --mode … --set …`.
  Live providers need `KNOWLEDGE_EVAL_LIVE=1`, the explicit flag and
  `KNOWLEDGE_EVAL_API_KEY`; never run them without approval.
- Jest does not run the React hooks lint; the production build does. Always build.

### Frontend (React)

Node is not installed on the host; use a disposable `node:20.18.1-bookworm-slim`
container with the repository mounted (`-u $(id -u):$(id -g) -e HOME=/tmp`).
Since the Phase 6B legacy removal, `CI=true yarn build` compiles with zero warnings; keep it
that way (CI treats warnings as errors).

```bash
cd frontend
yarn install
yarn start                     # dev server (port 3000)
yarn build                     # production build
yarn test --watchAll=false
```

## 4. Testing protocol

- `test_result.md` is a YAML communication file between main and testing
  agents — do not delete or reformat it.
- New behaviour needs automated tests that exercise behaviour (not word
  searches). Rejected operations must leave the database unchanged.
- Passing tests are necessary, not sufficient, for production readiness.

## 5. Development approach

Spec-driven: write `specs/<NNN>-<slug>/spec.md` before implementing any
non-trivial feature (spec → plan → implement). Specs 002-012 exist under `specs/`.

## 6. Legacy codebase (pre-Spec 012) — removed in Phase 6B

Production still runs the legacy application (`bitbotpro-api:spec011-f245d7ed3bdb`, built from
commit `1c20d2a`). Its source was removed from the working tree in Phase 6B; any hotfix to the
running production version must start from that commit, never from this tree.

- Legacy multi-tenancy used the `X-Instance-ID` header and "workspaces"; both are gone and must
  never return.
- Kept security modules (reused by Spec 012): `auth_security`, `password_policy`,
  `rate_limit`, `client_ip`, `security_headers`, `log_redaction`,
  `email_delivery`, `runtime_config`, `oauth_state`, `webhook_auth`,
  `outbound_guard`, `security_utils`, `chat_sessions` (+ `api_integrations`, retained for the port).
- Frontend (`frontend/src/`): React 19 + React Router, Craco, `@/` → `src/`; the admin app is
  `src/app12/`; public pages `LandingPage` and `Docs`; old paths redirect (`App.js`);
  `components/ui/` (Radix/Shadcn primitives — extend these). Cutover settings:
  `PUBLIC_APP_URL`, `PUBLIC_API_URL`, `CORS_ORIGINS` (`operations.md` §5).
- Design system: `design_guidelines.json` (void black `#030712`, surface
  `#050B14`, panel `#0B132B`, accent `#00F5FF`, brand `#0088FF`; Outfit / IBM
  Plex Sans / JetBrains Mono; bento grid). Match it for new UI.
- `memory/WORKSPACE_ARCHITECTURE.md` and `memory/PRD.md` describe the obsolete
  Workspace architecture; they are historical only.
