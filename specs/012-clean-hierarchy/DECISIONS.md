# Spec 012 — Architectural Decision Record

Approved decisions for Spec 012 (consolidated approval of 2026-11-18, recorded
in `spec.md` §19). This file does not reinterpret them. If the specification
and the implementation disagree, the disagreement is listed under
**Open discrepancies**. It is not resolved silently.

Test paths are relative to `backend/tests/`. "Phase N" names the phase that
implements the runtime part of a decision. Decisions whose behaviour lives in a
later phase list the tests that exist now and what is still pending.

---

## D-Q1 — Replies without a knowledge base / knowledge modes

- **Approved outcome:** three explicit knowledge modes, ordered by strictness:
  - `general` answers normally.
  - `knowledge_preferred` prioritises approved knowledge. It falls back to
    general knowledge **only when Enterprise policy permits it**
    (`knowledge.general_fallback_allowed`, default *not permitted*).
  - `strict_knowledge_only` refuses unsupported answers and fails closed.

  A missing or invalid AI provider or credential is an **admin configuration
  error**. End users get a safe reply, never a misleading "no knowledge base"
  message.
- **Rationale:** today AI replies are effectively off because a canned
  "no knowledge base" text is returned. Enterprises need provable control over
  what an assistant may answer from.
- **Modules:**
  - `core/config_registry.py`: `KnowledgeMode`, the `knowledge.*` properties and
    policies.
  - `core/resolver.py`: `policy_fallback` clamping; `ai` is reported as blocked
    when there is no provider or credential.
  - `core/knowledge.py`: `plan_for`, `gate_before_generation`, `validate_answer`,
    `refusal`, `fail_closed`.
- **Tests:**
  - `unit/test_core_knowledge.py`: scenarios A–J, `test_mode_inherits_down_the_hierarchy`,
    `test_descendant_may_choose_a_stricter_mode`.
  - `unit/test_core_config.py::test_ai_blocked_without_credential`.
  - The runtime reply path (admin error versus end-user message) is Phase 4.

## Strict Knowledge Only — final clarification (D-SK)

- **Approved outcome:**
  - Strict Knowledge Only is a **mandatory Enterprise capability**.
  - If approved evidence is absent, insufficient, contradictory or unavailable,
    the agent must refuse or use its configured fallback.
  - It must **never silently fall back to general AI knowledge** in strict mode.
  - Restrictions are enforced in the **backend runtime**, not merely through
    prompts.
  - Retrieval is tenant-scoped and evidence is validated: every claim must cite
    evidence that was supplied to the model; uncited claims, unknown citations or
    extra free text lead to refusal.
  - No mathematical guarantee of zero hallucinations is claimed. The controls
    make strict answers evidence-bound and refuse whatever the checks cannot
    support.
  - An Enterprise minimum mode can only be tightened below it. Weaker stored
    overrides are ignored, reported and clamped to the minimum.
  - Phase 4 cannot be declared complete while strict mode lacks an enforceable
    retrieval engine and refusal behaviour. A bounded sub-phase "4K" may be
    proposed for this.
- **Rationale:** corporate data protection and answer grounding are core
  enterprise requirements.
- **Modules:**
  - `core/knowledge.py`: the 7-step contract in `spec.md` §19.2.
  - `core/config_registry.py`: `knowledge.minimum_mode` (STRICTEST),
    `knowledge.general_fallback_allowed` (ALLOW, Enterprise-authoritative), and
    `knowledge.allowed_sources`, `access_scope`, `min_evidence_floor`,
    `require_citations`, `document_acl_required`.
  - `core/resolver.py`: `effective_policy` applies the Enterprise default before
    the Organisation, so an Organisation cannot grant fallback.
- **Tests:**
  - `unit/test_core_knowledge.py`: A–J, plus
    `test_strict_mode_forces_citations_and_evidence_floor`,
    `test_approved_sources_never_broaden`,
    `test_unapproved_sources_mean_nothing_is_approved`,
    `test_access_scope_restricts_wider_sources`,
    `test_sources_of_sibling_branches_are_never_in_scope`,
    `test_document_level_acls`.
  - `unit/test_core_architecture.py`: strict-mode inheritance cannot be weakened
    through the API write path.
  - `unit/test_api_config.py`: strict-minimum enforcement through `PATCH …/config`
    and `PATCH …/policy`.

## D-Q2 — "Disabled" = Suspended

- **Approved outcome:** there is no fifth state. "Disabled" means `suspended`
  with authority `object`. An Enterprise or platform suspension can only be
  lifted by that authority or a higher one.
- **Rationale:** one lifecycle model; clear enforcement semantics.
- **Modules:** `core/lifecycle.py` (`Authority`, `apply_action`),
  `core/authz.py::lifecycle_authority`, `api/lifecycle.py`.
- **Tests:**
  - `unit/test_core_lifecycle.py`: `test_suspend_and_resume_by_same_authority`,
    `test_lower_authority_cannot_lift_higher_suspension`,
    `test_suspension_can_be_escalated`.
  - `unit/test_api_lifecycle.py`.

## D-Q3 — Permanent deletion also for Bots and Bot Instances

- **Approved outcome:** Enterprises, Organisations, Bots, Bot Instances and
  Users can all be permanently deleted with the same workflow: preview, then
  confirmation token plus typed name, then immediate deactivation, then an
  idempotent descendant-first job.
- **Rationale:** consistent data-removal guarantees at every level.
- **Modules:** `core/deletion.py`, `api/deletion.py`, `persistence/mongo.py`.
- **Tests:** `unit/test_api_deletion.py`, `unit/test_core_deletion.py`,
  `integration/test_spec012_mongo.py`.

## D-Q4 — Dedicated Discord worker container at cutover

- **Approved outcome:** at production cutover the Discord gateway runs in a
  second container built from the same image (`APP_MODE=worker`). This is a
  compose change, not a VPS, Caddy or WireGuard change.
- **Rationale:** the gateway needs a long-running process, and Discord is
  currently disabled in production.
- **Modules:** `runtime/discord_gateway.py::GatewayManager` runs in the worker
  process, never in FastAPI (Phase 4D). Per D-Q14 it supervises one gateway
  connection per enabled Enterprise application. The compose change is Phase 7.
- **Tests:** `unit/test_api_discord.py::GatewayTests` and
  `integration/test_spec012_mongo.py::MongoPhase4DTests::test_gateway_lease_on_mongo`.
  These use mock connections only; no live gateway is started.

## D-Q5 — Discord DMs

- **Approved outcome:** DMs are off by default (`dm_enabled = false`). They are
  enabled per Instance only when routing is unambiguous and authorised. If
  routing is ambiguous, the request fails closed. DMs are never routed to the
  "first tenant found".
- **Rationale:** today DM routing is cross-tenant.
- **Modules:**
  - `core/hierarchy.py::DiscordBinding`: off by default, and DMs need a bound
    guild.
  - `runtime/routing.py` (Phase 4D): the candidates are only the DM-enabled
    instances of **the application that received the DM** (D-Q14), in servers
    that Discord confirms the author belongs to. Exactly one candidate routes;
    none is silent; more than one is refused (`ambiguous_binding`).
  - `runtime/discord_channel.py::_dm_guilds`.
- **Tests:** `unit/test_core_hierarchy.py::test_discord_validation_and_dm_rule`,
  `unit/test_runtime_pipeline.py::RoutingTests`, and
  `unit/test_api_discord.py::DMTests`. These cover: disabled, one destination,
  several destinations, cross-Enterprise, lifecycle, and setting and
  permission changes.

## D-Q6 — Discord scripted mode retired

- **Approved outcome:** scripted mode (scripted replies, counters, sandbox,
  scenarios) is retired. Staff handoff and passive mode are preserved.
- **Rationale:** about 600 lines plus UI, reachable only from a legacy page.
  It can return later under its own spec.
- **Modules:**
  - `core/hierarchy.py::DiscordBinding`: has `passive_mode`, `staff_role_name`
    and the `handoff_*` fields, and no scripted fields.
  - `integration-parity.md`: D-rows.
- **Tests:** `unit/test_core_hierarchy.py::test_round_trip`.
  `unit/test_core_architecture.py` checks that the binding schema keeps handoff
  and passive mode and rejects scripted fields.

## D-Q7 — Discord and Telegram suggestions retired

- **Approved outcome:** Discord and Telegram are auto-reply channels.
  Agent-assist suggestions remain a Zendesk feature only.
- **Rationale:** the Discord/Telegram suggestions path is V2-only and practically
  unreachable today.
- **Modules:** the channel binding schemas (no suggestion settings) and the
  Phase 4 Zendesk adapter.
