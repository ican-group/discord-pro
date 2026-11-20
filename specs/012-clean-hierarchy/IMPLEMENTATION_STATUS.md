# Spec 012 — Implementation Status

Last updated: 2026-11-19. Update this file whenever a phase starts, finishes or
is accepted.

Production runs `bitbotpro-api:spec011-f245d7ed3bdb` on BBP1. No Spec 012 code
is deployed, mounted or committed.

| Phase | Scope | Status |
|---|---|---|
| 1 | Design, inventory, parity matrices (`spec.md` §1–18, `integration-parity.md`) | **Completed and approved** (2026-11-18) |
| 2 | Core foundation (`backend/core/`) | **Completed; provisionally accepted** (2026-11-18) |
| 3 | MongoDB persistence and new REST API (isolated, not mounted) | **Approved** (2026-11-18), including Phase 3.1 |
| 3.1 | Corrections X4, X12, X13, X14 (+ X7 alignment) | **Approved** (2026-11-18) |
| 4A | Authentication, runtime foundation, strict-knowledge enforcement boundary (test doubles), credential scopes (X6), workers (mock adapters) | **Approved** (2026-11-18). The strict-knowledge capability is **not** accepted as complete end to end; that needs 4K |
| 4B | Webchat (channel, public endpoints, widget/embed, sessions, origins, OpenAI/Anthropic adapters, conversations) | **Provisionally accepted** (2026-11-18); W-A–W-D approved; billing-idempotency correction done in 4C Part C. **Parity incomplete:** W2, W8, W9 and W12 are pending frontend work (Phase 5); strict knowledge is incomplete until 4K |
| 4C | Telegram (+ Webchat billing-idempotency correction) | **Implemented locally; awaiting review** (2026-11-18) |
| 4D | Enterprise Discord (D-Q14) + Telegram T-A durable delivery correction | **Provisionally accepted** (2026-11-18); corrections C1–C6 implemented locally, awaiting review. T-A and T-B approved. **Not operationally complete:** the real gateway adapter is a mandatory 4F release blocker |
| 4E | Zendesk Bot Integration | **Provisionally accepted** (2026-11-18); decisions Z-A … Z-H reviewed and approved after the Z-G/Z-C/Z-D/Z-E corrections |
| 4K | Knowledge retrieval and strict evidence-based answering (end to end) | **Provisionally accepted** (2026-11-18); K-A … K-G approved (K-E amended for PDF); K-H proposed. Not complete for release before live verification |
| 4F | Integration preparation and security validation (simulated services only) | **Provisionally accepted** (2026-11-18); F-A approved in Phase 5 (sliding-window form). No live service verified |
| 5 | Frontend rewrite (`frontend/src/app12/`, `/app/*`) + Phase 4F corrections B1–B4 | **Provisionally accepted** (2026-11-18); W2 approved in Phase 6A |
| 6A | Local end-to-end integration (real frontend + API + MongoDB + browser), launch-critical gaps F1–F6, knowledge harness, deletion safety | **Accepted** (2026-11-19, final run on tree `790495a4`; the two earlier failed runs are not successful runs) |
| 6B | Release-candidate preparation: legacy cleanup, security review, key rotation, MongoDB file-limit and backup rehearsals, RC images, configuration audit, K-H protocol, cutover plan | **Accepted** (2026-11-19, RC1 tree `05d31ae188ec`) |
| 6C | Private beta preparation: B-ACCT account creation by invitation, decisions B-API/changelog/new database, zlib fix, gateway Identify-spacing fix, real-service validation and beta activation plans | **Accepted** (2026-11-19, beta1 tree `4219354d1469`) |
| 6D | Private beta activation preflight (manifests, isolation, DNS/Caddy, beta database, e-mail, model allowlist) | **Preflight PASS (conditional), awaiting activation approval** (2026-11-19): `beta-preflight-6d.md`, `beta-activation-checklist.md`, `deploy/proposed/beta/`. Nothing started on BBP1; no code change |
| 7 | Production cutover (direct replacement at bitbotpro.io / api.bitbotpro.io; new database `bitbotpro_v12`, decision 6C-D) | **API CUT OVER 2026-11-19 11:01 UTC** — release `aa55976b` (tag `bitbotpro-spec012-r1`), image `spec012-beta1-4219354d1469` serving api.bitbotpro.io; Spec 012 frontend live on Emergent; legacy container `bitbotpro-api` stopped (kept); config fix: `ALLOWED_HOSTS` += `10.50.0.20` (Caddy health checks). **Real-service validation (V1–V5) pending; no testers invited.** Runbook: `phase7-cutover-runbook.md` |
| R | Restoration of missing features (approved 2026-11-19, prototype-first): R1 website URL import, R2 bounded same-site crawl, R3 API integrations | **Deployed to the API 2026-11-20** (commit `02bbd40`, image `spec012-r3-8163947a3153`); frontend via Emergent pending. See "Restoration R1–R3" below |
| S0 | Feature-parity Stage 0 (audit 2026-11-20): customer AI keys by name, provider/model selection, connection test, platform AI keys, GENERAL uses selected knowledge (G1) | **Implemented locally 2026-11-20** (not committed, not deployed). See "Stage 0 — AI keys" below; API reference `api-ai-settings.md` |

## Phase 2 — core foundation

### Files

| File | Content |
|---|---|
| `backend/core/__init__.py` | package; isolation rule |
| `backend/core/lifecycle.py` | states, suspension authority, transitions, availability |
| `backend/core/hierarchy.py` | Enterprise / Organisation / Bot / BotInstance; channel bindings; immutable ancestry; chain verification; store interface; `HierarchyService` |
| `backend/core/config_registry.py` | 22 properties and 22 policies; combine rules; security classes; knowledge modes |
| `backend/core/resolver.py` | the single effective-configuration resolver; override and policy write validation |
| `backend/core/authz.py` | roles, memberships, actions, delegation, grant rules, last-owner and last-superadmin checks |
| `backend/core/credentials.py` | ownership, Fernet encryption, use versus manage, rotation and revocation, platform credentials |
| `backend/core/knowledge.py` | knowledge plan, source scope and access lists, strict-mode fail-closed contract |
| `backend/tests/unit/_core_fixtures.py`, `test_core_{isolation,lifecycle,hierarchy,config,knowledge,authz_credentials}.py` | 107 tests |
| `backend/.dockerignore`, `backend/Dockerfile` | core copied into the **test** stage only |

### Test results (2026-11-18)

- Core suites on the host: 107 passed.
- Full backend unit suite on the host: 701 passed (195 skipped: FastAPI or
  other dependencies are absent on the host).
- Docker `test` target: 701 passed (85 host-only skips).

### Known limitations and remaining risks (not resolved by Phase 2)

1. **MongoDB concurrency and atomic operations were not implemented in Phase 2.**
   The Phase 2 store is in-memory only. Discord guild/channel overlap and
   last-owner checks were read-then-check. *Phase 3 implemented these with
   single-document atomic operations and tests (see below); this needs your
   review before it counts as accepted.*
2. **The knowledge retrieval engine does not exist yet.** Strict mode is only
   as good as its relevance scores and the structured-claims output. This is
   Phase 4 (possibly 4K).
3. **Rotation of the master encryption key (`ENCRYPTION_KEY`) is not yet
   designed.**
4. **AI provider allowlists and registry defaults need review.**
   `MODEL_ALLOWLIST`, `DEFAULT_MODEL` and limits are placeholders to check
   against current provider offerings.
5. **The production runtime has not been integrated with the new core.** The
   running application does not use `backend/core/`.

## Phase 3 — authorised scope (2026-11-18)

Authorised:
- MongoDB persistence for Enterprises, Organisations, Bots, Bot Instances,
  memberships, invitations, credentials, platform credentials, deletion jobs and
  audit records, with explicit schemas and indexes, Enterprise scoping, and full
  ancestry validation on load.
