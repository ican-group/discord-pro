# 015 — API Integrations: read-only transaction lookup

## Origin

Reference code from `origin/bitbotpro-v1.0.0`:
- `1c20d2a` — the original API tab (`frontend/src/pages/ApiIntegrations.js`), `outbound_guard.py`
  (SSRF guard) and `api_integrations*.py`;
- `02bbd40` — the rewritten request and import code (`runtime/api_calls.py`: `build_request`,
  `auth_for`, `parse_openapi`, `parse_reference`).

Changes from both: no LLM chooses endpoints or parameter values. An admin configures a
**lookup**; BridgeBot extracts and validates the input in code, calls one GET endpoint, and gives
the bot only the response fields the admin selected, formatted in code. The tester and the bot
run the same function (`run_lookup`).

## Admin flow (API Integrations tab, per workspace)

1. **Integration**: name, base URL (`https://` public host, optional base path), authentication
   (none, header, bearer, query parameter) and a secret. The secret is encrypted at rest and never
   returned (the UI only sees `has_secret`).
2. **Import** an OpenAPI 3 / Swagger 2 JSON file or a custom JSON reference
   (`[{path, method, summary, parameters: [{name, in, required, description}]}]`, also
   `{"endpoints": [...]}`). Up to 500 endpoints; only GET endpoints can be selected.
3. **Lookup**: name, one GET endpoint, and for each parameter its source:
   - *Transaction hash from the message* — with a value template (`$input`, e.g. `{$input}`
     for Koios' array syntax);
   - *Fixed value*;
   - *Not sent*.
   Then the response **fields**: path (`0.fee`, `outputs[0].amount`), label, unit, decimal shift
   (e.g. 6 for lovelace → ADA) and format (auto, number, text, Unix timestamp).
4. **Test lookup**: enter a message or hash; shows the request (URL without credentials),
   HTTP status, raw JSON (truncated for display) and the formatted preview — the exact text the bot
   would receive. Leaf paths of the raw JSON can be added as fields with one click.
5. **Bot access** is off by default and enabled per workspace.

## Bot behaviour

Web chat, Discord and the external API (`/api/v1/chat`), when the workspace has bot access on and
an enabled transaction lookup:
- The message is scanned for a 64-character hex string not part of a longer hex run. No hash, no
  call. The first hash is used; one lookup per message.
- The lookup runs (10 s timeout, 500 KB cap, at most 30 lookups per workspace per minute).
- The bot's prompt gets only the selected fields as labelled lines inside `<api_lookup>`, marked
  as data, not instructions — never the raw response, URL or credentials. Not found / API errors are
  passed as a short status so the bot can say so. Passive Discord messages don't trigger lookups.
- With lookup data, the knowledge gate doesn't block answering about that transaction; other
  questions keep the normal knowledge-base rules.
- Each call is recorded in `api_lookup_log` (workspace, lookup, source tester/bot, status,
  duration; no response data, hash prefix only).

## Outbound policy (ported from `outbound_guard.py`)

https only; host names must be public (no IP-literal private ranges, internal suffixes or
non-canonical IP spellings); every DNS answer must be a public unicast address, and the connection
is pinned to the checked address with TLS verified against the host name; no redirects, no
proxies, no caller-set Host/forwarding headers; every request URL must stay on the integration's
origin and under its base path (path values are percent-encoded); credentials are only attached to
that origin. Error messages are fixed and never include upstream content.

## Storage

Mongo: `api_integrations` (with imported `endpoints` and `secret_enc`), `api_lookups`,
`api_lookup_log`; `bot_config.api_lookups_enabled`. All scoped by `instance_id`; deleting a
workspace removes them.
