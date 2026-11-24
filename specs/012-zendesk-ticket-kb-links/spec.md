# 012 — Link KB entries to Zendesk tickets; two-group evidence for suggested replies

## Problem

Suggested replies search the whole Knowledge Base as one pool. Saved Zendesk ticket solutions
(stored as `faq`) compete equally with curated FAQs and Tech Notes, and operators can't
point the bot at the entries they know are relevant to a ticket.

## Provenance

A Knowledge Base entry is a **saved ticket solution** when its URL is a Zendesk agent ticket
URL (`https://<subdomain>.zendesk.com/agent/tickets/<id>`) or it has a `zendesk-ticket-<id>`
tag. Both "Save to Knowledge Base" and "Add to KB" store entries this way. Every other `faq`
or `tech_note` entry is a **manual** FAQ or Tech Note.

## Linked KB entries (ticket panel)

- A "Linked KB entries" section in the Zendesk ticket panel lists each linked entry's title
  and type (FAQ, Tech Note, Ticket solution), with **Unlink**.
- Operators search the workspace's active FAQs and Tech Notes (including saved ticket
  solutions) by keyword or meaning, and **Link** results. Several entries can be linked; the
  same entry can't be linked twice (409).
- Links are stored in BridgeBot (Mongo `zendesk_kb_links`, keyed by workspace, ticket and
  entry), so they persist when the ticket is reopened. Nothing is written to Zendesk.
- Linking works on any ticket status. Linking reads the ticket from Zendesk to confirm it
  exists; the entry must belong to the same workspace (404 otherwise).
- Deleting an entry removes its links; deleting a workspace removes its links. Links to an
  entry that no longer exists are dropped when listed.
- Add to KB / Save to Knowledge Base stay restricted to solved and closed tickets.

API: `GET /api/zendesk/tickets/{id}/kb-links`, `POST /api/zendesk/tickets/{id}/kb-links`
`{"source_id"}`, `DELETE /api/zendesk/tickets/{id}/kb-links/{source_id}`,
`GET /api/zendesk/kb-entries?q=`.

## Suggested replies

Every suggested reply runs two hybrid (BM25 + vector) searches over active entries of the
workspace, with the same query as before (subject, latest requester update, description):

1. **Workspace knowledge:** manual FAQs and Tech Notes.
2. **Ticket solutions:** saved solutions from solved or closed tickets, linked or not.

Within each group, entries linked to this ticket get a relevance boost, and linked entries
that the search didn't match are added at the end of their group. The model receives the two
groups separately and is told to:
- give relevant workspace knowledge precedence;
- use ticket solutions as supporting evidence, or as the main evidence only when workspace
  knowledge has no relevant match, saying so and citing the ticket number;
- suggest clarifying questions when neither group gives enough evidence.

Budgets: up to 8 parts / 16,000 characters of workspace knowledge and 6 parts / 12,000
characters of ticket solutions. The response lists the entries used in each group, and the
panel shows them under the draft. Scraped URLs and uploaded documents are not part of either
group.

General chat search (`search_knowledge`) is unchanged.

## Verification

Isolated database copies on a separate port; Zendesk reads only (before/after snapshot).
- Link, list, duplicate (409), unlink, reopen (links persist), entry from another workspace
  (404), another workspace's links invisible, link on open and solved tickets, Add to KB
  still refused on an open ticket.
- Suggested replies on an unlinked and a linked ticket: logs show both searches ran; manual
  knowledge outranks ticket solutions; linked entry boosted/added; clarifying questions when
  nothing matches.
- `search_knowledge` output identical to the previous code for a set of queries.