- Database-enforced atomicity for:
  - exclusive Discord, Telegram and Webchat bindings;
  - overlapping Discord server and channel bindings;
  - the last-owner and last-superadmin safeguards;
  - membership updates;
  - deletion state transitions.

  Concurrency tests are included.
- One authentication dependency covering identity, session validity, session
  version, superadmin status, tenant membership, target ancestry and permission.
  Tenant headers are never trusted.
- REST endpoints for:
  - hierarchy objects;
  - effective configuration, overrides and policies;
  - lifecycle;
  - memberships and invitations;
  - credentials, including rotation and revocation;
  - deletion preview, confirmation and job status;
  - audit records.
- The safe permanent-deletion foundation: preview, short-lived confirmation
  token, immediate deactivation, repeatable descendant-first jobs, audit, and
  cross-tenant protection. It is never executed against production data.
- Tests:
  - route × role × tenant matrix, with database-unchanged assertions on
    rejection;
  - persistence, authentication, inheritance, lifecycle, credential, deletion
    and concurrency tests;
  - security regression tests.

Boundaries:
- The new router is not mounted in production.
- No deploy, no restart, no data reset.
- No runtime or frontend rewrite.
- No changes to the VPS, Caddy, WireGuard, DNS or Gridscan.
- No commit or push.

### Phase 3 result (2026-11-18): implemented locally, awaiting review

Not mounted, not committed, not deployed. Production is unchanged.

**Topology finding.** Production MongoDB is a standalone `mongod` 8.0
(`--wiredTigerCacheSizeGB 2`, no `--replSet`), so multi-document
transactions are unavailable. Every Phase 3 guarantee is built from
single-document atomic operations instead: unique keys, compare-and-swap on
`version`, conditional delete, and `find_one_and_delete`. The guarantees were
tested on a disposable standalone mongod of the same image. No guarantee was
weakened, so nothing had to stop. See `spec.md` §21 and `DECISIONS.md` X4.

**Files.**

New:
- `backend/core/`: `bindings.py`, `memberships.py`, `identity.py`,
  `deletion.py`, `audit.py`
- `backend/persistence/`: `__init__.py`, `mongo.py`
- `backend/api/`: `__init__.py`, `app.py`, `common.py`, `hierarchy.py`,
  `config.py`, `lifecycle.py`, `members.py`, `credentials.py`, `deletion.py`
- Unit tests in `backend/tests/unit/`:
  - `test_core_{concurrency,deletion,architecture}.py`
  - `test_api_{matrix,auth,config,lifecycle,members,credentials,deletion,security}.py`
  - `_api_fixtures.py`
- Integration tests: `backend/tests/integration/test_spec012_mongo.py`

Modified:
- `backend/core/hierarchy.py`: store contract with CAS and unique keys,
  versions, atomic binding claims, post-insert parent check, guild only via
  the install flow.
- `backend/core/credentials.py`: persistence helpers and Telegram bot
  identity.
- `backend/tests/unit/test_core_isolation.py`
- `backend/.dockerignore` and `backend/Dockerfile`: the **test** stage only.
- `CLAUDE.md`, `memory/WORKSPACE_ARCHITECTURE.md` (marked superseded),
  `spec.md` (status and §21), `DECISIONS.md`, this file.

**Test results (2026-11-18).**

| Gate | Result |
|---|---|
| Host unit suite | 792 run, OK (243 skipped: no FastAPI or motor on the host) |
| Docker `test` target (`bitbotpro-api-test:spec012-phase3`, backend tree `db32b76d9162`) | 792 run, OK (86 host-only skips) |
| Real MongoDB (disposable standalone mongod, isolated internal network) | 21 run, OK (13 Spec 012 + 8 Spec 008); no test databases left behind |
| Permission matrix | 56 routes × 7 actors = 392 requests; every rejection leaves the database unchanged; route-coverage test passes |

**Remaining risks (not resolved).**

1. The knowledge retrieval engine does not exist (Phase 4 or 4K).
2. Rotation of the master encryption key is not yet designed.
3. The AI provider allowlists and registry defaults need review.
4. The production runtime is not integrated with the new core. The legacy app
   still serves all traffic.
5. Crash windows leave state that fails closed but is untidy:
   - a binding claim without an instance (cleared by
     `BindingRegistry.reconcile` after a grace period; no scheduler runs it
     yet);
   - a superadmin guard count below the actual number of superadmins (it only
     ever blocks a demotion);
   - a user deletion job stopped mid-way (resumable).
6. Deletion jobs run inline or as a request background task. There is no
   worker or scheduler yet (Phase 4 worker), and no stuck-job alerting.
7. External shutdowns are recorded as `deferred` and are not executed (X12).
8. CSRF protection for cookie sessions relies on a required custom header.
   CORS/origin policy must be set when the router is mounted (Phase 7).
9. The API has no rate limiting yet. The legacy rate-limit module will be
   applied when the router is mounted.
10. The per-Enterprise roster document limits an Enterprise to roughly 100k
    membership entries (the 16 MB document limit). See X4.
11. The new collection names (`organisations`, `bots`, `bot_instances`,
    `invitations`, `users`) overlap the legacy production names.
    `prepare_database` refuses non-empty and legacy databases, so the new API
    can only run on a fresh database, which is the Phase 7 reset.

## Phase 3 review (2026-11-18)

Decisions are recorded in `DECISIONS.md`:
- **Approved:** X1, X3, X5, X7, X8, X9 and X10 (L-1).
- **Deferred to Phase 4:** X2 and X11.
- **Preserved, not yet fully met:** X6, a per-credential `usable_by` scope,
  planned for Phase 4.
- **Approved for correction in Phase 3.1:** X4, X12, X13 and X14.

## Phase 3.1: corrections (authorised 2026-11-18; implemented locally, awaiting review)

Not mounted, not committed, not deployed. Production is unchanged.

**Changes.**
- **X4, memberships:** one document per (`enterprise_id`, `user_id`), with a
  unique compound index. Every membership change and every delegation-policy
  change goes through a serialised intent protocol on `membership_guards`:
  1. a consistent snapshot;
  2. pure checks;
  3. a compare-and-swap commit (the serialisation point);
  4. account preconditions re-verified (actor active; superadmin still a
     superadmin) together with document versions;
  5. idempotent, tagged application.

  The guard keeps an exact Owner count. Crashed intents are rolled forward or
  aborted by the next change in that Enterprise, or by a stale-intent check
  (older than 5 s) during request authorisation.
- **X12, deletion:** a per-action external cleanup state machine:
  - action states: `pending`, `retry_wait`, `succeeded`, `not_required`,
    `failed`, `abandoned`;
  - job states: `pending`, `running`, `awaiting_external_cleanup`,
    `completed`, plus the failure states `failed` and `cleanup_failed`;
  - credentials needed for cleanup are kept (encrypted) and cannot be revoked
    or deleted while they are needed;
  - leave-guild is decided at execution from the live claims of every tenant;
  - errors are recorded only as a sanitised category, with a capped
    exponential back-off;
  - audit events are written for failures and resolutions;
  - superadmin operator routes: resume (a fresh retry budget) and resolve as
    `not_required` (a reason is required).

  Phase 3.1 has no adapters, so jobs that need external cleanup stay
  `awaiting_external_cleanup`.
- **X13, knowledge sources:** `knowledge.allowed_sources` is unset by default,
  which means no sources (deny by default). Descendants may only narrow it, and
  the knowledge plan intersects with it as defence in depth.
- **X14, invitations:** expired invitations are shown as `expired` (hidden
  unless `include_expired=true`); lookup is restricted to the verified
  addressee.
- **X7, Organisation deletion:** permanent deletion of Organisations is limited
  to Enterprise Owners. The code allowed Enterprise Admins and has been aligned
  with the approved decision.

**Test results (executed 2026-11-18).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| Host unit suite | 822 | 0 | 252 (no FastAPI or motor on the host) |
| Docker `test` target (`bitbotpro-api-test:spec012-phase3.1`, backend tree `f124b1b41c21`) | 822 | 0 | 86 (host-only) |
| Real standalone MongoDB 8.0.38 (Spec 012: 15, Spec 008: 8), three consecutive runs | 23 each | 0 | 0 |
| Permission matrix | 59 routes × 7 actors = 413 requests | 0 | — |

