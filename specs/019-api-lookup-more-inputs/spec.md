# 019 — API lookup inputs: pools, DReps, committee, numbers, proposals, and follow-up answers

Builds on spec 018 (API lookup routing). Spec 018 left 29 of GridScan's 89 GET endpoints unusable
because their required values can't be recognised in a message. This spec adds those inputs, and
lets the bot remember its own question so a short answer ("512", "pool1…", "block") completes the
lookup. Routing stays deterministic. Saved lookups, workspace isolation, the outbound guard and the
rate limit are unchanged.

## 1. New inputs (`detect_inputs`)

| Kind | Recognised from | Validation |
|---|---|---|
| `pool_id` | `pool1…` | bech32 checksum, 28-byte payload |
| `drep_id` | `drep1…` (CIP-129, or legacy CIP-105), `drep_script1…`, `drep_vkh1…` | bech32 checksum. 29 bytes with a CIP-129 DRep header (`0x22` key, `0x23` script), or 28 bytes |
| `committee_credential` | `cc_cold1…`, `cc_hot1…`, and the CIP-105 `_vkh` / `_script` forms | bech32 checksum. 29 bytes with a CIP-129 committee header (`0x02`/`0x03` hot, `0x12`/`0x13` cold), or 28 bytes |
| `gov_action` | `gov_action1…` (CIP-129) | bech32 checksum, 33 bytes. Split into the transaction hash (32 bytes) and the proposal index (1 byte) |
| `tx_hash` + `proposal_index` | `<64-hex>#<n>` (the usual `tx#index` form) | the hash as before, and the index 0–255 |
| `epoch_number` | `epoch 512`, `epoch #512`, `epoch no. 512` | integer |
| `slot_number` | `slot 123456` | integer |
| `block_number` | `block 1234567` | integer. Fills only parameters documented as accepting a block number |
| `metadata_label` | `label 674`, `metadata label 674` | integer |
| `proposal_index` | `proposal index 0`, `index 0` | integer 0–255 |

- **Numbers need a cue word directly before them.** A bare number is only accepted as the answer
  to the bot's own question (section 3). So "top 10 pools" and "2 ADA" are never inputs.
- **Pool, DRep, committee and governance-action ids carry no network.** Any provider can use them.
- **Invalid ids are rejected with the reason,** as for addresses. An example: "That DRep id doesn't
  look valid (checksum does not match)."
- **A hash that a proposal or governance action refers to** (the cue word right before it is
  "proposal" or "action", or it comes from `gov_action1…` / `tx#index`) never goes to a saved
  transaction lookup.

## 2. Parameters and endpoints

- **Recognising the new parameters (`param_kinds`).** Each new kind is matched by the parameter's
  name and description:
  - pool: name contains `pool`, or the description mentions `pool1`;
  - DRep: name or description mentions `drep`;
  - committee credential: the description mentions `cc_cold` / `cc_hot`, or "committee";
  - epoch number: name `epoch_number`, or `number` under `/epochs/`;
  - slot number: name `slot_number`, or `slot`;
  - metadata label: name `label` under `/metadata/`;
  - proposal index: name `cert_index` or `index`, under `/proposals/`;
  - block number: a block parameter whose description says "or block number".
- **Endpoints with up to three required values.** Each value is filled by a different input. Two
  examples:
  - `/governance/proposals/{tx_hash}/{cert_index}/votes` takes the transaction hash and the
    proposal index;
  - `/blocks/epoch/{epoch_number}/slot/{slot_number}` takes the epoch and the slot.
- **`bot_usability`** follows the same rule. For GridScan, 83 of 89 GET endpoints become usable.
- **Still out of scope: native assets and policy ids** (6 endpoints). Users know a token by its
  name, not by the 56-hex policy id plus hex asset name. A 56-hex value also can't be told apart
  from a pool or DRep key hash, and a 4-byte asset name makes a 64-hex unit that looks like a
  transaction hash. This needs a name-to-unit search step, which is a separate change.
- **The 56-hex form of pool, DRep and committee ids** isn't recognised, for the same reason. The
  bech32 forms cover every endpoint.

## 3. Asking for a missing value, and remembering the question

- **Missing values for any recognised kind.** When the question clearly matches an endpoint and
  exactly one of its required values is missing, the bot asks for that value. "Clearly" means:
  - the endpoint scores at least `MISSING_VALUE_MIN`;
  - and it scores better than any endpoint that can be called with what was given.

  The bot asks a question like these:
  - "Which proposal index in that transaction? It's usually 0."
  - "Which stake pool (pool1…)?"
  - "Which epoch number?"

  The spec-018 rules still apply: personal questions with no input ("my rewards"), and never for
  how-to or why questions.
- **Pending question.** Every `ask` the bot sends is remembered for 10 minutes. This covers:
  - a missing value;
  - transaction or block;
  - mainnet or testnet;
  - which of two endpoints.

  It is stored in `api_pending` with a TTL index, keyed by workspace and conversation:
  - **web chat and the external API:** the `session_id`;
  - **Discord:** the user and the channel.
- **The next message** in that conversation takes the pending question (it is removed either
  way).
  - **When the reply counts as an answer.** The reply is combined with the remembered question
    (the original question, then the reply) when both hold:
    - it is short: at most 12 words and 200 characters;
    - it looks like an answer: an input (valid or not), a known concept word, a network name, a
      transaction, block or proposal word, or a bare number when a number was asked for.
  - **Which reading is used.** The combined message is used when it gives a call, a rejection or a
    question, and either:
    - the reply alone wouldn't call anything; or
    - the reply supplies exactly the value asked for. For example, a bare `stake1…` after "What
      are my staking rewards?" gives the rewards, not the account summary.

    Otherwise the reply is a new question and is answered on its own. "latest block" is one
    example; "thanks" is not an answer.
  - **Bare numbers.** A bare number gets the cue word of the kind asked for, so "512" after
    "Which epoch number?" reads as "epoch 512". A bare number is never an answer to a question
    that didn't ask for a number.
  - **Expiry and scope.** Entries expire after 10 minutes; the expiry time is checked on read, so
    the TTL monitor's delay doesn't matter. They are never shared across workspaces, sessions,
    channels or users.
- **Passive Discord messages** never create or use a pending question.

## 4. Unchanged

- Output formatting, provider names, units, and the four outcomes (spec 018).
- **Saved lookups** still answer bare hashes and transaction hashes first.
- **The tester** routes one question without a conversation, so nothing is remembered.
  - It shows the new input kinds.
  - For a missing-value question, it shows the endpoint and the value the bot asks for.
  - The endpoint table shows multi-value endpoints as "Transaction hash + Proposal index".

## Testing

- **Unit tests** (`tests/test_api_routing_unit.py`) cover:
  - the id parsers, including checksum, length and header failures;
  - input detection, including numbers without a cue word;
  - routing to all the newly usable endpoints, and multi-value calls;
  - missing-value questions and follow-up answers.
- **Fixture:** `gridscan-openapi-subset.json` adds 15 governance and metadata paths from GridScan's
  public document.
- **End to end** on isolated database copies with the GridScan mock host:
  - the tester;
  - web chat and the external API follow-ups, including isolation, expiry and new questions;
  - Discord (same user and channel only, passive unchanged);
  - the browser click-through;
  - the spec-018 suites rerun.