- **Tests:** `unit/test_core_architecture.py`, which checks that channel binding
  schemas have no reply-mode or suggestion setting. Zendesk suggestion parity is
  Phase 4.

## D-Q8 — Logos in MongoDB

- **Approved outcome:** small logos are stored in MongoDB: at most 256 KB, MIME
  type sniffed from the bytes, with access restrictions.
- **Rationale:** object storage is not configured in production, so uploads fail
  today.
- **Modules:** `core/hierarchy.py::sniff_logo`, `Bot.logo`. The upload endpoint
  is Phase 4/5.
- **Tests:** `unit/test_core_hierarchy.py::test_sniffing`.

## D-Q9 — Zendesk credentials

- **Approved outcome:** one **platform** Zendesk OAuth client, managed by
  superadmins only, plus a tenant-scoped API-token mode per credential.
- **Rationale:** today any tenant can edit the global OAuth client.
- **Modules:**
  - `core/credentials.py`: `PlatformCredentialKind.ZENDESK_OAUTH_CLIENT`,
    `Provider.ZENDESK_OAUTH`, `Provider.ZENDESK_API_TOKEN`,
    `check_manage_platform`.
  - `api/credentials.py`: platform credential routes.
- **Tests:**
  - `unit/test_core_authz_credentials.py::test_platform_credentials_are_superadmin_only`.
  - `unit/test_api_credentials.py`.
  - Phase 4E: `unit/test_api_zendesk.py::CredentialTests`, `OAuthTests` (the
    platform client needs a `client_id`; OAuth tokens come only from its flow;
    API-token credentials are validated against Zendesk on save).

## D-Q10 — Zendesk webhook signatures required

- **Approved outcome:** new Zendesk integrations require webhook signatures.
  `security.require_webhook_signatures` defaults to true at platform level and
  can only stay required.
- **Rationale:** unauthenticated webhooks can trigger AI replies on tickets.
- **Modules:** `core/config_registry.py` (`PLATFORM_POLICY`, REQUIRE combine).
  Enforced in `api/integrations.py::zendesk_hook` (Phase 4E): no signing
  secret ⇒ every webhook refused; HMAC + ±5-minute timestamp via the kept
  `webhook_auth` module, checked before any other work.
- **Tests:** `unit/test_core_config.py::test_required_controls_stay_required`,
  `unit/test_api_zendesk.py::WebhookTests`.

## D-Q11 — Zendesk ticket creation outside Spec 012

- **Approved outcome:** ticket creation and other new ticket actions (status,
  assignee) are out of scope; a separate spec is needed if wanted.
- **Rationale:** these features do not exist today, so this is not a parity
  item.
- **Modules / tests:** none (recorded in `integration-parity.md`). Phase 4E
  adds no ticket creation; every Zendesk test asserts that no
  `POST /api/v2/tickets` call was made (`unit/test_api_zendesk.py` teardown).

## D-Q12 — Legacy API diagnostics retired

- **Approved outcome:** `api_diagnostics*.py` (about 1.8 k lines) is retired in
  Spec 012. API integrations themselves are ported, keeping their Spec 011
  guards.
- **Rationale:** a large admin-only surface whose value does not justify porting
  it.
- **Modules:** removed in Phase 6B (`api_diagnostics.py`, `api_diagnostics_routes.py`, frontend `ApiDiagnostics`).
- **Tests:** `unit/test_production_app.py::PublicRouteInventoryTests` (reviewed route inventory) and
  `unit/test_core_architecture.py::test_runtime_image_is_the_spec012_release_candidate`.

## D-Q13 — Leaving a Discord server

- **Approved outcome:** leave a Discord server only when no remaining Instance
  needs it and no other tenant could be disrupted.
- **Rationale:** an Enterprise application (D-Q14) is shared by every
  Organisation of that Enterprise.
- **Modules:**
  - `core/bindings.py`: claims are keyed per application and server
    (`discord:<application_id>:<guild_id>`).
  - `core/deletion.py` (X12): "leave guild" is a best-effort cleanup action per
    application and server, decided **when it runs** from the live claims. If
    any instance of that application still holds the server, the action is
    `not_required` and Discord is never called.
  - `runtime/discord.py::discord_cleanup_executors` uses the Enterprise
    application's token, which is kept until the Enterprise job finalises.
  - `api/discord.py::discord_channel_op` (disconnect) re-checks the claims
    immediately before leaving.
- **Tests:**
  - `unit/test_core_deletion.py::ExternalCleanupTests::test_leave_guild_is_decided_at_execution_from_live_claims`
  - `unit/test_api_discord.py::SharedServerTests::test_disconnect_removes_only_this_instance_and_leaves_only_when_unused`
  - `unit/test_api_discord.py::DeletionTests`

## D-Q14 — One Discord application per Enterprise (approved 2026-11-18, Phase 4D) — PERMANENT INVARIANT

> **Permanent architectural invariant (reaffirmed 2026-11-18 with the Phase 4D
> conditional acceptance).** ONE DISCORD APPLICATION PER ENTERPRISE. THE
> PLATFORM SUPERADMIN EXCLUSIVELY CONFIGURES AND CONTROLS THAT APPLICATION.
> ALL ORGANISATIONS WITHIN THE ENTERPRISE USE THAT APPLICATION TO CONNECT THEIR
> AUTHORISED DISCORD BOT INSTANCES. ORGANISATION ADMINS CAN INVITE AND CONNECT
> THEIR BOTS BUT CANNOT EDIT THE ENTERPRISE DISCORD APPLICATION. This must
> never change without explicit authorisation: no global application, no
> Organisation- or Bot-owned applications. It **supersedes** the old
> platform-global Discord application model (spec §5/§6 as first written,
> `PlatformCredentialKind.DISCORD_APP`, parity D1/D3/D15/D16 as first
> written). Permanent regression tests: `unit/test_api_discord.py`
> (`ApplicationOwnershipTests`, `OAuthTests`, `SharedServerTests`,
> `GatewayTests`, `ShareLinkTests`, `SharedCleanupTests`, `DeletionTests`) and
> the matrix rows for every Discord route.

- **Approved outcome (verbatim):** "One Discord application per Enterprise,
  exclusively configured and controlled by the Platform Superadmin, shared by
  all Organisations within that Enterprise for their authorised Bot Instance
  connections."
- **Supersedes:** the single global platform Discord application
  (`spec.md` §5, §6 `PlatformCredentialKind.DISCORD_APP`, and parity D1, D3,
  D15 and D16 as originally written). There are no per-Organisation or per-Bot
  applications.
