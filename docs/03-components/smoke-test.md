# Component Spec: Smoke-test

**Purpose:** Prove the trained model actually changed behavior — the MVP's validation gate. Loads the base model with and without the trained adapter, runs held-out prompts, and writes a before/after transcript. Not a benchmark; a human-readable sanity check that closes the loop from raw data to observable model behavior.

## CLI

```
tuner smoke --run-id <RUN_ID> [--config configs/pipeline.yaml]
```

Env: `TUNER_S3_*`, `HF_TOKEN`, `MLFLOW_TRACKING_URI`. Requires CUDA for `method: qlora`² (same fallback note as the Trainer).

## Input / Output

- **Input:** `tuner-artifacts/{run_id}/adapter/`¹ and `{run_id}/tokens/index_map.json`; `tuner-gold/{run_id}/` (to fetch prompt text for eval-split record IDs).
- **Output:** `tuner-artifacts/{run_id}/smoke/transcript.json` ([02 §5.3](../02-data-contracts.md)); the same file attached as an artifact to the Trainer's MLflow run — located by filtering on **both** tags `tuner.run_id == {run_id}` and `tuner.stage == "trainer"` ([01 §7](../01-architecture.md)); exactly one run must match, else exit 2.

¹ **`train.method: full` clarification (T12):** this resolves to `{run_id}/model/` instead, mirroring the Trainer's own output split ([02 §5.1](../02-data-contracts.md)) — core logic 3–4 below already say what changes for that case. The `08-test-specs/smoke.md` suite's own `SMK-I-005` row already names both directory names for this reason.

² **CUDA scope (T12), mirrors the Trainer's own [03/trainer.md](trainer.md) footnote 1 exactly:** `bitsandbytes` 4-bit quantization is what actually needs CUDA, so this gates `method: qlora` specifically; `method: full` runs on whatever device torch/accelerate find, same as the Trainer.

## Config (`smoke.*`)

`num_prompts` (default 8), `max_new_tokens` (default 256).

## Core logic

1. Resolve adapter from config; read `index_map.json`; take the first `num_prompts` eval-split record IDs (eval split = data the model never trained on).
2. Fetch those Gold records; for each, the **prompt** is `to_chat_messages(conversation)` minus its final message; that final message's `content` is kept as `reference` (02 §5.3 — not `to_chat_messages` applied to the truncated conversation, which could reshape differently for some adapter).
3. Load the base model once — quantized only for `method: qlora`² (04-model-adapters.md §1), plain otherwise. Generate per prompt (greedy, `temperature 0`, `max_new_tokens`) → `base_output`.
4. For `method: qlora`, attach the PEFT adapter and generate again → `tuned_output`. For `method: full`¹, there is no PEFT adapter to attach — the fine-tuned weights loaded directly, in place of the base model, are the tuned model.
5. Write `transcript.json`:

```json
{
  "run_id": "run-...", "model_version": "gemma-e4b-run-...",
  "generation": {"max_new_tokens": 256, "strategy": "greedy"},
  "samples": [
    {"record_id": "...", "prompt_messages": [...], "reference": "...",
     "base_output": "...", "tuned_output": "..."}
  ]
}
```

6. Upload to `tuner-artifacts/{run_id}/smoke/` (delete-then-write); log as MLflow artifact `smoke/transcript.json` on the Trainer's run.

## Error handling

- Missing adapter dir ⇒ exit 2 ("trainer has not completed for this run ID").
- Fewer eval records than `num_prompts` ⇒ use all available, warn; zero eval records ⇒ exit 3.

## Acceptance criteria

- On the steel-thread run, transcript contains `num_prompts` samples, each with non-empty `base_output` and `tuned_output`, and prompts drawn only from eval-split record IDs.
- The MLflow trainer run shows the transcript artifact.
- Deterministic generation: two smoke runs against the same artifacts produce identical outputs.

## MVP scope

All of the above. No automatic quality scoring of outputs — the transcript is for human review.

## Future phases

Phase 2+: score `tuned_output` vs `reference` with the Judge's rubric for a regression number; that number becomes the canary comparison metric consumed by [inference.md](inference.md).
