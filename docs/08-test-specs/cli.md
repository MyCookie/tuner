# Test Suite: CLI, Driver & Registry List (`CLI`)

Specs under test: [01-architecture.md §2, §4.4](../01-architecture.md), [registry.md MVP scope](../03-components/registry.md). Files: `tests/unit/test_cli.py`, `tests/integration/test_driver.py`, `tests/integration/test_registry_list.py`.

## CLI shell (unit)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLI-U-001 | `eftp --help` | Lists exactly: ingest, clean, judge, tokenize, train, smoke, run, registry |
| CLI-U-002 | Stage subcommand without `--run-id` | Usage error, exit 2 |
| CLI-U-003 | `--run-id not-a-run-id` (bad format) | Exit 2 with the format regex in the message |
| CLI-U-004 | Default `--config` | Resolves to `configs/pipeline.yaml` |

## Driver (integration; stage entrypoints monkeypatched to recorders for speed)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLI-I-010 | `eftp run` happy path | Generates one valid run ID; invokes ingest→clean→judge→tokenize→train→smoke in exactly that order, each receiving the same run ID and config path |
| CLI-I-011 | Stage failure (judge recorder exits 1) | Driver stops: tokenize/train/smoke never invoked; driver exits 1 naming the failed stage |
| CLI-I-012 | Stage exit 3 (zero records) | Driver aborts with a distinct "pipeline empty at <stage>" message, exit 3 |
| CLI-I-013 | Completion output | Prints run ID, adapter/model URI, transcript URI, MLflow run URL (assert all four present on stdout) |
| CLI-I-014 | Real mini-run (no mocks): fixtures + mock judge + tiny-test through the actual driver | Exit 0 — the pre-E2E integration checkpoint |

## Registry list (integration)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLI-I-020 | Two seeded registry manifests | Table shows both: model_version, adapter, created_at, status, final_eval_loss; sorted newest-first |
| CLI-I-021 | Empty registry bucket | Friendly "no models registered" message, exit 0 |
| CLI-I-022 | Seeded manifest that fails schema validation | Listed as `INVALID` row with its key, exit 0 (list is a diagnostic tool; it must not die on one bad object) |
