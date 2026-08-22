# Test Suite: Ingestor (`ING`)

Spec under test: [ingestor.md](../03-components/ingestor.md). Files: `tests/unit/test_sources.py`, `tests/integration/test_ingestor.py`.

## Setup

Unit cases use temp files (local paths only¹). Integration cases use compose MinIO + `fixtures/`.

## Sources (unit)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| ING-U-001 | `CsvSource` over a 3-row temp CSV | Yields 3 `(locator, raw)`; locators `row:1..3`; `raw` keys = header names, values = cell strings |
| ING-U-002 | CSV `mapping` referencing a column absent from the header | Config validation error → exit 2, no records yielded |
| ING-U-003 | `JsonlSource` over 3-line temp file | Locators `line:1..3`; `raw` = parsed objects |
| ING-U-004 | `JsonlSource` hitting a malformed line (`fixtures/bad_lines.jsonl`) | Raises parse error carrying the line number (CLI → exit 2) |
| ING-U-005 | Unknown `type: parquet` in source config | Exit 2 naming known types² |
| ING-U-006 | Byte fidelity: CSV cell with leading/trailing spaces, embedded quotes, unicode | `raw` values byte-identical to source (no trimming at Bronze) |

¹ **Scope decision (T06):** [ingestor.md](../03-components/ingestor.md)'s Input line specs `csv`/`jsonl` sources from "local file paths or `s3://` URIs readable via `StorageClient`". No case in this suite exercises an `s3://` source URI, and `StorageClient` has no primitive for reading an arbitrary single object by key today (`read_json`/`read_jsonl` assume the tier-manifest layout). T06 implements local file paths only; `s3://` source URIs are deferred to whichever later task first needs them, at which point `StorageClient` gains the primitive and this ID list gains its own case rather than retrofitting one here.

² **Scope decision (T06):** `IngestSourceConfig.type` ([core/config.py](../../src/tuner/core/config.py)) is `str`, not the `Literal["csv", "jsonl"]` T01 originally gave it — ING-U-005 needs `sources.py`'s own registry to be the thing that rejects an unknown type (per [ingestor.md](../03-components/ingestor.md): "Registered by type string ... `sql`, `pdf`, `api` reserved (Future). Unknown type ⇒ exit 2"), not pydantic short-circuiting first. A `Literal` would also reject a config naming a reserved-but-unimplemented type outright instead of failing with a message naming what's actually supported today. No existing test relied on the `Literal`.

## Pipeline behavior (integration)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| ING-I-010 | Ingest `fixtures/support_dialogs.csv` | 100 Bronze envelopes, all valid vs. schema; manifest `{read:100, written:100, dropped:0}`, `input: null` |
| ING-I-011 | `content_hash` spot-check: recompute canonical-JSON sha256 for 5 records | Matches stored hash |
| ING-I-012 | Re-run same run ID | Same count, no duplicate objects, fresh manifest (idempotency) |
| ING-I-013 | Two sources (csv + `extra_dialogs.jsonl`) in one config | One merged Bronze tier; per-record `source.uri` distinguishes; counts sum |
| ING-I-014 | Source yielding zero records (empty CSV with header) | Exit 3, no manifest written |
| ING-I-015 | Unreadable source URI | Exit 2 **before** any object is written (bucket prefix empty) |
| ING-I-016 | Induced failure mid-write (monkeypatch shard write to raise on shard 2, shard size 10) | No `manifest.json` in the prefix → CORE-I-031 behavior downstream |
| ING-I-017 | Shard boundary: 25 records, shard size injected to 10 | Shards `00000..00002` with 10/10/5; manifest `files` lists all three in order |
| ING-I-018 | IAM: ingestor credentials attempt a write to `tuner-gold` | Denied by policy (AccessDenied) — [05 §5](../05-infrastructure.md) |
