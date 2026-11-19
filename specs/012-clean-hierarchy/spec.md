# Spec 012 — Clean Enterprise Hierarchy, Configuration Inheritance and Core Integrations

## Status

- **Phase 1 (design):** approved 2026-11-18 together with the consolidated
  instruction set ("Final architecture, product requirements and Phase 2
  authorisation"), which supersedes earlier Spec 012 prompts where they conflict.
  §19 records the approved decisions and the knowledge-control design; §20 maps
  Phase 2 to code.
- **Phase 2 (core foundation):** implemented in `backend/core/`; provisionally
  accepted 2026-11-18.
- **Phase 3 (MongoDB persistence + REST API):** authorised 2026-11-18;
  implemented locally in `backend/core/`, `backend/persistence/`, `backend/api/`
  (not mounted, not committed, not deployed) — awaiting review (§21).
- **Phase 3.1 (corrections X4, X12, X13, X14; X7 alignment):** authorised and
  implemented locally 2026-11-18 — awaiting review (§22).
- **Phase 3 + 3.1:** approved 2026-11-18.
- **Phase 4** is split into separately approved subphases 4A (auth, runtime
  foundation, credential scopes, workers), 4B Webchat, 4C Telegram, 4D Discord,
  4E Zendesk, 4K knowledge retrieval and strict evidence-based answering, and
  4F cross-channel integration, parity and security testing. **4A** was
  authorised and implemented locally on 2026-11-18 (§23); it has been
  approved, but strict knowledge is not complete end to end until 4K. **4B
  (Webchat)** was authorised and implemented locally on 2026-11-18 (§24) and
  is provisionally accepted (W-A–W-D approved). **4C (Telegram + Webchat
  billing idempotency)** was implemented locally on 2026-11-18 (§25) and is
  awaiting review. **4D (Enterprise Discord + Telegram T-A correction)** was
  authorised and implemented locally on 2026-11-18 (§26). It introduces
  **D-Q14: one Discord application per Enterprise, controlled only by
  platform superadmins**. It was **provisionally accepted** on 2026-11-18;
  the targeted corrections C1–C6 are done (§26). **Discord is not
  operationally complete:** the real gateway connection adapter is a
  mandatory Phase 4F release blocker. **4E (Zendesk Bot Integration)** was
  authorised and implemented locally on 2026-11-18 (§27) and **provisionally
  accepted**; its decisions were reviewed and Z-C/Z-D/Z-E/Z-G corrected.
  **4K (knowledge retrieval and strict evidence-based answering)** was
  authorised and implemented locally on 2026-11-18 (§28) and is awaiting
  review; strict knowledge is not marked complete for release before live
  verification. 4K was **provisionally accepted**; **4F (integration
  preparation and security validation, simulated services only)** was
  implemented locally on 2026-11-18 (§29) and is awaiting review.
- Remaining phases each require separate approval. Production keeps running
  `bitbotpro-api:spec011-f245d7ed3bdb`.
- Decisions and open spec/code discrepancies: [`DECISIONS.md`](DECISIONS.md);
  phase status, risks and authorisations: [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

Companion document: [`integration-parity.md`](integration-parity.md) — the
Discord, Telegram, Webchat and Zendesk feature-parity matrices (existing
feature → current implementation → new implementation → tests → E2E →
limitations). No essential integration feature is removed silently: every
existing feature is marked `PORT`, `PORT+FIX`, `DECIDE` or `RETIRE?`.

Guiding principle: **one hierarchy, one configuration model, one permission
model, one runtime pipeline, four fully functional integrations.**

---

## 1. Findings that drive the design

| Area | Today |
|---|---|
| Hierarchy | Four overlapping bot concepts: V1 `bot_instances`, V2 `bots`, `bots.legacy_instance_id`, V2 `connections`; "instance" has three meanings. "Workspace" is the tenant root. |
| Configuration | Three disagreeing resolvers (`resolve_bot_config`, `webhook_router.load_connection_context`, `_resolve_llm_config`) + a display-only one; settings stored 2-6 times (persona, listen mode, Discord token, active flags). |
| Lifecycle | No status on workspaces/organisations/bots; nothing checks ancestors at runtime; hard deletes without cascade. |
| Runtime | Discord/Telegram work only through V1; V2 is mostly bookkeeping. Discord is **disabled in production** (`APP_MODE=api`). With the Mongo KB disabled, chat returns a canned "no knowledge base" text whenever a bot has no API integration — AI replies are effectively off. |
| Security debt (pre-012) | Plaintext provider tokens (Discord, Telegram, Zendesk); global Zendesk OAuth client editable by any tenant; any bot member can restart the shared Discord client with their own token or change its avatar; Discord DMs routed to the first tenant found; exception text posted into Discord channels; no embed-origin restriction. |
| Authorisation | Two auth paths (JWT claims vs DB), two superadmin fields, ~111 `X-Instance-ID` routes + 67 V2 routes, no bot-level roles, unvalidated workspace roles, any org member can edit everything. |

## 2. Architecture overview

```
                      BitBotPro Platform  (platform superadmins, platform credentials,
                           │               platform policy & security invariants)
                       Enterprise          (tenant root: plan, policy, defaults, credentials)
                           │
                      Organisation         (team/customer: policy, defaults, credentials)
                           │
                          Bot              (AI assistant: behaviour, AI provider, tools,
                           │                 integrations e.g. Zendesk)
                      Bot Instance         (one deployment on one channel:
                                             discord | telegram | web)
```

Backend modules (small, cohesive; `server.py` becomes app wiring only):

| Package | Responsibility |
|---|---|
| `core/identity.py` | users, sessions, one auth dependency, platform superadmin |
| `core/hierarchy.py` | Enterprise/Organisation/Bot/BotInstance models, ownership, repositories (tenant-scoped queries) |
| `core/config_registry.py` | the single property/policy registry |
| `core/resolver.py` | the single resolver (effective config, sources, policy, availability, permissions) |
| `core/lifecycle.py` | state model, transitions, availability |
| `core/authz.py` | memberships, roles, permission checks |
| `core/credentials.py` | encrypted credential store, use/manage separation, rotation/revocation |
| `core/deletion.py` | archive, deletion preview, deletion jobs |
| `runtime/pipeline.py` | the one bot runtime pipeline |
| `runtime/channels/{discord,telegram,web}.py` | channel adapters |
| `runtime/integrations/zendesk.py` | Zendesk integration adapter |
| `api/*.py` | REST routers (thin) |
| `worker.py` | long-running process: Discord gateway, deletion jobs, scheduled tasks |
| kept | `auth_security`, `password_policy`, `rate_limit`, `client_ip`, `security_headers`, `log_redaction`, `email_delivery`, `runtime_config`, `oauth_state`, `webhook_auth`, `outbound_guard`, `security_utils`, `chat_sessions`, `api_integrations` (rekeyed to bot) |

## 3. Data model

### 3.1 Hierarchy objects

All four share: `id` (UUIDv4, server-generated), `name`, `lifecycle{…}` (§7),
`overrides{key: value}`, `created_at/by`, `updated_at/by`. Enterprise and
Organisation also have `policy{key: value}`.

| Collection | Object-specific fields | Ancestry (server-assigned, immutable) |
|---|---|---|
| `enterprises` | `slug` (unique), `plan` | — |
| `organisations` | `description` | `enterprise_id` |
| `bots` | `description`, `logo` (D-Q8) | `enterprise_id`, `organisation_id` |
| `bot_instances` | `channel{type: discord\|telegram\|web, …binding}`, `public_id` (web), `webhook_secret_hash` (telegram), `credential_id` (telegram) | `enterprise_id`, `organisation_id`, `bot_id` |

Ownership rules (all server-side): parents are loaded from the database by the
id in the URL of the create call; ancestry fields are copied from the loaded
parent and never accepted from clients; no update path can change them; every
child query includes `enterprise_id` (compound indexes); a child can only be
created under an existing, non-deleted parent, so no orphans exist; deletion is
descendant-first (§8).

Multiple instances of one bot on the same channel type are allowed when their
bindings differ. Exclusive bindings are claimed atomically: Discord
`(guild_id, channel scope)` (a whole-guild binding overlaps every channel of
that guild); Telegram **credential id and Telegram bot identity** (two
credentials holding one token can never serve two instances; a rotated token
must belong to the same bot — amendment X3); web `public_id`. Channel binding
fields (including `public_id`) live inside the instance's `channel{}`; the
Telegram webhook secret is added in Phase 4 (amendment X5).

### 3.2 Supporting collections

| Collection | Responsibility |
|---|---|
| `users` | identity: email, password hash, `is_platform_superadmin` (the only superadmin field), verification, `token_version` |
| `memberships` | **one document per (enterprise_id, user_id)** (unique index): Enterprise `role` (if any) + `grants[{scope_level: organisation\|bot, scope_id, role}]` + `version`; changes serialised through `membership_guards` (exact owner count, intent protocol) — amendment X4 |
| `invitations` | `token_hash, email, enterprise_id, scope, role, invited_by, status, expires_at` |
| `credentials` | `enterprise_id, organisation_id?, provider, name, secret_enc, fingerprint, status (active\|revoked), usable_by_scope, created_by, rotated_at` |
| `platform_credentials` | superadmin-only: `zendesk_oauth_client` (D-Q9). There is no global Discord application (D-Q14) |
| `discord_applications` | D-Q14: one per Enterprise (id = Enterprise id; unique `application_id`), encrypted bot token and client secret, permissions, intents, enabled/disabled, superadmin-set gateway desired state. Superadmin-only administration |
| `discord_installations`, `discord_gateway_status`, `oauth_states` | Servers where an application is installed; sanitised per-application gateway status with a worker lease; hashed single-use OAuth states and pending channel selections (TTL) |
| `outbound_deliveries` | T-A: durable outbound replies (Telegram, Discord) with per-part state; ancestry-scoped |
| `bot_integrations` | Bot-owned integrations: `type: zendesk \| api` (API integrations of spec 011), settings, `credential_id`, status, webhook/sidebar secrets (hashed) |
| `api_endpoints` | endpoints of `api` integrations (spec 011 guard) |
| `conversations` | runtime state: ancestry + `instance_id` or `integration_id`, `session_id`, messages, `expires_at` (retention policy) |
| `suggestions` | Zendesk agent-assist/suggest-only outputs (integration-scoped) |
| `usage_events` | metering (LLM calls, skips, tool calls) with ancestry; monthly counters per enterprise/organisation |
| `channel_state` | runtime-only state: Discord handoff per (instance, channel), Telegram callback mappings, dedupe keys (TTL) |
| `deletion_jobs` | archive/deletion audit + job progress (§8) |
| `audit_events` | new audit collection for the Spec 012 API (amendment X8) |
| `security_counters`, `oauth_states` | unchanged responsibilities |

Configuration (overrides/policy), identity (users), permissions (memberships)
and runtime state (conversations, channel_state, usage) are kept in separate
collections.

## 4. Configuration inheritance and policy

### 4.1 Registry (single source of truth: `core/config_registry.py`)

Every property is declared once with: **name, type, default, levels where it
may be set, inheritable, overridable, validator, security class
(`public` = may reach anonymous clients, `internal` = admins only,
`credential_ref` = id of a credential, never a secret value), policy
constraint.** Unknown keys are rejected on write and ignored on read.

Levels: E Enterprise · O Organisation · B Bot · I Bot Instance · Z Zendesk
integration (Bot-scoped; inherits the Bot's effective values).

| Key | Type / values | Default | Set at | Security | Policy constraint |
|---|---|---|---|---|---|
| `ai.provider` | anthropic \| openai (\| ollama if platform-allowlisted) | anthropic | E O B | internal | `allowed_ai_providers` |
| `ai.model` | registry allowlist per provider | provider default | E O B | internal | `allowed_models` |
| `ai.credential_id` | credential in ancestry, provider must match | — ⇒ AI blocked | E O B | credential_ref | `credential access` |
| `behaviour.persona` | text ≤4000 | platform text | B I Z | internal | — |
| `behaviour.instructions` | text ≤8000 | "" | B I Z | internal | — |
| `behaviour.tone_instructions` | text ≤4000 | "" | B I Z | internal | — |
| `behaviour.tone_examples` | list ≤10 | [] | B | internal | — |
| `behaviour.language` | BCP-47 | en | E O B I Z | internal | — |
| `behaviour.knowledge_gate` | strict \| flexible | strict | E O B I Z | internal | `require_strict_knowledge_gate` |
| `behaviour.knowledge_gate_pct` | 0-100 | 50 | E O B I Z | internal | — |
| `behaviour.fallback_message` | text ≤1000 | platform text | E O B I Z | public | — |
| `tools.api_calls_enabled` | bool | false | B I Z | internal | `external_api_calls` |
| `limits.end_user_msgs_per_minute` | int ≥1 | 10 | E O B I | internal | `max_end_user_msgs_per_minute` |
| `widget.title`, `widget.welcome_message`, `widget.theme`, `widget.default_open`, `widget.custom_css_url` (https) | text/map/bool/url | platform | B I | public | — |

Channel and integration settings are **object-specific** (live on the instance
`channel{}` / integration, validated by the same registry module, not
inherited): Discord `guild_id, channel_ids, listen_mode, mention_overrides_channels,
reply_style, passive_mode, staff_role_name, handoff_cooldown_minutes,
handoff_followup_message, dm_enabled`; Telegram `credential_id, listen_mode,
chat_ids`; Web `allowed_origins`; Zendesk `credential_id, reply.mode
(auto_reply | agent_assist | suggest_only), ticket_groups, ticket_tags,
sidebar_enabled`.

### 4.2 Policies (enforced, tighten-only)

| Policy key | Type | Combine down the tree | Set at |
|---|---|---|---|
| `ai.allowed_providers` | set | intersection | Platform, E, O |
| `ai.allowed_models` | set (unset = all registry models) | intersection | E, O |
| `channels.allowed` | ⊆ {discord, telegram, webchat} | intersection | E, O |
| `integrations.allowed` | ⊆ {zendesk, api} | intersection | E, O |
| `tools.external_api_calls_allowed` | bool (allowed) | AND | E, O |
| `webchat.public_access_allowed` | bool (allowed) | AND | E, O |
| `webchat.allowed_embed_origins` | set of https origins (unset = any) | intersection | E, O |
| `knowledge.*` | see §19.2 (supersedes `require_strict_knowledge_gate`) | | E, O |
| `security.require_webhook_signatures` | bool (required control) | OR | Platform, E, O |
| `limits.max_end_user_msgs_per_minute` | int | min | Platform, E, O |
| `limits.max_monthly_ai_messages` | int (quota) | min; usage counted at E and O | E (plan), O (sub-quota) |
| `retention.conversation_days` | int | min | Platform, E, O |
| `credentials.allow_enterprise_credentials` | bool (may bots use Enterprise credentials?) | AND | E |
| `delegation.*` (`org_admins_manage_members`, `org_admins_manage_credentials`, `bot_admins_manage_members`) | bool | AND | E |

Key names are the namespaced names of `core/config_registry.py`, the
authoritative registry (amendment X1, approved 2026-11-18).

Platform security invariants are not keys at all and cannot be configured
anywhere: specs 009-011 outbound guards (SSRF, DNS rebinding, redirects, TLS,
metadata/internal blocking), Zendesk host construction, Ollama allowlist, KB URL
imports disabled, secret redaction, rate-limit floors, encryption at rest.

### 4.3 Storage and resolution

Objects store only explicit `overrides` (and `policy`). Effective values are
never copied to descendants, so parent changes propagate automatically and
`PATCH {key: null}` removes an override and restores the inherited value.

`resolver.resolve(chain, actor=None) → Effective` (pure; one DB loader fetches the
chain by id and verifies that ancestry ids match, failing closed otherwise):

1. **Availability** — every object in the chain is `active` (§7); else
   `available=false` with the first blocking `(level, id, state)`.
2. **Policy** — platform policy, then each level's policy combined with the
   key's operator ⇒ effective policy `P` (never weaker than any ancestor).
3. **Values** — for each key, walk the chain from most specific to Enterprise,
   considering only levels where the key may be set; the first override that is
   valid **and complies with `P`** wins (`source = level+id`). Invalid or
   non-compliant stored overrides (e.g. after a parent tightened its policy) are
   skipped and listed in `conflicts`; then registry default. If no compliant
   value exists the key is `blocked` with a reason (e.g. no allowed AI provider ⇒
   AI unavailable).
4. **Channel/integration checks** — channel type ∈ `allowed_channels`, web
   requires `public_web_access`, integration type ∈ `allowed_integrations`,
   credential usable (active, in scope, provider matches).
5. **Permissions** (when `actor` given) — effective role and allowed actions on
   the object and whether each key is overridable *by this actor* (role ∩
   `delegation.*` policy).

Output `{available, reason, values, sources, policy, conflicts, blocked,
overridable, permissions}`; values classed `credential_ref` are returned as
`{id, name, provider, fingerprint, status}` only. The admin API, the UI, every
runtime adapter, streaming and background jobs use this function; no other code
reads `overrides`.

**Write validation** uses the same registry: key known; level allowed; actor
permitted; type and validator pass; value complies with the node's current
effective policy; a policy write may only tighten its parent's effective policy.
A parent change never rewrites descendants.

## 5. Bot Instances and integrations

- **Bot Instance** = one channel deployment: `discord`, `telegram` or `web`.
  Lifecycle hooks drive external state: activate ⇒ register (Telegram
  `setWebhook`, Discord routing entry, web public id live); suspend/archive/delete
  ⇒ deregister (`deleteWebhook`, routing removal, optional Discord leave-guild).
- **Discord (amended by D-Q14, 2026-11-18):** each **Enterprise** has its own
  Discord application, configured and controlled exclusively by platform
  superadmins. All Organisations of that Enterprise use it to install and
  connect their own Bot Instances. Instances bind to a server (and an optional
  channel scope) through the OAuth install flow, as exclusive claims
  `discord:<application_id>:<guild_id>`. Different Enterprises' applications in
  the same server never collide. The gateway runs in the **worker** process
  (D-Q4), with one supervised connection per enabled Enterprise application.
- **Telegram**: one bot token (Organisation credential) per instance; webhook
  per instance with path secret + required `secret_token` header.
- **Web**: `public_id` for anonymous clients; `allowed_origins` enforced (new).
- **Zendesk** is a **Bot Integration** (`bot_integrations.type = zendesk`):
  credential (OAuth or API token, Organisation-owned), webhook endpoint per
  integration, routing filters, reply mode, sidebar key. Its actions (fetch
  ticket/comments, add public comment or private note with tags) run through
  the Zendesk adapter, called by the runtime pipeline with the Bot chain + the
  integration's resolved settings.
- Full feature lists, tests and limitations: `integration-parity.md`.

## 6. Credentials

- Owned by an **Enterprise** or **Organisation** (`credentials`); platform-level
  `zendesk_oauth_client` in `platform_credentials` (superadmin only). Each
  Enterprise's Discord application (D-Q14) lives in `discord_applications` and
  is administered only by superadmins. Enterprise and Organisation users can
  use it (install, connect) but never manage it.
- Secrets are encrypted at rest (Fernet, existing `ENCRYPTION_KEY`), write-only:
  APIs return `{id, name, provider, fingerprint, status, rotated_at}` only; never
  logged (log redaction), never in errors, never in effective configuration
  (only `credential_ref`), never copied by cloning (clones keep the reference and
  are only allowed inside the credential's scope).
- **Use** vs **manage**: *manage* (create, replace secret, revoke, delete) =
  admin of the owning scope (subject to `delegation.org_admins_manage_credentials`);
  *use* (reference from a bot/instance/integration) = editor+ on the referencing
  object **and** the referencing object lies inside the credential's
  `usable_by_scope` (default: owner subtree; `credential_scope` policy may forbid
  Enterprise credentials for bots). Cross-tenant references are impossible
  (same `enterprise_id` + ancestry check on write and on resolve).
- Replacement keeps the id (references unchanged), records `rotated_at`, clears
  provider caches; revocation sets `revoked` (runtime fails closed with a typed
  error), triggers provider revocation where supported (Zendesk OAuth), and is
  audited.

## 7. Object lifecycle

States: **Active · Suspended · Archived · Deleted** (identical for all four types).

`lifecycle = {state, changed_at, changed_by, reason, suspension_authority?}`

| State | Meaning | Data | Runtime |
|---|---|---|---|
| Active | normal | kept | available if all ancestors active |
| Suspended | temporarily disabled. `suspension_authority` records who suspended it: `object` (the object's own admins — this is "disabled"), `enterprise` (enterprise owner on a descendant), `platform` (superadmin enforcement). Only the same or a higher authority may resume. | kept, editable | unavailable |
| Archived | retired; configuration read-only; hidden by default; reversible (unarchive) by admins | kept | unavailable |
| Deleted | permanent deletion in progress (§8): access revoked and runtime off immediately; records removed by the deletion job; after completion only the `deletion_jobs` audit entry remains | being removed | unavailable |

- **Availability** = own state Active **and** every ancestor Active, computed by
  the resolver on every runtime operation (web request, Discord event, Telegram
  update, Zendesk webhook, streaming continuation, background job step). No
  inherited flags are written to children.
- Each object keeps its own state: resuming a parent never changes a child's own
  Suspended/Archived state.
- Suspension takes effect immediately: runtime adapters resolve per event, the
  Discord worker's routing table and caches are invalidated on change, and
  in-flight streams check availability before each LLM call.
- Children can be created only under Active or (object-)Suspended parents.

## 8. Archive and permanent delete

**Archive** (all four types): admin action; state → Archived; reversible; data retained.

**Permanent delete** (Enterprise, Organisation, Bot, Bot Instance, User):

1. `POST /api/{type}/{id}/deletion-preview` → exact counts per collection,
   external actions (Telegram `deleteWebhook`, Zendesk token revocation, Discord
   routing removal/leave-guild), safeguard results, and a short-lived
   `preview_token` bound to (actor, object, counts).
2. `POST /api/{type}/{id}/delete {preview_token, confirm_name}` (typed name of the
   object). Server re-checks authorisation and safeguards, then **immediately**:
   state → Deleted on the target (all descendants unavailable via §7), memberships
   and invitations of the subtree deleted (access revoked), channel binding
   claims released. External cleanup is recorded per resource as an action
   (amendment X12): mandatory (Telegram `deleteWebhook`, Discord routing
   removal, Zendesk token revocation and webhook-subscription removal) or
   best-effort (Discord leave-guild, decided at execution from the live claims of
   all tenants). Actions are idempotent, retried with back-off and audited; the
   credentials they need are kept (encrypted) until they resolve; a job reaches
   `completed` only when every mandatory action has succeeded or a platform
   superadmin has explicitly established it as not required (job states
   `pending → running → awaiting_external_cleanup → completed`, failure states
   `failed`, `cleanup_failed`).
3. A `deletion_jobs` record drives an idempotent, resumable job (worker):
   descendant-first — external shutdowns → conversations, suggestions, usage,
   channel_state → bot integrations, API endpoints → credentials → instances →
   bots → organisations → enterprise. Every step deletes by ownership query, so
   re-running is safe; progress, counts and errors are recorded; completion
   status is auditable (`GET /api/deletion-jobs/{id}`).
4. Scope guarantees: the job only matches documents with the target's ancestry
   ids (no cross-tenant deletion); deleting an Organisation never touches its
   Enterprise or siblings; no usable orphans remain (children are unavailable
   from step 2 and removed by step 3).

**User deletion** (self-service or superadmin/enterprise owner for members):
removes the user document, all memberships, invitations addressed to the user,
pending invitations the user issued, sessions (document gone ⇒ tokens invalid),
and personal data (profile). Shared resources the user created (bots,
credentials, conversations) stay with their owning scope; audit entries keep
only the user id.

**Safeguards:** the last platform superadmin cannot be deleted or demoted; the
last Owner of an Enterprise cannot leave, be removed, be demoted or delete their
account until another Owner exists or the Enterprise is deleted; Enterprise
deletion requires Owner (or superadmin); **Organisation deletion requires an
Enterprise Owner** (or superadmin) for the initial release; Organisation admins
cannot delete their Organisation (amendment X7).

**Backups:** permanent deletion removes live data only. Copies in database
backups (`~/.bitbotpro/backups`, retention documented in the operations runbook,
proposed 30 days) persist until they age out; the UI and docs must say so.

## 9. Authentication, memberships and permissions

- **One authentication model:** login issues a JWT `{sub, tv, iat, exp}` in an
  httpOnly cookie (Bearer accepted for API clients); every request loads the
  user from MongoDB and checks `token_version`. Password reset/change, role
  changes and deletion end sessions. One superadmin field:
  `users.is_platform_superadmin`.
- **Roles:** Owner (Enterprise only) · Admin · Editor · Viewer; memberships at
  Enterprise, Organisation or Bot scope. A user's effective role on an object =
  highest role among memberships on the object and its ancestors, limited by
  Enterprise `delegation.*` policy. Superadmins act above Enterprises (audited).

| Action | Viewer | Editor | Admin | Owner |
|---|---|---|---|---|
| Read objects, effective config (with sources), conversations, usage | ✓ | ✓ | ✓ | ✓ |
| Edit overrides, behaviour, widget, integration settings, approve conversations | | ✓ | ✓ | ✓ |
| Use credentials (reference) | | ✓ | ✓ | ✓ |
| Create children, channel binding, suspend (object authority), archive, policy, manage credentials, manage members ≤ own role | | | ✓ | ✓ |
| Permanently delete Bots and Bot Instances (preview + confirm) | | | ✓ | ✓ |
| Delete Enterprise or Organisation, manage Owners, billing/plan, enterprise-authority suspension | | | | ✓ |

- **Rules:** server-side only; unknown or unauthorised ids ⇒ 404 (no existence
  oracle); a user can grant only roles ≤ their own effective role at that scope
  (Owner only by Owner); Organisation/Bot memberships require Enterprise
  membership; no client header carries authority (no `X-Org-ID`,
  `X-Instance-ID`, `X-Workspace-ID`); membership changes are audited.
  Authorisation (`core/authz.py`) is independent of configuration inheritance;
  the resolver only reports the result.

## 10. One runtime pipeline

```
adapter.receive(event) → authenticate source (session token | webhook secret+signature | gateway)
  → load chain (instance or integration → bot → organisation → enterprise)
  → resolve (availability, values, policy, credential)      ── unavailable ⇒ adapter.silent/404
  → limits (per-end-user rate, monthly quota at E and O; atomic counters)
  → conversation context (session per channel rules)
  → prompt (one builder from resolved values + knowledge/tool results)
  → AI provider adapter (anthropic | openai | allowlisted ollama), decrypted credential in memory only
  → stream or collect (availability re-checked before each provider call)
  → persist conversation + usage_event
  → adapter.deliver (Discord send/split, Telegram send/buttons, SSE events, Zendesk comment/note/suggestion)
errors → typed (UserFacing, ProviderAuth, ProviderRate, Unavailable, Internal) → adapter maps to a fixed message; details only in redacted logs
```

Discord, Telegram and Web are channel adapters; Zendesk is an integration
adapter invoked by the pipeline for Zendesk-triggered workflows (webhook,
sidebar). API tool calls (spec 011) run inside the pipeline when
`tools.api_calls_enabled` and policy allow.

## 11. REST API (Enterprise terminology; replaces `/api/v2/*` and legacy routes)

```
Auth         POST /api/auth/{login,logout,forgot-password,reset-password,change-password}  GET /api/auth/me
Enterprises  GET|POST /api/enterprises (create: platform superadmins only at launch — L-1)   GET|PATCH /api/enterprises/{id}
Hierarchy    GET|POST /api/enterprises/{id}/organisations   GET|PATCH /api/organisations/{id}
             GET|POST /api/organisations/{id}/bots          GET|PATCH /api/bots/{id}
             GET|POST /api/bots/{id}/instances              GET|PATCH /api/instances/{id}
Lifecycle    POST /api/{type}/{id}/{suspend,resume,archive,unarchive}
             POST /api/{type}/{id}/deletion-preview   POST /api/{type}/{id}/delete   GET /api/deletion-jobs/{id}
Config       GET|PATCH /api/{type}/{id}/config          GET|PATCH /api/{enterprises|organisations}/{id}/policy
Members      GET|POST /api/{enterprises|organisations|bots}/{id}/members   PATCH|DELETE …/members/{user_id}
             POST|GET /api/enterprises/{id}/invitations   DELETE /api/invitations/{id}
             POST /api/invitations/lookup {token}   POST /api/invitations/accept {token}   (tokens only in bodies — X9)
Users        GET|PATCH|DELETE /api/users/me (+deletion-preview)   /api/platform/users… (superadmin)
Credentials  GET|POST /api/{enterprises|organisations}/{id}/credentials
             PATCH(replace)|POST(revoke|test)|DELETE /api/credentials/{id}
Channels     POST /api/instances/{id}/channel/{connect,disconnect,test,rotate-secret}   (Telegram: 4C; Discord: disconnect, test)
             GET  /api/instances/{id}/channel/{status,discord/channels}   POST …/discord/install-url
             PUT  /api/instances/{id}/channel/discord/{settings,nickname}   GET /api/instances/{id}/deliveries (T-A)
             POST /api/instances/{id}/deliveries/{did}/resolve (C5)   POST|GET|DELETE …/channel/discord/share-links (C2)
             GET …/channel/discord/handoffs   PUT …/handoffs/{channel_id}   POST …/handoffs/{channel_id}/resume (C3)
Discord app  GET /api/enterprises/{id}/discord (members: configured/available only)
             GET|PUT /api/platform/enterprises/{id}/discord-application   POST …/{rotate-token,rotate-secret,enable,disable,avatar}
             POST …/discord-application/gateway/{start,stop,restart}   (superadmin only; D-Q14)
Integrations GET|POST /api/bots/{id}/integrations   GET|PATCH|DELETE /api/integrations/{id}   GET|PATCH …/config (X2)
             POST /api/integrations/{id}/{test,zendesk/oauth/start,rotate-webhook-secret,zendesk/webhook/provision}
             GET …/zendesk/webhook   PUT …/zendesk/signing-secret   POST|DELETE …/sidebar-key
             GET /api/integrations/{id}/zendesk/sidebar-app.zip   GET …/suggestions   POST …/suggestions/{sid}/{accept,dismiss}
             GET …/deliveries   (+ spec 011 api-integration routes under /api/integrations/{id}/…, not yet ported)
Runtime data GET /api/instances/{id}/conversations   GET /api/integrations/{id}/suggestions   GET /api/{type}/{id}/usage
Knowledge    POST|GET /api/{enterprises|organisations}/{id}/knowledge/sources   GET|PATCH|DELETE /api/knowledge/sources/{id}
             PUT /api/knowledge/sources/{id}/documents/{name} (raw body)   GET …/documents
             GET|DELETE /api/knowledge/documents/{id}   PUT …/acl   POST …/reprocess
Zendesk ops  GET /api/integrations/{id}/events   POST …/events/{event_id}/resolve (charge|waive)
             POST …/zendesk/webhook/deprovision   PUT …/zendesk/webhook/owner-credential
Public       GET /api/public/discord/install/{token} (C2 share link → Discord authorisation)
             GET /api/public/instances/{public_id}[/logo]  POST …/sessions  POST …/chat  POST …/chat/stream
             GET …/sessions/{session_id}/history (X-Chat-Session-Token; W-D)  GET /api/embed.js
             GET /api/public/widget/{public_id}  GET /api/public/widget.{js,css} (API-hosted widget; W-A)
Hooks        POST /api/hooks/telegram/{instance_id}/{secret}   POST /api/hooks/zendesk/{integration_id}/{secret}
             POST /api/hooks/zendesk/sidebar/generate   GET /api/hooks/{discord,zendesk}/oauth/callback
Platform     /api/platform/{stats,enterprises,users,credentials/zendesk_oauth_client,workers,audit}
```

`{type}` ∈ enterprises | organisations | bots | instances. Every handler:
authenticate → load target by id → derive ancestry from the database → authorise.

## 12. Frontend

- URL is the selection state: `/app` → `/app/enterprises/:id` →
  `/app/organisations/:id` → `/app/bots/:id` → `/app/instances/:id`
  (+ `/app/integrations/:id`, `/app/platform/*`); breadcrumbs from ancestry; no
  `selected*` cascade, no `X-Instance-ID` interceptor; only the session cookie.
- Pages: Enterprises list · Enterprise (Organisations, Settings, Policy, Members,
  Credentials, Usage) · Organisation (Bots, Settings, Policy, Members,
  Credentials, Usage) · Bot (Instances, Behaviour, Integrations, Members, Usage)
  · Instance (Channel setup, Overrides, Install/embed, Conversations) ·
  Integration (Zendesk: connect, filters, reply mode, webhook, sidebar,
  suggestions) · Platform (Enterprises, Users, Platform credentials).
- Shared components: `ConfigPanel` (effective value, origin badge "Inherited from
  Organisation ‹name›", override toggle, reset to inherited, policy lock with
  reason), `PolicyPanel`, `LifecycleMenu` (+ "unavailable because Enterprise is
  suspended"), `DeleteDialog` (preview counts + external actions + typed
  confirmation + job progress), `MembersPanel`, `CredentialsPanel`, `Breadcrumbs`.
- Retained/retargeted: public chat pages (ChatWidget, ChatPage), auth pages,
  Profile, Docs, LandingPage, InviteAccept, DiscordInvited.

## 13. Security requirements carried forward

All protections of specs 009-011 are platform invariants and keep their test
suites (ported to the new routes): strict ownership/ancestry validation, no
cross-tenant access (generated route × role × tenant matrix), credential
redaction, Zendesk host construction, outbound validation with DNS-rebinding
protection, no redirects, TLS verification, internal/metadata blocking,
Ollama allowlist. Knowledge-base URL imports stay disabled until their outbound
paths use `outbound_guard`. New: encryption of all provider secrets, removal of
the tenant-reachable Discord restart/token/avatar actions, cross-tenant DM fix,
no exception text to end users, embed origin enforcement.

## 14. Legacy code removal (after parity is verified in Phase 4)

Backend: `migrate_v2.py`, `migrate_workspaces.py`, `tenant_scope.py`,
`v2_helpers.py`, `workspace_routes.py`, `org_routes.py`, `bot_routes.py`,
`account_routes.py`, `connection_routes.py`, `webhook_router.py`,
`platform_routes.py`, `telegram_routes.py`/`zendesk_routes.py` (replaced by
adapters), `api_diagnostics*.py` (D-Q12), `manage.py legacy-migrations`; from
`server.py` every `X-Instance-ID` route, the V1/V2 Discord paths, Mongo KB
routes, templates, `api_keys`, `bot_config`, embed config, analytics (→ usage),
the `emergent` provider and `emergentintegrations`, duplicate superadmin checks.
Frontend: InstanceSelect, Instances, Workspaces, OnboardingWizard,
DiscordSettings, DiscordAppSetup, ProviderApps, TelegramSettings,
ZendeskSettings, IntegrationsPage, ConnectedAccounts, Analytics,
ApiKeyAnalytics, ApiDiagnostics, EmbedCode, KnowledgeBase,
KnowledgeContentViewer, SetupGuide, Users, WorkspaceSettings, EnterpriseWorkspaces,
Organisations, BotList, BotOverview, BotSettings, Dashboard, SuggestionsInbox
(functions move into the new pages). Removal happens only for features whose
parity row is verified or explicitly retired.

## 15. Data reset and recovery (Phase 7, separate approval)

1. **Backup** `mongodump --db bitbotpro --gzip --archive` →
   `~/.bitbotpro/backups/pre-spec012-<UTC>.archive.gz` (0600, sha256 recorded).
2. **Verify backup** by restoring into an isolated throwaway mongod
   (`deploy/scripts/backup-restore-check.sh`) and comparing per-collection counts.
3. **Verify release + tooling**: all gates green; `manage.py reset-application-data`
   rehearsed on the restored copy.
4. **Preview**: dry run lists the exact collections and document counts to
   remove (allowlist below) and the users kept.
5. **Explicit approval** of the preview, then `--apply --confirm bitbotpro
   --preview-sha <hash>` (refuses if counts changed since the preview, if the
   database name differs, or without confirmation).
6. **Allowlist only**: legacy and new application collections (`workspaces`,
   `workspace_members`, `organisations`, `bots`, `bot_instances`, `bot_config`,
   `connections`, `connected_accounts`, `discord_config`, `telegram_config`,
   `zendesk_*`, `embed_configs`, `conversations`, `invitations`, `llm_usage`,
   `v2_suggestions`, `api_*`, `knowledge_*`, `scripted_*`, `passive_skips`,
   `processed_discord_messages`, `discord_app_config`, `platform_config`,
   `provider_apps`, `workspace_discord_apps`, `onboarding`, …) plus all
   non-superadmin users. Never: other databases, PostgreSQL, Docker volumes,
   secret files, `deploy/.env`, backups, Gridscan.
7. **Superadmin**: kept (`admin@bitbotpro.io`), converted to
   `is_platform_superadmin`; if absent, `manage.py bootstrap-superadmin`.
8. **Sessions**: `token_version` bumped for every retained user (old JWTs invalid).
9. **Empty-state onboarding**: superadmin logs in, sees the empty Enterprise list
   and the "Create Enterprise" flow.
10. **Fresh Enterprise**: create Enterprise → Organisation → credentials → Bot →
    web, Telegram and Discord instances + Zendesk integration; verify all four.
11. **Rollback**: previous API/frontend image tags + `mongorestore --drop` of the
    step-1 archive; rehearsed on the throwaway database before step 5.

## 16. Implementation phases and complexity

| Phase | Scope | Estimate (new code + tests) | Main risks |
|---|---|---|---|
| 1 | This design + parity matrices | done | — |
| 2 | Core models, registry, resolver, policies, lifecycle, authz, credentials store; pure and in-process tests | M: ~2.5-3.5 k lines incl. tests | getting the registry and policy semantics right first |
| 3 | REST API: hierarchy, config, policy, members/invitations, credentials, lifecycle, deletion preview/jobs; tenant-isolation matrix | L: ~3-4 k lines | permission matrix completeness |
| 4 | Runtime pipeline + adapters (web, Telegram, Discord worker, Zendesk) + parity verification incl. provider sandboxes | XL: ~4-6 k lines | Discord gateway in a worker; real-provider E2E needs test guild/bot/sandbox |
| 5 | Frontend rewrite (hierarchy navigation, ConfigPanel, lifecycle/deletion, integrations) | L: ~4-5 k lines | UX consistency |
| 6 | Legacy removal, full gates, images, R8 scans, runbooks | M | hidden coupling in `server.py` |
| 7 | Production cutover (backup, deploy API + worker + frontend, reset, verify) | S-M, needs a short maintenance window and a new `worker` container | first worker deployment |

## 17. Automated tests

| Area | Tests |
|---|---|
| Registry & resolver | table-driven key × level × actor; nearest compliant override wins; removal restores; parent changes propagate; every combine operator; tighten-only policy writes; invalid stored overrides ignored + reported; blocked keys; credential_ref redaction; property-based fuzz over random chains |
| Lifecycle | 4 types × 4 states availability matrix; suspension authorities (object/enterprise/platform) and who may resume; parent resume preserves child state; runtime re-check per event and per stream step |
| Deletion | preview counts exact; token binding/expiry; immediate access revocation; idempotent resumable job (crash mid-job, re-run); descendant-first order; no cross-tenant or sibling impact; external shutdown calls recorded; last-superadmin/last-owner safeguards; user deletion keeps shared resources |
| Authorisation | generated route × role × own/foreign tenant matrix with DB-unchanged assertions; route-coverage test; grant ≤ own role; enterprise membership prerequisite; no tenant headers (source test); 404 for foreign ids |
| Credentials | encryption at rest, never in responses/logs/errors/config, use vs manage, scope enforcement, rotation keeps references, revocation fails closed |
| Runtime & integrations | every parity row's automated test (see `integration-parity.md`); single-resolver source test; quota and rate limits; streaming parity; typed errors never leak details |
| Security regression | specs 009-011 suites ported; outbound/Zendesk/Ollama guard suites unchanged |
| Reset tooling | disposable Mongo: preview, allowlist-only drop, superadmin kept, token_version bumped, refusals (wrong DB, changed counts, no confirm), idempotent |
| Frontend | Jest for ConfigPanel, DeleteDialog, routing, API client; production build |
| End-to-end | disposable stack (Mongo + API + worker images): full §15 step 10 scenario; provider sandboxes for Discord, Telegram, Zendesk where credentials are supplied |

## 18. Open questions (recommendation first)

- **D-Q1 Replies without a knowledge base.** Today a bot with no KB and no API
  integration returns a canned "no knowledge base" text (AI effectively off in
  production). *Recommend:* answer from persona/instructions when
  `knowledge_gate = flexible`, use `fallback_message` when `strict`.
- **D-Q2 Lifecycle "disabled".** *Recommend:* no fifth state — "disabled" is
  `Suspended` with `suspension_authority = object`; enterprise/platform
  suspensions can only be lifted by that authority.
- **D-Q3 Permanent delete for Bots and Bot Instances too** (spec requires it for
  Users, Organisations, Enterprises). *Recommend:* yes, same workflow.
- **D-Q4 Runtime worker.** Discord needs a long-running gateway (disabled today
  in `APP_MODE=api`). *Recommend:* a second container from the same image
  (`APP_MODE=worker`) at cutover — a compose change, no VPS/Caddy/WireGuard change.
- **D-Q5 Discord DMs.** Today routed to the first tenant found (cross-tenant).
  *Recommend:* off by default; per instance `dm_enabled` only when the user
  shares a guild bound to that instance.
- **D-Q6 Discord scripted mode** (scripted replies, counters, sandbox, scenarios;
  ~600 lines + UI; reachable only via the legacy page). *Recommend:* explicit
  retirement now, re-introduce as its own spec if needed. Passive mode and staff
  handoff are ported.
- **D-Q7 Agent-assist suggestions for Discord/Telegram** (V2-only, practically
  unreachable today). *Recommend:* not ported; Discord/Telegram are auto-reply
  channels; suggestions remain a Zendesk feature.
- **D-Q8 Logo storage.** Object storage is not configured in production, so
  uploads fail today. *Recommend:* store logos (≤256 KB, type-sniffed) in MongoDB.
- **D-Q9 Zendesk OAuth client and auth modes.** *Recommend:* one platform OAuth
  client (superadmin) plus API-token mode as an alternative per credential.
- **D-Q10 Zendesk webhook signatures.** *Recommend:* required for new
  integrations (`require_webhook_signatures` platform default true), with UI to
  paste the signing secret.
- **D-Q11 Zendesk ticket creation** and other new ticket actions (status,
  assignee) do not exist today. *Recommend:* out of scope for 012; separate spec
  if wanted.
- **D-Q12 API diagnostics tooling** (spec 011-guarded, admin-only, ~1.8 k lines).
  *Recommend:* retire in 012; API integrations themselves are ported.
- **D-Q13 Discord "leave guild" on instance deletion.** *Recommend:* yes when no
  other active instance uses the guild.


---

## 19. Consolidated approval (2026-11-18)

### 19.1 Approved decisions

| Id | Decision |
|---|---|
| D-Q1 | Approved with explicit knowledge modes (§19.2): GENERAL answers normally; KNOWLEDGE_PREFERRED prioritises approved knowledge and falls back to general knowledge only when Enterprise policy permits; STRICT_KNOWLEDGE_ONLY refuses unsupported answers and fails closed. A missing/invalid AI provider or credential yields an admin configuration error and a safe end-user reply — never a misleading "no knowledge base" message. |
| D-Q2 | Disabled = Suspended (authority `object`). |
| D-Q3 | Permanent deletion also for Bots and Bot Instances. |
| D-Q4 | Dedicated Discord worker container at production cutover. |
| D-Q5 | Discord DMs off by default; enabled per Instance only when routing is unambiguous and authorised; ambiguous ⇒ fail closed; never "first tenant found". |
| D-Q6 | Discord scripted mode retired; staff handoff and passive mode preserved. |
| D-Q7 | Discord and Telegram suggestions retired; Zendesk suggestions preserved. |
| D-Q8 | Small logos in MongoDB with size (≤256 KB), sniffed MIME type and access restrictions. |
| D-Q9 | One platform Zendesk OAuth client + tenant-scoped API-token mode. |
| D-Q10 | Zendesk webhook signatures required for new integrations. |
| D-Q11 | Zendesk ticket creation outside Spec 012. |
| D-Q12 | Legacy API diagnostics retired. |
| D-Q13 | Leave a Discord server only when no remaining Instance needs it and no other tenant can be disrupted. |

### 19.2 Enterprise AI knowledge control (critical requirement)

Three modes, ordered by strictness: **GENERAL < KNOWLEDGE_PREFERRED <
STRICT_KNOWLEDGE_ONLY**.

Registry properties (inherited E → O → B → I, explicit overrides only):
`knowledge.mode`, `knowledge.approved_sources` (narrowing: an override must be a
subset of the inherited set; unset = nothing approved), `knowledge.min_evidence_score`,
`knowledge.require_citations`, `knowledge.insufficient_fallback` (public),
`knowledge.human_handoff`.

**Enterprise source approval is deny by default (amendment X13, approved
2026-11-18).** A knowledge source can be approved or used anywhere in an
Enterprise only if the Enterprise lists it in `knowledge.allowed_sources`; an
unset and an explicitly empty Enterprise list both mean *no approved sources*.
Organisations may narrow that list; Organisations, Bots and Instances may
narrow `knowledge.approved_sources` and never broaden it. Removing a source at
Enterprise level makes stored descendant approvals that include it
non-compliant (ignored, reported). GENERAL mode is unaffected.

Policies (tighten-only): `knowledge.minimum_mode` (strictest wins — if an
Enterprise requires STRICT, no descendant can select a weaker mode; stored weaker
overrides are ignored, reported and clamped to the minimum),
`knowledge.general_fallback_allowed` (Enterprise-authoritative; default **not
permitted**; an Organisation can only remove it), `knowledge.allowed_sources`
(intersection), `knowledge.access_scope` (enterprise ⊃ organisation ⊃ bot;
narrower wins), `knowledge.min_evidence_floor` (max), `knowledge.require_citations`
and `knowledge.document_acl_required` (required controls stay required).
Together these form the `knowledge_access_policy`.

Corporate data protection (separate from grounding): sources are owned by an
Enterprise, Organisation or Bot; a bot can only use sources on its own branch of
the hierarchy, within the access scope, explicitly approved, active, and — where
access lists exist or are required — readable by the authenticated requester.
Evidence from another Enterprise is discarded even if it claims an approved
source id. No cross-Enterprise retrieval is possible by construction.

**Strict-mode fail-closed runtime contract** (`core/knowledge.py`), mandatory
for every channel adapter and integration adapter in Phase 4:

1. resolve the chain and policies (`plan_for(resolve(chain))`);
2. compute the requester's authorised source ids (`authorised_sources`);
3. retrieve only from those sources (retrieval engine — later phase);
4. `gate_before_generation`: drop unauthorised/cross-tenant/low-score evidence;
   STRICT refuses when knowledge is unavailable, absent, insufficient or
   conflicting; KNOWLEDGE_PREFERRED falls back to GENERAL only when permitted;
5. generate with the model constrained to the gated evidence, returning
   **structured claims with evidence citations** (free text is not deliverable
   in strict mode);
6. `validate_answer`: every claim must cite evidence supplied to the model and
   must not be marked unsupported by the verifier; uncited claims, unknown
   citations or extra free text ⇒ refusal;
7. deliver the answer with traceable source references, or `refusal` /
   `fail_closed` with the configured fallback (never internal detail).

No generative system can guarantee zero hallucinations; these controls make
strict answers evidence-bound and refuse everything the checks cannot support.
**Phase 4 cannot be declared complete while STRICT_KNOWLEDGE_ONLY lacks an
enforceable retrieval engine and refusal behaviour**; if the secure knowledge
subsystem is large, a bounded sub-phase (4K) will be proposed before runtime
acceptance.

### 19.3 Changes relative to the Phase 1 text above

- `knowledge.gate`/`knowledge_gate_pct` (§4.1) are superseded by the knowledge
  properties in §19.2.
- Instance channel type `web` is named **`webchat`**.
- Policies may be set by the platform, Enterprises and Organisations; Bot and
  Instance levels tighten through their own (narrowing/constrained) settings.

## 20. Phase 2 implementation map (backend/core, isolated)

| Requirement | Module |
|---|---|
| Enterprise, Organisation, Bot, Bot Instance models; channel bindings (Discord, Telegram, Webchat); logo sniffing | `core/hierarchy.py` |
| Server-assigned immutable ancestry, chain verification (fail closed), binding uniqueness, store interface (in-memory; MongoDB in Phase 3), index specification | `core/hierarchy.py` |
| Configuration registry (properties, policies, combine rules, security classes) | `core/config_registry.py` |
| Sparse overrides, central resolver, parent-policy validation, conflicts, provenance, overridability | `core/resolver.py` |
| Lifecycle states, suspension authority, availability | `core/lifecycle.py` |
| Memberships, roles, permissions, delegation, grant rules, last-owner/superadmin safeguards | `core/authz.py` |
| Credential ownership, encryption, use vs manage, rotation, revocation, platform credentials | `core/credentials.py` |
| Knowledge modes, source scope and policies, strict-mode fail-closed contract | `core/knowledge.py` (+ registry) |
| Isolated unit tests (107) | `tests/unit/test_core_*.py`, `tests/unit/_core_fixtures.py` |

## 21. Phase 3 implementation map (persistence + API, isolated)

Production MongoDB is a standalone `mongod` (no replica set), so no
multi-document transactions exist. Every invariant is built from
single-document atomic operations: unique keys, compare-and-swap on a
`version` field, conditional deletes and `find_one_and_delete`. Deviations and
discrepancies are listed in `DECISIONS.md` (X1–X12) and are **not** resolved
silently.

| Requirement | Module |
|---|---|
| Store contract (CAS, unique keys, single-use take), in-memory reference store | `core/hierarchy.py` (`Store`, `InMemoryStore`, `UNIQUE_FIELDS`) |
| MongoDB adapter, `$jsonSchema` validators, indexes, legacy-database refusal | `persistence/mongo.py` |
| Exclusive channel bindings (Discord guild/channel overlap, Telegram credential + bot identity, Webchat public id) | `core/bindings.py`, `HierarchyService._rebind` |
| Memberships: *superseded by Phase 3.1 (X4, §22)* — now one document per (Enterprise, user) with a serialised intent protocol | `core/memberships.py` |
| Users, sessions, superadmin lower-bound guard | `core/identity.py` |
| Deletion preview, single-use confirmation token, idempotent descendant-first jobs, user deletion | `core/deletion.py` |
| Audit records | `core/audit.py` |
| One authentication dependency, authorisation, error mapping | `api/common.py` |
| REST routes (§11 subset: hierarchy, config, policy, lifecycle, members, invitations, credentials, platform credentials, deletion, jobs, users, audit) | `api/*.py`, `api/app.py` (router factory, not mounted) |
| Tests | `tests/unit/test_core_{concurrency,deletion,architecture}.py`, `tests/unit/test_api_*.py`, `tests/integration/test_spec012_mongo.py` |

## 22. Phase 3.1 implementation map (corrections, isolated)

| Correction | Module |
|---|---|
| X4 membership documents per (Enterprise, user); serialised intent protocol through `membership_guards` (exact owner count, linearizable revocation, account preconditions re-verified, crash roll-forward) | `core/memberships.py`, `persistence/mongo.py`, `api/members.py`, `api/config.py` (delegation policy writes) |
| X12 external cleanup state machine, credential holds, operator resolution | `core/deletion.py`, `api/deletion.py`, `api/credentials.py` |
| X13 deny-by-default knowledge sources | `core/config_registry.py`, `core/knowledge.py` |
| X14 invitation expiry in listings, addressee-only lookup | `api/members.py` |
| X7 Organisation deletion by Enterprise Owners only | `core/authz.py` |

## 23. Phase 4A implementation map (isolated; not mounted)

| Requirement | Module |
|---|---|
| Login, logout, sessions, password change and reset, CSRF, throttling | `api/auth.py`, `api/passwords.py`, `core/identity.py`, `core/throttle.py`, `api/common.py` |
| Superadmin bootstrap (one-time, race-safe) | `core/identity.bootstrap_superadmin`, `api/bootstrap.py` |
| Channel-independent pipeline (binding → chain → availability → resolver → limits → knowledge boundary → provider → validation → usage) | `runtime/pipeline.py`, `runtime/routing.py`, `runtime/usage.py`, `runtime/errors.py`, `runtime/interfaces.py` |
| Strict-knowledge enforcement boundary (test doubles; end to end only after 4K) | `runtime/pipeline.py` + `core/knowledge.py` |
| X6 per-credential usage scopes | `core/credentials.py`, `api/credentials.py` |
| Workers (deletion jobs, cleanup, binding reconcile, membership intents, Discord desired state) | `runtime/worker.py` |
| Runtime configuration preview and worker status | `api/runtime_routes.py` |

## 24. Phase 4B: Webchat (isolated; not mounted)

| Requirement | Module |
|---|---|
| Public endpoints (config, logo, sessions, chat, SSE stream, history, preflight), uniform 404 | `api/webchat.py` |
| Embed loader, widget page/JS/CSS served by the API with CSP `frame-ancestors` | `api/webchat_assets.py`, `api/webchat.py` |
| Conversation sessions (id + hashed 256-bit token, instance-bound, 30 days), conversations (ancestry, retention TTL), request idempotency | `runtime/conversations.py` |
| Anthropic and OpenAI adapters (fixed hosts, timeouts, bounded retry, safe errors, strict claim parsing) | `runtime/providers.py` |
| Per-IP, per-session, per-instance daily, per-end-user and monthly quota limits | `api/webchat.py`, `runtime/usage.py`, `core/throttle.py` |
| Admin: conversations, approve-as-tone-example, bot logo, embed snippet | `api/webchat.py` |

**Browser security model.**
- **Embedding:** the widget iframe page is served from the API origin, and
  its CSP `frame-ancestors` lists the instance's effective allowed origins
  (instance ∩ Enterprise `webchat.allowed_embed_origins`). Unrestricted means
  any origin.
- **Widget calls:** the widget's API calls are same-origin.
- **Direct cross-origin calls:** these must come from an allowed origin.
  CORS echoes exactly that origin, never `*`, and never with credentials (no
  cookies are used).
- **Non-browser clients** can forge `Origin`, so origin checks are an
  embedding control, not authentication. Abuse is bounded by per-IP,
  per-session, per-instance and per-end-user limits and by monthly quotas.
- **Conversation access** needs the session id **and** its token. Sessions are
  bound to one instance and its ancestry, and the public id is never treated
  as proof of conversation ownership.

## 25. Phase 4C: Telegram and billing idempotency (isolated; not mounted)

| Requirement | Module |
|---|---|
| Durable request idempotency and usage ledger (reserve → generate → store → settle → conversation) | `runtime/conversations.py`, `runtime/usage.py`, `runtime/pipeline.py`, `api/webchat.py`, `runtime/worker.py` |
| Telegram Bot API client (fixed host, token never logged), listen modes, chunking, suggestions, cleanup adapter | `runtime/telegram.py` |
| Telegram webhook state, sync, update handling through the shared pipeline | `runtime/telegram_channel.py` |
| Webhook endpoint, channel routes, credential test, lifecycle and credential hooks | `api/telegram.py`, `api/lifecycle.py`, `api/credentials.py`, `api/hierarchy.py` |

## 26. Phase 4D: Enterprise Discord and Telegram T-A (isolated; not mounted)

| Requirement | Module |
|---|---|
| Durable outbound delivery (T-A): record before send, leases, per-part state, safe retry only on proven non-delivery, uncertainty on unknown outcome, operator view | `runtime/delivery.py`, `runtime/conversations.py::complete_request`, `runtime/telegram.py` (error categories), `runtime/telegram_channel.py`, `runtime/worker.py`, `api/telegram.py` (`GET /instances/{id}/deliveries`), `api/runtime_routes.py` |
| D-Q14: Enterprise Discord application (superadmin-only administration, encrypted token/secret, token↔application verification, unique application id) | `api/discord.py`, `runtime/discord.py`, `persistence/mongo.py`, `core/hierarchy.py::UNIQUE_FIELDS`; `core/credentials.py` (`DISCORD_APP` removed) |
| Application-scoped bindings and routing (`discord:<app>:<guild>`); server-set `application_id`; disconnect | `core/hierarchy.py` (`DiscordBinding.application_id`, `bind_discord_guild`, `unbind_discord`), `core/bindings.py`, `runtime/routing.py`, `runtime/interfaces.py` |
| OAuth install and callback (hashed single-use state, re-authorisation, MANAGE_GUILD from Discord, bot presence, shared-server pending selection), channel listing and validation, settings, nickname, status, test | `api/discord.py`, `api/hierarchy.py` (generic PATCH validation), `api/telegram.py` (generic channel routes dispatch) |
| Gateway supervision in the worker (per-application lease, back-off, restart, failed_auth, sanitised status); mock connection | `runtime/discord_gateway.py`, `runtime/worker.py` |
| Message handling: listen modes, DMs (D-Q5), staff handoff (role ids from Discord), staff-only `!bot` commands, passive mode, splitting, safe mentions, nonce delivery | `runtime/discord_channel.py`, `runtime/pipeline.py` (`InboundMessage.passive`) |
| Deletion: application-scoped routing-removal and leave-guild actions; application data removed only when the Enterprise job finalises | `core/deletion.py`, `runtime/discord.py::discord_cleanup_executors` |

**Frontend contracts for Phase 5 (documented, not built).**
- *Superadmin Enterprise Discord page.*
  - `GET …/discord-application` returns `configured`, `application_id`,
    `bot_user_id`, `bot_username`, `permissions`, `intents`, `status`,
    `gateway_desired`, `token_fingerprint`, `client_secret_fingerprint`,
    `redirect_uri`, `oauth_scopes` and
    `gateway{state,last_error,attempts,heartbeat_at,…}`.
  - The secret inputs are write-only.
- *Organisation instance page.*
  - Call `POST …/channel/discord/install-url`, then open `url` in a new tab.
  - The callback returns JSON `{status: connected | channel_selection_required
    | invalid_state | not_authorised | not_server_manager | bot_not_present |
    application_changed | oauth_failed | cancelled, detail}`. Phase 5 decides
    the redirect target for this.
  - Then `GET …/channel/discord/channels` and
    `PUT …/channel/discord/settings`.
  - Pending selections expire after 30 minutes.

**Phase 4D corrections (C1–C6, 2026-11-18).** See `DECISIONS.md` "Phase 4D
corrections". C1 records the gateway adapter as a Phase 4F release blocker;
C2 share links (`api/discord.py`, `discord_share_links`); C3 handoff
administration; C4 passive-mode usage rules; C5 delivery retention
(`runtime/delivery.py`, TTL on terminal records only); C6 shared-server
cleanup tests.

## 27. Phase 4E: Zendesk Bot Integration (isolated; not mounted)

| Requirement | Module |
|---|---|
| Bot Integration model (Enterprise → Organisation → Bot → Integration), settings, routing filters | `core/integrations.py`, `persistence/mongo.py` (`bot_integrations`) |
| X2: integration-level configuration through the one registry/resolver | `core/config_registry.py::INTEGRATION_PROPERTIES`, `core/resolver.py::resolve_integration` |
| Guarded Zendesk client (kept `outbound_guard`), OAuth (PKCE), refresh with CAS lock, credential handling, cleanup executors | `runtime/zendesk.py` |
| Event state machine, shared-pipeline processing, reply modes, suggestions, safe-update delivery, sidebar generation, worker maintenance | `runtime/zendesk_integration.py`, `runtime/pipeline.py` (`InboundMessage.integration_id`), `runtime/delivery.py` (integration ancestry), `runtime/worker.py` |
| Routes: CRUD, config, OAuth, webhook (signed, mandatory), provisioning, sidebar keys and ZIP, suggestions, deliveries | `api/integrations.py`; credential rules in `api/credentials.py` |
| Deletion: `zendesk_webhook_subscription_removal`, `zendesk_token_revocation` (credential holds) | `core/deletion.py`, `runtime/zendesk.py::zendesk_cleanup_executors` |

**Frontend contracts for Phase 5 (documented, not built).**
- *Integration page:*
  - `GET /integrations/{id}` returns `settings`, `credential_id`,
    `webhook{signing_secret_configured, provisioned}`, `sidebar_key{configured,
    hint}`, `connection{status, subdomain, checked_at}`.
  - `GET …/zendesk/webhook` returns the URL to paste into a Zendesk trigger.
  - `POST …/zendesk/oauth/start` then open `url`. The callback returns JSON
    `{status: connected | invalid_state | not_authorised | client_changed |
    account_mismatch | oauth_failed | cancelled | changed}`; the redirect
    target is a Phase 5 decision.
  - The API-token alternative is created with the generic credential route.
- *Sidebar:*
  - `POST …/sidebar-key` shows the key once; `GET …/sidebar-app.zip`.
- *Suggestions inbox:*
  - `GET …/suggestions`; accept or dismiss.
- *Configuration:*
  - `GET|PATCH …/config` returns values with sources (`integration` level)
    and conflicts.

## 28. Phase 4K: knowledge retrieval and strict evidence-based answering (isolated; not mounted)

**Ownership.** `knowledge_sources` belong to an Enterprise or one Organisation (server-set from the
route and the database); documents and chunks copy the source's `enterprise_id`/`organisation_id`.
Use requires Enterprise approval (`knowledge.allowed_sources`, deny by default, X13) ∩ Bot/Instance/
Integration selection (`knowledge.approved_sources`, narrowing only) ∩ access scope ∩ source status ∩
requester groups ∩ document ACL (`org:`/`bot:` scopes inside the source's scope).

**Ingestion** (`runtime/knowledge_store.py::Ingestor`): raw uploads ≤ 2 MB; text, Markdown, HTML,
CSV, JSON, DOCX (bounded, no entity expansion); PDF and URL ingestion not supported. Idempotent per
(source, name, SHA-256); versioned (new chunks → version flip by compare-and-swap → old chunks removed);
failures recorded and retried by the worker; deletion marks `deleting` before removing chunks, files
and the record.

**Retrieval** (`StoreRetriever`): a single query on `knowledge_chunks` constrained by
`enterprise_id`, `source_id` ∈ authorised, `acl_scopes` ∈ {`*`, org, bot} and query terms — nothing
outside the filter is read; candidates re-checked; each chunk's document must be `ready` at the same
content version and its source `active` (fail closed); BM25 ranking; evidence score as K-B;
contradiction heuristic K-D; bounded by a 5-second timeout (outage/timeout ⇒ knowledge unavailable).
No evidence cache.

**Generation and validation** (`core/knowledge.py`, `runtime/providers.py`, `runtime/pipeline.py`):
the model sees only gated evidence, wrapped as untrusted data; grounded answers must be JSON claims
with evidence ids and verbatim quotes; `validate_answer` applies K-C before delivery; streaming sends
only validated output (W-B); provenance `(source, document, evidence)` is attached to answers.

**Modes.** GENERAL: no knowledge needed. KNOWLEDGE_PREFERRED: grounded when evidence suffices;
otherwise a general answer only if the Enterprise policy `knowledge.general_fallback_allowed`
permits, else the configured fallback. STRICT_KNOWLEDGE_ONLY: grounded answers only; absent,
insufficient, contradictory, unauthorised or unavailable evidence ⇒ configured fallback (and
handoff, K-G), recorded as `refusal`/`handoff` usage events.

**Cross-channel.** One pipeline: Webchat (chat, stream, sessions), Telegram (messages, follow-up
buttons re-ask through the pipeline), Discord (channels, DMs, passive mode, staff handoff, splitting
after validation), Zendesk (events, public comments, private notes, suggestions, sidebar). Delivery
retries only ever send stored validated text.

**Limits (documented).** Lexical retrieval and lexical claim validation (no semantic entailment);
contradiction detection is heuristic; injected text inside an approved document is that document's
content (it can be quoted, but cannot change instructions or license unsupported claims); no zero-
hallucination guarantee.

## 29. Phase 4F: integration preparation and security validation (isolated; simulated services only)

| Area | Module / result |
|---|---|
| Knowledge validation layer 2 (sentence alignment), evaluation corpus and harness | `core/knowledge.py::sentence_aligned`; `tests/unit/knowledge_eval_corpus.py`, `test_knowledge_eval.py` |
| Document validity windows (expired policies) | `PUT /api/knowledge/documents/{id}/validity`; retrieval and pre-delivery checks |
| Pre-delivery evidence re-verification | `runtime/pipeline.py`, `runtime/knowledge_store.py::StoreRetriever.verify` |
| PDF ingestion (K-E amended) | `runtime/knowledge_store.py::extract_pdf`, `runtime/pdf_extract_worker.py` (isolated child process, `pypdf==6.21.4`) |
| Zendesk per-request deletion fence | `runtime/zendesk.py::ZendeskClient(fence=…)`, `runtime/zendesk_integration.py::fenced` |
| Real Discord gateway adapter | `runtime/discord_gateway_ws.py`; supervisor session recovery in `runtime/discord_gateway.py` |
| Atomic throttle counters | `Store.increment` (`core/hierarchy.py`, `persistence/mongo.py`), `core/throttle.py` |
| Deletion grace re-purge | `core/deletion.py::repurge_due`, worker |
| Body limit and security headers | `api/app.py` (`BodyLimitMiddleware`, `SecurityHeadersMiddleware`) |
| Load and concurrency suite | `tests/integration/test_spec012_load.py` (MONGO_TEST_URL + LOAD_TEST=1) |

Results, findings and remaining blockers: `IMPLEMENTATION_STATUS.md` (Phase 4F) and `DECISIONS.md` (K-*, F-*).


## 30. Phase 5: frontend and corrections (local only; not deployed)

**Corrections (Part B).**

| Item | Module | Tests |
|---|---|---|
| B1 intermittent build failure: investigation | — (no code defect found; see `IMPLEMENTATION_STATUS.md` Phase 5) | 6 full unit runs; both failing trees re-run |
| B2 F-A approved: atomic increment + sliding-window estimate | `core/throttle.py` | `test_core_throttle.py`, `test_api_ratelimits.py` |
| B3 K-H experimental semantic verifier (off by default) | `runtime/semantic_verifier.py`, `runtime/pipeline.py::_semantic_check`, policy `knowledge.semantic_verification` | `test_semantic_verifier.py`, `test_knowledge_eval.py::HoldoutEvaluation`, `knowledge_holdout_corpus.py` |
| B4 Zendesk deletion state in the integration view | `core/integrations.py::public_view` (`deletion`) | `test_api_zendesk_lifecycle.py` (in-flight race, UI states) |
| C14 OAuth result redirect (opt-in) | `api/common.py::oauth_result`, `Settings.oauth_result_origin` | `test_api_discord.py`, `test_api_zendesk.py` |
| Admin-origin CORS (opt-in) | `api/app.py`, `Settings.admin_origins` | `test_api_security_4f.py::AdminOriginCorsTests` |
| Contract export | `backend/tests/contract/export_frontend_contract.py` → `frontend/src/app12/contract/{routes,samples}.json` | frontend `client.test.js` |

**Admin app routes (`frontend/src/app12/`, mounted at `/app/*` in `App.js`).**

| Route | Page | API (all through `api/endpoints.js`) |
|---|---|---|
| `/app/login`, `/app/forgot-password`, `/app/reset-password#token=`, `/app/invitations#token=` | `pages/Auth.js` | `/auth/*`, `/invitations/lookup`, `/invitations/accept` |
| `/app/account` | `pages/Auth.js::AccountPage` | change password, logout-all, `/users/me/deletion-preview`, `/users/me/delete` |
| `/app` | Enterprises list | `GET /enterprises` |
| `/app/enterprises/:id` | Organisations · Defaults · Policy · Members (+ invitations) · Credentials · Knowledge (approval via `knowledge.allowed_sources`) · Discord (availability only) · Audit · Lifecycle | hierarchy, config, policy, members, invitations, credentials, knowledge, `GET /enterprises/{id}/discord`, audit, lifecycle, deletion |
| `/app/organisations/:id` | Bots · Defaults · Policy · Members · Credentials · Knowledge · Lifecycle | same at Organisation level |
| `/app/bots/:id` | Instances · Behaviour (ConfigPanel with provenance) · Integrations · Credentials (references) · Members · Logo & lifecycle | instances, integrations, config, members, logo |
| `/app/instances/:id` | Channel (Discord / Telegram / Webchat panel) · Overrides · Lifecycle | channel routes, Discord routes, deliveries, embed snippet, conversations |
| `/app/integrations/:id` | Zendesk: Connection (OAuth / API token) · Replies · Webhook · Sidebar · Suggestions · Events & deliveries · Overrides · Delete (state machine) | `/integrations/*` |
| `/app/platform` | Enterprises (create) · Platform credentials (Zendesk OAuth client) · Operations (workers, deletion jobs, deliveries) · Audit · Users (superadmin grant/revoke, deletion) | `/platform/*`, `POST /enterprises` |
| `/app/platform/enterprises/:id/discord` | Enterprise Discord application (superadmin only): setup, rotation, enable/disable, gateway start/stop/restart, avatar | `/platform/enterprises/{id}/discord-application*` |
| `/oauth/:provider/result` | OAuth result (Discord, Zendesk) | none (status from the redirect) |

**Shared components:** `ConfigPanel` (effective value, origin badge, override, reset to inherited = `null`, policy lock), `PolicyPanel`, `LifecycleMenu`, `DeleteDialog` + `JobView` (preview counts, external actions, backups note, typed name, job progress, resume, resolve with reason), `MembersPanel`, `CredentialsPanel` (write-only secrets), `KnowledgePanel`, `Breadcrumbs` (from server ancestry), accessible `Tabs`.

**Session model (C1).** httpOnly SameSite=Strict cookie only; `credentials: "include"`; CSRF header `X-Requested-With: BitBotPro` on every unsafe request; no web storage; any 401 on an authenticated call ends the session in the UI ("Your session has ended").

**Tests.** `frontend/src/app12/__tests__/` — `client.test.js` (session model, CSRF, contract), `util.test.js`, `components.test.js` (ConfigPanel, DeleteDialog), `flows.test.js` (integration flows against the mocked API built from the manifest and real samples: C1, C2, C4, C7–C11, C14–C16).

## 31. Phase 6A: local end-to-end integration and release corrections (local only)

| Item | Module | Tests |
|---|---|---|
| W2 approved: API-hosted widget only | `frontend/src/app12` (no second widget); legacy React pages removed at cutover (DECISIONS P5-H) | `w2.test.js`; `test_api_webchat.py` W1–W6 |
| F1 usage | `api/usage.py`; `runtime/usage.py` (`LABELS`, `labels=`); ledger indexes in `persistence/mongo.py` | `test_api_phase6a.py::UsageTests`, matrix |
| F2 user discovery | `api/users.py`; `Store.find_prefix` (`core/hierarchy.py`, `persistence/mongo.py`) | `UserDiscoveryTests`, matrix |
| F3 invitation e-mail | `api/members.py` (`delivery`), `api/mail_templates.py`, `Mailer.send_invitation`, `Settings.invitation_url_base` | `InvitationEmailTests` |
| F4 credential tests | `api/telegram.py::test_credential`, `runtime/providers.py::check` | `CredentialTestTests` |
| F5 preview accounting | `api/webchat.py::preview_session`, `runtime/conversations.py` (`preview_by`), widget fragment handling (`api/webchat_assets.py`) | `PreviewAccountingTests`, `WidgetPreviewScriptTests` |
| F6 deferred features labelled | Integration page Usage tab notice; knowledge OCR note | `phase6a.test.js` |
| H deletion safety | `runtime/pipeline.py::_still_available` (before and after generation), Telegram/Discord senders, `runtime/zendesk_integration.py::_lease` | `test_deletion_safety.py` |
| Discord availability for Organisation members | `api/discord.py::enterprise_discord` | `test_api_discord.py`, e2e |
| G knowledge harness | `backend/tests/knowledge_eval/harness.py` | `test_knowledge_harness.py` |
| Local e2e environment | `backend/tests/e2e/server.py`, `backend/tests/e2e/test_e2e_http.py`, `e2e/` (Playwright 1.49.1, axe 4.10.1) | real HTTP journeys; real Chromium (desktop + mobile) |

**Running the local stack** (isolated `--internal` network; no internet):
`frontend/build-e2e` (same-origin build) → disposable `mongo:8.0.38` → `python -m tests.e2e.server` in the test image →
`python -m unittest tests.e2e.test_e2e_http` (E2E_BASE_URL) → `npx playwright test` in `mcr.microsoft.com/playwright:v1.49.1-noble`.
Seeded test-only users (password in `/__e2e/ids`): root (superadmin), owner/admin/editor/viewer (Acme), orgadmin (Support
admin), restricted (Sales viewer), ownerb (Globex owner), newcomer (no membership).

## 32. Phase 6B: release-candidate preparation (local only; not deployed, not committed)

Authorised 2026-11-19 after Phase 6A acceptance (tree `790495a4`). Isolated environments only.

| § | Item | Where |
|---|---|---|
| 32.1 | Phase 6A evidence preserved (final run + both failed runs, checksums); regression tests for both failures | `evidence/phase-6a/`, `test_core_throttle.py`, `test_api_webchat.py::TamperedTokenRegressionTests` |
| 32.2 | Legacy cleanup (inventory before removal, proofs, dispositions) | `legacy-inventory.md`; decisions 6B-A…6B-D |
| 32.3 | Encryption keys: versioned key ring, crash-safe rotation, runbook, rollback | `core/credentials.py::KeyRing`, `core/key_rotation.py`, `api/rotate_keys.py`, `operations.md` §1 |
| 32.4 | MongoDB open-file limit: findings, proposed override, rehearsal | `operations.md` §2, `deploy/proposed/docker-compose.db.nofile.yml`, `deploy/rehearsal/mongo-nofile-rehearsal.sh` |
| 32.5 | Backup/restore rehearsal and production checklist | `operations.md` §3, `deploy/rehearsal/backup-restore-rehearsal.sh`, `tests/integration/backup_restore_check.py` |
| 32.6 | Production wiring and release-candidate images | `api/production.py`, `server.py`, `worker.py`, `healthcheck.py`, `Dockerfile`, `deploy/proposed/docker-compose.spec012.yml`, `operations.md` §4 |
| 32.7 | Security review (S-1…S-11) and Specs 004–011 traceability | `security-review-6b.md`, `security-traceability.md` |
| 32.8 | Integration configuration audit | `operations.md` §5 |
| 32.9 | Strict Knowledge Only release gate and evaluation protocol (K-H experimental) | `knowledge-eval-protocol.md` |
| 32.10 | Phase 7 cutover plan (not executed) | `cutover-plan.md`; decision 6B-E |

**Release candidate 1** — backend tree `05d31ae188ec40e76dece8c7d45bf158c9ebafef`, frontend tree
`62c6e8fed6b745f588ff10632fb1624d9a652995`; images (local, never pushed):
`bitbotpro-api:spec012-rc1-05d31ae188ec` (`sha256:c51ab48217d8e736df331af99e846b95eab9c7069fb05ecce29ce8304ead9f3a`,
300 MB, label `io.bitbotpro.source-tree`), test image `bitbotpro-api-test:spec012-rc1-05d31ae188ec`
(`sha256:d6bb862905144ff3240008cda3485e1ae5d88138035aa85fb61298aec79992c1`). Base
`python:3.11.18-slim-trixie`; 34 pinned requirements (35 installed distributions incl. pip).
Trivy: 0 critical, 48 high (all Debian base packages; only zlib1g has a published fix).
Gate logs: `evidence/phase-6b/final-run-05d31ae1/`.

## 33. Phase 6C: private beta preparation (local only; not deployed, not committed)

| § | Item | Where |
|---|---|---|
| 33.1 | B-ACCT account creation by e-mailed invitation | `api/members.py` (`POST /api/invitations/register`), `frontend/src/app12/pages/Auth.js::CreateAccountFromInvitation`; decision 6C-A |
| 33.2 | B-API deferred, changelog retired, new isolated database | decisions 6C-B…6C-D; `integration-parity.md` §4b; `CHANGELOG.md` |
| 33.3 | Discord gateway: Identify spacing measured at send time | `runtime/discord_gateway_ws.py::IdentifyLimiter` |
| 33.4 | Image: base pinned by digest, zlib1g fix, residual Debian review | `Dockerfile`, decision 6C-E, `security-review-6b.md` §3 |
| 33.5 | Real-service validation (V1–V5, mandatory) | `real-service-validation.md` |
| 33.6 | Beta activation (beside production, no downtime) | `beta-activation-plan.md` |

**Beta candidate 1** (local, never pushed): `bitbotpro-api:spec012-beta1-4219354d1469`
(`sha256:181265070b34d076111edd4338d7f91c823ddad6bb63e9fbc6bd47b203abb0f4`, 301 MB); test image
`bitbotpro-api-test:spec012-beta1-4219354d1469` (`sha256:70ddcde19ca6…`); backend tree `4219354d1469`, frontend tree
`1a54c99f4c56`; locks `requirements.txt` sha256 `4065d46c3abebcbc…`, `yarn.lock` `2f88ea42122a289e…`,
`e2e/package-lock.json` `5bced45fe8c6df08…`; base `python:3.11.18-slim-trixie@sha256:07d4824036af…`.
