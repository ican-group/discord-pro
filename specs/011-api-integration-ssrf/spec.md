# Spec 011 — API Integration SSRF Hardening

## Status

**Approved for local implementation (2026-11-17).** Builds on specs 009 and 010,
which it keeps unchanged. No commit, push, container restart or deployment
without separate approval.

**Deployed (2026-11-17 23:41 UTC):** production API runs
`bitbotpro-api:spec011-f245d7ed3bdb` (healthy; compose run as the docker-group
user, identical to `api-compose.sh` minus sudo). Verifier sections 1-4 PASS;
database containers unchanged; no rollback. Authenticated verifier section 5
is run by the owner. Frontend (GitHub/Emergent) not yet deployed.

**Progress (2026-11-17, prepared):** backend host 594 OK; Docker
`test` target 594 OK (85 host-only skips); security modules 105/105 in the test
image; new tests fail against the spec 010 code (47 failures + 1 error);
real-MongoDB disposable run PASS (incl. a dotted Docker alias resolving to a
private IP blocked at connect time). Frontend gate PASS. Image
`bitbotpro-api:spec011-f245d7ed3bdb`; R8 scan PASS. Steps: `deploy-runbook.md`.

## Problems (found in the spec 010 review)

The API-integration feature (`api_integrations.py`, `api_integrations_routes.py`;
used by test-connection, endpoint test, discovery, chat-time API queries and
diagnostics) had its own blocklist `_is_safe_url`, but:

1. **Redirect SSRF.** `httpx` followed up to 3 redirects and only the *final*
   URL was checked — the internal request had already been made.
2. **DNS rebinding.** The check resolved the host, then `httpx` resolved it
   again to connect.
3. **Discovery bypass.** `POST /integrations/{id}/discover` fetched
   `doc_source_url` with a raw `httpx` client and no check at all.
4. **Gaps:** IPv4 embedded in IPv6 (NAT64, 6to4, most `::ffff:` ranges),
   alternative IP spellings resolved by the OS, internal Docker names.
5. **Credential leakage.** Integration credentials were sent to
   `doc_source_url` on any host; LLM-chosen path parameters were inserted
   unencoded (e.g. `@evil.example`, `//evil.example`), and the auth header name
   was free text (could be `Host`).
6. **Information exposure.** Error strings included upstream bodies and raw
   exception text.

## Requirements

R1 Every outbound integration request goes through
`outbound_guard.guarded_request` (no module opens its own HTTP client; enforced
by a source test).

R2 URLs: `https` only in production; no credentials, fragments, backslashes,
whitespace/control or non-ASCII characters; canonical hosts only — alternative
IPv4 spellings (`2130706433`, `0x7f.1`, `0177.0.0.1`, `127.1`) and IPv6 zone IDs
are rejected; single-label and internal names (`localhost`, `*.internal`,
`*.local`, Docker service names) are rejected; non-public IP literals are
rejected when saved.

R3 Every DNS answer must be public (spec 010 address policy); the connection is
pinned to the checked address; TLS SNI and certificate verification use the
real host name.

R4 Redirects (301/302/303/307/308) are never followed; they are reported.

R5 Headers: callers cannot set `Host`, framing, hop-by-hop, proxy or
client-address headers; values may not contain CR/LF/NUL. The auth header
name is validated on save and on every request.

R6 Credentials are sent only to the integration's own origin (`base_url`).
Path parameters are percent-encoded and the final URL must stay on the base
origin.

R7 Errors return fixed messages (HTTP status + reason, "Could not connect",
"TLS certificate verification failed", …). Response data is returned only for
2xx responses. Responses are size-capped while streaming.

R8 Development only: `APP_ENV=development` and
`API_INTEGRATIONS_ALLOW_PRIVATE_NETWORKS=1` allow http and private targets
(metadata/link-local stay blocked).

## Knowledge-base URL imports (not enabled)

`/api/knowledge/sources/url`, `/url/rescrape`, `/url/crawl`,
`/url/crawl-stream`, `/bulk-rescrape` and `/github/*` in `server.py` fetch
user-supplied URLs with redirect-following `httpx` clients and **no SSRF
protection**. They are unreachable in production (`APP_MODE=api`: the
`_KnowledgeBaseGuard` returns 503 for all `/api/knowledge` routes) and must stay
disabled.

**Activation requirement:** before the Knowledge Base is enabled in any
deployment reachable by untrusted users, every one of these fetches must use
`outbound_guard.guarded_request` (or an equivalent pinned, no-redirect fetch
with the same URL/address policy), crawling must re-check every discovered
link, and regression tests equivalent to `test_api_integrations_ssrf.py` must
cover them. A comment at `_KnowledgeBaseGuard` in `server.py` records this.

## Tests

`tests/unit/test_api_integrations_ssrf.py` (25) and new cases in
`tests/unit/test_outbound_guard.py` (+6): redirect SSRF, DNS rebinding,
alternative IP encodings, internal Docker names, cloud metadata, Host/header
injection, credential leakage, error sanitisation, size cap, legitimate public
HTTPS APIs, and a real local TLS server proving that IP pinning keeps
certificate host-name verification.