**Environment finding (operations, not a code defect).** The first
integration runs crashed the *disposable* test mongod. WiredTiger reported
`EMFILE` (too many open files): containers default to `nofile=1024`, and the
suite creates a fresh database per test. With `--ulimit nofile=64000:64000`
three consecutive runs passed. **Production `bitbotpro-mongo` also runs with
the default limit of 1024.** MongoDB recommends 64000. Raising it is a
production change that needs separate approval.

**Remaining risks.**
1. The knowledge retrieval engine does not exist yet (Phase 4 or 4K).
2. Rotation of the master encryption key is not yet designed.
3. AI provider allowlists and registry defaults need review.
4. The production runtime is not integrated with the new core.
5. The production MongoDB file-descriptor limit is the default 1024 (see
   above).
6. Membership changes within one Enterprise are serialised. That is fine at
   admin frequency, but a very large bulk import would need batching.
7. A crashed requester's intent blocks membership changes in that Enterprise
   until the next change or a stale-intent check resolves it (seconds).
8. Deletion jobs have no worker or scheduler yet. External cleanup can only
   complete through Phase 4 adapters or explicit operator resolution.
9. CORS and rate limiting must be applied when the router is mounted.
10. X6 (per-credential usage scope) is not yet implemented.
11. Collection names overlap the legacy ones; the database guard refuses
    non-empty legacy databases.

## Phase 4A (authorised 2026-11-18; implemented locally, awaiting review)

Not mounted, not committed, not deployed. No live service was contacted.
Production is unchanged.

**Authentication** (`api/auth.py`, `api/passwords.py`, `api/bootstrap.py`,
`core/identity.py`, `core/throttle.py`):
- Login, logout, logout-all, session view, change password, and a forgot/reset
  password flow. Email goes through a `Mailer` interface; there is no live
  delivery in 4A.
- Server-side session records (`sessions`, with TTL). The signed token carries
  only `sid`, `sub`, `tv` and `typ`. Every request re-checks the session, the
  token version and account status, and reads superadmin status from the
  database.
- The cookie is httpOnly, Secure, SameSite=Strict, Path=/api. Every unsafe
  request that can carry it, login included, needs the CSRF header.
- Hashing uses bcrypt (cost 12), compatible with the retained legacy hashes.
  Password policy uses the shared `password_policy` module (the stricter rule
  for superadmins).
- Throttling (atomic, keys hashed): failed logins per account (5 per 15 min)
  and per IP (30 per 15 min); reset requests per account (3 per hour) and per
  IP (10 per hour). Responses never reveal whether an account exists.
- Reset tokens: stored as a hash, single use, valid 30 minutes. A new password
  that fails policy does not spend the token. A reset or password change ends
  all sessions.
- Superadmin bootstrap: one-time and race-safe, refused once any superadmin
  exists. The CLI reads the password interactively and refuses non-Spec 012
  databases.

**Runtime foundation** (`runtime/`):
- `interfaces.py`: channel adapter, AI provider, knowledge catalog and
  retriever interfaces; masked `Secret`.
- `routing.py`: binding claim to instance. An ambiguous match (for example two
  DM-enabled instances on shared guilds) fails closed, as does a mismatch
  between claim and instance document.
- `usage.py`: per end-user rate limit; monthly quota counted at Enterprise and
  Organisation with atomic reservation and release; usage events with full
  ancestry.
- `errors.py`: typed errors and fixed safe messages.
- `pipeline.py`, in order:
  1. binding resolution;
  2. verified chain;
  3. availability;
  4. single resolver (policy, channel policy, credential scope, knowledge
     mode);
  5. rate limit;
  6. strict-knowledge boundary;
  7. AI provider configuration (D-Q1 admin issue plus safe reply);
  8. availability re-check;
  9. quota;
  10. generation;
  11. answer validation;
  12. usage event.

**Strict-knowledge enforcement boundary.** When strict and no evidence is
approved, no factual answer is given, and the retriever is never queried.
When the knowledge service is missing or unavailable, or evidence is
insufficient, contradictory or cross-tenant, the request is refused before
any AI call. A grounded answer must consist only of claims that cite the
gated evidence. Knowledge-preferred falls back to general knowledge only when
the Enterprise permits it. GENERAL mode works without a knowledge base. All of
this is verified with test doubles. **It is not end to end until 4K.**

**Credential scopes (X6).**
- `usable_by` on credentials: an Enterprise credential can be limited to
  selected Organisations and Bots; an Organisation credential to selected Bots.
- The scope is validated against the owner hierarchy on create and on
  `PUT /credentials/{id}/scope`.
- It is enforced in `check_use`, so assignment, resolution and runtime use are
  all covered. A credential is never usable outside its owner hierarchy, and
  revocation fails closed.

**Workers** (`runtime/worker.py`):
- Restart-safe deletion-job processing (leases; recovery from expired leases;
  bounded retries of failed jobs, default 5; operator resume).
- External cleanup through injected executors with per-(job, action)
  idempotency keys. Mock adapters only.
- Binding-claim reconciliation; sweep of crashed membership intents.
- Discord desired-state computation for a future connection manager (mock).
- Heartbeats and sanitised audit events.

**API additions** (not mounted):
- `POST /auth/{login,logout,logout-all,change-password,forgot-password,reset-password}`
  and `GET /auth/session`;
- `PUT /credentials/{id}/scope` and `usable_by` on credential create;
- `GET /instances/{id}/runtime-config`;
- `GET /platform/workers`.

**Test results (executed 2026-11-18).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| Host unit suite | 876 | 0 | 268 (no FastAPI or motor on the host) |
| Docker `test` target (`bitbotpro-api-test:spec012-phase4a`, backend tree `f5f931da3ff4`) | 876 | 0 | 86 (host-only) |
| Real standalone MongoDB 8.0.38 (Spec 012: 19, Spec 008: 8), three consecutive runs, `nofile=64000` on the disposable server | 27 each | 0 | 0 |
| Permission matrix | 63 routes × 7 actors = 441 requests | 0 | — |

## Phase 4B: Webchat (authorised 2026-11-18; implemented locally, awaiting review)

Not mounted, not committed, not deployed. No live AI provider or other
service was contacted. Production is unchanged.

**Files.**
- New:
  - API: `api/webchat.py`, `api/webchat_assets.py`;
  - runtime: `runtime/providers.py`, `runtime/conversations.py`;
  - tests: `tests/unit/test_api_webchat.py`, `tests/unit/test_runtime_providers.py`.
- Modified:
  - runtime: `runtime/interfaces.py` (history, token usage), `runtime/pipeline.py` (history, usage details);
  - API: `api/common.py` (settings, catalog), `api/app.py`, `api/runtime_routes.py`;
  - core: `core/hierarchy.py` (`set_bot_logo`), `core/deletion.py` (chat data in deletion);
  - persistence: `persistence/mongo.py` (schemas, indexes, TTLs);
  - tests: `tests/unit/test_api_matrix.py`, `tests/unit/test_core_architecture.py`, `tests/integration/test_spec012_mongo.py`.

**Usage counting (Webchat).**
- **Successful answer:** counted once, as a quota reservation plus an
  `llm_call` usage event with token counts.
- **Provider failure** (timeout, 5xx, rate limit, auth): the reservation is
  released and the request is not counted.
- **Not billable:** refusals (strict mode, before any AI call), rate-limited
  requests, quota-exceeded requests and unavailable instances. Refusals are
  recorded as `refusal` events.
- **Interrupted request** (client disconnect): generation continues (shielded)
  and is stored and counted once. Reconnecting with the same
  `client_message_id` replays it.
- **Retries and duplicates** with the same `client_message_id` replay the
  stored result and are never re-billed. A concurrent duplicate gets 409.
