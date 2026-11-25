# 018 — API lookup routing: imported endpoints answer questions directly

Builds on specs 015 (saved transaction lookups) and 017 (URL import, connection check,
diagnostics). The importer, tester, formatter and diagnostics are reused; saved lookups keep working
unchanged.

## 1. Imported read-only endpoints are available to the bot

- **Per integration "Bot answers from …" switch, off by default.** When it's on, **every imported
  GET endpoint** can answer questions; there is nothing to set up per endpoint. The workspace's
  existing "Bot lookups" switch stays the master switch.
- **Per endpoint** (endpoint table): turn it off for the bot, or override its formatting. A
  formatting override sets the fields, their order, labels, units, the decimal shift and the decimals
  shown. Stored in `api_integrations.endpoint_settings`.
- **Network:** each integration has a Cardano network: mainnet, testnet (preprod/preview) or any.
  It is inferred once at import, when the document names only one kind of network (for example
  `` `addr1…` `` vs `` `addr_test1…` ``), and the admin can change it.
- **Unchanged:**
  - Workspace isolation: providers are only the workspace's own integrations.
  - Outbound protections: every call goes through spec 015's guard (https, public DNS answers
    only, pinned address, no redirects, same origin and base path, key only to the API).
  - Rate limit: 30 calls per workspace per minute.
- **Imported vs usable.** An imported GET endpoint is usable by the bot only when every required
  value comes from one recognised input, or none is needed (`bot_usability`, the same rule the
  router applies). The bot panel shows "N GET endpoints imported; the bot can use M". It lists the
  rest grouped by the value they need: stake pool id, DRep id, committee credential, native asset or
  policy id, or a number such as an epoch, slot, metadata label or proposal index. In the endpoint
  table, usable rows have the On/Off switch. The others show "Imported only" and the value they
  need, and the filters "Bot can use" and "Imported, bot can't use" separate them. For GridScan's
  document: 89 GET, 60 usable, 29 not.
- **Saved transaction lookups still answer transaction hashes and bare hashes first,** as before.
  A hash the question explicitly calls a block goes to the router, even when a saved transaction
  lookup is enabled. Examples: "block hash …", "which transactions are in block …".

## 2. Inputs and routing (`backend/api_routing.py`, deterministic)

No language model chooses the endpoint or the values.

- **Inputs:**
  - **Payment addresses** (`addr1…`, `addr_test1…`) and **stake addresses** (`stake1…`,
    `stake_test1…`) are parsed per CIP-19: bech32 checksum (BIP-173 without the 90-character
    limit, per CIP-5), header type, the header's network id must match the prefix, and payload
    length. Byron addresses aren't recognised.
  - **64-hex hashes** are a transaction hash or a block hash depending on the wording
    (transaction, tx, utxo, metadata, fee… vs block). A cue in the three words before the hash
    wins: "transactions in block …" is a block, and "what block is transaction … in?" is a
    transaction. Otherwise a transaction cue anywhere wins over a block cue. With neither, the bot
    asks: "Is … a transaction hash or a block hash?"
- **What an endpoint needs:** each required parameter's accepted input is inferred from its name
  and documented description. Examples: "Payment address in bech32 form (`addr1…`)", "Stake
  address", "Transaction hash", "Block hash … or block number".
  - Endpoints whose required values can't come from a message (pool ids, asset ids, numbers) aren't
    used by the router.
  - Documented `pattern`, `enum`, `minLength` and `maxLength` are checked before calling.
  - Lists ask for `order=desc` and `count=10` when the endpoint documents them.
- **Choosing the operation:** endpoints are scored against the question's concepts (balance,
  rewards, fee, status, latest, delegation, utxo…). A concept scores by where it appears in the
  endpoint's documentation:

  | Where the concept appears | Score |
  |---|---|
  | Path | 4 |
  | Summary | 3 |
  | First sentence of the description | 2 |
  | Rest of the description, or tags | 1 |

  An extra path segment the question doesn't mention costs 1.2, and a path that ends with the
  parameter (the plain resource) gets 0.5. So "balance of addr1…" goes to the address summary,
  "utxos of addr1…" to `/utxos`, and "rewards stake1…" to `/rewards`.
- **No input** (network status, current epoch, latest block, price, stats): the bot calls only
  when the question asks for live data (latest/current, status, price, stats, supply), the match is
  clear, and it isn't a how-to or definition question. Otherwise the knowledge base answers.
- **One short question,** sent as the bot's reply without calling the model:
  - an ambiguous hash;
  - two endpoints of the same provider within 1 point (when an address or hash is given);
  - a missing value for a personal request ("What are my staking rewards?" → "Which stake address
    (stake1…) should I look up?"); how-to and why questions are excluded;
  - providers for different networks answering the same question when the input doesn't say which
    ("Do you mean Cardano mainnet (GridScan) or testnet (Preprod mock)?"). Naming the network in the
    question picks it.
- **Rejected without a call:**
  - an invalid address, with the reason (for example, checksum);
  - a testnet address when only mainnet providers are on (and the reverse): "That's a testnet stake
    address, but GridScan serves Cardano mainnet data. Please send a mainnet address (stake1…)."

## 3. Output

One formatter (`format_result`) serves the tester and all bot channels.

- **Single result:** a short labelled list of the documented fields, in documented order, plus one
  level of nested objects. The value the user asked about is left out, long ids are shortened, and
  the list stops at 12 lines ("…and N more fields").
- **Repeated results:** up to 4 columns and 10 rows, with "Showing 10 of N." when there are more.
  - **Web chat** (plain text, as the widget shows it): a bullet list.
  - **Discord:** Markdown with an aligned code-block table.
  - **External API:** Markdown with a pipe table.
- **Units come from the documentation:**
  - "Lovelace" or "Lovelaces" → ADA (÷10⁶);
  - "UNIX time" → a UTC date;
  - `[{unit, quantity}]` amount lists → "404.551473 ADA + 1 other token".
- **Overrides** replace the automatic fields.
- **The provider name and endpoint summary head every result.**
- **The four outcomes are worded differently:**

  | Outcome | When | Shown as |
  |---|---|---|
  | Genuine zero | A real value of 0 | "0 ADA" |
  | Genuine empty | The API answered with an empty list | "No account rewards yet: GridScan answered with an empty list…" |
  | No match | HTTP 404 where the documentation says 404 means an unknown id, or an empty object | "No match: … has no record of this …" |
  | API unavailable | Anything else | "Lookup unavailable: … This says nothing about whether it exists." |

- **The bot's prompt** gets the provider, endpoint, input, status and the formatted RESULT, which it
  must include exactly as written. It never gets the raw response, URL or key.
- **The tester** ("Test a question") runs the same code and shows:
  - the detected input (type, network, address type) and the selected endpoint with its score,
    values and next-best alternatives;
  - the request URL without the key and the formatted preview for the chosen channel;
  - the raw JSON and response quality (spec 017);
  - or the question or reply the bot would send.

## Discord

The reply logic moved from `on_message` into `_discord_reply` so it can be tested. Its behaviour is
unchanged: passive messages (not mentioned, not a DM) never trigger lookups and may still be
skipped. The only addition is that direct lookup replies are sent as they are.

## Testing

- **Unit tests:** `tests/test_api_routing_unit.py`, with fixtures `gridscan-openapi-subset.json`
  (a subset of GridScan's public document) and `blockfrost-style-mock.json` (a preprod mock written
  for these tests).
- **End to end:** isolated database copies, with mock hosts serving GridScan's real document and
  documented examples, plus the preprod mock.
