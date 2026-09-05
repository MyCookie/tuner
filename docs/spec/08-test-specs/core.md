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
| CORE-U-007 | `merge_hyperparameters` with an override key that isn't one of the defaults' own fields | `ConfigError` naming the unknown key |

CORE-U-007 was added at T09 (round 1 review on PR #9): `merge_hyperparameters` originally did no such check (`{**base, **overrides}` merges anything in silently); `ADP-U-031` needed this from the adapter side, which surfaced that nothing in `src/` actually rejected an unknown key at all.

**Decision (T15, round 1 review on PR #19):** the shipped `configs/pipeline.yaml` briefly set `judge.model: mock-judge` so the README quick start (`uv run tuner run --config configs/pipeline.yaml`) would not exit 2 at the Judge stage. That was reverted; `CORE-U-001` asserts `judge.model == ""`, matching [01 §6](../01-architecture.md) exactly, for two reasons: (1) it preserves the fail-fast guarantee CORE-U-005/JDG-I-027 exist to test — an operator who forgets to configure a judge endpoint should get exit 2, not a silent run against test infrastructure; (2) `judge.model` is written into every Gold record and MLflow judge-run param as provenance ([02-data-contracts.md](../02-data-contracts.md), [03-components/judge.md](../03-components/judge.md)), and a shipped default of `mock-judge` would stamp that name into a production audit trail for anyone who didn't override it. The real problem the `mock-judge` change was trying to solve — the quick start doesn't actually run out of the box — is a documentation gap, not a config default: [00-getting-started.md §5](../../00-getting-started.md) now states explicitly what a real end-to-end run needs (the compose `e2e` profile bringing up `mock-judge`, `TUNER_JUDGE_BASE_URL=http://localhost:8088`, `judge.model: mock-judge` passed via config override or `configs/pipeline.e2e.yaml`, and a GPU or host-venv fallback for `train`/`smoke`).

## IDs (`test_ids.py`)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CORE-U-010 | `new_run_id()` format | Matches `^run-\d{8}-\d{6}-[0-9a-f]{6}$`; timestamp part is UTC now (injected clock) |
| CORE-U-011 | 1 000 generated run IDs / record IDs | All unique — run IDs across distinct seconds, record IDs valid UUIDv4; same-second run IDs keep a high-entropy suffix¹ |
| CORE-U-012 | `python -m tuner.core.ids` | Prints exactly one valid run ID and a trailing newline, exit 0 |

¹ **Spec decision:** run IDs are asserted unique across *distinct* seconds, not within one. The [01 §4.2](../01-architecture.md) format ends in 6 hex chars — 24 bits — so 1 000 IDs minted inside the same second collide with probability ≈ 2.9 %; asserting zero collisions there asserts something the format does not promise, and made this case fail ~1 run in 34. Uniqueness genuinely comes from timestamp + suffix, and the orchestrator mints one run ID per pipeline run, so distinct seconds is the real-world condition. The same-second case is still covered, by the stronger claim it can actually support: the suffix stays high-entropy (≥ 990 distinct out of 1 000; ≥ 10 collisions has probability ~1e-22, so it fails only on a broken RNG, never on luck). Widening the suffix was rejected — [01 §4.2](../01-architecture.md) is load-bearing across manifests, MLflow tags, and registry entries.

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
| CORE-I-047 | `write_bytes` / `read_bytes` round-trip, including a non-UTF-8 payload | Identical bytes; absent key returns `None` |
| CORE-I-048 | `download_dir` with an empty `prefix` | Downloads every object in the bucket, keys unchanged as relative paths — not zero objects |

CORE-I-047 was added at T10: SafeTensors shards are raw binary, and `StorageClient` had no raw-bytes path — only jsonl and JSON. Added the same paired `write_x`/`read_x` shape the class already uses for `json`/`jsonl`, rather than a write-only method.

CORE-I-048 was added at T13: `download_dir` unconditionally forced a trailing slash onto `prefix`, turning `""` into `"/"` — an S3 prefix that matches no real key, since object keys never start with a literal slash. `tuner registry list` (T13) needs exactly this: enumerate every `{model_version}/manifest.json` in `tuner-registry`, which has no shared parent prefix to filter on. Fixed to treat `""`/`"/"` as "no prefix filter" (matching S3's own `list_objects_v2` semantics), reusing `download_dir` rather than adding a new listing method.
