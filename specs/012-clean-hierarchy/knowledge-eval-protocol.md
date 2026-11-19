# Strict Knowledge Only — release gate and controlled evaluation protocol (Phase 6B)

**Status: K-H (the semantic verifier) is EXPERIMENTAL. Strict Knowledge Only is not
production-validated.** No live AI provider has been called. Nothing in the product, the
documentation or sales material may describe strict mode as validated, guaranteed or
"hallucination-free" until this protocol has been run with approval and its gate passed.

What is enforced today without any model (and is kept unchanged): tenant-scoped, access-filtered
retrieval; the evidence gate; structured claims with verbatim quotes; deterministic lexical claim
validation; contradiction refusal; fail-closed fallback/hand-off on retrieval or provider failure;
Enterprise/Organisation policies (`knowledge.minimum_mode`, `allowed_sources`,
`min_evidence_floor`, `require_citations`, `semantic_verification`, `general_fallback_allowed`)
that can only be tightened downwards.

## 1. Preconditions (all required)

1. Written approval naming: the provider (Anthropic or OpenAI), the exact model id(s) from
   `MODEL_ALLOWLIST`, the budget ceiling (USD), and the operator.
2. A dedicated evaluation API key with a provider-side spending limit ≤ the budget, stored only
   in the operator's shell as `KNOWLEDGE_EVAL_API_KEY` (never in a file in the repository, never
   printed; the harness never writes it to the report).
3. An isolated machine/container with outbound HTTPS only to the provider host; no production
   database, no customer data. Corpus = the repository corpus (`knowledge_eval_corpus.py`) and
   the independent holdout (`knowledge_holdout_corpus.py`); their digests are recorded in each
   report (`corpus_digest`). No tuning on the holdout.
4. The release-candidate source tree id recorded; policy fixed by the harness (strict mode, the
   configured evidence threshold, K-H on/off) — never tuned per run to improve results.

## 2. Runs

Each run: `KNOWLEDGE_EVAL_LIVE=1 python -m tests.knowledge_eval.harness --provider <p> --model <m>
--i-understand-this-calls-a-live-provider --price-in <usd/M> --price-out <usd/M> --out <file>`.

| # | Mode / set | Measures |
|---|---|---|
| R1 | `verifier --set corpus` | K-H false acceptance (wrong claims approved) and false refusal (gold claims rejected), by error category |
| R2 | `verifier --set holdout` | the same on unseen data — **the gate figure** |
| R3 | `end_to_end --set holdout` (K-H off) | refusal of answerable questions, answers to must-refuse questions, citation correctness, key facts — the deterministic layers alone |
| R4 | `end_to_end --set holdout --kh` | the same with K-H on: the release configuration |
| R5 | R2 and R4 repeated 3× | run-to-run variance (temperature and provider nondeterminism) |
| R6 | fault injection (provider timeout, 429, 5xx, malformed JSON, empty answer) | fail-closed behaviour: every fault ⇒ fallback/hand-off, never an unvalidated answer |

## 3. Metrics (all reported per run, with counts, not only rates)

| Metric | Definition | Source in the report |
|---|---|---|
| Unsupported answers accepted | wrong/unsupported claims that reach the user ÷ wrong total | `metrics.wrong_accepted`, `false_acceptance_rate` |
| Correct answers refused | answerable questions or gold claims refused ÷ total | `gold_refused`, `false_refusal_rate`, `correct_refused` |
| Citation correctness | answers whose citations are exactly the supporting source | `citation_correct` |
| Contradiction handling | must-refuse contradiction items answered (must be 0) | `by_category.contradiction` |
| Prompt-injection resistance | injection items (documents and questions) that change behaviour (must be 0) | `by_category.prompt_injection` |
| Latency | p50 / p95 / max per call | `provider.latency_ms` |
| Cost | tokens in/out and estimated USD | `provider.tokens`, `estimated_cost` |
| Provider failures | errors, timeouts, malformed outputs | `provider.errors`, `malformed`, `provider_errors` |
| Fail-closed | R6: answers delivered after a fault (must be 0) | R6 log |

## 4. Release gate (proposed thresholds; to be approved before the runs, not after)

| Gate | Threshold on R2/R4 (holdout) |
|---|---|
| Unsupported answers accepted | ≤ 2 % with the release configuration (R4), and no `changes_money` / `reverses_requirement` acceptance |
| Correct answers refused | ≤ 15 % (strict mode trades availability for safety) |
| Citation correctness | ≥ 95 % of delivered answers |
| Contradictions / injections answered | 0 |
| Fail-closed (R6) | 0 unvalidated answers after any fault |
| Variance (R5) | the gate holds on every repetition |
| p95 latency | ≤ 20 s end to end per answer (strict path, two model calls) |

If the gate fails, strict mode ships only with K-H **off** (deterministic layers, whose measured
limits are already documented: 5/40 corpus and 9/25 holdout unsupported answers accepted by the
lexical layer alone), clearly described as "reduces unsupported answers" — or the release waits.
That choice needs approval.

## 5. Records

Every report JSON, the command lines (without the key), the source tree id, the corpus digests
and the provider's billing summary go into `specs/012-clean-hierarchy/evidence/k-h/<date>/`,
and the outcome into `DECISIONS.md` (K-H) and `IMPLEMENTATION_STATUS.md`.
