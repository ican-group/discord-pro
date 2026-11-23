# 008 — Chunked knowledge base with hybrid (BM25 + vector) search

Supersedes the passage selection in spec 007: once entries are stored as parts, each part
is short enough to send whole.

## Problems

1. URL and file imports keep only the first 6,000 characters; the rest of a long page is
   silently dropped.
2. Search ranks whole entries, so a long entry competes as one document and the model only
   sees a slice of it.
3. Search is keyword-only (BM25). A question that uses different words from the article
   ("wallet won't sync" vs "node is behind the chain tip") finds nothing.

## Goals

- Store long content in full, split into overlapping parts, each searchable on its own.
- Find parts by keywords **and** by meaning, using ParadeDB (pg_search BM25) and pgvector in
  the same database.
- Keep embeddings local: knowledge base text and user questions never leave the server.

## Design

### Storage

- `knowledge_sources_index` stays the source of truth: one row per entry with the full text,
  title, priority, active flag. The Knowledge Base page still lists entries, not parts.
- New table `knowledge_chunks`, one row per part:
  `id` (`<source_id>:<part>`), `source_id` (FK → `knowledge_sources_index.id`,
  `ON DELETE CASCADE`), `instance_id`, `part`, `parts`, `title`, `content`,
  `embedding vector(384)`, `embed_model`.
- Indexes: BM25 (`id`, `title`, `content`), HNSW on `embedding` (cosine), btree on
  `(instance_id)` and `(source_id)`.
- Parts follow the entry: deleting an entry or instance cascades; active/priority are read
  from the entry at search time (no re-indexing); a title or content change re-splits the
  entry.

### Splitting (same approach as bitbotpro `chunk_text`)

- Windows of about 900 characters, cut at the last paragraph break, sentence end or line
  break in the window when it is past the halfway point; 150-character overlap.
- Entries of 900 characters or fewer are one part.
- Limits: 1,500,000 characters per entry, 2,000 parts.

### Ingestion limits

- URL import and file upload keep up to 1,500,000 characters (was 6,000). The response
  includes `truncated: true` and the original length when text was cut, and the Knowledge
  Base page shows a notice.

### Embeddings

- `fastembed` with `BAAI/bge-small-en-v1.5` (384 dimensions, ONNX on CPU, ~130 MB, cached in
  `backend/.cache/fastembed`, downloaded on first use). Parts are embedded as passages with
  their title; questions use the model's query form.
- Embedding runs in a worker thread so requests aren't blocked. If the model can't load,
  search falls back to BM25 only and logs a warning.
- On startup, a background task splits and embeds any entry that has no parts yet, or parts
  from a different embedding model.

### Search (`search_knowledge`)

1. BM25: top 40 parts matching the question (instance's active entries only).
2. Vector: top 40 parts by cosine similarity, keeping only those at or above a minimum
   similarity, so an unrelated question still finds nothing and the knowledge gate
   ("Would you like me to use general knowledge?") still applies.
3. Combine with reciprocal rank fusion (k = 60), plus the existing small priority boost.
4. Send the top parts, up to 12 parts and 30,000 characters, labelled
   `[Title — part 3 of 18]` (no label for single-part entries).
5. Return `""` when both lists are empty. The no-search-terms path is unchanged.

### Knowledge Base page

- Entries split into more than one part show a "N parts" badge.

## Out of scope

- Website crawling (multi-page import).
- Re-ranking with a cross-encoder.
- Changing how the empty-knowledge-base case is reported (handled on bot-config-guardrails).

## Verification

- Unit: splitting sizes, boundaries, overlap, limits; labels.
- ParadeDB, throwaway instance: a long article is stored in full as N parts; an exact
  error-code question finds the right part via BM25; a paraphrased question with no shared
  keywords finds it via vector search; an unrelated question returns nothing; disabling the
  entry hides its parts; deleting it removes them.
- URL import of a long public page through the API, on a throwaway instance, deleted after.
