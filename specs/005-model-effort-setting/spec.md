# Spec 005 — Effort setting with a usage-based recommendation

## Status
Implemented (local, uncommitted)

## Problem

BridgeBot now runs the Claude 5.5 models (Haiku, Sonnet, Opus), which think
before replying. How much they think is set by the API's `effort` parameter.
BridgeBot never sends it, so every request runs at the model's default:
`high` on Sonnet 5.5 and `medium` on Opus 5.5 and Haiku 5.5. For a chat bot
that adds latency and cost to every reply, while Zendesk summaries and drafts
over long tickets can benefit from more careful reading.

Admins can already choose the model and a per-bot Anthropic API key on Bot
Settings, but not the effort.

## Goal

1. Let admins set the effort per bot: **Model default**, **Low**, **Medium** or
   **High**.
2. Recommend an effort level from the bot's own recent Claude usage, explain
   why, and let the admin apply it with one click.

## Non-Goals

- Separate effort levels per task type (chat vs Zendesk). One setting per bot.
- `xhigh` / `max` effort (cost is hard to predict for a support bot).
- Automatically changing the effort without the admin saving it.

## Design

### Storage and requests (`backend/server.py`)

- `bot_config.effort`: `"low" | "medium" | "high"`, or absent for the model
  default. `PUT /api/admin/bot-config` accepts `effort` with those values, or
  `"default"` to clear it. Anything else is a 400.
- `call_claude(..., effort=...)` sends `output_config: {effort}` when set, on
  the primary and the fallback model. All five call sites pass the bot's
  setting: Discord, web chat, external API chat, Zendesk summary, Zendesk draft.

### Recommendation: `GET /api/admin/effort-recommendation`

Scoped by `X-Instance-ID`. Reads `llm_usage` for the last 30 days:

- **Chat requests:** platforms `discord`, `web`, `api`.
- **Ticket work:** platform `zendesk` (summaries and drafts).

Rules, in order:

| Condition | Recommendation |
|---|---|
| Fewer than 20 requests | `low`, marked low-confidence: a starting point for a chat bot |
| Chat share ≥ 50% | `low`: short chat replies; the model skips thinking on most simple messages, keeping replies quick and cheap |
| Otherwise (mostly ticket work) | `medium`: long ticket threads benefit from more careful reading |

`high` is never recommended automatically; the UI explains it is for when
answers are not good enough at Medium.

Response:

```json
{
  "recommended": "low",
  "confidence": "high" | "low",
  "reason": "…",
  "window_days": 30,
  "stats": {"total": 0, "chat": 0, "ticket": 0, "avg_input_tokens": 0,
            "avg_output_tokens": 0, "fallback_rate": 0.0, "failure_rate": 0.0},
  "current": "low" | null,
  "model_default": "high"
}
```

### Frontend (`frontend/src/pages/BotSettings.js`)

Below the model picker: an Effort picker (Model default / Low / Medium / High)
showing the selected model's default, and a recommendation card with the
reason, the usage numbers behind it and a "Use this" button. The setting is
stored on "Save All Settings", like the other fields.

## Verification

- Effort is validated, saved, cleared with `"default"`, and sent as
  `output_config.effort` on primary and fallback requests; not sent when unset.
- Recommendation rules return the expected level for: no data, mostly chat,
  mostly Zendesk.
- Live request at each effort level succeeds.
