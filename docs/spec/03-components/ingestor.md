# Component Spec: Ingestor

**Purpose:** Convert external data sources into Bronze envelopes ([02-data-contracts.md §1](../02-data-contracts.md)), preserving raw content byte-faithfully. The Ingestor is the only component that touches the outside world (SAS §3.1).

## CLI

```
tuner ingest --run-id <RUN_ID> [--config configs/pipeline.yaml]
```

Env: `TUNER_S3_*` ([01-architecture.md §4.3](../01-architecture.md)). Exit codes per [01 §4.4](../01-architecture.md); exit 3 if all configured sources yield zero records.

## Input / Output

- **Input:** sources listed under config `ingest.sources`. MVP source types: `csv`, `jsonl` (local file paths or `s3://` URIs readable via `StorageClient`).
- **Output:** `tuner-bronze/{run_id}/records-*.jsonl` + `manifest.json` (tier manifest with `input: null`).

## The `Source` interface

```python
class Source(ABC):
    def __init__(self, cfg: SourceConfig): ...
    @abstractmethod
    def records(self) -> Iterator[tuple[str, dict]]:
        """Yield (locator, raw_record) pairs, e.g. ("row:42", {...})."""
```

Registered by `type` string: `csv` → `CsvSource`, `jsonl` → `JsonlSource` (MVP); `sql`, `pdf`, `api` reserved (§Future). Unknown type ⇒ exit 2.

- **`CsvSource`**: reads with `csv.DictReader`; yields each row as the raw dict; locator `row:{n}` (1-based, excluding header). The `mapping` config (`prompt_column`/`response_column`/`system_column`) is validated here (missing column ⇒ exit 2) but **applied by the Cleaner** — Bronze keeps whole rows.
- **`JsonlSource`**: yields each parsed line; locator `line:{n}`. Lines may already be in Multimodal Contract shape or arbitrary objects; no interpretation at this stage. Malformed JSON line ⇒ exit 2 (Bronze must be complete or absent, never partial-silent).

## Core logic

1. Load and validate config; instantiate each configured `Source`.
2. Delete `tuner-bronze/{run_id}/` if present (idempotency, [01 §5.3](../01-architecture.md)).
3. For each source, for each `(locator, raw)`: build a Bronze envelope — new UUIDv4 `id`, `source` block, `content_hash` over canonical `raw`.
4. Stream envelopes to `records-{NNNNN}.jsonl` shards of ≤ 50 000 records.
5. Write the tier manifest last (commit marker). `counts.read` = records yielded; `dropped` = 0 always in MVP (the Ingestor preserves, never filters).

## Error handling

- Unreadable source URI ⇒ exit 2 before any output is written.
- A failure mid-run leaves no manifest, so downstream stages refuse the partial output.

## Acceptance criteria

- Ingesting `fixtures/support_dialogs.csv` (100 rows) yields exactly 100 Bronze envelopes that all validate against the Bronze schema, plus a manifest with `counts: {read: 100, written: 100, dropped: 0}`.
- Every `raw` object round-trips byte-identically to its source row/line.
- Re-running with the same run ID produces an identical record count and no duplicate objects.
- Two sources in one config produce one merged Bronze tier with distinct `source.uri` values.

## MVP scope

`CsvSource` and `JsonlSource` only, from local paths or `s3://` URIs — `s3://` is deferred at T06 to whichever task first needs it (see the footnote on this scope in [08 ingestor.md](../08-test-specs/ingestor.md)). Sharding logic included (fixtures fit in one shard).

## Future phases

- **`SqlSource`** (Phase 2+): connection string via env `TUNER_SQL_DSN`, query in config; locator `offset:{n}`.
- **`PdfSource`** (Phase 2+): per-page text extraction; locator `page:{n}`; extraction library choice deferred to its own build task.
- **`ApiSource`** (Phase 3): paginated HTTP pulls with cursor persistence.
- **Multimodal assets** (Phase 4): binary files are copied to `tuner-assets/{run_id}/media/{asset_id}.{ext}`; the Bronze `raw` stores the asset URI, never inline bytes.
