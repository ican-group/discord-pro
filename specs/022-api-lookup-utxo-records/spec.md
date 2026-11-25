# 022 — Explicit UTxO requests, exact UTxO records, Discord card and utxos.json

Builds on specs 018–021. Production on 2026-11-25 showed three problems.

1. **A UTxO request called Address Tx Count.** The request was:

   > Run a fresh UTxO lookup for addr1qxqwa25…q7zyg2m. Reply with one JSON code block containing
   > txHash, outputIndex, lovelace, and assets for each output. Include each asset's policyId,
   > assetNameHex, and quantity when available. Use assets: null if you only have a token count.
   > If no fresh lookup runs, say so.

   It called **Address Tx Count**, and the bot reused an earlier UTxO summary. The router scored
   the whole message, so the formatting instructions counted as lookup intent: "token **count**",
   "**tx**Hash", "code **block**". At 16:52, "…UTxOs for **P**addr1…" wasn't recognised as an
   address at all; "current" plus "code block" then made it call Latest Block.
2. **Data was lost before the model saw it.** GridScan's Address UTxOs response has the full
   address, the creating `tx_hash`, `output_index`, and `amount` as `[{unit, quantity}]`: exact
   lovelace, and each asset's policy id plus asset name in hex. Our parsed result dropped them:
   - the table kept 4 columns, so `tx_hash` was lost;
   - long values were shortened;
   - amounts were summarised ("1.17663 ADA + 1 other token").

   So the bot answered `txHash: null`. The request also asked for `count=10`, which cut larger
   UTxO sets.
3. **Earlier data wasn't marked as earlier.** Data from an earlier lookup was shown as "still
   valid", with no time.

## Changes

- **Intent vs instructions (`intent_text`).** The message is split into sentences, also before
  "Reply/Respond/Answer/Format/Return with/in/as…" and at double spaces. Sentences that only say
  how to reply are left out of intent scoring and hash-kind cues: reply/respond/use/include/if
  no…, or ones that mention JSON, code block or Markdown. A sentence carrying the address, hash or
  id is always kept. Inputs are still read from the whole message, and the "this address"
  follow-up uses the intent text too.
- **Glued addresses.** "Paddr1…" is rejected with "it has extra characters 'P' before 'addr1…'",
  never routed elsewhere.
- **UTxO sets keep the API's page size and order** (GridScan: 100, ascending), instead of
  `count=10`.
- **One structured result (`structured_result`).** An address's UTxO list becomes
  `{kind: utxos, count, utxos: [...]}`, and a transaction's inputs and outputs become
  `{kind: tx_utxos, ...}`. Each record keeps:
  - `txHash` (creating transaction), `outputIndex` and the full `address`;
  - `lovelace` as the exact integer string;
  - `assets: [{policyId, assetNameHex, assetName (UTF-8 if printable), quantity}]`;
  - `block`, `txIndex`, `dataHash`, `inlineDatum`, `referenceScriptHash` and `stakeAddress` when
    the API gives them.

  From that one result:
  - **the bot's prompt** gets RECORDS (exact JSON, for any JSON the user asks for);
  - **on Discord**, the reply carries a card (an embed: full `txHash#index`, ADA and exact
    lovelace, each asset's quantity, name and policy id; up to 10 UTxOs) and a **utxos.json**
    attachment with every UTxO.
- **Fresh vs earlier.** The status line says "fresh lookup at <time>". Earlier lookup data is
  labelled with its time as "real but not refreshed by this message". The bot must:
  - say when a fresh lookup failed or didn't run;
  - label any earlier values as earlier data, with their time;
  - never present earlier data as fresh.
- **Unchanged:**
  - "how many transactions" questions still go to Address Tx Count;
  - other formatting and outcome wording;
  - passive Discord behaviour.

## Where each field comes from (GridScan Address UTxOs)

| Field | Raw response | Parsed (before) | Structured result (now) |
|---|---|---|---|
| Address | `address` (full) | shortened | full |
| Creating transaction | `tx_hash` | dropped (table column limit) | `txHash` |
| Output index | `output_index` | kept | `outputIndex` |
| Lovelace | `amount[unit=lovelace].quantity` (string) | "1.17663 ADA" | exact string |
| Native assets | `amount[].unit` = policy id (56 hex) + asset name hex, `quantity` | "+ 1 other token" | `policyId`, `assetNameHex`, `assetName`, `quantity` |

No detail endpoint is needed: the list response already includes every field.
