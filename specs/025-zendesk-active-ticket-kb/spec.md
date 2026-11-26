# 025 — Reviewed Zendesk solutions from active tickets

Agents can save a reviewed solution to the Knowledge Base from **New, Open and Pending** tickets, as
they could from solved ones. Suggested replies on later tickets then find it by the issue it solved.

## Saving (Solution → Add to KB)

- Shown on New, Open, Pending, Solved and Closed tickets. Other statuses (for example Hold) are
  refused with 409.
- The agent reviews or types the solution. On active tickets the AI summary also proposes one, but
  only from a resolution confirmed in the conversation. Once the agent has typed in the box, the
  summary doesn't overwrite it.
- **Stored entry:** one Knowledge Base entry per ticket (type FAQ), keyed by the ticket's agent URL.
  - **Title:** the ticket subject.
  - **Tags:** `zendesk`, `zendesk-ticket-<id>`, `zendesk-reviewed-solution`.
  - **Content:**
    ```
    Ticket description:
    <the ticket's description>

    Reviewed solution:
    <the reviewed solution>
    ```
  - Email addresses in the title and content are replaced with `[email]`.
  - A separator line inside the description is defused, so the description can't pose as a solution.
- **Saving again** updates the same entry. Its creation time and active/inactive state are kept.
- **Zendesk is only read.** The ticket's status and comments are unchanged.

## Suggested replies

- The draft's search uses the ticket subject (first 15 words), description (first 45 words) and the
  requester's latest update, up to 80 words.
- **Pairing:** when the search matches any part of a reviewed-solution entry, the evidence sent to
  the model is that entry's description paired with its reviewed solution.
  - Description up to 1,000 characters, solution up to 2,200, once per entry.
  - This holds even when only the description part matched.
- The model is told to compare the saved description with the current issue first, and use the
  reviewed solution only when they match. A description reports an issue, so it is never a fix.
- Entries saved before this change (solution only) are used as before.
