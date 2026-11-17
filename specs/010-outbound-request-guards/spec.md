# Spec 010 — Outbound Request Guards (Ollama SSRF, Zendesk token exposure)

## Status

**Approved for local implementation (2026-11-17).** Builds on spec 009, which it
keeps unchanged. No commit, push, container restart or deployment without
separate approval.

**Progress (2026-11-17, prepared, not deployed):** implemented with 42 new
tests (`test_outbound_guard.py` 20, `test_outbound_routes.py` 22). Backend:
host 563 OK; Docker `test` target 563 OK (85 host-only skips). Real-MongoDB
disposable run PASS (incl. an allowlisted internal host blocked by real DNS).
Frontend gate PASS. Image `bitbotpro-api:spec010-5e9a61bf49ff`; R8 scan PASS.
Steps: `deploy-runbook.md` (supersedes the spec 009 runbook).

## Problems (found in the spec 009 security review)

1. **Ollama SSRF.** Any organisation member could set a bot's `ollama_base_url`
   to any URL (e.g. `http://bitbotpro-mongo:27017`, `http://169.254.169.254`,
   `http://10.50.0.1`) and trigger it with *Test*. The API sits on the
   database network, and up to 200 characters of the response body were
   returned in the error (streaming returned the raw exception text).
2. **Zendesk token exposure.** The subdomain was normalised loosely, then put
   into `https://{subdomain}.zendesk.com/...`. A value such as
   `evil.example#` produced `https://evil.example#.zendesk.com/...`, sending the
   stored OAuth/API token (and, in the OAuth callback, the client secret) to
   `evil.example`.

## Requirements

R1 Ollama is disabled unless the operator sets `OLLAMA_ALLOWED_BASE_URLS`
(comma-separated exact origins). Production sets none, so it is disabled.
An invalid list disables Ollama (fail closed).

R2 A bot's Ollama URL must normalise to exactly one allowlisted origin
(`scheme://host[:port]`, no credentials/path/query/fragment). Checked on save
(`PUT …/llm-config`, 400) and on every request.

R3 At request time the host is resolved once; every address must be public
unicast. Always blocked: loopback, RFC 1918/ULA/shared, link-local, cloud
metadata (169.254.169.254, 169.254.170.2, 100.100.100.200, 192.0.0.192,
fd00:ec2::254), multicast, reserved, unspecified, documentation ranges, and
IPv4 embedded in IPv6 (mapped, 6to4, Teredo, NAT64). `APP_ENV=development` with
`OLLAMA_ALLOW_PRIVATE_NETWORKS=1` may allow loopback/private only.

R4 The connection is pinned to the checked address (Host header, TLS SNI and
certificate verification keep the host name), so DNS rebinding cannot swap in
another address. Redirects are never followed; proxy env vars are ignored.

R5 Ollama errors return only fixed messages and the HTTP status code — never
upstream bodies or exception text (normal, streaming and *Test* paths).

R6 Zendesk subdomains must be one DNS label `[a-z0-9-]{1,63}` (no leading/
trailing hyphen). Accepted spellings: `acme`, `acme.zendesk.com`,
`https://acme.zendesk.com[/]`. Everything else → 400. Validated on save
(connected accounts, OAuth start) and on every send.

R7 Every Zendesk URL is built by one function that fixes the host to
`<label>.zendesk.com`, accepts only safe API paths and re-checks the parsed
host. Token-bearing account tests never follow redirects.

R8 Regression tests cover all of the above; all spec 009 tests keep passing.

## Implementation

`backend/outbound_guard.py` (stdlib only); used by `server.py` (`_ollama_target`,
`_call_ollama`, `_stream_ollama`), `bot_routes.py` (llm-config save/read/test),
`v2_helpers.llm_configured`, `zendesk_routes.py` (`_normalize_subdomain`,
`_zd_url` at all six call sites) and `account_routes.py`. The frontend offers
Ollama only when the server reports `ollama_enabled`.
