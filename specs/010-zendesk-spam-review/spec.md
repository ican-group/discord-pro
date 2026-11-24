# 010 — Zendesk spam review (BridgeBot only)

## Problem

Spam tickets (marketing, SEO offers, phishing, automated junk) sit in the New, Open and
Pending queues next to real customer tickets. Operators have no way to set them aside in
BridgeBot.

## Goal

BridgeBot flags suspected spam with a short reason. An operator confirms it (**Mark as spam**)
or clears it (**Not spam** / **Restore**). Confirmed spam leaves the normal tabs and appears in
a **Spam** tab. This first version is local to BridgeBot: nothing is changed in Zendesk
(no status, tag, comment or suspension).

## Behaviour

### Detection
- When the New, Open or Pending tab loads, the page sends the ticket IDs to
  `POST /api/zendesk/spam-states`. Tickets not checked before are read from Zendesk
  (`tickets/show_many.json`, one read request) and classified in one model call per batch
  of up to 25: subject, channel and the first 1,500 characters of the description.
- The model is the workspace's configured model and API key, at low effort, with ticket
  text treated as untrusted data. It returns, per ticket, spam yes/no and a reason of at most
  120 characters. Usage is recorded like other Zendesk calls (platform `zendesk`).
- Results are cached per workspace and ticket in Mongo `zendesk_spam`, so each ticket is
  classified once. If the call fails or the reply can't be parsed, nothing is cached and the
  ticket shows no flag; the next load retries.
- Solved and closed tickets are not checked.

### States (per ticket)
| State | Meaning | Shown as |
|---|---|---|
| `suspected` | AI flagged it; no operator decision | "Suspected spam" tag with the reason, in its normal tab |
| `spam` | Operator marked it as spam | Hidden from New/Open/Pending/Recently solved; listed in Spam |
| `clear` | AI found nothing, or operator chose Not spam / Restore | No tag |

An operator decision always wins over the AI verdict and is never overwritten by it.

### Operator actions
- `POST /api/zendesk/tickets/{id}/spam` with `{"spam": true}` marks the ticket as spam;
  `{"spam": false}` clears it (Not spam, or Restore from the Spam tab). The ticket's subject
  and Zendesk status are read once (read-only) and stored for the Spam tab. Who decided
  and when are recorded.
- The selected-ticket panel shows the flag and reason, and **Mark as spam** or
  **Not spam** / **Restore** buttons. These buttons never call Zendesk write APIs.

### Spam tab
- `GET /api/zendesk/spam-tickets` lists the workspace's confirmed spam, most recently
  marked first (up to 100), with the reason and who marked it.
- The Zendesk ticket lists drop confirmed spam, so a page can show fewer than 25 tickets.

## Out of scope
- Writing anything to Zendesk (marking as spam, suspending, tagging, closing).
- Automatic actions without an operator. Re-checking a ticket after its first check.

## Verification
- Isolated Mongo and ParadeDB copies on a separate port; Zendesk reads only, with a
  before/after `updated_at` snapshot showing no ticket changed.
- Classification of the live queue and of synthetic spam and genuine tickets.
- Mark as spam → hidden from its tab, listed in Spam; Restore → back in its tab; caching
  (second load makes no model call); operator decision survives re-checks; workspace isolation.
- Frontend production build.
