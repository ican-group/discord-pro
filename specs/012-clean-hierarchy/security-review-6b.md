# Spec 012 — release-candidate security review (Phase 6B, 2026-11-19)

Scope: the release candidate (`backend/` Spec 012 packages, the new production wiring
`api/production.py`, `server.py`, `worker.py`, `healthcheck.py`, the frontend `src/app12` and
public pages) on simulated services only. Method: reading the code paths, targeted greps,
existing and new automated tests, the image checks in the gate, and the full Specs 009–011
regression suites (kept-module suites + `security-traceability.md`). No security requirement
was weakened to make a test pass.

## 1. Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| S-1 | **High** | `log_redaction` only knew the legacy webhook paths. Uvicorn's access log would have written the Spec 012 Telegram/Zendesk webhook **path secrets** (`/api/hooks/{telegram,zendesk}/{id}/{secret}`) and Discord **share-link tokens** (`/api/public/discord/install/{token}`) in plain text. Redaction was also installed only at import, before uvicorn created its handlers | **Fixed**: new patterns (sidebar/OAuth paths excluded) + `install()` again in the API lifespan; `test_log_redaction` (+4 vectors), `test_production_app.LogRedactionInstalledTests` |
| S-2 | **Medium** | `POST /auth/forgot-password` awaited the mailer only for existing accounts: answer **timing** revealed account existence, and with a real mail provider a delivery failure became a **500** for existing accounts only (latent while the mailer was a no-op) | **Fixed**: e-mail sent as a background task after the response, failures swallowed (type logged, never the link), 250 ms response floor; `test_api_auth.test_reset_answers_are_identical_even_when_e_mail_delivery_fails` |
| S-3 | Low | Discord share-link creation raised a 500 after the link was stored when the invitation e-mail failed | **Fixed**: `email_sent: false` reported (same pattern as F3 invitations); `ShareLinkTests.test_a_failing_mail_provider_does_not_fail_the_share_link` |
| S-4 | Low (defence in depth) | The gateway adapter resumed to any `resume_gateway_url` (from READY or the stored state); Resume carries the **bot token** | **Fixed**: only `wss://*.discord.gg` or the configured gateway host; otherwise the configured URL. `test_discord_gateway_ws.ResumeUrlTrustTests` |
| S-5 | Low | No Host-header validation in the Spec 012 app (Caddy forwards `Host {host}`; nothing builds URLs from Host — verified by grep — but a foreign Host was accepted) | **Fixed**: `TrustedHostMiddleware` (`ALLOWED_HOSTS`, default API host + loopback); `test_host_header_allowlist`, image smoke test |
| S-6 | Medium (design) | Single unversioned encryption key, no rotation path | **Fixed**: versioned key ring + crash-safe rotation (`operations.md` §1) |
| S-7 | Info | Fernet has no associated data: with database write access a ciphertext could be moved between documents | Accepted (database write access is already full compromise); AES-GCM with the document id as AD proposed for later |
| S-8 | Info | `MODEL_ALLOWLIST` predates the current provider model families | Open requirement 3 (review with the product owner; not changed here) |
| S-9 | Info | The frontend `package.json` still lists packages used only by removed legacy pages (they are no longer in the bundle: −363 kB) | Cleanup proposed for Phase 7 (lock-file change) |
| S-10 | **High** | The first release-candidate image scan (Trivy, gate run 1) found 5 critical and 123 high findings: stale Python pins (PyJWT 2.11.0, anyio, certifi, cryptography 46.0.5, dnspython, pymongo 4.10.1, starlette 0.37.2, urllib3) and Debian bookworm packages without fixes | **Fixed by refresh**: every Python pin moved to the fixed version (FastAPI 0.165 / Starlette 1.10, cryptography 50.0.2, PyJWT 2.15.1, PyMongo 4.20, …), base `python:3.11.18-slim-trixie`, `setuptools`/`wheel` removed from the image; re-scanned in the final gate (§32.7 of `spec.md`) |
| S-11 | Medium (test integrity) | With FastAPI ≥ 0.13x, `app.routes` holds lazily included routers, so the permission-matrix coverage test, the CSRF sweep, the public-route review and the contract exporter would have **silently seen zero routes and passed** | **Fixed**: `api.app.route_table()` flattens both forms; all four use it; guard test `test_the_route_table_sees_every_route` (≥ 150) |
| B-ACCT | **Release blocker** | No account-creation path (invitations require an existing verified account) | Decision 6B-C proposed |
| B-API | **Release blocker** | Spec 011 API integrations not ported (source retained, not mounted) | Decision 6B-B |

