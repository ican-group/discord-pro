# 020 — API lookup answers: relevant fields, natural replies, follow-ups on the same thing

Builds on specs 018 and 019. It fixes three problems seen in production on 2026-11-25 with Grid
Scan's transaction lookup:

1. **The wrong fields were shown.** The reply listed the first 12 documented fields, so "Asset mint
   or burn count", "Delegation count: 0" and "Invalid before: none" appeared before fees and amounts.
   It ended with "…and 10 more fields".
2. **The model talked about the cut-off.** It only received those 12 fields, so it said the rest
   "were not included" and pointed to an explorer.
3. **Follow-ups were lost.** "Can you show me the inputs for this transaction?" contains no hash, so
   no lookup ran, although GridScan has `/txs/{hash}/utxos`.

## 1. Field choice (`format_result(..., question=)`), without an admin override

- **Single records.** At most 8 lines (`MAX_SHOWN`), in this order:
  1. fields the question names or asks about (by field-name words, or by concept: "fee" → `fees`);
  2. amounts and times (documented units, Lovelace → ADA, UNIX times);
  3. the rest, in documented order.
- **Left out unless the question asks for them:**
  - empty values (`null`, `""`, `[]`);
  - zero counts (`*_count`);
  - zero amounts.

  A real zero the user asked about is still shown: "Deposit: 0 ADA" for "what was the deposit?".
- **No "…and N more fields" line.** Every other field goes into `details`, which is only for the
  bot.
- **Lists inside a record** (a transaction's `inputs` and `outputs`) are shown as channel-suited
  tables. The column order is address, then amounts, then short values, then long ids. Columns that
  are empty in every row are dropped. When the question names a list ("inputs"), only that list is
  shown.
- **Unchanged:**
  - lists of records (tables of up to 10 rows), except that empty columns are dropped;
  - the four outcome wordings;
  - admin overrides, which still show exactly their fields.

## 2. What the model is told

- **The prompt receives:**
  - the RESULT block, as before;
  - OTHER FIELDS, the rest of the record, for follow-up questions.
- **The model is told to:**
  - start with a direct answer in one or two plain sentences, using the values;
  - include the RESULT block as written;
  - use OTHER FIELDS when asked, without listing them otherwise.
- **The model must not:**
  - convert units, round, or invent values;
  - mention field counts or omitted fields;
  - suggest an explorer, unless the lookup was unavailable.
- **The tester** shows these extra fields under "Other fields the bot can use".

## 3. Follow-ups about the last thing looked up

- **What's remembered.** After a lookup that answered (ok or not found), the inputs it used are
  remembered for 30 minutes in `api_subject` (TTL index). They are written so the router
  recognises them again ("transaction <hash>", "proposal <hash> index 0", "epoch 512", a bech32
  id). The key is the same as for pending questions: the web or API session, or the Discord user
  and channel. A saved transaction lookup also records its transaction.
- **When a message is a follow-up (`about_subject`).** All of these must hold:
  - it has no input of its own;
  - it is at most 300 characters;
  - it isn't a how-to or why question;
  - it refers back: this, that, it, its, same, those, these, them, "the transaction", or a leading
    "and…" / "what about…";
  - it asks about something lookup-shaped (a known concept, or "show", "list", "details",
    "more").
- **What happens.** When the message alone gives no call (none, or a question), the router runs on
  the message plus the remembered subject. The result is used only when it gives a call.
  "Can you show me the inputs for this transaction?" → `GET /api/txs/{hash}/utxos`.
- **Concepts.** "input(s)" and "output(s)" belong to the UTXO concept.
- **Pending questions (spec 019) are handled first.** Passive Discord messages never use either.

## Testing

- **Unit tests:**
  - field order, noise removal, a kept real zero, and `details`;
  - nested-list tables;
  - subject text round trips;
  - follow-up detection, positive and negative cases;
  - routing of the follow-ups.
- **End to end** on isolated copies with the GridScan mock: the production conversation is
  replayed on web chat and Discord with the real model, and the spec 018 and 019 suites are rerun.
