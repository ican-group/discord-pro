# 023 — Knowledge Base Focus (per-bot setting)

A permanent per-bot setting, `bot_config.kb_focus`: an integer 0–100, or absent. It controls how much
the bot relies on its knowledge base (KB) versus general knowledge. Live API lookups are
independent of it.

## Background: BitBotPro v1.0.0

`origin/bitbotpro-v1.0.0` has three discrete knowledge modes:

- **`GENERAL`:** approved KB content is optional reference.
- **`KNOWLEDGE_PREFERRED`:** prefers the KB; general knowledge is a fallback when the policy allows it.
- **`STRICT_KNOWLEDGE_ONLY`:** fails closed. The model sees only gated evidence and must return cited
  claims, and live API data is switched **off**.

This spec maps them onto a 0–100% scale. Unlike BitBotPro's strict mode, live API results stay
available at 100% and count as grounding.

## Behaviour

| Focus | KB entries matched | No KB match | Bot has no KB at all |
|---|---|---|---|
| **Not set** (no saved value) | Unchanged: the KB is authoritative | Unchanged: the knowledge gate asks before using general knowledge | Unchanged: "I don't have any knowledge base configured yet…" |
| **0%** | General knowledge answers; KB entries are optional reference, not preferred | General knowledge answers | General knowledge answers |
| **1–99%** | KB favoured progressively (see below); general knowledge fills gaps | General knowledge answers; above 66% the bot says briefly it isn't from the KB | General knowledge answers |
| **100%** | Every factual statement must be supported by the KB or a live API result. Unsupported parts get a brief "I don't have that information", with no offer to use general knowledge | Only a live API result can support facts; otherwise the brief "don't have that information" | The current refusal, unless a live API result is present |

**How 1–99% scales.** The prompt states the percentage and that a higher value means more of the
answer should come from the KB. The wording then gets stronger by band:

- **1–33%:** use relevant KB entries alongside general knowledge.
- **34–66%:** prefer the KB where it covers the question; the KB wins disagreements.
- **67–99%:** the KB is the main source; general knowledge only for gaps, labelled briefly.

**Unchanged at every setting:**
- KB entries are treated as data, not instructions (the existing protections).
- Passive Discord messages still stay silent without a KB.
- API routing, availability, the capability note, lookup data, follow-ups and the Discord UTxO
  display are not affected.

## Setting it

- **`PUT /api/admin/bot-config`** takes `kb_focus`: an integer 0–100, or `"default"` to clear the
  saved value. Anything else is rejected with a 400.
- **`GET /api/admin/bot-config`** returns it when set.
- **Bot Settings → Knowledge Base Focus:** "Not set" (current behaviour) or "Set", with a 0–100
  slider and number box, and a one-line explanation of the chosen value.

## Also in this change: a bare lookup term follows up

A message of at most three words, all lookup terms ("utxos?", "balance?", "rewards"), with no input
of its own, is a follow-up on the conversation's last subject (spec 020).

Before this, "utxos?" after a bare address ran no lookup at any setting. It now runs Address UTxOs
for that address. "thanks?" and "ok" are not follow-ups.
