# Specs 004–011 security protections → Spec 012 tests (Phase 6B traceability)

Phase 6B removed the legacy application (`legacy-inventory.md`). Some security suites tested
legacy routes directly, so they were retired with the routes. This table maps every retired test
class to the Spec 012 test that now enforces the same protection. It also says where a protection
no longer applies because the feature does not exist in Spec 012. The retired suites are preserved
in git (commit `1c20d2a`).

**Unchanged.** The suites for the kept modules still run as before (with legacy references removed):
`test_auth_security`, `test_oauth_webhook_auth`, `test_outbound_guard`, `test_security_utils`,
`test_security_headers`, `test_rate_limit`, `test_log_redaction` (+ 4 Spec 012 vectors),
`test_client_ip`, `test_public_urls`, `test_password_policy`, `test_chat_sessions`,
`test_email_delivery`, `test_runtime_config`, `test_entrypoint`, and the module-level part of
`test_api_integrations_ssrf`.

| Retired class (tests) | Protection | Spec 012 enforcement |
|---|---|---|
| server_app.KnowledgeBaseDisabledTests (4), server_static.KnowledgeBaseGuardTests (4) | Mongo KB disabled in `APP_MODE=api` | **N/A**: the Mongo KB is removed. Spec 012 knowledge: `test_knowledge.py` (access filter in the query, ACLs), `test_api_matrix` rows for `/knowledge/*` |
| server_app.UnaffectedRouteTests (4), server_static.HealthTests (1) | `/auth/me` needs auth; health reports DB state | `test_production_app.ApiAppTests.test_health_and_readiness`, `test_unauthenticated_requests_are_refused_without_detail` |
| server_app.CorsRuntimeTests (2), server_static.CorsTests (2), Stage10EdgeTests.cors_* | CORS only for validated origins | `test_api_security_4f.AdminOriginCorsTests`, `test_production_app.ApiAppTests.test_cors_only_for_the_admin_origin`, `ConfigTests` (wildcard and `http://` refused) |
| server_app.DiscordDisabledTests (3), server_static.DiscordTests (3) | No Discord client in the API; restart only by authorised users | `test_production_app.StaticTests.test_the_gateway_is_only_built_inside_the_worker`, `test_no_emergent_or_legacy_discord_client_in_shipped_code`; gateway controls superadmin-only: `test_api_discord.ApplicationOwnershipTests`, matrix |
| server_app.ProductionEmailEndpointTests (2), server_static.CredentialLoggingTests (7) | E-mail failure is safe; no secrets in logs | `test_api_auth.test_reset_answers_are_identical_even_when_e_mail_delivery_fails` (**new, Phase 6B finding S-2**), `test_api_phase6a.InvitationEmailTests`, `test_api_discord.ShareLinkTests.test_a_failing_mail_provider_does_not_fail_the_share_link`, `test_production_app.MailerTests`, `StaticTests.test_no_secret_is_logged_by_the_wiring`, `LogRedactionInstalledTests` |
| server_app.ImportTimeConfigTests (2), server_static.StartupTests (4) | Invalid configuration refuses to start; start-up creates no accounts | `test_production_app.ConfigTests`, `ProcessTests.test_server_refuses_to_start_*`; bootstrap is a separate command (`test_api_auth.BootstrapTests`) |
| server_app.Stage2AppTests (6), server_static.Stage2StaticTests (8) | No API docs in production; webhook secrets constant-time and indistinguishable; no plaintext sidebar keys | `test_production_app.ApiAppTests.test_security_headers_and_no_api_documentation`; `test_api_telegram.T3WebhookSecurityTests`, `test_api_zendesk.WebhookTests`, `SidebarTests` (`bbz_` keys hashed, Z-H) |
| server_app.SidebarKeyHashingTests (5) | Sidebar key shown once, stored hashed, per integration | `test_api_zendesk.SidebarTests` |
| server_app.AccountTakeoverFlowTests (22), server_static.Stage3StaticTests (8) | Hashed single-use reset tokens, brute-force limits, lockout, session revocation, policy, IP limits not bypassed by forged headers | `test_api_auth.PasswordTests` (flow, expiry, throttling, single use under concurrency, sessions revoked, **floor + uniform answers**), `LoginLogoutTests` (per-account and per-IP throttles, concurrency, account enumeration), `SessionTokenTests` (token version, deleted users), `test_client_ip` (forged headers), `test_core_throttle` (boundary regression) |
| server_app.AuthorizationScopingTests (11), server_static.Stage3bAuthorizationStaticTests (6) | Superadmin-only platform operations; scoped invitations, no escalation, listings scoped | `test_api_matrix` (134 routes × 7 actors, DB unchanged on refusal), `test_api_members.py`, `test_api_discord.ApplicationOwnershipTests`, `test_api_phase6a.UserDiscoveryTests` |
| server_app.ChatSessionSecurityTests (15), server_static.Stage4ChatStaticTests (7) | Server-issued session tokens, no first-bot fallback, per-IP/per-bot limits, size limits, no chat content in logs | `test_api_webchat.W6SessionSecurityTests` (+ `TamperedTokenRegressionTests`), `W7LimitTests`, `W10AvailabilityTests`, `W5ChatAndStreamingTests` |
| server_app.PublicOriginAndEmbedTests (8), server_static.Stage9bStaticTests (4) | `embed.js` cannot be injected; sandboxed iframe; links use public URLs; sidebar manifest allowlist | `test_api_webchat.W1EmbedLoaderTests`, `W11OriginTests`, `W2WidgetPageTests`; `test_production_app.ConfigTests` (URL bases); `test_api_zendesk.SidebarTests` |
| server_app.Stages5to8Tests (20), server_static.Stages5to8StaticTests (6) | Invitation-only registration, uniform answers, response floor, OAuth state random/hashed/bound/single-use, signed webhooks, body limits, webhook rate limits | `test_api_members` (invitations atomic, re-validated, indistinguishable), `test_api_auth` (floor), `test_api_discord.OAuthTests`, `test_api_zendesk.OAuthTests`/`WebhookTests`, `test_api_telegram.T3WebhookSecurityTests`. **Registration: B-ACCT (no account creation yet)** |
| server_app.Stage10EdgeTests (5), server_static.Stage10StaticTests (3) | Security headers on every response, preflights, no caching, client IP only from the configured proxy | `test_api_security.SecurityRegressionTests`, `test_production_app.ApiAppTests.test_security_headers_*`, `test_client_ip_trusts_only_the_configured_edge`, `test_client_ip` |
| server_static.EmergentImportTests (3) | No `emergentintegrations` | `test_production_app.StaticTests.test_no_emergent_or_legacy_discord_client_in_shipped_code` (the dependency is no longer installed) |
| public_routes.PublicRouteInventoryTests (1) | Every unauthenticated route reviewed | `test_production_app.PublicRouteInventoryTests` (23 reviewed routes) |
| tenant_scope.AccessibleInstancesTests (11) | Login lists only accessible bots | **N/A** (legacy `X-Instance-ID` scoping). Spec 012: navigation filtered by memberships, `test_api_matrix`, `test_core_isolation` |
| v2_tenant_isolation.TenantIsolationTests (23) | Tenant headers grant nothing; foreign ids 404 and change nothing; nested resources | `test_api_auth.SessionTokenTests.test_tenant_headers_never_grant_access`, `test_api_matrix` (foreign tenant actor on every route, DB snapshot unchanged), `test_core_isolation`, `test_api_security` |
| v2_tenant_isolation.SecretExposureAndProviderDefaultTests (7), MissingKeyMessageTests (2) | Provider keys never in responses; clear errors without a key | `test_api_credentials` (write-only, fingerprints), `test_api_webchat.ProviderAndCredentialTests`; templates and Emergent defaults are retired |
| outbound_routes.OllamaConfigEndpointTests (4), OllamaCallTests (10) | Ollama SSRF guard | **N/A**: Ollama is not offered (platform policy `ai.allowed_providers` = Anthropic, OpenAI; `test_core_config`); providers call fixed hosts only (`test_runtime_providers`) |
| outbound_routes.ZendeskAccountTests (5), ZendeskRoutesHelperTests (3) | Subdomain validation, no redirects, pinned address | `test_api_zendesk.OutboundGuardTests`, `test_outbound_guard` |
| api_integrations_ssrf.IntegrationRouteTests (7 route-level) | Spec 011 API-integration routes | **Retired with the unmounted routes; must be rebuilt in the B-API port.** Module-level guards still run (`GuardedRequestTests`, `EndpointExecutionTests`, `TLSPinningTests`, `SourceGuardTests`: 16) |

New protections added in Phase 6B: S-1 log redaction for the Spec 012 secret paths; S-2 uniform,
floored, background password-reset e-mail; S-3 Discord share link survives mail failure; S-4
trusted resume URLs for the Discord gateway; S-5 Host-header allowlist; versioned keys and key
rotation (`test_key_rotation.py`). See `security-review-6b.md`.
