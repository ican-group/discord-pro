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
  and type (FAQ, Tech Note, Ticket solution #N, Web page, Document), with **Unlink**.
- Operators search the workspace's active Knowledge Base entries by title, keyword or
  meaning, and **Link** results. Several entries can be linked; the same entry can't be linked
  twice (409).
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

Every suggested reply runs four hybrid (BM25 + vector) searches over active entries of the
workspace, linked and unlinked, with the same query as before (subject, latest requester
update, description). Groups, in priority order:

| # | Group | Entries | Budget |
|---|---|---|---|
| 1 | Workspace knowledge | manual FAQs and Tech Notes | 8 parts / 16,000 chars |
| 2 | Ticket solutions | saved from solved or closed tickets (by provenance) | 6 / 12,000 |
| 3 | Web pages | scraped URLs | 6 / 12,000 |
| 4 | Documents | uploaded documents | 6 / 12,000 |

Relevance check: every part sent to the model must pass the same check, linked or not: a BM25
match, or cosine similarity at or above `KB_MIN_SIMILARITY` (0.52).

Links within a group: a linked part that passed the check is boosted (×1.5) only when it is
itself a strong match, judged on raw scores. With both a BM25 and a vector score, both must be
strong: BM25 at least 60% of the group's best and similarity within 0.15 of the group's best
(one rare shared word can lift BM25 alone). With only one score, that one must be strong: BM25
at least 60%, or similarity within 0.05. It must also be near the best match
across all groups (BM25 at least 30% of the overall best, or similarity within 0.15 of it), so a
noise match that leads a weak group isn't treated as strong. (Fused RRF ranks are too
compressed to judge this: a one-word match can score 97% of the best.) Links reorder close
matches, but an unrelated linked entry can't displace a relevant unlinked one. Linked entries
that passed the check but don't make the group's budget reach the model in extra slots after
it (best part of each, up to 3 parts / 6,000 characters per group), so they never push out a
match. Linked entries that fail the check are not sent; they stay linked and visible in the
ticket panel, and the draft's "Sources considered" lists them as linked but not sent.

The model receives the four groups separately and is told to:
- use only relevant evidence; when relevant sources disagree, follow the higher-priority group
  and mention the conflict briefly;
- use relevant lower-priority evidence to fill gaps, saying when a step rests only on a past
  ticket, web page or document and citing it;
- treat all four groups as Knowledge Base content for workspace rules that require a KB
  article;
- suggest clarifying questions when the evidence is too weak.

The response lists the entries retrieved per group; the panel shows them as "Sources
considered" (not verified citations).

General chat search (`search_knowledge`) is unchanged.

## Verification

Isolated database copies on a separate port; Zendesk reads only (before/after snapshot).
- Link, list, duplicate (409), unlink, reopen (links persist), entry from another workspace
  (404), another workspace's links invisible, link on open and solved tickets, Add to KB
  still refused on an open ticket.
- Suggested replies on an unlinked and a linked ticket: logs show all four searches ran.
- A web page alone, and a document alone, can support a draft; FAQs/Tech Notes win when
  sources conflict; an unrelated linked entry doesn't displace a relevant unlinked one;
  clarifying questions when nothing matches.
- `search_knowledge` output identical to the previous code for a set of queries.
