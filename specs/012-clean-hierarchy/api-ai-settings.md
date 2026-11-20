# API: AI provider, model and credential selection (Stage 0, 2026-11-20)

Audience: the Emergent frontend. Claude Code owns these contracts; Emergent owns the design.
All routes are under `https://api.bitbotpro.io/api`, use the session cookie plus the CSRF header
(`X-Requested-With: BitBotPro`) like every other admin route, and are listed in
`frontend/src/app12/contract/routes.json`. Real response bodies are in `contract/samples.json`
(`ai_settings_bot`, `ai_settings_enterprise`, `ai_settings_put`, `ai_test`, `platform_ai_credentials`).

**Rule for every UI: never ask a user to type or copy an internal id.** Every choice below is a list of
named options returned by the server.

## Concepts

- **Credential**: an encrypted secret owned by an Enterprise or an Organisation (`/{owner}/credentials`), or a
  **platform AI key** owned by BitBotPro and granted by a superadmin to chosen Enterprises. Secrets are
  write-only: no response ever contains a secret; only `fingerprint` (`sha256:…`, first 10 hex).
- **Selection**: `ai.provider`, `ai.model`, `ai.credential_id` are normal configuration values set on an
  Enterprise, Organisation or Bot and inherited downwards (sparse overrides). `null` on a level means "inherit".
- **Policy**: the platform allows `anthropic` and `openai`; an Enterprise/Organisation can narrow this with the
  policies `ai.allowed_providers` and `ai.allowed_models` (`PATCH /{enterprises|organisations}/{id}/policy`).
  The options below already reflect the policy (`allowed: false` entries must be shown disabled or hidden).

## Permissions (enforced server-side; the UI uses the flags in the response)

| Action | Who |
|---|---|
| See the AI options (`GET …/ai`) | any member of the object (Viewer+) |
| Change provider/model/key (`PUT …/ai`), run a test (`POST …/ai/test`) | Editor+ on the object (`can_edit`), within policy |
| Add/rotate/revoke/delete an Enterprise key | Enterprise Admin/Owner |
| Add/rotate/revoke/delete an Organisation key | Organisation Admin (if the Enterprise allows delegation) or Enterprise Admin/Owner |
| Platform AI keys (`/platform/ai-credentials`) | Platform superadmins only; everyone else gets 404 |
| Use a key | only inside its owner (Organisation keys: that Organisation; Enterprise keys: the whole Enterprise unless restricted with `usable_by`; platform keys: granted Enterprises) |

## `GET /{enterprises|organisations|bots}/{id}/ai`

Options and current selection for one object.

```json
{
  "target": {"type": "bots", "id": "…", "name": "Support bot"},
  "can_edit": true, "can_test": true,
  "current": {
    "provider": "anthropic", "model": "claude-haiku-4-5", "credential_id": "…",
    "credential": {"id": "…", "name": "Support Claude key", "provider": "anthropic", "owner": "organisation",
                   "owner_name": "Acme Support", "fingerprint": "sha256:96be92d977", "status": "active", "…": "…"},
    "sources": {"ai.provider": {"kind": "override", "level": "bot", "object_id": "…"},
                "ai.model": {"kind": "default"}, "ai.credential_id": {"kind": "override", "level": "organisation", "object_id": "…"}},
    "blocked": null
  },
  "providers": [
    {"id": "anthropic", "label": "Anthropic (Claude)", "allowed": true, "default_model": "claude-sonnet-4-6",
     "models": [{"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "allowed": true}, "…"]},
    {"id": "openai", "label": "OpenAI", "allowed": true, "default_model": "gpt-4o", "models": ["…"]}
  ],
  "credentials": [
    {"id": "…", "name": "Support Claude key", "provider": "anthropic", "owner": "organisation", "owner_name": "Acme Support",
     "fingerprint": "sha256:…", "status": "active", "provider_allowed": true},
    {"id": "…", "name": "BitBotPro shared Claude", "provider": "anthropic", "owner": "platform", "owner_name": "Platform", "…": "…"}
  ],
  "can_add_credential_to": [{"type": "enterprises", "id": "…", "name": "Acme"}, {"type": "organisations", "id": "…", "name": "Acme Support"}]
}
```

- `credentials` contains **only keys usable at this object** (all AI providers); filter by the chosen provider.
  Label suggestion: `"{name} — {owner_name} (Enterprise|Organisation)"`, platform keys "shared by BitBotPro".
