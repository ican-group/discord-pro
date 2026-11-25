# 021 — API lookups: the bot writes the answer

Builds on specs 018–020. Production feedback on 2026-11-25 ("why is the response so primitive?")
showed three problems:

1. **The bot still dumped fields.** Every reply was a fixed block of fields, not an explanation.
2. **"Ok what about now, can you query a transaction for me" looked up the latest block's
   transactions.** "now" counted as a request for current data, so the bot listed 10 hashes nobody
   asked for.
3. **A bare hash got "Is … a transaction hash or a block hash?"** The bot can find out on its own.

## Changes

- **The bot writes the answer.** The prompt receives the looked-up values as plain text with
  units: the most relevant fields first, then the rest of the record. It no longer receives a
  pre-formatted block to paste. The bot is told to:
  - lead with what was asked, in plain language;
  - keep it short;
  - show a list or table only when the user asks for one;
  - use values exactly as given (hashes and addresses may be shortened);
  - name the source once;
  - follow the channel's reply format: Discord Markdown, plain text for the web widget, Markdown
    for the API.

  The rules against inventing values, field counts, explorer links and retractions, and the
  wording for not-found and unavailable, are unchanged.
- **Where the funds went.** For a general question about a transaction ("tell me about …", or just
  the hash), the bot also fetches the provider's inputs-and-outputs endpoint for the same
  transaction (`related_endpoint`: a child path such as `/txs/{hash}/utxos` with the same
  required value). It can then say where the funds came from and went. That's one extra call,
  within the same rate limit.
- **A bare hash** is looked up as a transaction. If the provider answers "not found", the bot
  tries it as a block (`fallback`). It asks "transaction or block?" only when neither kind of
  endpoint is available. Saved transaction lookups still take bare hashes first.
- **"now"** no longer counts as asking for current data ("current", "latest" and "tip" still do).
- **The tester** is unchanged: it still shows the formatted preview of the data.
