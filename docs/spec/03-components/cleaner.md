# Component Spec: Cleaner

**Purpose:** Deterministic scrubbing and normalization: convert Bronze envelopes into Silver Multimodal Contract records ([02-data-contracts.md §2](../02-data-contracts.md)) with `evaluation: null`. No LLM calls, no randomness — same input always yields the same output (SAS §3.1).

## CLI

```
tuner clean --run-id <RUN_ID> [--config configs/pipeline.yaml]
```

Env: `TUNER_S3_*`. Exit 3 if zero records survive.

## Input / Output

- **Input:** `tuner-bronze/{run_id}/` (manifest first; absent ⇒ exit 2).
- **Output:** `tuner-silver/{run_id}/records-*.jsonl` + tier manifest.

## Config (`clean.*` + `ingest.sources[].mapping`)

`min_chars`, `max_chars` (total text characters across all turns), `pii` (list of enabled scrubbers: `email`, `phone`).

## Core logic

1. Read Bronze manifest, then stream envelopes; validate each against the Bronze schema.
2. Delete `tuner-silver/{run_id}/` (idempotency).
3. **Structure mapping** — build the `conversation` array:
   - `source.type == "csv"`: use the source's `mapping` config → optional system turn from `system_column`, user turn from `prompt_column`, assistant turn from `response_column`. Missing/empty prompt or response ⇒ drop `unmappable`.
   - `source.type == "jsonl"`: if `raw` already has a contract-shaped `conversation`, adopt it; else if it has exactly the keys of a known flat shape (`prompt`/`response` or `question`/`answer`, optional `system`), map as CSV; else drop `unmappable`.
   - Every text value becomes a single `{"type": "text", "value": ...}` content part (array-wrapped per contract).
4. **Scrubbing** (each text value, in order): Unicode NFC normalization; strip control chars except `\n`/`\t`; collapse >2 consecutive blank lines; trim. PII scrubbers replace matches with placeholder tokens: emails → `[EMAIL]`, phone numbers → `[PHONE]` (regexes fixed in `src/tuner/cleaner/patterns.py` with unit-tested cases).
5. **Filters** (drop with reason): `too_short` (< `min_chars`), `too_long` (> `max_chars`), `empty_turn` (any turn empty after scrub), `bad_structure` (violates turn rules of contract §2 — e.g. no assistant turn).
6. **Exact dedup:** drop `duplicate` when the sha256 of the scrubbed conversation (canonical JSON) was already emitted this run; first occurrence wins.
7. Write Silver records (Bronze `id` preserved, `lineage` block filled), then the tier manifest with per-reason drop counts.

Fixed drop reasons: `unmappable`, `too_short`, `too_long`, `empty_turn`, `bad_structure`, `duplicate`.

## Error handling

Invalid Bronze record ⇒ exit 2 (upstream bug, not a drop). Drops are only for records that are structurally readable but fail cleaning policy.

## Acceptance criteria

- The fixture dataset ([06-testing.md §3](../06-testing.md)) produces the expected per-reason drop counts (fixtures include planted dirty rows: dupes, empties, over-long, PII).
- Output validates against the Silver schema; every record's `evaluation` is `null`; `id`s are a subset of Bronze `id`s.
- Running twice yields byte-identical `records-*.jsonl` (determinism).
- PII fixtures: no email/phone patterns remain in any output text.

## MVP scope

All of the above. Near-dedup (MinHash/embedding), language filtering, and toxicity filters are explicitly out — quality judgment belongs to the Judge.

## Future phases

**Phase 4 (multimodal):** validate `image`/`audio` parts — asset URI exists in `tuner-assets` (HEAD via `StorageClient`), extension allowed, size cap; drop reason `bad_asset`. Text scrubbing unchanged.
