# 007 — Search inside long knowledge base entries

## Problem

`search_knowledge` ranks whole entries with BM25, then sends the model only the first
1,200 characters of each match. In a long entry (a scraped page, a guide, a saved ticket)
the answer is often further down, so the model never sees it even though the right entry
was found.

## Goal

When a matching entry is longer than the per-entry budget, send the parts of it that match
the question instead of its opening.

## Behaviour

- Entry ranking is unchanged: BM25 over whole entries, the priority boost, the 30,000-character
  total context budget.
- Entries of 1,200 characters or fewer are sent whole, as before.
- Longer entries are split into passages of about 400 characters on paragraph and sentence
  breaks (hard cuts only for text with no breaks). Each passage is scored by the question
  words it contains, ignoring common stopwords; words that are rarer within the entry count
  more. The best passages that fit in 1,200 characters are sent in document order, with
  `…` marking gaps.
- If no question word appears in the entry, the opening 1,200 characters are sent, as before.
- The no-search-terms path (priority-ordered dump) is unchanged.

## Out of scope

- Storing chunks in ParadeDB with their own BM25 index. That would also improve which
  entries are ranked first for long entries, but needs a schema change and re-indexing.
  Revisit if long entries are found but ranked too low.
- Stemming and synonyms beyond a plural "s".

## Verification

- Synthetic 11,000-character article with the answer at character 9,659: the old code misses
  it, the new code includes it for three different phrasings, within 1,200 characters.
- Same article through ParadeDB with two decoy entries: same ranking, answer included.
- The longest real entry (10,588 characters), read-only: sentences at characters 4,150, 7,834 and
  10,205 are found by the new code and missed by the old.