- **Permission boundaries:**
  - *Platform Superadmin only.* Create, replace or disable the application;
    set the application/client id; set or rotate the bot token and client
    secret; set OAuth permissions and gateway intents; start, stop or restart
    the gateway; set the bot avatar (global to every server); read the full
    secret-free status. Every tenant (including the Enterprise Owner) gets
    404 on these routes.
  - *Enterprise members.* They see only `{configured, available}`.
  - *Instance admins (MANAGE_CHANNEL; Organisation Admins on their own
    Organisation's instances).* They may install through the Enterprise
    application, select channels, set listen mode, reply style, passive mode,
    handoff and DMs, view status, test, disconnect, and change the nickname
    only where their instance is the application's only user of that server.
    They can never edit the application, its token, secret, gateway, avatar,
    OAuth permissions or intents.
- **Binding rules:**
  - Claims are keyed `discord:<application_id>:<guild_id>`. The application id
    is server-set (never from a client) and a binding without it is invalid.
  - Within one application and server, channel scopes are exclusive and a
    whole-server claim (`*`) excludes every channel claim (atomic
    compare-and-swap).
  - Different Enterprises' applications in the same server never collide.
  - An event is routed only through the claims of the application that
    received it. The routed instance must belong to that application's
    Enterprise.
  - Application ids are unique across Enterprises (unique index).
  - An application can be replaced only after every instance using it is
    disconnected.
- **Security:**
  - Token and secret are encrypted at rest and shown only as fingerprints.
  - They are never in responses, logs, audit events, worker status, effective
    configuration or errors.
  - A new or rotated token must belong to the stated application (verified
    against Discord).
- **Modules:** `runtime/discord.py`, `runtime/discord_channel.py`,
  `runtime/discord_gateway.py`, `api/discord.py`, `core/bindings.py`,
  `core/hierarchy.py` (`DiscordBinding.application_id`, `bind_discord_guild`,
  `unbind_discord`), `core/credentials.py` (`DISCORD_APP` removed),
  `persistence/mongo.py`.
- **Tests:** `unit/test_api_discord.py` (57 tests) and the
  `unit/test_api_matrix.py` rows for every new route.

---

## Phase 4D corrections (authorised 2026-11-18 with the conditional acceptance)

- **C1 — Discord gateway release blocker (recorded, not implemented).** The
  supervisor (`runtime/discord_gateway.py`) exists; the real gateway
  connection adapter does not. Discord is **not operationally complete**.
  This is a **mandatory Phase 4F release blocker**. The adapter must provide:
  Enterprise-scoped connection (one per Enterprise application), Discord
  authentication and Identify, heartbeats (with ACK tracking and zombie
  detection), sequence tracking, Resume and reconnect (resume URL, session
  id), gateway rate limits (identify concurrency, 120 events/60 s send
  limit), intent validation against the stored intents, session recovery
  after worker restart, event dispatch to `DiscordChannel.handle_event`,
  worker-lease integration (stop when the lease is lost), safe shutdown, and
  token redaction (never in logs, errors or status). It may be implemented and
  tested against a *simulated* gateway in 4F, subject to separate approval;
  no live gateway is connected.
- **C2 — D3 share link and invite email (implemented).** An instance admin
  creates a revocable, single-use, 7-day share link
  (`discord_share_links`, token stored hashed) optionally restricted to one
  server and optionally emailed (mocked mailer; names stripped of control
  characters; 20 invites/hour/user). The link contains only an opaque token.
  Opening it re-checks the creator's authority, the link, the instance and
  the Enterprise application, then issues a **fresh** 10-minute OAuth state
  bound to that link and instance. The invited Discord administrator's own
  Manage Server authority is verified from Discord at the callback; the link
  is consumed by compare-and-swap. No OAuth state, bot token or client secret
  is ever in a shareable URL.
- **C3 — D11 handoff administration (implemented).**
  `GET /instances/{id}/channel/discord/handoffs` (viewer+),
  `PUT …/handoffs/{channel_id}` (admin; 1–1440 minutes, channel validated
  against the instance's claimed scope) and `POST …/handoffs/{channel_id}/resume`
  (admin). Tenant-scoped by the instance id in the stored key. Staff-only chat
  commands unchanged. The debug-roles endpoint stays retired.
- **C4 — Passive-mode usage rules (documented and tested).** No AI generation
  ⇒ no generation charge (refusals, quota, rate limits and provider failures
  before generation are silent and unbilled). A generation that happened is
  billed **once** (ledger key per message) even when the bot does not post.
  Usage events carry Enterprise, Organisation, Bot and Instance ancestry.
  Duplicate gateway events are deduplicated before the pipeline. Strict
  knowledge cannot be bypassed.
- **C5 — `outbound_deliveries` retention (implemented).** Only terminal
  records (`delivered`, `failed_before_send`, `resolved`) get
  `expires_at_dt` = 30 days (MongoDB TTL). That is far longer than the 24-hour
  request-dedupe window, so expiry can never allow a duplicate send or charge.
  `pending`/`sending`/`retry`/`uncertain` never expire; an admin records the
  review of an uncertain delivery (`POST /instances/{id}/deliveries/{id}/resolve`,
  note required) and it then follows the terminal policy. Tenant-pinned;
  deletion jobs remove a subtree's records with its other runtime data.
- **C6 — Shared-server cleanup (verified and tested).** Disconnect and
  deletion remove only the target instance's claims, never stop the
  Enterprise gateway, never touch the application token, and leave a server
  only when the live claims (re-read immediately before the call) show no
  remaining instance of that application. **Residual risk:** a new install of
  the same server that completes in the milliseconds between that re-check
  and Discord processing the leave can end up bound to a server the bot has
  just left; `channel/test` reports it (the bot is not present) and a
  reinstall fixes it.

## Phase 4E — Zendesk decisions (reviewed 2026-11-18 under the Phase 4E conditional acceptance)

The approved principles of the Phase 4E acceptance were compared with each record. Z-A, Z-F
and Z-H matched and are **APPROVED**. Z-G differed (deletion did not disable the integration
or audit a failed cleanup); it was stopped, amended, approved by the owner and is now
implemented. Z-C, Z-D and Z-E were corrected as directed and are approved because the
implementation and tests now match the records below.

| # | Topic | Decision | Code | Tests |
|---|---|---|---|---|
| X2 | `behaviour.*` at Zendesk integration level | **CLOSED.** A Zendesk integration stores sparse overrides for `config_registry.INTEGRATION_PROPERTIES`; `resolver.resolve_integration` applies them on the Bot's effective values with the same validators, policy constraints and narrowing. Policies are never set on integrations | `core/config_registry.py`, `core/resolver.py`, `api/integrations.py` | `unit/test_api_zendesk.py::ConfigTests` |
| Z-A | Public replies | **APPROVED.** Only a validated *answer* is ever public, only in `auto_reply`, only while policy `zendesk.public_replies_allowed` (ALLOW, default true) permits; refusals, fallbacks and strict-mode refusals are private notes. The sender re-checks mode and policy at send time | `runtime/zendesk_integration.py` | `TicketTests`, `test_knowledge.py::CrossChannelTests` |
| Z-B | Webhook signatures | **Implements D-Q10 strictly.** No unsigned mode; no signing secret ⇒ every webhook refused | `api/integrations.py` | `WebhookTests` |
| Z-C | Webhook provisioning and ownership | **APPROVED (corrected).** Provisioning records `webhook.provision` = {Enterprise, Organisation, integration, Zendesk subdomain, provisioning credential, external webhook id, status}. Rotation, deprovisioning and every cleanup (API or deletion job) use **only** that credential and account, and act only after a GET confirms the webhook still points at this integration (`/api/hooks/zendesk/<integration>/`). Changing or clearing the integration's credential is refused while a webhook is provisioned (deprovision first). If the provisioning credential is revoked, cleanup fails with `credential_unavailable` (integration stays disabled, job preserved) until an admin recovers ownership with a credential for the **same** account that can see the webhook (`PUT …/zendesk/webhook/owner-credential`) — never another account | `api/integrations.py`, `runtime/zendesk_integration.py::_remove_webhook`, `runtime/zendesk.py::zendesk_cleanup_executors`, `core/deletion.py` | `test_api_zendesk_lifecycle.py::WebhookOwnershipTests` |
| Z-D | Comment delivery guarantee | **APPROVED (verified, conservative).** Zendesk documents optimistic locking for ticket updates (`safe_update` + `updated_stamp`; a stale stamp gets HTTP 409 `UpdateConflict`; since 15 May 2025 all ticket updates are optimistically locked) but no idempotency key for ticket updates and no comparison precision (`updated_at` is exposed with one-second resolution). Therefore: an unknown outcome (timeout, dropped connection, 5xx, oversized response after sending) is **never retried automatically**; the part becomes `uncertain`. A 409 proves non-application, so a part's first attempt may refresh the stamp once. The worker reconciles uncertain parts by reading the ticket's comments (same body, visibility and our Zendesk user ⇒ delivered, stored as `comment:<id>`); otherwise it stays uncertain for an operator. Audit ids from Zendesk are stored as `audit:<id>`. At most once per part with explicit uncertainty; never exactly-once | `runtime/zendesk_integration.py::send_part`, `reconcile_deliveries`, `runtime/delivery.py::reconcile_part` | `test_api_zendesk_lifecycle.py::DeliveryGuaranteeTests`, `test_api_zendesk.py::DeliveryTests` |
| Z-E | Event storage and generation identity | **APPROVED (corrected).** One logical event = `<integration>:<sha256(event_id)>` from the signed body (replays dedupe; a new Zendesk event on the same ticket has a new id). **One stable generation = billing identity** `zd:<event>`: a definite pre-generation failure releases it and the retry re-reserves the *same* key; an uncertain provider outcome or a lost result keeps it `uncertain` (never released, never regenerated) until an admin resolves it as `charge` or `waive` (`POST …/events/{id}/resolve`). Retention: 30 days; unresolved uncertain events are kept until resolved. No Zendesk transcript is stored; suggestions are the record | `runtime/zendesk_integration.py`, `runtime/usage.py`, `api/integrations.py` | `test_api_zendesk_lifecycle.py::GenerationIdempotencyTests`, `integration/...::MongoPhase4KTests` |
| Z-F | OAuth credentials | **APPROVED.** OAuth tokens come only from the platform-client flow, stored as Organisation credentials (token set only; the platform client secret is never copied or exposed); reconnecting revokes the replaced token; the platform client (`client_id` required) stays superadmin-only | `api/integrations.py`, `api/credentials.py` | `OAuthTests`, `CredentialTests` |
| Z-G | Integration deletion | **APPROVED (amended 2026-11-18).** Durable **disable → cleanup → finalise**: (1) the request is written atomically on the integration (`deletion` record; `status` = disabled in the same write unless a send lease is live, in which case new work is already blocked and the driver disables it once the lease is free — sends and the request are serialised on one document without transactions); (2) `integration.delete_requested` is audited with a deterministic id (recoverable, exactly once); (3) mandatory cleanup (Z-C rules; an unknown outcome is reconciled by GET before any retry); (4) only then `integration.delete` is audited and the integration, keys, events, suggestions, deliveries and states are removed (audit events kept; OAuth credentials created only for this integration are revoked). A failed cleanup keeps it disabled with a sanitised error and attempt count, audits `integration.delete_cleanup_failed`, returns 502 and is retried by the API or the worker with back-off; re-enabling or changing it is refused (409). Inbound events, generation, suggestions and deliveries all check `active()` (enabled and no deletion) before any externally visible action | `api/integrations.py`, `runtime/zendesk_integration.py` (`request_deletion`, `advance_deletion`, `_lease`) | `test_api_zendesk_lifecycle.py::DeletionStateMachineTests`, `integration/...::MongoPhase4KTests` |
| Z-H | Sidebar app key | **APPROVED.** `bbz_` + 32 random bytes, stored only as SHA-256 in `zendesk_app_keys`, scoped to one integration, shown once, rotated by replacement, revocable; route rate-limited and posts nothing; no `bbk_` keys | `api/integrations.py` | `SidebarTests` |
| Z-I | Not ported | Analytics views are Phase 5 UI; posting on suggestion accept stays NEW-only-if-approved | — | — |

## Part C — provider outcomes and billing (2026-11-18, applies to every channel)

- The provider adapters retry only when the previous attempt **definitely** produced no
  generation (connection refused/connect timeout; 5xx). A timeout or dropped connection
  after sending, or an unusable 200 response, is `ProviderUncertain`: never retried.
- The pipeline keeps an uncertain reservation `uncertain` (counts against quota, never
  settled or released automatically) and returns `kind="uncertain"`; channels never
  regenerate it (Webchat answers 409 "send it again as a new message"; Zendesk events
  wait for an admin's charge/waive decision).
- The usage ledger may re-reserve a key only from `released`/`rejected` (definitely
  unbilled), so retries after confirmed pre-generation failures keep one billing identity.
- No exactly-once AI execution is claimed: an uncertain provider outcome may have generated
  and been charged by the provider.

## Phase 4K — knowledge decisions (reviewed 2026-11-18 under the Phase 4K conditional acceptance)

Each record was compared with the approved principles. K-A, K-B, K-C, K-D, K-F and K-G
matched (K-B, K-C and K-D already described themselves as heuristic/lexical with documented
limits) and are **APPROVED** as stated below. K-E differed only in the point the approval
itself changes (PDF): it is **APPROVED as amended**.

| # | Topic | Decision |
|---|---|---|
| K-A | Storage and engine | **APPROVED.** Knowledge on the existing standalone MongoDB; lexical BM25 retrieval with the access filter inside the query; no new database or hosted retrieval service without approval |
| K-B | Evidence score | **APPROVED (provisional).** Formula unchanged; calibrated on the Phase 4F synthetic corpus: at the default threshold 0.5 recall 16/16 and precision 16/16; every gold chunk scored ≥ 0.7 (13/16 ≥ 0.8) while unrelated chunks scored ≤ 0.4 (2/8 ≥ 0.3). Must be re-calibrated on real documents with a live model (4F live) |
| K-C | Claim validation | **APPROVED as the first layer only; never proof of semantic correctness.** Phase 4F adds layer 2, *sentence alignment*: qualifiers (numbers, currency, dates, negations, modality/permission, conditions/exceptions) in a claim must come from the evidence sentence it quotes, that sentence's conditions/exceptions/negations/modals and the quote's numbers must not be dropped, and ≥ 70% of the claim's content words must come from that sentence. Measured on the synthetic corpus: unsupported answers accepted 28/40 (layer 1) → 5/40 (layers 1+2); correct answers refused 0/16 in both. Residual: appended unsupported conclusions and role/meaning swaps (semantic). See K-H |
| K-D | Contradictions | **APPROVED (provisional).** Deterministic heuristic; direct contradictions (different numbers/negation across documents) are refused in every knowledge mode; indirect contradictions are not detected by the heuristic (documented); strict mode fails closed whenever support is not established |
| K-E | Formats | **APPROVED as amended (2026-11-18).** Text, Markdown, HTML, CSV, JSON, DOCX and **PDF**. PDF uses `pypdf==6.21.4` (new, pinned, pure-Python dependency, documented in `requirements.txt`; PyPDF2 3.0.1 is unmaintained) **only inside an isolated child process**: `%PDF-` signature, ≤ 2 MiB, ≤ 300 pages, ≤ 1.5 M characters, CPU 10 s, address space 512 MiB, 32 open files, no core dumps, 20 s wall clock, minimal environment, `python -I`, networking disabled before pypdf is imported; encrypted PDFs refused; no JavaScript, actions, attachments or external references are ever processed. **OCR is not supported** (scanned/image-only PDFs are refused with that reason). URL ingestion stays deferred |
| K-F | Ownership and permissions | **APPROVED.** Enterprise-approved sources, deny by default; descendants only narrow; document ACLs inside the source scope; Phase 4F adds document validity windows (expired/not-yet-effective documents are never evidence) |
| K-G | Human handoff | **APPROVED.** Only real configured channels: Discord staff handoff when a staff role and cooldown are configured; Zendesk private handoff note; Webchat/Telegram deliver the configured fallback and record the event (no invented destination) |
| K-H | Semantic verifier | **IMPLEMENTED 2026-11-18 as an EXPERIMENTAL additional layer; OFF by default; NOT production-ready.** `runtime/semantic_verifier.py` + `runtime/pipeline.py::_semantic_check`, enabled only by the policy `knowledge.semantic_verification` (REQUIRE: once a parent enables it, descendants cannot disable it). Runs only for grounded answers that already passed the deterministic layers 1+2 (which stay mandatory). The second model call receives **only** the proposed claims (text, verbatim quote, cited ids) and the cited, already-authorised evidence — no history, persona, user identity or uncited evidence. Strict JSON per claim (`supported`, `issues`: changed_condition/exception/role/number, unsupported_conclusion, contradicted, not_in_evidence); every claim must be explicitly supported with no issue; malformed or extra output (including an injected answer), a missing/duplicate claim, uncertainty, provider errors, uncertain provider outcomes and quota exhaustion all **refuse** (fallback/handoff). It never produces answer text, never widens retrieval or access and never bypasses policy. Billing: reserved and settled in the shared ledger under `<generation key>:verify`; a definite provider error releases only the verification reservation; uncertain outcomes are marked uncertain. Evaluation: semantic accuracy (false-acceptance/false-refusal of K-H itself) **cannot be measured without a live model**, which Phase 5 forbids; tests use a scripted verifier and prove the plumbing and fail-closed behaviour only (`test_semantic_verifier.py`, 10 tests). Holdout set for the deterministic layers: `knowledge_holdout_corpus.py` (see IMPLEMENTATION_STATUS Phase 5). A second model can also be wrong; K-H lowers but cannot eliminate unsupported answers |

## Phase 4F — integration preparation and security validation (2026-11-18)

| # | Topic | Decision / finding |
|---|---|---|
| F-A | Throttle under contention | **APPROVED 2026-11-18 (Phase 5, under the condition that it preserves or strengthens every limit; conditions verified).** Finding (4F load test): the compare-and-swap throttle could exhaust its retries under bursts and refuse traffic within the limit (32/100 Telegram replies). Approved form (`core/throttle.py`): one atomic `Store.increment` (MongoDB `$inc` upsert) per attempt on fixed windows aligned to the window length (`<key>@<index>`), and the decision uses a **sliding-window estimate** (current count + previous window count × the share of the sliding window still covering it). This closes the 2× burst a plain fixed window (and the old design) allowed across a boundary, so every limit is at least as strict as before. Refused attempts are undone (not counted); counters expire after two windows; windows use the server clock (workers need synchronised clocks, NTP). Verified: exact limit under 300 concurrent hits, boundary burst refused, weight decay, expiry, key/tenant isolation, reset (`test_core_throttle.py`); every consumer at the API — login per account and per IP, password reset per account and per IP, webchat per IP/session/session-creation, end-user per minute from configuration, Telegram webhook per instance, Zendesk admin-outbound, knowledge upload and Discord invite per user (`test_api_ratelimits.py`); load re-measured in 4F (100/100, 0 spurious 429) |
| F-B | Deletion vs in-flight requests | **Finding (load test):** an Organisation deletion racing 40 chats left 2 runtime records written by requests already in flight. **Fix:** completed deletion jobs schedule a one-time grace re-purge of the same tenant-scoped runtime data after 300 s (> request leases and timeouts), run by the worker. Re-measured: 2 → 0 |
| F-C | Request bodies and headers | **Finding (review):** the new API had no global body limit (only uploads/webhooks were bounded) and no baseline security headers. **Fix:** a middleware buffering at most 1 MiB (3 MiB for document uploads) before the app runs (chunked bodies included; FastAPI's own body errors no longer decide the status), and `nosniff`, `no-referrer`, `no-store`, `X-Frame-Options: DENY` unless a route sets its own CSP (the Webchat widget keeps `frame-ancestors`). Per-user limits on knowledge uploads (120/h) and admin-triggered Zendesk calls (60/h) |
| F-D | Discord gateway adapter | **Implemented (C1 adapter):** `runtime/discord_gateway_ws.py` (Identify, heartbeats/ACK, zombie detection, sequence, Resume/reconnect, op 7/op 9, close-code classification, identify spacing, send budget, intent validation, bounded dispatch queue, safe shutdown, redaction, session recovery via the supervisor). **Verified only against a simulated gateway**; two adapter bugs found by the simulator were fixed (identify wait blocked the read loop; status write raced lease renewal). Live verification is still required |
| F-E | Zendesk fence | **Tightened:** every outbound Zendesk request (not only comment sends) holds its own request lease on the integration document; the deletion transition waits until no lease is live; nothing starts after it (only cleanup/recovery calls are deliberately unfenced) |
| F-G | Knowledge changes during generation | **Fix:** before delivery the pipeline re-verifies the cited evidence (document ready at the same version, source active, validity window) and fails closed if a source was revoked or a document deleted/replaced/expired during generation |

## Phase 5 — frontend and corrections (2026-11-18)

| # | Topic | Decision / finding |
|---|---|---|
| P5-A | Admin app location | The Spec 012 admin app is a new route tree `/app/*` (`frontend/src/app12/`), mounted beside the legacy pages, following spec §12 (URL = selection state; no `selected*` cascade; no `X-Instance-ID`). Legacy pages stay until Phase 6 (spec §14). Its API client (`app12/api/client.js`) is separate from the legacy `utils/api.js` interceptor |
| P5-B | OAuth result pages (C14) | **Narrow backend change.** `Settings.oauth_result_origin` (default unset ⇒ unchanged JSON). When set, the Discord and Zendesk callbacks answer `303` to `<origin>/oauth/<provider>/result?status=<code>[&instance_id|integration_id|guild_id|subdomain]`; only the status code and non-secret ids travel (no code, state or token), `Cache-Control: no-store`, `Referrer-Policy: no-referrer`. Tests: `test_api_discord.py`, `test_api_zendesk.py` (`…redirects_to_the_admin_result_page_when_configured`) |
| P5-C | Cross-origin admin app (CORS) | **Narrow backend change.** `Settings.admin_origins` (default empty ⇒ no CORS headers). When set: Starlette CORS with credentials for the listed exact `https://` origins only (wildcards, `http://` and trailing slashes refused at start-up), methods GET/POST/PUT/PATCH/DELETE, headers `Content-Type` and `X-Requested-With`. CORS never grants authority (the session cookie and CSRF header are still required). Tests: `test_api_security_4f.py::AdminOriginCorsTests`. Must be configured at cutover (bitbotpro.io → api.bitbotpro.io) |
| P5-D | Links carrying tokens | Reset links default to `https://bitbotpro.io/app/reset-password#token=…` (`Settings.reset_url_base`); invitation links are built by the admin UI from the token the API returns once to the creator: `/app/invitations#token=…`. Tokens travel in the URL fragment (never sent to the frontend host) and the page clears the fragment immediately. No invitation e-mail exists in Spec 012 yet (gap) |
| P5-E | No invented routes | `backend/tests/contract/export_frontend_contract.py` exports the mounted route manifest (149 routes) and real response samples from the test World into `frontend/src/app12/contract/`. The client refuses any route not in the manifest; the frontend tests serve only manifest routes with the real response shapes. Re-run the exporter whenever the API changes |
| P5-F | Zendesk deletion state in the API view (B4) | `GET /integrations/{id}` returns `deletion: null | {state: requested | cleanup_pending | cleanup_failed, error, attempts, requested_at, next_attempt_at, retryable}`; a missing integration is deleted. `DELETE` returns `{"deleted": true}` (200) only after finalisation, `202 {state}` while pending, `502 {state: cleanup_failed}` on cleanup failure. The UI never claims completion from anything but `deleted: true`, and hides re-enable/reconnect controls while a deletion is pending (the API also refuses them). Race test: `test_api_zendesk_lifecycle.py::test_deletion_response_never_claims_completion_while_a_comment_is_in_flight` |
| P5-G | Frontend test tooling | Test-only devDependencies pinned: `@testing-library/react 16.3.0`, `dom 10.4.0`, `jest-dom 6.6.3`, `user-event 14.6.1`. Jest maps `@/` and react-router 7 (whose `exports`-only package Jest 27 cannot resolve) and polyfills `TextEncoder`; the Emergent visual-edits Babel plugin is disabled when `NODE_ENV=test` only |
| P5-H | Webchat pages (W2) | **APPROVED 2026-11-18 (Phase 6A).** The API-hosted widget (W-A) is the sole supported widget; the legacy React `ChatWidget`/`ChatPage` are retired and no other widget may be introduced. The admin app uses only the API-provided embed snippet, widget URL and preview URL (`frontend/src/app12/__tests__/w2.test.js`). **Removed in Phase 6B** together with the legacy API (whose `embed.js` pointed customer iframes at the frontend `/widget` route): the release frontend and API go live together (cutover plan §11), and `/widget` and `/chat` show a "this chat window has moved" notice instead of a blank page. Behaviour comparison: the API widget covers popup/full-page modes, theme parameters, welcome text, title, logo, default-open, custom CSS, streaming, session restore and history (`test_api_webchat.py` W1–W6); it renders plain text only (safer, by design) and has no per-message copy button — the two legacy differences |
| P5-I | Gaps found while building the UI (not invented in the frontend) | No usage/billing read endpoint (spec §12 "Usage" tabs not built); no platform user listing or search (superadmin user actions take a user id); no invitation e-mail; credential `test` exists only for Telegram tokens; webchat preview is real usage (W9 "preview" flag not implemented); Zendesk analytics views (Z-I) not built — no endpoint |

## Phase 6A — local end-to-end integration and release corrections (2026-11-18)

| # | Topic | Decision / finding |
|---|---|---|
| 6A-A | Local end-to-end environment | `backend/tests/e2e/server.py` (test only): the real Spec 012 app + worker loop in one process and one event loop, disposable standalone MongoDB (`DB_NAME` must start `bbp_e2e`; refuses the production host), fake Discord/Telegram/Zendesk/AI/mail, same-origin static frontend build (`frontend/build-e2e`), all on an `--internal` Docker network (no internet: no live service is reachable). `/__e2e/*` (outbox, seeded ids) exists only there. Cookie `Secure` is off only in this environment (http) |
| 6A-B | Usage (F1) | `GET /{enterprises|organisations|bots|instances}/{id}/usage?period=YYYY-MM`, `GET /integrations/{id}/usage`. READ on the target; ledger counts by billing state filtered by the target's own id and Enterprise; preview and K-H verification reported separately; quotas: the Enterprise counter only for callers who can read the Enterprise. New ledger indexes per level+period+state |
| 6A-C | User discovery (F2) | `GET /platform/users?q=&after=&limit≤50` (superadmin only; 404 otherwise), e-mail prefix range with cursor pagination via a new narrow store method `find_prefix` (server-built strings only; `_translate` still rejects operators, no regular expressions). `GET /{scope}/{id}/member-candidates?q=` (MANAGE_MEMBERS; ≥3 characters; ≤20 results; only users already in the same Enterprise — no global directory) |
| 6A-D | Invitation e-mail (F3) | `delivery: "email"` (default) renders an escaped template (`api/mail_templates.py`) with `Settings.invitation_url_base` (`…/app/invitations#token=`) and sends it through `Mailer.send_invitation`; the token is not returned. `delivery: "link"` returns the token once (previous behaviour). Unchanged: hashed tokens, 7-day expiry, single use, verified addressee only, authority re-check. New limits: 30 e-mails per inviter per hour, 5 per recipient per day. Mail failure keeps the invitation (`email_sent: false`). No real mail in 6A (mock outbox); a production mail adapter is a cutover item |
| 6A-E | Credential tests (F4) | `POST /credentials/{id}/test` now covers Anthropic/OpenAI (one `GET /v1/models` on the fixed provider host; no generation, no redirects) and Zendesk API-token/OAuth credentials (`users/me` through the guarded Zendesk client), as well as Telegram. Requires *manage* permission on the owner (stricter than use), 30 tests per user per hour, audited, sanitised categories only |
| 6A-F | Preview accounting (F5) | Policy: **preview traffic is counted against every quota and billed exactly like live traffic; it is only labelled.** The label is server-side only: `POST /instances/{id}/preview-session` (EDIT_CONFIG; CSRF via the session; 30/h per user; available instances only) creates a 2-hour chat session marked `preview`; its ledger entries carry `preview: true`. The widget takes that session from a strict URL fragment and clears it. URL/body flags are ignored or refused, so a forged flag can neither label nor bypass anything |
| 6A-G | Deletion safety (Part H) | Finding confirmed: in-flight requests could write runtime records after an Organisation purge. Fixes: (1) the pipeline re-checks availability **after** generation — nothing is stored or delivered for an object suspended/archived/deleted while the provider ran (billed once, since the provider ran); (2) Telegram and Discord senders refuse outbound parts for unavailable instances; (3) the Zendesk request lease also requires the Bot chain to be available. Orphans written in the window are inert (every route 404s; sessions cannot chat; deliveries are not sent) and the grace re-purge removes them (idempotent, tenant-scoped, audit kept). Tests: `test_deletion_safety.py` (7) + mutation checks of all three re-checks |
| 6A-H | Discord availability for Organisation members | `GET /enterprises/{id}/discord` (only `configured`/`available`) now uses navigation access (any member inside the Enterprise), so an Organisation Admin can see whether the Enterprise application is configured before creating an instance. Application administration stays superadmin-only (D-Q14; tested through UI and API) |
| 6A-I | Frontend corrections found by the real stack | Organisation-level members get a limited Enterprise view (their Organisations only; the API refuses the Enterprise object to them by design); breadcrumbs take the Enterprise name from the list they may read; a hook-order defect (caught only by the production build's lint) fixed; WCAG AA colour contrast fixed (muted text, and the primary button colour scoped to the admin app) after the axe audit |
| 6A-J | Knowledge evaluation harness (Part G) | `backend/tests/knowledge_eval/harness.py`: deterministic, verifier (K-H false acceptance/refusal) and end-to-end modes over the corpus and the holdout, with provider errors, latency and tokens/cost. Live providers are refused unless `KNOWLEDGE_EVAL_LIVE=1`, the explicit flag and `KNOWLEDGE_EVAL_API_KEY` are all present. Phase 6A ran scripted mode only (plumbing, not accuracy). Policy is fixed by the harness and recorded in the report |

## Phase 6B — release-candidate preparation (2026-11-19)

| # | Topic | Decision / finding |
|---|---|---|
| 6B-A | Legacy cleanup | Removed per spec §14 (inventory and proofs: `legacy-inventory.md`): Workspace/V2 route modules, `tenant_scope`, `v2_helpers`, migrations and the `manage.py legacy-migrations` command, API diagnostics (D-Q12), the legacy monolith in `server.py` (now wiring only, spec §2) with its scripted mode (D-Q6), templates, `bbk_` API keys, Mongo KB, analytics, customer-owned Discord app setup, Discord/Telegram suggestions (D-Q7); the legacy frontend pages, AuthContext and `X-Instance-ID` client; the React ChatWidget/ChatPage (W2, P5-H — `/widget` and `/chat` show a "moved" notice). 64 legacy live-server suites and 6 legacy unit suites retired with the code (traceability: `security-traceability.md`). Preserved: staff/passive Discord, Zendesk suggestions, the API-hosted widget, Specs 009–011 protections, audit/deletion behaviour, D-Q14 |
| 6B-B | Spec 011 API integrations (B-API — APPROVED: deferred, 6C-B) | Not ported in Phases 4–6A (spec §11 "not yet ported"). Not removed: `api_integrations.py` and `api_integrations_routes.py` stay in the tree, **not mounted and not shipped** in the image, with their module-level SSRF tests (16). The port (Bot Integration `type: api`, guards unchanged) or an explicit retirement must be decided before cutover |
| 6B-C | Account creation (**release blocker B-ACCT** — APPROVED and implemented in 6C-A) | Spec 012 creates users only in the superadmin bootstrap; `/invitations/accept` needs an existing verified account, so nobody new can join. Proposal: when an invitation's address has no account, the invitation page offers "create your account": `POST /api/invitations/register {token, name, password}` (public, CSRF header, per-IP + per-token throttles, password policy, response floor) creates the user with `email_verified=true` (possession of the e-mailed single-use token proves the address), consumes the invitation atomically and grants the role with the same re-validated authority as accept. No self-service sign-up otherwise (L-1). **Not implemented: awaits approval** |
| 6B-D | Changelog (APPROVED: retired, 6C-C) | The legacy admin changelog is not in §14 and not ported. Proposal: retire (no tenant function). Not served by the release candidate; needs approval or a small port |
| 6B-E | Cutover database (APPROVED as design, 6C-D) | Start Spec 012 on a new empty database `bitbotpro_v12`; leave `bitbotpro` untouched until a later allowlisted removal; superadmin re-bootstrapped with the same e-mail. Rollback becomes "start the old image". See `cutover-plan.md` |
| 6B-F | Production wiring | `api/production.py` builds everything from the environment (names kept from the legacy compose: `PUBLIC_API_URL`, `PUBLIC_APP_URL`, `CORS_ORIGINS`, `TRUSTED_PROXIES`, `FROM_EMAIL`); refuses bad configuration (exit 78, variable named, value never shown); `prepare_database` before the first request; `/api/health` + `/api/ready`; Host allowlist; real adapters only; `ResendMailer` over the kept `email_delivery`. `worker.py` (D-Q4) runs the worker and the real gateway adapter; heartbeat health check |
| 6B-G | Encryption keys | Versioned key ring (`enc1:<id>:<token>`), `ENCRYPTION_KEYS(_FILE)` + `ENCRYPTION_ACTIVE_KEY_ID` (single `ENCRYPTION_KEY` = id `k1`), crash-safe resumable rotation with field-level compare-and-set, audit without secrets, rollback by re-activating the old key (`operations.md` §1) |
| 6B-H | Security review | Findings S-1…S-9 (`security-review-6b.md`); S-1…S-6 fixed with tests |
| 6B-I | Images | One image for API and worker; explicit runtime file list; 37 pinned packages (from 140); source-tree label; non-root; health check; Trivy scan in the gate |
| 6B-J | Regression evidence | Phase 6A logs preserved in `evidence/phase-6a/` (final run + both failed runs, checksums); regression tests for the two failures (`test_boundary_sweep_never_allows_more_than_the_limit`, mutation-checked; `TamperedTokenRegressionTests`) |

## Phase 6C — private beta preparation (2026-11-19)

| # | Topic | Decision / finding |
|---|---|---|
| 6C-A | **B-ACCT — APPROVED and implemented** (resolves 6B-C) | `POST /api/invitations/register {token, name, password}` + the admin app's "Create your account" invitation screen. Public (the e-mailed token is the credential), CSRF header, 20/h per IP and 5/h per token, password policy, audited (`user.register_by_invitation`, `invitation.accept`). **Only e-mail-delivered invitations create accounts** (the invitation now records `delivery`): receiving the token at the address is the e-mail verification, so the account is created `email_verified=true`. Link invitations and addresses that already have an account answer 409 "sign in, then open the link again" (the token holder already knows the address — no enumeration). Unknown/used/revoked/expired tokens: uniform 404. Claim and grant are shared with `accept` (atomic single use; role and scope only from the invitation; inviter authority re-validated on the membership snapshot; no cross-tenant grant); the users' unique e-mail index prevents duplicate identities; any failure removes the new account and releases the invitation. The new user is signed in (same session cookie as login). Tests: `test_api_registration.py` (10), Jest `registration.test.js` (3), real-HTTP `test_12`, Chromium B-ACCT test |
| 6C-B | **B-API — APPROVED: deferred** (resolves 6B-B) | The Spec 011 API integrations are not needed by Discord, Telegram, Webchat or Zendesk. Deferred inventory: external API integrations per bot (OpenAPI import, Gridscan reference import, endpoint selection, guarded execution, discover, test connection) — `api_integrations.py`, `api_integrations_routes.py`; registry key `tools.api_calls_enabled` and policy `tools.external_api_calls_allowed` have no runtime effect in the beta. Kept unmounted, disabled and outside the image (tested); no legacy endpoint is exposed |
| 6C-C | **Legacy admin changelog — APPROVED: retired** (resolves 6B-D) | Not rebuilt. A developer-facing `CHANGELOG.md` at the repository root tracks releases |
| 6C-D | **New Spec 012 database — APPROVED as the cutover design** (resolves 6B-E; amends spec §15 for the beta) | A new isolated database (`bitbotpro_v12`) on the existing mongod; the legacy `bitbotpro` database and the production image stay untouched for rollback; the superadmin is bootstrapped with `python -m api.bootstrap` (password typed interactively, never copied). Approves the design only, not execution (`beta-activation-plan.md`) |
| 6C-E | Image hardening | Base image pinned by digest (`python:3.11.18-slim-trixie@sha256:07d48240…`); the only published Debian fix among the remaining high findings (zlib1g `deb13u1`) installed by exact version; residual findings reviewed in `security-review-6b.md` §3 |

## Restoration R1–R3 — restore missing features, prototype-first (approved 2026-11-19)

The user approved restoring the Spec 011 capabilities that the rebuild had removed or deferred: website URL import,
multi-page crawl and refresh (R1/R2) and API integrations with OpenAPI discovery, connection/endpoint tests and
execution during conversations (R3). This **supersedes the 6C-B deferral of B-API**.

- Development is **prototype-first**: working functionality first; fix a specific security problem rather than
  disable a feature; focused tests during development, the full gate before production deployment.
- Kept protections: authentication and roles, tenant isolation, encrypted credentials, the outbound guard against
  private/internal networks (also on every redirect hop), safe webhook handling, basic rate and resource limits,
  audit of sensitive operations, Gridscan and unrelated infrastructure untouched.
- Website content is ingested through the Spec 012 knowledge pipeline (never the retired legacy knowledge base); no
  Cloudflare/anti-bot bypass.
- API calls in conversations: allowed in GENERAL and KNOWLEDGE_PREFERRED when configured and authorised (Enterprise
  policy, Bot configuration, enabled integrations/endpoints); **disabled in STRICT_KNOWLEDGE_ONLY**. API responses are
  untrusted data and never approved knowledge.
- Deployment and pushes still need explicit approval each time.

## Stage 0 — customer AI keys are a core feature (owner requirement 2026-11-20)

The owner confirmed that customer-provided AI API keys, several providers and several saved keys per provider are
essential, and decided parity-audit item **G1**. Implemented in the existing credential architecture (no second
credential system):

- **Selection by name.** `GET/PUT /{enterprises|organisations|bots}/{id}/ai` returns the allowed providers and models
  (platform + Enterprise policy), the credentials usable at that object (`check_use`) and the current selection; the
  PUT validates provider, model and key together (`resolver.apply_override_patch` now applies `ai.provider`,
  `ai.model`, `ai.credential_id` in order). Users never type internal ids. API reference: `api-ai-settings.md`.
- **Editors select.** `ai.provider`, `ai.model` and `ai.credential_id` need Editor (was Admin) — credential *use*
  already needed Editor; Enterprise policies `ai.allowed_providers` / `ai.allowed_models` bound the choice. Managing
  keys still needs Admin on the owning Enterprise/Organisation.
- **Connection test** `POST …/ai/test`: one tiny real generation (≤ 32 output tokens), 20/user/hour, usage event
  `ai_test`, audit `ai.test`, sanitised categories only.
- **Platform AI keys** (`platform_ai_credentials`, superadmin-only routes `/platform/ai-credentials`), granted to
  selected or all Enterprises. Loaded by `enterprise_credentials` only for granted Enterprises as ordinary
  `Credential` objects (`platform=True`), so `check_use`, the resolver and the runtime need no second path; tenants
  can select but never read, rotate or revoke them. (Spec §14 removed the *Emergent* platform key; this is a
  superadmin-controlled replacement, approved by the owner.)
- **Models.** Current Claude models added to the allowlist (`claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`,
  `claude-fable-5-2`), verified as Active against the Claude API model reference (2026-11-20); `claude-sonnet-4-5` removed
  (deprecated, retires 2026-11-30; stored selections fall back to the provider default); defaults unchanged
  (`claude-sonnet-4-6`, `gpt-4o`). These models think by default and `max_tokens` caps thinking plus answer, so the
  adapter sends Haiku 5.5 `thinking: disabled`, Sonnet 5.5 `thinking: between_tools`, and Opus 5.5 / Fable 5.2 (thinking
  cannot be disabled) `output_config.effort: low` with +2048 tokens headroom; OpenAI reasoning models (GPT-5 family,
  o3, o4-mini) get the same headroom. Re-verified on platform.claude.com before deployment (model status table,
  thinking and effort pages): IDs Active; Haiku 5.5 accepts `disabled` at its default effort (medium); Sonnet 5.5
  rejects `disabled` and accepts `between_tools` at its default (high) = no thinking without tools; Opus 5.5 / Fable 5.2
  cannot disable thinking and accept `output_config.effort: low`; `max_tokens` caps thinking + text. Because these
  fields are documented with `anthropic-version: 2026-10-21`, the four newer models are called with that version (its
  breaking changes do not touch our requests); older models keep `2023-06-01` unchanged. An empty provider answer
  (refusal stop or budget spent on thinking) is never delivered: the Bot's fallback message is sent and a usage
  event `empty_answer` recorded. Real calls remain to be confirmed in the Anthropic validation.
- **G1 — GENERAL mode uses approved, selected knowledge as optional reference context.** Retrieval runs in GENERAL when
  the object has approved sources selected (Enterprise approval ∩ selection, unchanged); the evidence is passed as
  untrusted reference material, the answer is a normal general answer (no claim validation, no refusal, no
  citations), conflicting evidence is not offered. KNOWLEDGE_PREFERRED and STRICT_KNOWLEDGE_ONLY are unchanged
  (strict never falls back).

## L-1 — Enterprise creation at launch (approved 2026-11-18, resolves X10)

- **Approved outcome:** for the initial BitBotPro release, **only Platform
  Superadmins can create Enterprises** and name the initial Owner. There is no
  self-service Enterprise creation unless it is separately approved.
- **Rationale:** a controlled launch, with no self-service sign-up abuse
  surface.
- **Modules:** `api/hierarchy.py::create_enterprise` (`require_superadmin`).
- **Tests:** in `unit/test_api_matrix.py`, the `POST /enterprises` row returns
  404 for every non-superadmin actor and 2xx for the superadmin.

## Strict Knowledge Only — Phase 4 completion gate (reaffirmed 2026-11-18)

Phase 4 cannot be declared complete until strict, evidence-based answering
works end to end: tenant-scoped retrieval, the evidence gate, a structured-claims
answer and its validation, and a fail-closed fallback, across every channel and
integration adapter. This requirement must not be weakened to simplify the
runtime.

**Status after Phase 4K (2026-11-18):** the real end-to-end path exists and is tested on
Webchat (including streaming), Telegram, Discord and Zendesk with a mocked AI provider:
store-backed ingestion and retrieval with access control in the query, the evidence gate,
structured claims with verbatim quotes, deterministic validation, contradiction refusal,
retrieval outage/timeout fail-closed, and fallback/handoff. It is **not yet verified with a
live AI provider or production data** (Phase 4F), and claim validation is lexical, not
semantic (K-C). It is not marked complete for release.

## Spec/implementation deviations X1–X14 — decision log (2026-11-18)

Statuses: **APPROVED** (the spec has been amended to match), **APPROVED +
FIXED IN 3.1** (corrected in Phase 3.1), **DEFERRED** (a requirement kept for a
named later phase), **PRESERVED** (a requirement kept and not yet fully met).

| # | Topic | Decision | Spec | Code | Tests |
|---|---|---|---|---|---|
| X1 | Policy key names | **APPROVED:** the namespaced registry keys are authoritative | §4.2 amended | no change | `unit/test_core_config.py` |
| X2 | `behaviour.*` at Zendesk integration level | **CLOSED in Phase 4E** (see the Phase 4E table): integration overrides through the one registry/resolver | §4.1 level Z | `core/resolver.py::resolve_integration` | `unit/test_api_zendesk.py::ConfigTests` |
| X3 | Telegram uniqueness | **APPROVED:** unique by credential and by Telegram bot identity; a rotated token must belong to the same bot | §3.1 amended | as implemented | `unit/test_core_concurrency.py` (telegram), `unit/test_api_credentials.py::test_telegram_token_identity` |
| X4 | Membership storage | **APPROVED (option C) + FIXED IN 3.1:** one document per (enterprise_id, user_id); serialised intent protocol through `membership_guards` | §3.2 amended, §22 | `core/memberships.py` | `unit/test_core_concurrency.py` (`MembershipConcurrencyTests`, `PermissionRevocationConcurrencyTests`), `unit/test_api_members.py::MembershipRevocationApiTests`, `integration/test_spec012_mongo.py::MongoMembershipProtocolTests` |
| X5 | Binding field location | **APPROVED:** binding fields grouped in `channel{}`; required webhook secrets come in Phase 4 | §3.1 amended | as implemented; secrets in Phase 4 | `unit/test_core_hierarchy.py` |
| X6 | Credential usage scope | **PRESERVED; implemented in Phase 4A** (per-credential `usable_by`) | §6 | `core/credentials.py` (`normalise_usable_by`, `check_use`), `api/credentials.py` | `unit/test_core_credential_scope.py`, `unit/test_api_credentials.py::CredentialScopeApiTests`, `unit/test_runtime_pipeline.py::CredentialScopeRuntimeTests`, `integration/test_spec012_mongo.py::MongoPhase4ATests` |
| X7 | Organisation deletion | **APPROVED FOR INITIAL RELEASE:** only Enterprise Owners (or superadmins) may permanently delete Organisations; never Organisation Admins | §8, §9 amended | `core/authz.py` aligned in 3.1 (Enterprise Admins could previously delete Organisations) | `unit/test_core_authz_credentials.py::test_delete_needs_authority_above_the_object`, `unit/test_api_matrix.py` (organisation deletion row) |
| X8 | Audit collection | **APPROVED:** `audit_events` | §3.2 amended | as implemented | `unit/test_api_security.py` |
| X9 | Invitation tokens | **APPROVED:** tokens only in request bodies, never in URLs | §11 amended | as implemented | `unit/test_api_members.py` |
| X10 | Enterprise creation | **APPROVED (L-1):** platform superadmins only at launch | §11 amended | as implemented | `unit/test_api_matrix.py` |
| X11 | Integration-dependent routes | **Implemented in Phase 4E** (Zendesk routes, spec §11/§27) | §11 | `api/integrations.py` | `unit/test_api_zendesk.py`, matrix |
| X12 | External deletion cleanup | **APPROVED + FIXED IN 3.1:** state machine, credential holds, operator resolution | §8 amended, §22 | `core/deletion.py`, `api/deletion.py`, `api/credentials.py` | `unit/test_core_deletion.py::ExternalCleanupTests`, `unit/test_api_deletion.py::ExternalCleanupApiTests`, `integration/test_spec012_mongo.py::test_enterprise_deletion_on_mongo` |
| X13 | Knowledge-source approval | **APPROVED + FIXED IN 3.1:** deny by default; descendants only narrow | §19.2 amended | `core/config_registry.py`, `core/knowledge.py` | `unit/test_core_knowledge_approval.py` |
| W-A | Widget hosting (parity W2) | **APPROVED 2026-11-18.** The iframe widget page is served by the API (`/api/public/widget/{public_id}`), so its CSP `frame-ancestors` enforces the allowed embedding origins. A frontend-hosted page could not do this, because the API cannot set the frontend host's headers. The React `ChatWidget`/`ChatPage` are not yet wired to the new endpoints (Phase 5). | §12 / W2 | `api/webchat.py`, `api/webchat_assets.py` | `unit/test_api_webchat.py` (W2, W11) |
| W-B | Streaming granularity | **APPROVED 2026-11-18.** For safety, every mode streams only *validated* output: the whole answer is generated and validated first, then sent as `token` chunks (same `session`/`token`/`done`/`error` protocol). Live token-by-token model output is not streamed, so the first token arrives later than with the legacy stream. | W5 | `api/webchat.py` | `unit/test_api_webchat.py` (`KnowledgeModeTests`, `W5ChatAndStreamingTests`) |
| W-C | Restricted instances and non-browser clients | **APPROVED 2026-11-18.** When an instance restricts origins, unsafe public requests must carry an allowed `Origin` (browsers always do). Server-to-server callers must therefore send one. This is not authentication. | W11 | `api/webchat.py` | `unit/test_api_webchat.py::W11OriginTests` |
| W-D | Public route shapes | **APPROVED 2026-11-18.** Added `POST /api/public/instances/{public_id}/sessions`. History is at `GET /api/public/instances/{public_id}/sessions/{session_id}/history` (header `X-Chat-Session-Token`) instead of `…/history`. | §11 | `api/webchat.py` | `unit/test_api_webchat.py` |
| T-A | Telegram reply delivery | **APPROVED 2026-11-18 with safeguards (implemented in 4D).** Durable outbound delivery records (`outbound_deliveries`) with states pending, sending, delivered, retry and failed_before_send (both definitely not sent), and uncertain. See the T-A details below. Not exactly-once. | T3/T6 | `runtime/delivery.py`, `runtime/telegram_channel.py`, `runtime/telegram.py`, `runtime/worker.py` | `unit/test_api_telegram_delivery.py`, `integration/test_spec012_mongo.py::MongoPhase4DTests` |
| T-B | Changing a Telegram instance's bot token | **APPROVED 2026-11-18.** A connected Telegram instance must be disconnected before its bot token (credential) is changed, then reconnected. Changing it while connected returns 409, so there is never an orphaned webhook on the old bot. | T1/T2 | `api/hierarchy.py` | `unit/test_api_telegram.py::T2WebhookLifecycleTests` |
| X14 | Invitation listing and lookup | **APPROVED + FIXED IN 3.1:** expired invitations are shown as `expired` (hidden by default); lookup only by the verified addressee | §11 | `api/members.py` | `unit/test_api_members.py::InvitationSecurityTests` |

Details of the approved Webchat decisions (2026-11-18):
- **W-A:** the embeddable widget stays API-hosted and independently
  deployable. The React frontend provides administration and navigation only,
  so there are never two competing widget implementations. Embedding
  restrictions (CSP `frame-ancestors`) are preserved.
- **W-B:** validated-output streaming. The complete answer is generated and
  validated before any factual content is sent, Strict Knowledge Only never
  streams unvalidated output, and the `session`/`token`/`done`/`error`
  protocol is preserved.
- **W-C:** allowed origins (instance ∩ Enterprise policy) restrict browser
  embedding. Origin is not authentication: session tokens, rate limits and
  quotas protect independently. Unrestricted widgets (no allowed origins) can
  be embedded by any site and called cross-origin by any page. That is
  intended for public marketing bots; Enterprises handling sensitive
  conversations should set allowed origins.
- **W-D:** explicit `POST …/sessions` and `GET …/sessions/{session_id}/history`
  (with `X-Chat-Session-Token`). History is never available from the public
  id alone.

Details of T-A (approved with these safeguards, 2026-11-18):
- **Record before send.** One delivery record per reply, with a stable id
  derived from the request. It carries ancestry, the chat or channel and
  source message, and the parts (chunks). It is written when the request
  completes and before any send.
- **No duplicate senders.** A compare-and-swap lease ensures concurrent
  workers never send the same delivery twice.
- **Part state is durable.** Each part is marked `sending` durably *before*
  the external call; the provider message id is recorded on success.
- **Crash recovery.** After a crash, an expired lease with a part in flight
  becomes `uncertain` and is never resent automatically. Parts that were never
  attempted are sent.
- **Safe retries only.** Retries (with back-off, at most 5 attempts) happen
  only when non-delivery is established: connection refused before sending, or
  a 429 rejection. A 400/401/403 is final (`failed_before_send`). A
  timeout/5xx after the request was sent is `uncertain`.
- **Operator visibility.** `GET /api/instances/{id}/deliveries` (admin, no
  message text) and counts in `GET /api/platform/workers`.
- **Isolation.** No secrets are stored; tokens are resolved at send time. The
  sender refuses a record whose ancestry does not match the instance
  (`ownership`).
- **Guarantee, stated precisely.** Telegram has no idempotency key, so the
  guarantee is **at most once per part, with explicit uncertainty**, not
  exactly once. Discord uses `nonce` + `enforce_nonce`, so a timed-out send is
  retried safely inside a 120-second window; after that it is `uncertain`.

### X6 review: credential usage scope

The current model covers three cases:
- An Organisation credential is usable only inside that Organisation.
- An Enterprise credential is usable everywhere in the Enterprise, unless the
  Enterprise policy `credentials.allow_enterprise_credentials` forbids all
  descendant use (all or nothing).
- Use needs Editor on the referencing object; management needs Admin on the
  owner.

What is **not** possible yet:
- restricting an Enterprise credential to particular Organisations or Bots;
- restricting an Organisation credential to particular Bots.

Phase 3 therefore did **not fully satisfy** "credential usage must be
restrictable by authorised scope". **Implemented in Phase 4A:** see the table
above.

**Retained requirement (Phase 4, credential routes):**
- Add a per-credential `usable_by` list of scope ids. It must be a subset of
  the owner subtree and be managed only by credential managers.
- Enforce it in `core.credentials.check_use` on writes and at runtime. An empty
  list means owner subtree only (the current behaviour).
- The capability must not be removed.