- **Storage failure after a successful generation** (rare): the request
  record is dropped so the client can retry. That retry may be billed again
  (accepted risk).

**Test results (executed 2026-11-18).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| Host unit suite | 919 | 0 | 311 (no FastAPI, httpx or motor on the host) |
| Docker `test` target (`bitbotpro-api-test:spec012-phase4b`, backend tree `e9cfd800a971`) | 919 | 0 | 86 (host-only) |
| Real standalone MongoDB 8.0.38 (Spec 012: 21, Spec 008: 8), three consecutive runs, `nofile=64000` on the disposable server | 29 each | 0 | 0 |
| Permission matrix | 68 routes × 7 actors = 476 requests (public routes are covered by `test_api_webchat.py`) | 0 | — |

## Phase 4C: Webchat corrections + Telegram (authorised 2026-11-18; implemented locally, awaiting review)

Not mounted, not committed, not deployed. No live Telegram or AI call was
made. Production is unchanged.

**Webchat decisions:** W-A–W-D approved and recorded (`DECISIONS.md`).
Webchat parity remains **incomplete**: W2, W8, W9 and W12 are pending frontend
work (Phase 5: React widget and chat pages, conversation administration,
logo/branding administration, the embed-code interface, documentation).
Strict knowledge remains incomplete until 4K.

**Billing idempotency (Part C).** Durable on standalone MongoDB; no
architectural change was needed.
- **Request records** (`chat_requests`, id `<session>:<client_message_id>`) move
  `processing` (leased) → `generated` (answer stored) → `done`, or to
  `uncertain`.
- **Usage ledger** (`usage_ledger`, the same id) moves `reserving` →
  `reserved` → `settled`, or to `released`, `rejected` or `uncertain`, by
  compare-and-swap. The billable event id is derived from the key.
- **Order:** reserve → generate → store result → settle → conversation →
  done.
- **Retries:** a retry completes `generated` requests, replays `done` ones, and
  never regenerates a request with a reservation (`uncertain` →
  409 "send it again as a new message").
- **Worker repair:** finishes `generated` requests, removes unreserved stale
  ones, marks reserved stale ones uncertain, and lowers leaked counters (never
  raises them).
- **Behaviour change:** a transient provider failure now returns a retryable
  503 (nothing billed) instead of a 200 fallback.

**Telegram (T1–T12).**
- **Credentials:** `getMe` validation on save; unique by credential and bot
  identity; usage scopes; revocation removes the webhook first; credential test
  endpoint.
- **Webhook:** path secret plus the required `X-Telegram-Bot-Api-Secret-Token`
  header, both server-generated, stored encrypted and hashed. Authenticated
  before any other work, with a 256 KB cap. Rate-limited after
  authentication. Duplicate updates are deduplicated by durable idempotency.
- **Webhook state:** connect, disconnect, rotate-secret, test and status;
  lifecycle-driven sync (suspend/archive remove the webhook, resume/unarchive
  register it); the worker retries failed syncs.
- **Message handling:** listen modes and chat allowlist (legacy semantics);
  one conversation per chat; per-user rate limit; replies chunked at 4096 on
  boundaries; Markdown with plain-text retry; `retry_after` honoured once (at
  most 30 s); follow-up buttons with opaque callback ids, only for general
  (uncited) answers, and a tap re-asks through the full pipeline.
- **Deletion cleanup:** an adapter that never touches another bot or a bot
  re-bound to another instance.

**Test results (executed 2026-11-18).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| Host unit suite | 957 | 0 | 349 (no FastAPI, httpx or motor on the host) |
| Docker `test` target (`bitbotpro-api-test:spec012-phase4c`, backend tree `44cab9eca50b`) | 957 | 0 | 86 (host-only) |
| Real standalone MongoDB 8.0.38 (Spec 012: 23, Spec 008: 8), three consecutive runs, `nofile=64000` on the disposable server | 31 each | 0 | 0 |
| Permission matrix | 73 routes × 7 actors = 511 requests (public and webhook routes are covered by their own suites) | 0 | — |

## Phase 4D: Enterprise Discord + Telegram T-A (authorised 2026-11-18; implemented locally, awaiting review)

Not mounted, not committed, not deployed. No live Discord, Telegram or AI
call was made, no gateway was started, and no existing Discord application was
touched. Production is unchanged (`bitbotpro-api:spec011-f245d7ed3bdb`).

**Decisions recorded (`DECISIONS.md`).**
- **D-Q14:** one Discord application per Enterprise, superadmin-controlled.
  It supersedes the global platform Discord application.
- **T-A:** approved with safeguards and implemented.
- **T-B:** approved.
- D-Q4, D-Q5 and D-Q13 are updated to the per-Enterprise model.

**Telegram T-A.**
- `runtime/delivery.py` adds durable delivery records with leases and
  per-part state.
- Replies are recorded before sending.
- Precise error categories:
  - connection refused → not sent, retried;
  - timeout or 5xx → uncertain, never resent;
  - 400/401/403 → final.
- Worker recovery: an in-flight part becomes uncertain.
- Operator view: `GET /instances/{id}/deliveries` (admins, no text) and
  worker counts.
- The guarantee is at most once per part with explicit uncertainty, not
  exactly once.

**Discord.** Parity D1–D19 per `integration-parity.md`:
- D1–D12 and D14–D18 are implemented with mocked Discord.
- D13 and D19 remain retired.
- D2's concrete gateway connection is injected but not implemented. The
  supervision is implemented.

**Test results (executed 2026-11-18).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| Host unit suite | 1024 | 0 | 416 (no FastAPI, httpx or motor on the host) |
| Docker `test` target (`bitbotpro-api-test:spec012-phase4d`, backend tree `7c21a66b5823`) | 1024 | 0 | 86 (host-only) |
| Real standalone MongoDB 8.0.38 (Spec 012: 27, Spec 008: 8), three consecutive runs, `nofile=64000` on the disposable server | 35 each | 0 | 0 |
| Permission matrix | 87 routes × 7 actors = 609 requests (OAuth callback, public and webhook routes covered by their own suites) | 0 | — |
| Mutation checks | 9 mutations of key protections in a scratch copy, all detected (2 were caught only after the tests were strengthened) | — | — |

New suites:
- `unit/test_api_discord.py`: 57 tests.
- `unit/test_api_telegram_delivery.py`: 10 tests.
- `integration/test_spec012_mongo.py::MongoPhase4DTests`: 4 tests.

**Known limitations of 4D.**
1. **No live gateway connection exists yet.** Before any live use it needs a
   concrete connection implementation (heartbeat, resume, identify
   rate limits) and live verification against a test application.
2. **OAuth callback result.** The callback answers with JSON; the browser
   redirect target is a Phase 5 decision.
3. **Not ported:** the share link and invite email (D3), and the handoff
   status/resume REST endpoints (D11). The chat commands remain.
4. **Mention override (D6)** is limited to the instance's own claimed
   channels. This is a deliberate change from the legacy behaviour.
5. **Retention.** `outbound_deliveries` has no TTL. Its retention policy is
   to be decided before cutover.
6. **Strict knowledge** stays fail-closed (refusal or fallback) until 4K.

## Phase 4D corrections C1–C6 and Phase 4E Zendesk (authorised 2026-11-18; implemented locally, awaiting review)

Not mounted, not committed, not deployed. No live Discord, Telegram, Zendesk
or AI call was made, and no gateway was started. Production is unchanged
(`bitbotpro-api:spec011-f245d7ed3bdb`, healthy).

**4D corrections** (details in `DECISIONS.md`):
- **C1:** the gateway adapter is recorded as a mandatory 4F release blocker;
  its required capabilities are listed.
- **C2:** Discord share links and invitation email.
- **C3:** handoff administration.
- **C4:** passive-mode usage rules with tests.
- **C5:** `outbound_deliveries` retention, plus an operator resolution route.
- **C6:** concurrent shared-server cleanup tests.
- D-Q14 is recorded as a permanent invariant in `CLAUDE.md` §1.4a,
  `DECISIONS.md` and `spec.md`.

