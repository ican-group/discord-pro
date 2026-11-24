# 014 — Knowledge Base import / export with category tabs

## Origin

Adapted from BitBotPro (`origin/bitbotpro-v1.0.0`, removed in `aa55976`; code read at `aa55976^`):
`GET /knowledge/export`, `POST /knowledge/import`, `_split_into_chunks`, the grouped list in
`GET /knowledge/sources` (rows grouped by `group_id`, `(part N/M)` stripped from titles), the
`category` field and the "Uncategorized" grouping in `KnowledgeBase.js`.

BitBotPro stored each long page as several rows of up to 8,000 characters sharing a
`group_id`. BridgeBot stores one entry per article in `knowledge_sources_index` and splits it
into its own search parts (`knowledge_chunks`, spec 008). So BridgeBot imports articles, not
rows, and splits them again on export.

## File format (version 1, unchanged)

```
{"version": 1, "exported_at", "source_instance_id", "count", "sources": [row, ...]}
row: id, type, title, content, url, priority, group_id, total_parts, is_active, created_at,
     category, [tags], [filename]
```
A multi-part article has `total_parts` rows sharing `group_id`, titled `<title> (part i/n)`.

## Import — `POST /api/knowledge/import` (selected workspace)

- Rows are grouped by `group_id` (a row without one is its own article) and ordered by the
  `(part i/n)` number. The title loses the part suffix.
- Text is rebuilt from the parts. BitBotPro cut at a paragraph break (whitespace stripped) or at
  exactly 8,000 characters, so parts are joined with a blank line, or directly after a full
  8,000-character part or when the cut kept its whitespace (BridgeBot exports). For the
  2026-11-15 file every rebuilt article splits back into exactly its original parts.
- Category, type (`faq`, `tech_note`, `url`, `document`; anything else becomes `document`),
  priority (0–2), active flag, URL, tags, filename and `created_at` are kept. A blank category
  shows as Uncategorized.
- Repeat imports don't duplicate: the file's `group_id` is stored as `import_key` (unique per
  workspace). An article is skipped when its `group_id` matches an `import_key` or entry id in
  the workspace, so re-importing the same file, or an export of the workspace, adds nothing.
- Articles whose parts don't add up (missing rows or numbers) are imported from the parts present
  and listed as incomplete in the response.
- The entry and its BM25 parts are written immediately; embeddings are added by a background task
  (the startup backfill completes any that are interrupted). Version other than 1, or an empty
  file: 422.

Response: `rows, articles, imported, skipped_existing, incomplete, categories, embeddings_pending`.

## Export — `GET /api/knowledge/export`

Every entry of the workspace, split into parts of up to 8,000 characters cut after a paragraph
break or other whitespace. Parts are exact slices, so they join back without loss. `group_id` is
the entry's `import_key` (the original group) or its id.

## Knowledge Base page

- Category tabs above the list: All, the named categories A–Z, then Uncategorized, with counts.
  One article per row; on All each row shows its category.
- Category field in the edit dialog (suggests existing categories); blank = Uncategorized.
- "Import into <workspace>" and "Export" buttons. Export downloads `kb-export-YYYY-MM-DD.json`.

## Storage

`knowledge_sources_index` gains `category TEXT NOT NULL DEFAULT ''` and `import_key TEXT` with
a unique index on `(instance_id, import_key)`. Additive: older code ignores them. Writes that
don't set them keep the stored values. The list endpoint returns up to 2,000 entries (was 200).
