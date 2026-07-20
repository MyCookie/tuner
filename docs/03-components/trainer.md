# Component Spec: Trainer

**Purpose:** Execute fine-tuning on tokenized tensors. Primary method **QLoRA** (portable, fits the 128 GB dev box); secondary **full-parameter** fine-tuning for small models where the adapter allows it (SAS §3.1). Logs everything to MLflow (SAS §3.2) and registers the result ([02-data-contracts.md §5.2](../02-data-contracts.md)).

## CLI

```
eftp train --run-id <RUN_ID> [--config configs/pipeline.yaml]
```

Env: `EFTP_S3_*`, `HF_TOKEN`, `MLFLOW_TRACKING_URI`. Requires CUDA; absence ⇒ exit 2 with a clear message (see host-venv fallback in [05-infrastructure.md §3](../05-infrastructure.md)).

## Input / Output

- **Input:** `eftp-artifacts/{run_id}/tokens/` (reads `index_map.json` first; absent ⇒ exit 2; its `adapter` field must equal `model.adapter` ⇒ else exit 2, tensors were built for a different model).
- **Output:** `eftp-artifacts/{run_id}/adapter/` (PEFT adapter dir, SafeTensors) — or `model/` for full FT; `eftp-registry/{model_version}/manifest.json`; one MLflow run.

## Config (`train.*` + `model.adapter`)

`method` (`qlora` | `full`), `hyperparameters` (partial overrides of the adapter's `TrainingDefaults`), `mlflow_experiment`.

## Core logic

1. Resolve adapter; merge hyperparameters (adapter defaults ← config overrides); validate `method` against `supports_full_ft`.
2. Download tensors + index_map via `StorageClient` to a local work dir.
3. Load base model: `adapter.load_base_model(quantized=(method=="qlora"))`; for QLoRA wrap with PEFT `LoraConfig` built from the merged hyperparameters (`r`, `alpha`, `dropout`, `target_modules`).
4. Build `Dataset` objects directly from the SafeTensors (`input_ids`, `attention_mask`, `labels` are already final — no collator logic beyond stacking).
5. Start the MLflow run (experiment from config, run name = run ID, tags `eftp.run_id`, `eftp.adapter`, `eftp.model_version`); log all effective hyperparameters, `gold_manifest_uri` and `index_map_uri` (the dataset version, SAS §3.2), package versions.
6. Train with HF `Trainer`: bf16, gradient checkpointing on, eval on the eval split each epoch, loss logged to MLflow every 10 steps. No early stopping in MVP; seed fixed at 42 and logged.
7. On completion: save adapter (+ tokenizer files) locally, upload dir to `eftp-artifacts/{run_id}/adapter/` (delete-then-write idempotency).
8. Write the registry manifest **last** with `status: "candidate"`, final train/eval loss, and the MLflow run id. Registry manifest presence is the "training succeeded" commit marker.
9. End the MLflow run (status FAILED on any exception, after logging the traceback as an artifact).

## Error handling

- CUDA OOM ⇒ exit 1 with a message naming the two knobs to reduce (`per_device_batch_size`, `max_seq_len`).
- Any failure before step 8 leaves no registry manifest, so the run is invisible to the registry/serving path.

## Acceptance criteria

- Steel-thread config ([06-testing.md §5](../06-testing.md), tiny model stub adapter) completes on the fixture pipeline; adapter dir contains `adapter_model.safetensors` and `adapter_config.json`; **no `.bin`/pickle files anywhere** (SAS §4.2).
- MLflow run contains: every merged hyperparameter as a param, ≥1 loss curve, dataset-version URIs, the three tags.
- Registry manifest validates against contract §5.2 and its `mlflow_run_id` resolves.
- `method: full` with `gemma-e4b` exits 2 with the sanctioned-models message.

## MVP scope

QLoRA path fully; full-FT path implemented behind the `supports_full_ft` gate but not exercised by any shipped adapter; single-GPU only (no DDP/FSDP/DeepSpeed).

## Future phases

Phase 2+: resume-from-checkpoint; Phase 3: K8s GPU scheduling, multi-GPU (FSDP) as a config addition; Phase 4: multimodal collators from adapter processors.