**4E Zendesk:**
- Bot Integration model, with X2 closed (integration overrides through the
  registry and resolver).
- Guarded client using the kept `outbound_guard`.
- OAuth with PKCE and hashed single-use state, re-authorisation and
  account-confusion checks; API-token mode.
- Mandatory signed webhooks; durable event state machine; shared pipeline.
- Reply modes, with only validated answers ever public.
- `safe_update` delivery; suggestions; sidebar app keys and ZIP.
- Deletion executors.
- No ticket creation (D-Q11).

**Test results (executed 2026-11-18).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| Host unit suite | 1097 | 0 | 489 (no FastAPI, httpx or motor on the host) |
| Docker `test` target (`bitbotpro-api-test:spec012-phase4e`, backend tree `4cd29a49634f`) | 1097 | 0 | 86 (host-only legacy tests) |
| Specs 009–011 security suites (14 modules, inside the image) | 240 | 0 | 0 |
| Real standalone MongoDB 8.0.38 (Spec 012: 31, Spec 008: 8), three runs, `nofile=64000` on the disposable server | 39 each | 0 | 0 |
| Permission matrix | 113 routes × 7 actors = 791 requests (public, hook and callback routes covered by their own suites) | 0 | — |
| Mutation checks | 13 mutations of key protections in a scratch copy; all detected (the share-link CAS only after a concurrency test was added) | — | — |

New and extended suites:
- `unit/test_api_zendesk.py`: 54 tests.
- `unit/test_api_discord.py`: 74 tests (17 new for C2–C6).
- `unit/test_api_telegram_delivery.py`: 12 tests.
- `integration/test_spec012_mongo.py::MongoPhase4ETests`: 4 tests.

**Known limitations:**
1. Discord gateway adapter missing (C1; 4F blocker).
2. Strict Knowledge Only stays fail-closed until 4K.
3. The Zendesk and Discord OAuth callbacks return JSON; the redirect targets
   are Phase 5 decisions.
4. Zendesk analytics, the suggestions inbox UI and admin screens are Phase 5.
5. The Spec 011 API integrations are not yet ported to Bot Integrations.
6. Zendesk decisions Z-A and Z-C … Z-H need approval.
7. C6 residual race (an install completing during a leave), documented in
   `DECISIONS.md`.

## Zendesk corrections (Z-G, Z-C, Z-D, Part C) and Phase 4K (authorised 2026-11-18; implemented locally, awaiting review)

Not mounted, not committed, not deployed; no live Discord, Telegram, Zendesk or AI call. Production
unchanged (`bitbotpro-api:spec011-f245d7ed3bdb`, healthy). Details: `DECISIONS.md` (Zendesk table,
Part C, Phase 4K) and `spec.md` §28.

**Test results (executed 2026-11-18).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| Host unit suite | 1153 | 0 | 544 (no FastAPI, httpx or motor on the host) |
| Docker `test` target (`bitbotpro-api-test:spec012-phase4k`, backend tree `4597b5f1b9c4`) | 1153 | 0 | 86 (host-only legacy tests) |
| Specs 009–011 security suites (14 modules) | 240 | 0 | 0 |
| Real standalone MongoDB 8.0.38 (Spec 012: 35, Spec 008: 8), three runs, disposable server with `nofile=64000` | 43 each | 0 | 0 |
| Permission matrix | 128 routes × 7 actors = 896 requests | 0 | — |
| Mutation checks | 13 mutations of new protections, all detected (3 only after targeted tests were added) | — | — |

New suites:
- `unit/test_knowledge.py`: 31 tests (ingestion, retrieval, modes, adversarial items 1–28, cross-channel).
- `unit/test_api_zendesk_lifecycle.py`: 24 tests (Z-G, Z-C, Z-D, Part C).
- `integration/test_spec012_mongo.py::MongoPhase4KTests`: 4 tests.

**Known limitations.**
1. **Discord gateway adapter still missing** (4F release blocker, C1).
2. **Strict knowledge is verified only with mocked AI providers.**
   - Claim validation is lexical, not semantic (K-C).
   - Contradiction detection is heuristic (K-D).
   - Retrieval is lexical BM25 (K-A).
   - No zero-hallucination guarantee.
3. **Ingestion:** PDF and URL ingestion are not supported (K-E).
4. **Uncertain outcomes need an operator:**
   - Zendesk comment deliveries are at most once per part with uncertainty (Z-D).
   - Provider and generation outcomes are never retried automatically (Part C).
5. **Z-G fence:** a send that started before a deletion request may complete; nothing starts after
   it.
6. **Approvals needed:** the 4K decisions K-A … K-G.

## Phase 4F: integration preparation and security validation (authorised 2026-11-18; implemented locally, awaiting review)

Simulated services only. No live Discord, Telegram, Zendesk or AI call; nothing deployed, committed or
mounted. Production unchanged (`bitbotpro-api:spec011-f245d7ed3bdb`, healthy). Decisions and findings:
`DECISIONS.md` (K-*, F-*); map: `spec.md` §29.

**Gate results (executed 2026-11-18, image `bitbotpro-api-test:spec012-phase4f`, backend tree `5972d7e2361d`).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| Docker `test` target unit suite | 1182 | 0 | 86 (host-only legacy tests) |
| Specs 009–011 security suites (14 modules) | 240 | 0 | 0 |
| Real standalone MongoDB integration (Spec 012: 42, Spec 008: 8), three runs | 50 each | 0 | 7 each (the load suite, gated by `LOAD_TEST=1`; not counted as passed) |
| Load suite (`LOAD_TEST=1`), disposable mongod, 1 GiB, cache 0.25 GiB | 7 | 0 | 0 |
| Permission matrix | 129 routes × 7 actors = 903 requests | 0 | — |
| Timing-sensitive suites (gateway simulation, PDF, Zendesk lifecycle), 8 repeats | 43 × 8 | 0 | 0 |
| Mutation checks (8 Phase 4F protections) | 8 detected | — | — |

**One intermittent failure.** Investigated in Phase 5 (B1); see "Phase 5 — B1" below. Not reproduced;
cause unknown; remains an open (low) risk.

**Knowledge evaluation** (synthetic corpus of 11 documents, 16 answerable questions, 40 wrong answers in
10 categories, 5 must-refuse cases; scripted model; the corpus was written alongside the validator, so the
rates describe this corpus only):

| Metric | Layer 1 only | Layers 1 + 2 |
|---|---|---|
| Unsupported answers accepted | 28/40 | 5/40 |
| Correct answers refused | 0/16 | 0/16 |
| Citation accuracy | 16/16 | 16/16 |

- **Retrieval:** recall 16/16 and precision 16/16 at threshold 0.5.
- **Must-refuse cases:** all 5 refused (no evidence, restricted, contradiction); the expired version was
  never used.
- **Accepted with both layers:**
  - 2 appended unsupported conclusions;
  - 2 role/meaning swaps;
  - 1 changed exception.

**Load (in-process harness, single event loop, simulated services).**

| Scenario | Result | p50 | p95 |
|---|---|---|---|
| 50 duplicate AI requests | 1 generation | 2.8 s | 2.8 s |
| 100 concurrent requests for 20 remaining quota | exactly 20 generations (20 × 200, 80 × 429) | 38.8 s | 39.0 s |
| 200 Telegram deliveries (100 unique) | 100 replies, 0 spurious 429 (32/100 before F-A) | 33 s | 89 s |
| 150 Discord events (100 unique) | 100 replies | 52 s | 77 s |
| 60 concurrent bot + instance creations across 2 Enterprises | 0 errors, 0 cross-tenant | 7.9 s | 8.0 s |
| 60 knowledge queries racing a revocation | 0 answers after the revocation | 9.2 s | 9.3 s |
| 20 concurrent callbacks for one OAuth state | exactly 1 connection | 0.10 s | 0.30 s |
| 10 credential rotations racing 40 chats | 0 errors, statuses 200/409 | 13.9 s | 17.3 s |
| Organisation deletion racing 40 chats | 1 orphan before the grace re-purge, 0 after (F-B) | 15.4 s | 17.4 s |

