# 009 — Zendesk ticket queues by status

## Problem

The Zendesk page's "Open" list searches Zendesk for `type:ticket status:open` only. Tickets
arrive with status **New** and stay New until an agent replies or changes them, so newly
arrived tickets never appear in BridgeBot. **Pending** tickets are not shown anywhere either.

## Goal

Show each unsolved Zendesk status in its own tab, without changing the existing tabs or the
review badges.

## Behaviour

- Tabs: **New**, **Open**, **Pending**, **Recently solved**. Open stays the default tab.
- `GET /api/zendesk/open-tickets` takes an optional `status` query parameter: `new`, `open`
  (default) or `pending`. Any other value is rejected with 422. Without the parameter the
  endpoint behaves exactly as before.
- Each tab searches Zendesk for `type:ticket status:<status>`, most recently updated first,
  25 per page with Previous/Next, and keeps only tickets whose status matches the tab.
- Review badges (Needs summary, Summary posted, Status unavailable) and the Added to KB tag
  work the same in New, Open and Pending. Recently solved is unchanged.
- Switching tabs resets to page 1. Loading, empty and error messages name the tab.

## Out of scope

- On-hold tickets (none in the queue today; can be added as another tab later).
- Ticket counts on the tabs.
- Changing ticket status in Zendesk.

## Verification

- Read-only Zendesk requests through the API on a separate port with isolated databases:
  each tab returns only its own status, matches a direct Zendesk search for that status, and
  the default (no parameter) equals the Open tab. Invalid status returns 422.
- Review badges load for New and Pending tickets. No ticket is changed in Zendesk.
- Frontend production build passes.
