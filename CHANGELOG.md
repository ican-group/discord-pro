# Changelog

Developer-facing release notes (decision 6C-C: the legacy in-app admin changelog is retired and not
rebuilt). Nothing here is deployed until the corresponding approval in
`specs/012-clean-hierarchy/IMPLEMENTATION_STATUS.md`.
## Unreleased — Spec 012 private beta 1 (prepared 2026-11-19, not deployed)

- New Enterprise → Organisation → Bot → Bot Instance hierarchy (Spec 012) with one permission
  model, inherited configuration and tighten-only policies.
- Channels: Discord (one application per Enterprise, superadmin-controlled; real gateway in a
  separate worker), Telegram (signed webhooks), Webchat (API-hosted widget); Zendesk as a Bot
  Integration (OAuth or API token, signed webhooks, public/private replies, agent suggestions,
  sidebar app).
- AI runtime with GENERAL, KNOWLEDGE_PREFERRED and STRICT_KNOWLEDGE_ONLY modes; knowledge
  sources and documents with access control; K-H semantic verifier experimental and off.
- Accounts by invitation: invited people create their account from the e-mailed link.
- Admin app at `/app`; old admin pages, Workspaces, scripted Discord mode, bot templates,
  `bbk_` API keys, API diagnostics, the Mongo knowledge base and the React chat widget removed.
- Security: versioned encryption keys with rotation, Host-header allowlist, log redaction of
  webhook secrets, uniform password-reset answers, pinned and refreshed dependencies.
- Deferred: Spec 011 API integrations (B-API), Zendesk analytics, OCR for scanned PDFs.

## v3.0.0 - Knowledge Retrieval & Platform Improvements

### Backend
- Removed dependency on Emergent private packages
- Replaced Emergent integrations with direct Anthropic SDK integration
- Added ParadeDB BM25 knowledge retrieval
- Added MongoDB fallback path for retrieval resilience
- Added knowledge retrieval observability and logging
- Added ParadeDB status and synchronization endpoints
- Added MongoDB → ParadeDB indexing workflow

### Knowledge Base
- Replaced static knowledge dumping with query-driven retrieval
- Added BM25 relevance ranking using ParadeDB
- Added knowledge source indexing and synchronization
- Preserved MongoDB as the source of truth for knowledge content

### Frontend
- Improved Analytics dashboard
- Improved Bot Settings experience
- Added support for ParadeDB administration and monitoring
