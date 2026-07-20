# Test Suite: Core (`CORE`)

Covers `tuner/core/`: config, ids, schemas, manifest helpers, StorageClient. Files: `tests/unit/test_config.py`, `test_ids.py`, `test_schemas.py`, `test_manifest.py`; `tests/integration/test_storage.py`. Coverage target: **100 %** of `tuner/core/*`.

## Setup

Unit cases need nothing. `CORE-I-*` need compose MinIO (`storage`, `run_id` fixtures).

## Config (`test_config.py`)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CORE-U-001 | Load the shipped `configs/pipeline.yaml` | Parses into the config model; every default matches [01 §6](../01-architecture.md) |
| CORE-U-002 | Config with an unknown key (top-level and nested, parametrized) | Validation error naming the key; CLI wrapper exits 2 |
| CORE-U-003 | Missing config file path | Exit 2 with the path in the message |
| CORE-U-004 | Precedence: adapter defaults vs. `train.hyperparameters` override of a single field | Overridden field wins; untouched fields keep adapter defaults |
| CORE-U-005 | `judge.model` empty string | Accepted at load (it's the Judge that rejects, JDG-I-027) — documents the boundary |
| CORE-U-006 | Type errors: `threshold: "high"`, negative `max_concurrency`, `eval_fraction: 1.5` (parametrized) | Each rejected with a field-specific error |

## IDs (`test_ids.py`)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CORE-U-010 | `new_run_id()` format | Matches `^run-\d{8}-\d{6}-[0-9a-f]{6}$`; timestamp part is UTC now (injected clock) |
| CORE-U-011 | 1 000 generated run IDs / record IDs | All unique; record IDs are valid UUIDv4 |
| CORE-U-012 | `python -m tuner.core.ids` | Prints exactly one valid run ID and a trailing newline, exit 0 |

## Schemas (`test_schemas.py`)

Doc-02 examples are stored under `tests/fixtures_schemas/` verbatim; drift between doc and model fails here.

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CORE-U-020 | Each doc-02 example (Bronze, Silver `evaluation: null`, Gold, tier manifest, index_map, registry manifest) — parametrized | Validates unchanged |
| CORE-U-021 | Bronze mutations: missing `id`, bad `source.type`, malformed `content_hash`, non-object `raw` (parametrized) | Each rejected |
| CORE-U-022 | Conversation rules ([02 §2](../02-data-contracts.md)): flat-string `content`, empty content array, `system` not first, two `system` turns, no `user` turn, no `assistant` turn, last turn not `assistant`, empty-after-trim text value, unknown content `type` (parametrized) | Each rejected |
| CORE-U-023 | `evaluation.score` −0.1 and 1.1; missing `judge_model` when `evaluation` non-null | Rejected |
| CORE-U-024 | Gold-context validation: `validate_gold(record)` with `evaluation: null` | Rejected (used by Tokenizer step 2) |
| CORE-U-025 | Tier manifest with `counts.read ≠ written + dropped`; unknown `drops[].reason` for the producing stage | Rejected |
| CORE-U-026 | Timestamps without `Z` / non-UTC offset | Rejected |
| CORE-U-027 | Registry manifest `status` outside `candidate\|promoted\|retired` | Rejected |
| CORE-U-050 | **Property (hypothesis):** valid records generated from the schema models (arbitrary text values, turn counts, drop lists) | `model → dict → JSON → model` round-trips equal; canonical-hash computation is stable across the round-trip |

## Manifest helpers (`test_manifest.py`)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CORE-U-030 | `records_hash` computation over two known shards | Equals sha256 of concatenated bytes in `files` order |
| CORE-I-031 | `read_tier(run_id)` when manifest absent but record shards present | Raises `UpstreamIncomplete` → CLI exit 2 (commit-marker rule, [02 §3](../02-data-contracts.md)) |
| CORE-I-032 | `write_tier(...)` call order | Manifest object is written after all shards (assert via recorded operation order on a spy client) |

## StorageClient (`test_storage.py`, integration)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CORE-I-040 | jsonl round-trip, single shard | Records identical, order preserved |
| CORE-I-041 | jsonl round-trip across shards (shard size injected to 10) | `records-00000/00001/...` naming; iterator yields all records in shard order |
| CORE-I-042 | `read_json` / `write_json` round-trip | Identical dict |
| CORE-I-043 | `upload_dir` / `download_dir` of a nested dir | Byte-identical tree |
| CORE-I-044 | `delete_prefix` of `{run_id}/` | Own prefix gone; a sibling run's objects untouched |
| CORE-I-045 | Client honors `TUNER_S3_ENDPOINT`/creds from env only | Constructing with env unset raises config error; no boto3 default-chain fallback |
| CORE-U-046 | Static: `grep -r "import boto3" src/tuner --include="*.py"` matches only `core/storage.py` | Rule [CLAUDE.md hard rule 1] holds (implemented as a real test) |
