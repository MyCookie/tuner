# Test Suite: Trainer (`TRN`)

Spec under test: [trainer.md](../03-components/trainer.md). File: `tests/integration/test_trainer.py`. Runs on CPU with the `tiny-test` adapter (`supports_full_ft: True`, method `full`, 1 epoch, ≤20 records) so CI can execute it; the QLoRA/bitsandbytes load path is `@pytest.mark.gpu` and covered manually in T15 (each such branch carries the justified `# pragma: no cover`).

## Setup

Seeded `tokens/` prefix from a real TOK run over seeded Gold (shared session-scoped fixture to keep runtime down); file-backed MLflow tracking URI.

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| TRN-I-001 | Full happy path (tiny-test, `method: full`, 1 epoch) | Exit 0; `eftp-artifacts/{run_id}/model/` (full-FT layout) uploaded; loss decreased or run completed with finite losses |
| TRN-I-002 | QLoRA config-construction path (GPU-free part): LoraConfig built from merged hyperparameters | `r/alpha/dropout/target_modules` match the merge result (unit-style assert on the constructed object, model never loaded) |
| TRN-I-003 | Artifact hygiene sweep | No `*.bin`, no pickle files anywhere under the run prefix (walk every object key) |
| TRN-I-004 | Registry manifest | Validates vs. [02 §5.2](../02-data-contracts.md); `status: candidate`; `model_version` = `tiny-test-{run_id}`; URIs resolve to existing objects; `mlflow_run_id` resolves to a run |
| TRN-I-005 | MLflow run | Every merged hyperparameter logged as param; ≥1 loss metric series; tags `eftp.run_id`/`eftp.adapter`/`eftp.model_version`; dataset-version params (`gold_manifest_uri`, `index_map_uri`) |
| TRN-I-006 | `method: full` with adapter where `supports_full_ft` is False (gemma-e4b, no model download happens) | Exit 2 before any load |
| TRN-I-007 | `index_map.adapter` ≠ `model.adapter` | Exit 2 (tensors built for a different model) |
| TRN-I-008 | Missing `tokens/index_map.json` | Exit 2 |
| TRN-I-009 | Induced failure during upload (monkeypatch `upload_dir` to raise) | No registry manifest (commit-marker rule); MLflow run status `FAILED` with traceback artifact |
| TRN-I-010 | Hyperparameter override via config (`epochs: 2`) | MLflow param shows 2; adapter default not used |
| TRN-I-011 | Re-run same run ID after TRN-I-001 | Adapter/model prefix rebuilt; exactly one registry manifest for the version (idempotency) |
| TRN-G-020 *(gpu)* | Real `gemma-e4b` QLoRA smoke: 10 fixture records, 1 epoch on the dev box | Completes; adapter dir loads with PEFT; VRAM within the 128 GB box — executed in T15, recorded in the run log |