## 2. Review checklist

| Area | Result | Evidence |
|---|---|---|
| Authentication and session security | Pass | HS256 with required `exp/iat/sub/sid`; server-side sessions revocable; token version; `HttpOnly; Secure; SameSite=Strict; Path=/api`; no web storage (Jest guard) — `test_api_auth`, Playwright C1 |
| CSRF | Pass | Custom header required for cookie sessions on unsafe methods; Bearer unaffected — `test_api_auth.test_csrf_header_required_for_cookie_flows`, Playwright |
| Password reset | Pass after S-2 | Hashed single-use 1-hour tokens, per-account/IP throttles, sessions revoked, policy enforced, uniform floored answers |
| Role permissions | Pass | Matrix 134 routes × 7 actors with DB-unchanged assertions; grant ≤ own role |
| Tenant isolation / non-disclosing 404 | Pass | Matrix foreign-tenant actor, `test_core_isolation`, e2e role journeys |
| Credential ownership and usage scope | Pass | `test_api_credentials`, `test_core_credential_scope` (use ≠ manage, X6 scopes, fail closed) |
| Secret encryption and redaction | Pass after S-1, S-6 | Key ring, write-only views, fingerprints, `test_key_rotation`, log redaction |
| Webhook authentication | Pass | Telegram path + header secrets (constant time, required); Zendesk path secret + mandatory HMAC ±5 min (D-Q10) |
| OAuth state validation | Pass | 32-byte state, hashed, bound (user, token version, tenant, instance/integration), 10 min, single use; PKCE S256 for Zendesk |
| SSRF / DNS rebinding / redirects | Pass | Kept `outbound_guard` for every Zendesk call (single resolution, every answer public, pinned IP + SNI, no redirects, size cap); providers and Telegram/Discord on fixed hosts; no URL fetching from tenant input |
| Host-header validation | Pass after S-5 | `TrustedHostMiddleware`; no URL built from Host |
| Rate limiting | Pass | Atomic sliding-window throttles (ceil fix + boundary regression tests); per-IP via `client_ip` with trusted proxies only |
| CORS | Pass | Exact https admin origins only, credentials, no wildcard |
| Knowledge-source access | Pass | Access filter inside the retrieval query; document ACLs; tenant-scoped indexes |
| Prompt-injection resistance | Pass (deterministic layers) | Injection items in corpus + holdout; claims must quote evidence verbatim; K-H experimental (`knowledge-eval-protocol.md`) |
| Quota accounting | Pass | Reservation ledger, preview counted and billed (F5), integration attribution |
| Deletion safety | Pass | 6A-G re-checks; `test_deletion_safety` (7), load test orphans 0 |
| Worker leases | Pass | Gateway and job leases with CAS; `GatewayTests`, Mongo lease test |
| Audit events | Pass | Security-relevant actions audited; key rotation audited without secrets |
| Replay protection | Pass | Telegram `update_id` and Zendesk `event_id` dedupe; Zendesk timestamp window; single-use OAuth state, invitation, reset and preview tokens |
| Container | Pass | Non-root, read-only root, `cap_drop ALL`, no secret ENV/ARG/history, retained legacy sources and tests not shipped; Trivy scan in the gate report |

## 3. Residual Debian findings (Phase 6C review, image `spec012-beta1-4219354d1469`)

Trivy: **0 critical, 47 high** (114 medium, 69 low, 10 unknown); **no Python-package findings**.
The published zlib1g fix (`deb13u1`) is installed by exact version: zlib1g no longer appears. All
47 remaining high findings are Debian trixie packages **with no fixed version published**:
util-linux family (util-linux, mount, login, bsdutils, libblkid1, libmount1, libsmartcols1,
libuuid1, liblastlog2-2: 4 CVEs each), libsqlite3-0 (3), perl-base, ncurses (base/bin, libtinfo6,
libncursesw6), libsystemd0/libudev1, libacl1.

Realistic exposure in this container: **low**. The process is the Python app only (no shell, no
login, no mount, no setuid: `no-new-privileges`, `cap_drop ALL`, non-root, read-only root file
system); the code never imports `sqlite3` or `curses` and runs no external program except the
sandboxed Python PDF extractor (`sys.executable`); untrusted input reaches only Python parsers.
Residual risk accepted for the private beta; re-scan and take Debian updates at every rebuild
(the base image is pinned by digest, so updates are deliberate).
