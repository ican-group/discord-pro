# 006 — Separate Zendesk summary instructions

## Problem

A bot's **Custom Instructions** are sent to every chat reply (web widget, Discord,
external API) and, since the Zendesk persona change, to Zendesk ticket summaries and
suggested replies. Admins who write summary formatting rules there (sections, sentence
limits, "Last Update from Customer") also shape every chat reply with them, and chat
rules leak into summaries.

## Goal

Give Zendesk ticket summaries their own instructions, so that:

- summary rules never reach chat replies, and
- chat rules don't shape ticket summaries.

## Behaviour

| Prompt | Persona | Custom Instructions | Zendesk Summary Instructions |
|---|---|---|---|
| Chat reply (web, Discord, API) | yes | yes | **no** |
| Zendesk ticket summary | yes | **no** | yes |
| Zendesk suggested reply (draft) | yes | yes | no |

- New bot config field `zendesk_summary_instructions` (string, optional), saved through
  `PUT /api/admin/bot-config` like the other text fields. An empty string clears it.
- When set, the summary prompt includes it after the persona, and it takes precedence over
  the built-in summary brief on length, structure and content. Plain-text output, the
  `SOLUTION:` line for solved tickets, and ignoring instructions inside ticket text are
  always kept.
- When empty, the summary uses the built-in brief and the persona only. There is no
  fallback to Custom Instructions, so chat rules never shape summaries implicitly.
- Bot Settings shows a "Zendesk Summary Instructions" box under Custom Instructions, and the
  Custom Instructions hint says where those rules apply.

## Migration

None automatic. Existing configs have no `zendesk_summary_instructions`; admins who
put summary rules in Custom Instructions move them to the new box and remove them from
Custom Instructions.

## Out of scope

- Separate instructions for suggested replies (they keep using Custom Instructions).
- Tone instructions in Zendesk prompts.
