# 013 — Internal notes mark a ticket as reviewed

## Problem

Posting the AI summary turns a ticket orange ("Summary posted") in the ticket list, but
posting a reply from **Post internal note** on the same screen leaves it red ("Needs summary").

## Change

- New review state `note_posted`: same orange as `summary_posted`, labelled "Note posted".
- `_zendesk_review_state` reads the ticket history in order. A private note with the BridgeBot
  response header ("★ BridgeBot AI-generated response") sets `note_posted`; a summary note
  (checked first, including legacy summaries under the response header) sets `summary_posted`;
  a later public reply from the requester resets to `needs_summary`. The latest wins.
- Agents' own private notes written in Zendesk, and public replies, don't change the state.
- After **Post internal note** succeeds, the list shows "Note posted" right away for
  unsolved tickets, as summaries already do.
- Solved and closed tickets stay "Solved". Nothing new is written to Zendesk.
