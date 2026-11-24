# 011 — Tech Note knowledge base entries

## Problem

Manual Knowledge Base entries can only be FAQs. Technical write-ups (known issues,
workarounds, internal procedures) are stored as FAQs too, so they can't be told apart in
the Knowledge Base list. This is also the entry type that Zendesk ticket linking (next step)
will attach to tickets.

## Behaviour

- `POST /api/knowledge/sources/faq` takes an optional `type`: `faq` (default) or
  `tech_note`. Any other value is rejected with 422. Requests without `type` create FAQs
  exactly as before.
- The Knowledge Base page's manual tab ("FAQ / Tech Note") has an Entry type choice:
  **FAQ** (question and answer) or **Tech Note** (title and technical content). The labels,
  placeholders, button and confirmation follow the chosen type.
- The list shows the type: "FAQ" or "Tech Note" badges. Existing entries are unchanged.
- Both types use the same storage, editing, priority, active toggle, chunking into parts,
  embeddings and hybrid (BM25 + vector) search. Editing keeps the entry's type.

## Out of scope

- Changing an existing entry's type.
- Linking entries to Zendesk tickets (next step).

## Verification

- API on a separate port with isolated databases: create one FAQ and one long Tech Note;
  both are listed with their type; the Tech Note is split into parts; keyword and paraphrased
  questions find each; editing a Tech Note keeps its type and re-splits it; an existing FAQ is
  unchanged; invalid type → 422; request without type → FAQ.
- Frontend production build.
