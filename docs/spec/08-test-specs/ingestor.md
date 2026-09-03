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
| ING-U-007 | CSV header line isn't valid UTF-8 | `SourceConfigError` at construction — exit 2, same bucket as an unreadable source URI |
| ING-U-008 | Non-UTF-8 byte deep in a CSV data row (header decodes fine) | `MalformedLine` from `records()`; row number is best-effort, not asserted³ |
| ING-U-009 | Non-UTF-8 byte in a JSONL data line (first line decodes fine) | `MalformedLine` from `records()` with the *exact* line number³ |
| ING-U-010 | CSV row with more fields than the header (`DictReader`'s `None` restkey) | `MalformedLine` naming the row, instead of a downstream crash sorting a mixed `None`/`str`-keyed dict |

¹ **Scope decision (T06):** [ingestor.md](../03-components/ingestor.md)'s Input line specs `csv`/`jsonl` sources from "local file paths or `s3://` URIs readable via `StorageClient`". No case in this suite exercises an `s3://` source URI. `StorageClient.read_json` does read an arbitrary object by key, but it JSON-decodes the whole body as one object — the primitive actually missing is a **raw-bytes/text** read that hands back an object's content unparsed, so a `CsvSource`/`JsonlSource` could stream and line-split it the same way it does a local file. T06 implements local file paths only; `s3://` source URIs are deferred to whichever later task first needs them, at which point `StorageClient` gains that primitive and this ID list gains its own case rather than retrofitting one here.

² **Scope decision (T06):** `IngestSourceConfig.type` ([core/config.py](../../../src/tuner/core/config.py)) is `str`, not the `Literal["csv", "jsonl"]` T01 originally gave it — ING-U-005 needs `sources.py`'s own registry to be the thing that rejects an unknown type (per [ingestor.md](../03-components/ingestor.md): "Registered by type string ... `sql`, `pdf`, `api` reserved (Future). Unknown type ⇒ exit 2"), not pydantic short-circuiting first. A `Literal` would also reject a config naming a reserved-but-unimplemented type outright instead of failing with a message naming what's actually supported today. No existing test relied on the `Literal`.

³ **Design note (T06 review round 2):** `JsonlSource` decodes one raw physical line at a time (binary mode, decode-per-line), so a decode error's line number is exact. `CsvSource` reads through a text-mode file object that `csv.reader` sits on top of, which decodes in internally-buffered chunks that can span multiple rows — a bad byte can surface on whichever `next()` call was in progress when the buffer needed refilling, not necessarily the row it's physically on. `CsvSource` can't switch to JsonlSource's approach: a quoted CSV field may legitimately contain an embedded newline, so "one physical line" isn't "one row" the way it is for JSONL. ING-U-008 accordingly does not assert an exact row number; ING-U-009 does. Two related `csv.DictReader` quirks are deliberately left as its standard semantics, not treated as errors: a short row (fewer fields than the header) fills missing columns with `None` rather than raising, and duplicate header column names silently keep only the last one's value. Neither is a case in this suite.

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
| ING-I-019 | A JSONL line that fails to parse, or one that parses to something that isn't an object (e.g. `[1, 2, 3]`) | Both exit 2 through the full `ingest()` pipeline, no manifest written⁴ |

⁴ **Note (T06 review round 2):** an empty line is not valid JSON, so a blank line in a JSONL source is deliberately treated the same as any other line that fails to parse — `MalformedLine` → exit 2, not silently skipped. Not its own case; none of the committed `fixtures/*.jsonl` files contain a blank line, so this doesn't affect any other suite's fixtures.