- `current.blocked` is a human-readable reason the object cannot answer yet (e.g. `"no AI credential is configured"`,
  `"The selected credential has been revoked."`); show it as a warning.
- `sources[k].kind`: `override` (set on `object_id`, "Set here" when it equals `target.id`), `default`, `policy`.
- `can_add_credential_to` lists the owners where this user may add a key; empty = hide "Add new key".
- Instances do not have AI settings (404); they inherit from their Bot.

## `PUT /{enterprises|organisations|bots}/{id}/ai`

Body (any subset; present fields are set, `null` returns that field to the inherited value):
`{"provider": "openai", "model": "gpt-4.1-mini", "credential_id": "…"}`

- Validated together: a provider switch plus its new model and key is one change. Omitting `model` while
  changing `provider` resets the model to that provider's default.
- `200` → the same body as `GET …/ai`. `403` not allowed (viewer, or locked by policy), `404` unknown/foreign object,
  `422` with `detail` in plain language (model not for that provider, provider not allowed, key not usable here,
  revoked key, key for a different service). Rejected requests change nothing.

## `POST /{enterprises|organisations|bots}/{id}/ai/test`

Body (optional; omitted fields use the current selection, so unsaved choices can be tested):
`{"provider": "anthropic", "model": "claude-sonnet-4-6", "credential_id": "…"}`

Performs **one tiny real generation** (≤ 32 output tokens) with the provider's fixed API host. Rate limit: 20 per
user per hour (`429` with `Retry-After`). Recorded as a usage event `ai_test` (token counts; not an AI message for
quotas) and in the audit log (`ai.test`).

```json
{"ok": true, "category": null, "message": "Connected: Claude Sonnet 4.6 answered.", "provider": "anthropic",
 "model": "claude-sonnet-4-6", "credential": {"id": "…", "name": "Support Claude key"}, "reply": "OK",
 "input_tokens": 14, "output_tokens": 2, "latency_ms": 840}
```

`ok: false` categories (show `message`, which is written for end users): `unauthorized` (key rejected),
`model_unavailable` (this key cannot use that model), `rate_limited`, `unavailable` (provider unreachable/5xx),
`uncertain` (timeout), `rejected`. Provider error text, URLs and keys are never returned.

## Credentials (existing routes, unchanged)

- `GET /{enterprises|organisations}/{id}/credentials` — list (public views, ids included for actions).
- `POST /{enterprises|organisations}/{id}/credentials` `{"provider": "anthropic"|"openai", "name": "…", "secret": "…",
  "usable_by": []}` → `201` public view. Several keys per provider are allowed.
- `PATCH /credentials/{id}` `{"name"?, "secret"?}` (rename / rotate; references keep working),
  `POST /credentials/{id}/revoke`, `DELETE /credentials/{id}`, `PUT /credentials/{id}/scope` (restrict to Organisations/Bots).
- `POST /credentials/{id}/test` — read-only key check (lists models; no generation, no cost).

## Platform AI keys (superadmin)

| Route | Body / result |
|---|---|
| `GET /platform/ai-credentials` | `{"credentials": [{"id", "provider", "name", "owner": "platform", "fingerprint", "status", "created_at", "rotated_at", "all_enterprises", "enterprises": [{"id", "name"}]}]}` |
| `POST /platform/ai-credentials` | `{"provider", "name", "secret", "all_enterprises": false, "enterprise_ids": []}` → `201` |
| `PATCH /platform/ai-credentials/{id}` | any of `name`, `secret` (rotate), `all_enterprises`, `enterprise_ids` |
| `POST /platform/ai-credentials/{id}/revoke` | immediate; bots using it stop answering (fail closed) |
| `DELETE /platform/ai-credentials/{id}` | removes it |
| `POST /platform/ai-credentials/{id}/test` | read-only key check `{"ok", "category"}` |

Granted Enterprises see a platform key in `GET …/ai` `credentials` (owner `platform`) and can select it; they can
never read, rotate or revoke it. Usage is counted against the tenant as for any other key.

## Knowledge modes and AI (behaviour, for UI copy)

- **General**: ordinary AI answers; if the bot has approved, selected knowledge sources, relevant excerpts are given
  to the model as optional reference material (no citations, never a refusal for missing knowledge).
- **Knowledge preferred**: answers from approved knowledge with citations; general answers only if the Enterprise
  allows the fallback.
- **Strict knowledge only**: only grounded, cited answers; otherwise the configured fallback. Never general knowledge.