- **mongod peak:** CPU 133%, memory 756 MiB of 1 GiB.
- **Latency caveat:** the latencies reflect hundreds of concurrent requests queued on one in-process
  event loop against one small mongod. They are **not** a production capacity estimate.

**Remaining release blockers.**
1. **Live verification of the Discord gateway adapter** with a test Enterprise application. It is
   simulation-only.
2. **Live verification of strict knowledge:**
   - a real AI provider and real documents;
   - re-calibration of K-B;
   - the residual semantic errors (K-C), and K-H if approved.
3. **No OCR:** scanned PDFs are refused.
4. **Zendesk and Telegram sandboxes:** live verification of both.
5. **Production capacity testing** with production-like processes and MongoDB sizing.
6. **Earlier requirements:**
   - the MongoDB file limit;
   - encryption-key rotation;
   - CORS when the API is mounted;
   - backup, restore and cutover rehearsal.
7. **The intermittent unit failure** above (B1: not reproduced in 7 full runs of the exact tree and current trees; open, low).

## Phase 5: frontend and Phase 4F corrections (authorised 2026-11-18; implemented locally, awaiting review)

Mocked Spec 012 API and simulated services only. No live Discord, Telegram, Zendesk or AI call. Nothing
deployed, committed or pushed; production unchanged (`bitbotpro-api:spec011-f245d7ed3bdb`, healthy).
Decisions: `DECISIONS.md` F-A (approved), K-H (experimental), P5-A … P5-I. Map: `spec.md` §30.

**Gate results (executed 2026-11-18, image `bitbotpro-api-test:spec012-phase5`, backend tree `411d73b6278e`).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| In-build unit suite (Docker `test` target, full plain-progress log kept) | 1210 | 0 | 86 (host-only legacy tests) |
| Unit suite in the image, verbose log kept | 1210 | 0 | 86 |
| Specs 009–011 security suites (14 modules) | 240 | 0 | 0 |
| Permission matrix | 129 routes × 7 actors = 903 requests | 0 | — |
| Knowledge evaluation + holdout | 2 | 0 | 0 |
| Real standalone MongoDB integration, three runs | 50 each | 0 | 7 each (load suite, gated; not counted as passed) |
| Load suite (`LOAD_TEST=1`) | 7 | 0 | 0 |
| Contract drift (re-exported route manifest vs `frontend/src/app12/contract/routes.json`) | 149 routes | 0 differences | — |
| Frontend Jest (`yarn test --watchAll=false`) | 122 (55 new admin-app tests + 67 existing) | 0 | 0 |
| Frontend production build | compiled; 0 warnings in `src/app12` (pre-existing legacy lint warnings remain; `CI=true` builds fail on those) | — | — |

The first frontend run had 1 failure: the existing guard test `apiBase.test.js` flagged the new W12 curl
example, which rebuilt an `/api` URL by hand. The example now derives from the server-supplied widget URL
and the rerun passed (122/122).

Load (same harness as 4F): 1 generation for 50 duplicates; exactly 20 generations for 100 requests on 20
remaining quota; Telegram 100/100 replies with 0 HTTP 429 under the sliding-window throttle; Discord
100/100; 0 cross-tenant; 0 answers after revocation; 1 OAuth connection from 20 callbacks; Organisation
deletion: 2 orphans before the grace re-purge, 0 after. mongod peak: CPU 140%, 745 MiB of 1 GiB.

**B1 — the unexplained build failures.**

| Failed build | Exact tree re-run | Finding |
|---|---|---|
| Phase 4K gate build, tree `6b3cd5a6a6fc` (17:36) | **Reproduced deterministically**: 3 failures (`test_api_runtime::test_runtime_config_preview`, `test_api_webchat::KnowledgeModeTests` ×2) | The snapshot was taken while 4 test files were still being updated; the next tree differs only in those test files. Not flakiness |
| Phase 4F gate build, tree `5972d7e2361d` (19:14) | **Not reproduced**: 1182 run, 0 failed | The failing test name was never captured (the build output was piped through `tail`). No backend file changed between this build and the passing rebuild at 19:40 |

- **Further runs:** six additional full verbose unit runs of the 4F image, two at a time to add load, all
  passed (1182 each, 0 failures). Earlier: 8 repeats of the timing-sensitive suites.
- **Fixed in the gate procedure:**
  - full build logs are now kept;
  - the tree is snapshotted only while nothing else edits it. The 4K mutation job edited files in place
    while a snapshot was being taken.
- **Remaining uncertainty:** the 4F failure is still unexplained. Suspects are a timing-sensitive test
  under build-time load (gateway simulation, PDF subprocess limits, Zendesk lease timing). The risk is
  judged low but open.

**B2 — F-A approved (sliding-window form).** Every throttle consumer was re-verified. See `DECISIONS.md` F-A.

**B3 — K-H (experimental, off by default).**
- K-H's own false-acceptance and false-refusal rates are **not measured**: that needs a live model, which
  Phase 5 forbids.
- The deterministic layers, which remain mandatory:

| Set | Unsupported accepted, layer 1 | Unsupported accepted, layers 1+2 | Correct refused | Must-refuse refused |
|---|---|---|---|---|
| Phase 4F corpus (written alongside the validator) | 28/40 | 5/40 | 0/16 | 5/5 |
| **Holdout** (new domains, written after layer 2 was frozen) | 18/25 | **9/25** | 0/12 | 2/2 (contradiction, no evidence) |

- **What the holdout shows:** layer 2 generalises less well than the tuned corpus suggested. Layers 1+2
  still accept these on the holdout:
  - 3/3 appended unsupported conclusions;
  - 3/3 role swaps;
  - 1 changed exception;
  - 1 changed number ("two-year" → "three-year", number words);
  - 1 reversed requirement ("excluded" → "included").
- **What K-H is designed for:** exactly these cases. It must be measured live before any production use.
- No zero-hallucination claim is made.

**B4.** `GET /integrations/{id}` exposes `deletion`; the admin UI shows requested, cleanup pending, cleanup
failed and deleted, and the in-flight race is tested.

**Frontend (Part C).**
- **Routes:** `/app/*` (see `spec.md` §30). C1–C16 are covered.
- **Roles:** gated by the API's effective `permissions`. The server remains the authority.
- **Discord:** Organisation pages have no application controls (D-Q14).
- **OAuth:** result pages need `oauth_result_origin`, and a cross-origin deployment needs `admin_origins`.
- **Accessibility:** labelled controls, a skip link, keyboard tabs and live regions. These are basic checks
  only; no automated audit (axe) and no screen-reader test.

**Gaps and blockers added by Phase 5:** see `DECISIONS.md` P5-I.
- No usage endpoint, no user search and no invitation e-mail.
- The W2 outcome needs confirmation (P5-H).
- The UI is untested against a running API and real browsers. There are no E2E browser tests; that is
  Phase 6/7.
- Cutover must configure `admin_origins`, `oauth_result_origin` and `reset_url_base`.

## Phase 6A: local end-to-end integration and release corrections (authorised 2026-11-18; implemented locally, awaiting review)

Isolated local environments only. External services were fakes on an `--internal` Docker network: no live Discord, Telegram, Zendesk, AI or mail. Nothing deployed, committed or pushed. Production is unchanged (`bitbotpro-api:spec011-f245d7ed3bdb`, healthy). Decisions: `DECISIONS.md` P5-H (W2 approved) and 6A-A … 6A-J. Map: `spec.md` §31.

