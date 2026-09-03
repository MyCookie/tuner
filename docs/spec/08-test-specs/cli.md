# Test Suite: CLI, Driver & Registry List (`CLI`)

Specs under test: [01-architecture.md §2, §4.4](../01-architecture.md), [registry.md MVP scope](../03-components/registry.md). Files: `tests/unit/test_cli.py`, `tests/integration/test_driver.py`, `tests/integration/test_registry_list.py`.

## CLI shell (unit)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLI-U-001 | `tuner --help` | Lists exactly: ingest, clean, judge, tokenize, train, smoke, run, registry |
| CLI-U-002 | Stage subcommand without `--run-id` | Usage error, exit 2 |
| CLI-U-003 | `--run-id not-a-run-id` (bad format) | Exit 2 with the format regex in the message |
| CLI-U-004 | Default `--config` | Resolves to `configs/pipeline.yaml` |
| CLI-U-005 | `_invoke_stage` (real subprocess wrapper) with `subprocess.run` monkeypatched | Builds `[sys.executable, "-m", "tuner", stage, "--run-id", ..., "--config", ...]`; returns the subprocess's exact returncode |
| CLI-U-006 | `tuner run` (the click command) with `run_pipeline` monkeypatched | Exits with exactly `run_pipeline`'s return value |

## Driver (integration; stage entrypoints monkeypatched to recorders for speed)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLI-I-010 | `tuner run` happy path | Generates one valid run ID; invokes ingest→clean→judge→tokenize→train→smoke in exactly that order, each receiving the same run ID and config path |
| CLI-I-011 | Stage failure (judge recorder exits 1) | Driver stops: tokenize/train/smoke never invoked; driver exits 1 naming the failed stage |
| CLI-I-012 | Stage exit 3 (zero records) | Driver aborts with a distinct "pipeline empty at <stage>" message, exit 3 |
| CLI-I-013 | Completion output | Prints run ID, adapter/model URI, transcript URI, MLflow run URL (assert all four present on stdout) |
| CLI-I-014 | Real mini-run (no mocks): fixtures + mock judge + tiny-test through the actual driver | Exit 0 — the pre-E2E integration checkpoint |
| CLI-I-015 | Unexpected mid-run failure (mocked: `mlflow.get_run` raises after a successful stage loop) | Exit 1 with a `run: ...` message, not a raw traceback |
| CLI-I-016 | A stage returns a non-canonical exit code (e.g. `-9`, signal-killed) | Normalized to exit 1 (never propagated raw) with a message naming the actual code |

## Registry list (integration)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| CLI-I-020 | Two seeded registry manifests | Table shows both: model_version, adapter, created_at, status, final_eval_loss; sorted newest-first |
| CLI-I-021 | Empty registry bucket | Friendly "no models registered" message, exit 0 |
| CLI-I-022 | Seeded manifest that fails schema validation | Listed as `INVALID` row with its key, exit 0 (list is a diagnostic tool; it must not die on one bad object) |
| CLI-I-023 | `tuner registry list` invoked through the real CLI group (not `registry_list()` directly) | Exit 0 -- proves `registry`/`list` actually resolve and run end to end; `CLI-U-001` only proves `registry` is *listed*, not that `list` itself works |

`CLI-U-005`/`006`/`CLI-I-015`/`016`/`023` were added in review round 1 (PR #13):

- `CLI-U-005`/`006` close a coverage gap `CLI-I-014`'s own real-subprocess design
  leaves (the driver's `_invoke_stage`/`run` are only exercised through an actual OS
  subprocess there, which coverage tooling can't observe, per
  `docs/spec/08-test-specs/README.md`'s own "never subprocess, keeps coverage measurable"
  convention) -- both are the in-process, monkeypatched-boundary equivalent.
- `CLI-I-015`/`016` regression-test round 1's two driver findings: `run_pipeline` had
  no top-level exception guard (an unreachable MLflow server or a missing registry
  manifest field raised a raw traceback instead of a `run: ...` message -- reproduced
  live with a bogus `mlflow_run_id` against the real compose server), and a stage
  returning a code outside `{0,1,2,3}` (e.g. a signal-killed subprocess's negative
  returncode) propagated raw instead of normalizing to 1 (01 §4.4's exit-code
  contract is supposed to hold for `tuner run` itself, not just the stages it calls).
- `CLI-I-023` closes a real gap the round-1 reviewer found: the original submission
  had an anonymous test exercising the same thing with no suite ID at all, which
  `check_test_ids.py` (T14) would fail CI on.
