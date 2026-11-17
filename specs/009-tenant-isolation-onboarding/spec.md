# Spec 009 — v2 Tenant Isolation, First-Run Onboarding, AI Provider Defaults

## Status

**Approved for local implementation (2026-11-17).** No commit, push, image
build for production, container restart or deployment without separate
approval. Production data must not be changed.

**Progress (2026-11-17):** Priorities 1–3 implemented locally, uncommitted.
Gates passed: backend unit suite (host 521 OK; Docker `test` target 521 OK,
85 host-only skips) including 32 new tests in
`backend/tests/unit/test_v2_tenant_isolation.py` (171 failures + 6 errors when
run against the pre-fix code); frontend gate (67 Jest tests, production build
with the API origin). Not deployed. **Deploy order: frontend first, then API**
(the API now rejects a mismatched `X-Org-ID`, which the old frontend sends on
multi-org pages).

**Progress (2026-11-17, deployment prepared, not executed):** image
`bitbotpro-api:spec009-75737f5c636c` built from the exact working tree
(backend tree `75737f5c636c…`); Docker `test` target 521 OK (85 host-only
skips, all passing on the host); R8 image scan PASS; disposable real-MongoDB
end-to-end check PASS. Production now has one org ("Ican Group"), so the API
may go first. Steps: `deploy-runbook.md`.

## Background (production investigation, 2026-11-17)

- `Cardano` exists as a **workspace** (`workspaces`), with an owner (platform
  superadmin) and one invited workspace **admin**. The `organisations`
  collection is empty, so there is nothing a bot can belong to.
- No `POST /api/v2/orgs/{org_id}/bots` ever reached the API. The frontend has
  no selected organisation, so bot creation fails in the browser before any
  request is sent ("Failed to create bot").
- Code review confirmed a cross-tenant authorisation flaw: the org guards in
  `v2_helpers.py` authorise the organisation named in the `X-Org-ID` header,
  while handlers read and write the `{org_id}` in the URL. A member of org A
  sending `X-Org-ID: A` could list, create or change records in org B.

## Priority 1 — Tenant isolation (critical)

R1.1 Every `/api/v2/orgs/{org_id}/…` route authorises against the
organisation in the URL path.

R1.2 If an `X-Org-ID` header is present and differs from the URL `org_id`, the
request is rejected with **400** before any database write.

R1.3 Handlers use the verified organisation (`org["id"]`) for every query and
write; the raw path value is never used after authorisation.

R1.4 Nested resources (bots, templates, accounts, connections, suggestions)
must belong to the verified organisation, otherwise **404**. Updates and
deletes filter on `org_id` as well as `id`.

R1.5 Organisation member management validates roles (`member` | `admin`).
`create_org` without a `workspace_id` may only auto-assign to a workspace
where the caller is owner/admin, and enforces that workspace's org limit.

R1.6 Platform superadmins (`role` or `platform_role` = `superadmin`) keep
access to every organisation. Workspace owners/admins keep implicit access to
organisations in their own workspace only.

R1.7 Regression tests cover cross-tenant attempts on every org-scoped route
and assert the database is unchanged after each rejection.

R1.8 The frontend sends `X-Org-ID` equal to the URL organisation on every
org-scoped call (so legitimate multi-org pages keep working under R1.2).

## Priority 2 — First-run onboarding (frontend)

R2.1 When the selected workspace has no organisations, the Bots page shows an
empty state that explains Workspace → Organisation → Bot and links to
organisation creation. Bot creation is not possible without a selected
organisation.

R2.2 Creating an organisation passes the selected workspace and automatically
selects the new organisation.

R2.3 API errors show the server's `detail` message.

R2.4 Organisations are **not** created automatically with a workspace.

## Priority 3 — AI provider defaults

R3.1 New bots (create, and create-from-template when the template has no
config) default to `{"provider": "anthropic", "model": null}` with no key.
Clones and templates keep their source configuration. Existing bots are not
migrated.

R3.2 Bot responses include `llm_configured` (bool) so the UI can show that an
Anthropic or OpenAI API key is required before AI chat works. The LLM config
response reports whether the Emergent key is available on this deployment.

R3.3 A missing per-bot key produces a clear error on both the normal and the
streaming chat paths.

## Verification

Backend unit suite (host + Docker `test` target), new tenant isolation tests,
frontend Jest tests, production frontend build, full diff review and secret
scan.