**Gate results (executed 2026-11-19, image `bitbotpro-api-test:spec012-phase6a`, backend tree `790495a48348`).**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| In-build unit suite (full plain-progress log kept) | 1242 | 0 | 86 (host-only legacy tests) |
| Unit suite in the image (verbose) | 1242 | 0 | 86 |
| — webchat / Telegram / Discord / Zendesk | 36 / 40 / 87 / 86 | 0 | 0 |
| — knowledge (incl. K-H, eval, harness) / deletion (incl. safety) / Phase 6A gaps | 79 / 33 / 19 | 0 | 0 |
| Specs 009–011 security suites | 240 | 0 | 0 |
| Permission matrix | 134 routes × 7 actors = 938 requests | 0 | — |
| Knowledge harness, scripted (deterministic corpus and holdout, verifier, end-to-end) | 4 runs | 0 | — |
| Contract drift (154 routes) | 1 | 0 differences | — |
| Real MongoDB integration, three runs | 50 each | 0 | 7 each (the gated load suite; not counted as passed) |
| Load suite | 7 | 0 | 0 |
| Frontend Jest | 132 | 0 | 0 |
| Frontend production builds (API origin; same origin for e2e) | 2 | 0 (0 warnings in `src/app12`) | — |
| **Real frontend/backend HTTP journeys** (C1–C20, roles, tenants, D-Q14 over HTTP) | 11 | 0 | 0 |
| **Real browser (Playwright 1.49.1, Chromium)**: 21 desktop + 2 mobile (Pixel 5), axe audit | 23 | 0 | 0 |

**Two earlier gate runs failed at the image build, one test each. Both were captured in full logs and fixed:**

1. **`test_api_auth::test_ip_throttle` (product fix, and the likely root cause of B1):**
   - The test ran at 23:14:58. Its 30 login attempts straddled the 15-minute window boundary.
   - The sliding-window estimate counted the previous window fractionally, so one extra attempt was allowed.
   - **Fix:** the weighted previous count is rounded up, which errs strict by less than one attempt.
   - **Regression test:** a boundary test that fails without the fix and passes with it.
   - **Link to B1:** the unexplained Phase 4F failure would have run this test just before 19:15:00. The time is estimated from the build start, not measured. F-A then used plain fixed windows, which reset at a boundary.
   - **Status:** B1 has a probable root cause, now fixed; it is no longer unexplained.
2. **`test_api_webchat::test_history_requires_the_session_token` (test defect from Phase 4B):**
   - The "tampered" token replaced the last character with `A`, which equals the real token about 1 time in 64.
   - **Fix:** the replacement now always differs.

**Real-stack findings fixed in Phase 6A:**
- Organisation members could not see whether the Enterprise Discord application was configured (6A-H).
- Organisation-level members hit an error on their Enterprise page; they now get a limited view (6A-I).
- A React hook-order defect existed that only the production build caught.
- WCAG AA colour-contrast failures were found by axe on three pages (6A-I).
- Several test assumptions were corrected against the approved permission matrix:
  - editors may edit Organisation details;
  - Enterprise admins may edit Enterprise policy;
  - forgot-password answers `202`.

**Deletion safety (Part H):**
- Organisation deletion racing 40 chats now leaves **0** orphans before the grace re-purge (2 in Phase 5) and 0 after.
- Orphans can no longer be read or used, and nothing generated during a deletion is delivered (6A-G).

**Knowledge (Part G):** K-H accuracy is **not measured**. The scripted harness runs are plumbing checks:
- The deterministic layers reproduce 5/40 (corpus) and 9/25 (holdout) unsupported answers accepted, with 0 correct answers refused.
- The scripted verifier, which approves everything, gives FAR 1.0 and FRR 0.0. That is the expected baseline, not a result.
- The scripted end-to-end model gives no claims, so it refuses everything. This tests the plumbing only.
- The live run needs approval.

**Browser coverage:**
- Covered:
  - Chromium desktop and mobile;
  - keyboard: Tab, Enter, arrow keys, Escape on dialogs, skip link;
  - form validation, session expiry, CSRF failure, refresh, direct navigation;
  - OAuth callback landing (Discord connected, replay rejected, Zendesk expiry);
  - error and loading states, accessible names;
  - an axe audit with no serious or critical violations on 7 pages.
- Not covered:
  - Firefox and WebKit;
  - screen readers;
  - a live Discord or Zendesk consent screen (the network is isolated);
  - cross-origin deployment (the e2e stack is same-origin; CORS is covered by unit tests only).

## Phase 6B: release-candidate preparation (authorised 2026-11-19; implemented locally, awaiting review)

Isolated environments only; production unchanged (`bitbotpro-api:spec011-f245d7ed3bdb`, healthy). Map: `spec.md` §32.
Decisions 6B-A … 6B-J (`DECISIONS.md`). Evidence: `evidence/phase-6b/`.

**Final gate (2026-11-19, backend tree `05d31ae188ec`, frontend tree `62c6e8fed6b7`):**

| Gate | Run | Failed | Skipped |
|---|---|---|---|
| Backend unit (in build; verbose in image) | 1042 each | 0 | 86 (host-only: compose 27, WireGuard 57, frontend parity 1) |
| — webchat / Telegram / Discord / Zendesk | 48 / 40 / 91 / 86 | 0 | 0 |
| — knowledge / deletion / Phase 6A gaps | 79 / 33 / 19 | 0 | 0 |
| — key rotation / production wiring | 18 / 26 | 0 | 0 |
| — kept Specs 009–011 suites / Spec 012 security suites | 243 / 61 | 0 | 0 |
| Permission matrix | 134 routes × 7 = 938 | 0 | — |
| Real MongoDB, 3 runs (incl. key rotation on Mongo) | 53 each | 0 | 7 each (gated load suite) |
| Load | 7 | 0 | 0 |
| Knowledge harness, scripted (plumbing only) | 4 | 0 | — |
| Contract | 154 routes, no drift | — | — |
| Backup/restore rehearsal | 10 validation checks | 0 | — |
| MongoDB nofile rehearsal | 1024 → 64000 → rollback, data kept | — | — |
| RC image smoke (read-only, non-root; legacy DB refused; bad config refused) | API + worker healthy | 0 | — |
| Frontend Jest / production builds (CI=true) | 102 / 2 (0 warnings) | 0 | 0 |
| Real-HTTP journeys / Chromium | 11 / 23 | 0 | 0 |

Backend tests fell from 1,242 to 1,042 and frontend from 132 to 102 because the legacy suites were retired with the
legacy code (`security-traceability.md`). An earlier gate run (tree `1dc72a1b`) failed on three test-harness defects
and showed 5 critical / 123 high image findings; it is kept, labelled, and **not** counted as a successful run.

**Release blockers after Phase 6B:** B-ACCT (no account creation for new invitees; 6B-C proposed), B-API (Spec 011 API
integrations not ported; 6B-B), changelog decision (6B-D), cutover database decision (6B-E), live verification of
Discord/Telegram/Zendesk/AI and K-H (approvals needed), model allowlist review (requirement 3), 48 high Debian base
findings (rebuild with OS updates and rescan at Phase 7), frontend legacy package cleanup, Firefox/WebKit/screen-reader
and cross-origin end-to-end coverage, production mail adapter live check, capacity testing.

## Phase 6C: private beta preparation (authorised 2026-11-19; implemented locally, awaiting review)

Decisions 6C-A … 6C-E (`DECISIONS.md`); map `spec.md` §33. Isolated environments only; production unchanged.

**Final gate (backend tree `4219354d14693023ccf8baa9d2dc2c53f8f1427e`, frontend tree `1a54c99f4c56`):**
backend unit 1052 run / 0 failed / 86 skipped (host-only) — incl. registration 10, auth+members 35, usage 29,
webchat 48, Telegram 40, Discord 91, Zendesk 86, knowledge 79, deletion 33, key rotation 18, production wiring 26,
kept Specs 009–011 243, Spec 012 security 61; matrix 134 routes / 938 requests; real MongoDB 3 × 53 (7 skipped each:
gated load suite); load 7; contract 155 routes, no drift; backup/restore 10/10 checks; nofile rehearsal; image smoke
(API + worker healthy, legacy DB and bad config refused); Trivy 0 critical / 47 high (Debian, no fix published);
Jest 105; builds 2 (0 warnings); real-HTTP 12; Chromium 24. Logs: `evidence/phase-6c/final-run-4219354d/`.
An earlier run (tree `7bc43778`) **failed** on `test_identify_spacing_and_send_budget` (a real gateway defect under
CPU load, fixed and mutation-checked); it is kept, labelled, and not counted.

