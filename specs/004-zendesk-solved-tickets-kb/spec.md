# Spec 004 — Save solved Zendesk tickets to the Knowledge Base

## Status
Implemented (local, uncommitted)

## Problem

Workspace → Zendesk lists open tickets only. Solved tickets hold proven answers
to real customer questions, but there is no way to turn one into knowledge the
bot can use. Admins would have to copy text out of Zendesk by hand.

## Goal

1. On Workspace → Zendesk, let the user switch the ticket list between
   **Open** and **Recently solved**. "Recently solved" shows the last 30
   tickets whose status is `solved` or `closed`, most recently updated first.
2. From a solved ticket, let the user prepare a Knowledge Base entry, review
   and edit it, and save it to the workspace's Knowledge Base.

## Non-Goals

- Bulk import of many tickets at once.
- LLM summarisation of the ticket (the draft is built deterministically; the
  user edits it). Can be added later reusing the existing draft-note flow.
- Syncing later changes in Zendesk back into the saved entry.

## Design

### Backend (`backend/server.py`)

| Endpoint | Purpose |
|---|---|
| `GET /api/zendesk/solved-tickets` | Zendesk search `type:ticket status>=solved`, sorted by `updated_at desc`, 30 results. Returns `{tickets: [{id, subject, status, updated_at}]}`. |
| `POST /api/zendesk/tickets/{id}/kb-draft` | Reads the ticket and its comments. Returns `{title, content, already_saved, source_id}`. Read-only. |
| `POST /api/zendesk/tickets/{id}/kb` | Body `{title, content}`. Re-checks the ticket is `solved`/`closed`, rejects duplicates (409), stores a knowledge source. |

All three use the existing `_zendesk_request` helper and `get_instance_access`,
so they are scoped to the workspace in `X-Instance-ID` and need an active
Zendesk connection. Only Zendesk read access is required.

**Draft content.** Subject as the title. Body contains the customer's request
and the **public** conversation only. Internal notes (`public: false`) are
excluded because Knowledge Base content can be quoted to end users. Authors are
labelled `Customer` (the ticket requester) or `Agent`, never by name, and email
addresses are masked as `[email]`. The user can edit everything before saving.

**Storage.** Saved as a normal knowledge source of type `faq` so the existing
Knowledge Base page lists, edits, prioritises and deletes it with no changes.
Tags: `zendesk`, `zendesk-ticket-<id>`. `url` / `canonical_url` hold the agent
URL `https://<subdomain>.zendesk.com/agent/tickets/<id>`, which is used to
detect duplicates per workspace.

**Limits.** Title 1–150 characters, content 1–20,000 characters. The draft
reads the first page (100) of ticket comments.

### Frontend (`frontend/src/pages/Zendesk.js`)

- An Open / Recently solved toggle above the ticket list. Paging applies to Open
  only; Recently solved is a single list of up to 30.
- When the loaded ticket is solved or closed, a "Save to Knowledge Base" panel
  appears: "Prepare entry" fills editable title and content fields, then
  "Save to Knowledge Base" stores it. Already-saved tickets show that state
  instead of allowing a duplicate.

## Errors

| Case | Response |
|---|---|
| No / expired Zendesk connection | 409 (existing helper) |
| Ticket not solved or closed | 422 "Only solved or closed tickets can be saved" |
| Already saved for this workspace | 409 with the existing source id |
| Zendesk 404 / 403 / 429 | Existing helper mapping |

## Verification

- Solved list returns ≤ 30 tickets, all `solved` or `closed`.
- Draft excludes internal notes and masks email addresses.
- Saving creates one `faq` source with the Zendesk tags; saving again returns 409.
- Saving an open ticket returns 422.
