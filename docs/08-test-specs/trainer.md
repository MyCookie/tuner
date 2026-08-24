# Test Suite: Trainer (`TRN`)

Spec under test: [trainer.md](../03-components/trainer.md). File: `tests/integration/test_trainer.py`. Runs on CPU with the `tiny-test` adapter (`supports_full_ft: True`, method `full`, 1 epoch, ≤20 records) so CI can execute it; the QLoRA/bitsandbytes load path is `@pytest.mark.gpu` and covered manually in T15 (each such branch carries the justified `# pragma: no cover`).

## Setup

Seeded `tokens/` prefix from a real TOK run over seeded Gold (shared session-scoped fixture to keep runtime down); file-backed MLflow tracking URI.

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| TRN-I-001 | Full happy path (tiny-test, `method: full`, 1 epoch) | Exit 0; `tuner-artifacts/{run_id}/model/` (full-FT layout) uploaded; loss decreased or run completed with finite losses |
| TRN-I-002 | QLoRA config-construction path (GPU-free part): LoraConfig built from merged hyperparameters | `r/alpha/dropout/target_modules` match the merge result (unit-style assert on the constructed object, model never loaded) |
| TRN-I-003 | Artifact hygiene sweep | No `*.bin`, no pickle files anywhere under the run prefix (walk every object key) |
| TRN-I-004 | Registry manifest | Validates vs. [02 §5.2](../02-data-contracts.md); `status: candidate`; `model_version` = `tiny-test-{run_id}`; URIs resolve to existing objects; `mlflow_run_id` resolves to a run |
| TRN-I-005 | MLflow run | Every merged hyperparameter logged as param; ≥1 loss metric series; tags `tuner.run_id`/`tuner.adapter`/`tuner.model_version`/`tuner.stage: trainer`; dataset-version params (`gold_manifest_uri`, `index_map_uri`) |
| TRN-I-006 | `method: full` with adapter where `supports_full_ft` is False (gemma-e4b, no model download happens) | Exit 2 before any load |
| TRN-I-007 | `index_map.adapter` ≠ `model.adapter` | Exit 2 (tensors built for a different model) |
| TRN-I-008 | Missing `tokens/index_map.json` | Exit 2 |
| TRN-I-009 | Induced failure during upload (monkeypatch `upload_dir` to raise) | No registry manifest (commit-marker rule); MLflow run status `FAILED` with traceback artifact |
| TRN-I-010 | Hyperparameter override via config (`epochs: 2`) | MLflow param shows 2; adapter default not used |
| TRN-I-011 | Re-run same run ID after TRN-I-001 | Adapter/model prefix rebuilt; exactly one registry manifest for the version (idempotency) |
| TRN-I-012 | `method: qlora` with no CUDA device available (mocked) | Exit 2 naming the host-venv fallback doc — the CLI section's own "requires CUDA" sentence, scoped to `qlora` (see [trainer.md](../03-components/trainer.md) fn. 1) |
| TRN-I-013 | Unknown `model.adapter` in config | Exit 2 |
| TRN-I-014 | Unknown key in `train.hyperparameters` | Exit 2 |
| TRN-I-015 | Present but schema-invalid `tokens/index_map.json` | Exit 2 |
| TRN-I-016 | Every record hashes to train — no eval split at all | Proceeds; `eval.final_eval_loss` mirrors `final_train_loss` in the registry manifest |
| TRN-G-020 *(gpu)* | Real `gemma-e4b` QLoRA smoke: 10 fixture records, 1 epoch on the dev box | Completes; adapter dir loads with PEFT; VRAM within the 128 GB box — executed in T15, recorded in the run log |

TRN-I-012..016 were added while implementing T11, as gaps noticed against the component spec's own CLI section and sibling suites' established patterns, rather than left as untested code paths with no case to justify them:

- **TRN-I-012** exercises the CLI section's literal "Requires CUDA; absence ⇒ exit 2" sentence, which no other case in the original table touched — see the scope-clarification footnote on that sentence in [trainer.md](../03-components/trainer.md) (the requirement is `bitsandbytes`', so it gates `qlora` specifically, not every training run).
- **TRN-I-013** mirrors every sibling stage's own "unknown adapter" case (`ADP-U-011` via `TOK-I-029`, etc.) — the original table omitted the Trainer's equivalent even though it resolves the adapter the same way.
- **TRN-I-014** exercises `merge_hyperparameters`'s `ConfigError` (`ADP-U-031`/`CORE-U-007`, T09) at the one CLI that actually calls it with a real adapter's `training_defaults` — Tokenizer and Judge never touch hyperparameter merging at all.
- **TRN-I-015** is `TRN-I-008`'s sibling: "missing" and "invalid" are both upstream-validation failure modes every other stage's own suite tests separately (`CLN-I-034/035`, `JDG-I-030/031`, `TOK-I-025/031`); the original table only had "missing."
- **TRN-I-016** is the Trainer-side counterpart to `TOK-I-027` (eval empties, one stage upstream) — `RegistryEval.final_eval_loss` is a required field with no "N/A" representation, so what happens when there is nothing to evaluate needed its own case, not just an assumption.

## Notes

- **`tokenized_run_id`'s 15 Gold records use fixed record IDs, not `new_record_id()`.** With random UUIDs, whether the resulting eval split is empty varies from run to run (`~10%` per record, so 0-eval outcomes over 15 records are common) — meaning the shared happy-path fixture would non-deterministically exercise (or fail to exercise) Trainer's has-eval-data branch depending on the luck of that run's UUIDs. Fixed at 13 confirmed-train + 2 confirmed-eval IDs (the same technique `TOK-U-001`/`TOK-I-027` already established) so the main suite consistently covers both branches, and `TRN-I-016` covers the all-train case on its own, separately-seeded run.
- **`tuner.cli`'s `train` (and future `smoke`) subcommands are lazily imported.** `tuner.trainer.cli` pulls in torch/transformers/peft/accelerate (the `train` extra, [05-infrastructure.md §3](../05-infrastructure.md)) — importing it eagerly in `tuner/cli.py` would make every `tuner` invocation, including `tuner ingest` or a bare `tuner --help`, require that extra, breaking the documented `--extra dev`-only setup for every CPU-only stage. A custom `click.Group` subclass imports each lazy subcommand's module only when that subcommand is actually invoked, with a static short-help string so the group's own `--help` listing doesn't need to import anything either (click's own `Group.format_commands` otherwise instantiates every listed command just to read its help text). `scripts/review-setup.sh` and [10-code-review.md](../10-code-review.md) are updated to sync `--extra train` too, from T11 on — otherwise a fresh reviewer worktree fails to even *collect* `test_trainer.py`.
- **SafeTensors written by the Tokenizer (`safetensors.numpy`, T10) load correctly via `safetensors.torch`, confirmed in practice, not just claimed.** The Trainer is the first real consumer of Tokenizer output and reads with `safetensors.torch.load_file` per [trainer.md](../03-components/trainer.md) core logic 4 — every integration test in this file exercises that read path against real Tokenizer output.