**Validation status:** everything above is implemented and tested with mocked external services (unit, real HTTP,
browser). **No real Discord, Telegram, Zendesk or AI provider has been validated** — `real-service-validation.md`
V1–V5 are mandatory before the first tester.

## Restoration R1–R3 (approved 2026-11-19; prototype-first development)

Approved by the user on 2026-11-19: restore website URL import (R1), multi-page crawling (R2) and API integrations
incl. runtime execution (R3) inside the Spec 012 architecture; supersedes the 6C-B deferral of B-API. Development
is prototype-first (`DECISIONS.md` "Restoration R1–R3"); essential protections stay (auth/roles, tenant isolation,
encrypted credentials, the outbound guard against internal networks, webhook safety, rate/resource limits, audit).

**R1/R2 (implemented locally, 2026-11-19).** `runtime/website.py`: website sources (`kind="website"`, single page or
crawl up to 200 pages) fetched by the worker (own loop in `run_worker`) only through `outbound_guard.guarded_request`;
redirects followed manually (≤ 5 hops, each hop re-validated, http upgraded to https); robots.txt rules and
Crawl-delay (≤ 5 s), sitemap discovery, `nofollow`/`noindex`, same-site only; pages ingested by the normal
`Ingestor` (one document per page, idempotent on unchanged content); refresh keeps pages that failed this time and
removes pages that disappeared; clear error categories. API: `POST /{owner}/knowledge/sources` with `website`,
`PATCH` website settings, `POST /knowledge/sources/{id}/refresh`, `GET /knowledge/documents/{id}/content`; uploads into
website sources are refused. Frontend: Knowledge panel "Add website", status/progress, last successful fetch, errors,
Refresh, page list with View content. Tests: `tests/unit/test_website.py` (24), matrix rows, Jest
`knowledgeWebsite.test.js`; full backend unit suite 1071 OK (R1), Jest 109/109, CI build clean. No Cloudflare bypass.

**R3 (implemented locally, 2026-11-19).** `runtime/api_calls.py` (ported Spec 011 OpenAPI/reference parsers, guarded
GET execution, connection probes, routing prompt) and `api/external_apis.py` (16 routes): Bot-owned API integrations
(`api_integrations`, `api_endpoints`, `api_activity`; deleted with their Bot/Organisation/Enterprise), auth none /
bearer / API-key header / API-key query with the secret stored as an encrypted Organisation credential (provider
`api_integration`) or a referenced credential (`check_use`); credentials only ever sent to the integration's own
origin; test connection, discover from the documentation URL or an uploaded OpenAPI JSON (admin enable/disable choices
survive refresh), manual GET endpoints, enable/disable and bulk, endpoint tests, activity and stats. Conversations: a
pipeline step (all channels incl. Zendesk) when `tools.api_calls_enabled` (Bot, admin) and the Enterprise policy
`tools.external_api_calls_allowed`, never in STRICT_KNOWLEDGE_ONLY or for passive messages: one routing AI call
(reserved and billed, label `purpose=api_routing`), ≤ 3 enabled GET endpoints in parallel (10 s, 500 KB), results as a
delimited untrusted data block, activity logged. Only GET endpoints are executable (as in Spec 011). Frontend: Bot →
"API integrations" tab; Credentials offer the `api_integration` provider. Tests: `tests/unit/test_api_integrations_r3.py`
(11), 16 matrix rows, Jest `apiIntegrations.test.js` (5). The Spec 011 root modules `api_integrations*.py` remain
(unshipped) only for their SSRF regression tests and can be removed.

## Stage 0 — AI keys, provider and model selection (2026-11-20)

Owner requirement: a normal authorised administrator adds their own AI key, selects provider and model, assigns the key
to a Bot and gets a real answer without developer help (`DECISIONS.md` "Stage 0"). Backend: `api/ai_settings.py`
(9 routes: `GET/PUT …/ai`, `POST …/ai/test`, `/platform/ai-credentials` CRUD + revoke + test), `core/credentials.py`
(platform AI keys through `enterprise_credentials`/`check_use`), `core/resolver.py` (ordered AI keys in one patch),
`runtime/providers.py` (`probe`), `core/knowledge.py` + `runtime/pipeline.py` (G1). Frontend (minimal): Bot/Organisation/
Enterprise "AI provider and model" panel (provider, model, named key dropdown, inline "Add a new API key", Test connection,
clear messages; raw `ai.*` fields hidden from the generic config panel); Platform → "Platform AI keys". Tests:
`tests/unit/test_api_ai_settings.py` (12), `test_runtime_pipeline.GeneralModeKnowledgeContextTests` (4), 9 matrix rows,
Jest `aiSettings.test.js` (6). **Real Anthropic validation pending** (owner supplies a temporary key through the UI).

## Unresolved production release requirements (recorded 2026-11-18)

None of these is resolved. Each must be closed before production cutover:

1. **Production MongoDB open-file limit.** It is the default 1024; MongoDB
   recommends 64000. Prepared and rehearsed in Phase 6B (`operations.md` §2); applying it needs approval.
2. ~~Rotation strategy for the master encryption key~~: implemented and tested in Phase 6B (`operations.md` §1).
3. **AI provider allowlists and registry defaults**, reviewed against current
   provider offerings.
4. **CORS and rate limiting** when the new router is mounted.
5. **Real knowledge retrieval and evidence validation** (4K). Strict Knowledge
   Only is not complete end to end until 4K passes.
6. **End-to-end integration parity** (4B–4F, `integration-parity.md`).
7. **External cleanup adapters for deletion jobs.** Telegram, Discord and
   Zendesk are implemented and mock-tested; live verification is still needed.
8. **Production backup, restore and cutover rehearsal** (`spec.md` §15): rehearsed on disposable data in Phase 6B;
   the production run is a Phase 7 step (`cutover-plan.md`).
9. **Discord gateway connection adapter (mandatory Phase 4F release
   blocker, C1).** Simulated-gateway tests in 4F, then live verification with
   a test Enterprise application. The worker container is D-Q4.
10. ~~Retention of `outbound_deliveries`~~: resolved by C5 (TTL on terminal
    records only).
11. **Zendesk sandbox verification** of OAuth, webhook triggers, reply modes
    and the sidebar app.
12. **Live verification of strict knowledge** with a real AI provider and real documents.
    Includes: false-refusal and false-answer rates, injection resistance, and the evidence
    threshold calibration (K-B).

## Proposed Phase 4 scope (superseded by the 4A–4F/4K subphase plan above)

1. Login/logout, password reset and change (one auth model), and a
   superadmin bootstrap command.
2. The runtime pipeline and the adapters: Webchat (public routes,
   origins), Telegram (webhook + secret), the Discord worker container
   (D-Q4/D-Q5, routing through binding claims, handoff, passive mode) and
   Zendesk (D-Q9/D-Q10, suggestions).
3. Execution of deferred external shutdowns; a deletion-job worker; a
   binding reconcile schedule.
4. Sub-phase 4K: a tenant-scoped retrieval engine, evidence scoring and a
   structured-claims verifier, so STRICT_KNOWLEDGE_ONLY can be enforced end to
   end.
5. Credential `test` endpoints through `outbound_guard`; channel, integration
   and usage routes; API integrations rekeyed to Bots (Spec 011 guards
   unchanged).
6. Parity tests for every `integration-parity.md` row and provider-sandbox
   end-to-end runs.

## Not authorised

5, 6 and 7 (see table). The Emergent frontend deployment of Specs 009–011 is a
separate pending item: it must ship before a second organisation exists in
production. Committing the Spec 012 work also needs approval.
