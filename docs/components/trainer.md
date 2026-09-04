# Trainer

The Trainer fine-tunes the selected adapter's base model on the tokenized
tensors the Tokenizer produced, logs everything to MLflow, and — on success —
writes the registry manifest that makes the resulting model version
discoverable. It's the stage with the widest storage footprint: it both reads
and writes the Artifact tier, and it's the only stage besides Registry ops
that writes to `tuner-registry`. See
[Architecture overview](../01-architecture-overview.md) for how it fits
between the Tokenizer and Smoke-test. Everything below comes from reading
`src/tuner/trainer/cli.py` and from running `tuner train`/`tuner run` on the
`tiny-test` adapter with `method: full` (this repository's own CPU-fast
path — see [Getting started §4](../00-getting-started.md)).

## What it does

Resolves the adapter, merges hyperparameters (adapter defaults overridden
field-by-field by `train.hyperparameters`), downloads the tokenized tensors,
builds a HF `Trainer` around them directly (no collator logic beyond
stacking, since `input_ids`/`attention_mask`/`labels` are already final), and
trains for the configured method — QLoRA (a PEFT adapter on a 4-bit
quantized base model) or full-parameter fine-tuning. On completion it saves
the result as SafeTensors, uploads it to the Artifact tier, and writes the
registry manifest — which is itself the "training succeeded" commit marker:
if anything fails before that write, the run is invisible to
`tuner registry list` and to the Smoke-test.

## Input and output

| | Bucket | Path |
| :--- | :--- | :--- |
| Reads | `tuner-artifacts` | `{run_id}/tokens/` (its `index_map.json` first) |
| Writes | `tuner-artifacts` | `{run_id}/adapter/` (QLoRA) or `{run_id}/model/` (full) |
| Writes | `tuner-registry` | `{model_version}/manifest.json`, plus one MLflow run |

A real registry manifest, from a `method: full` run against `tiny-test`:

```json
{
  "model_version": "tiny-test-run-20260904-002347-19e725",
  "run_id": "run-20260904-002347-19e725",
  "adapter_name": "tiny-test",
  "base_model": "HuggingFaceTB/SmolLM2-135M-Instruct",
  "method": "full",
  "created_at": "2026-09-04T00:24:14Z",
  "gold_manifest_uri": "s3://tuner-gold/run-20260904-002347-19e725/manifest.json",
  "index_map_uri": "s3://tuner-artifacts/run-20260904-002347-19e725/tokens/index_map.json",
  "weights_uri": "s3://tuner-artifacts/run-20260904-002347-19e725/model/",
  "mlflow_run_id": "4abdf60f53ba478aabfe2a6a1200f65f",
  "hyperparameters": { "learning_rate": 0.0002, "epochs": 1, "lora_r": 8, "...": "..." },
  "eval": {"final_train_loss": 1.845257043838501, "final_eval_loss": 1.7918636798858643},
  "status": "candidate"
}
```

And the same run's MLflow tags/params/metrics, captured directly via
`mlflow.search_runs`:

```
tags:    tuner.run_id, tuner.model_version, tuner.adapter=tiny-test, tuner.stage=trainer
params:  every merged hyperparameter (epochs, learning_rate, lora_r, ...),
         method=full, seed=42, gold_manifest_uri, index_map_uri,
         pkg.torch=2.13.0+cu130, pkg.transformers, pkg.peft, pkg.accelerate
metrics: loss, eval_loss, train_loss, grad_norm, learning_rate,
         train/eval_runtime + samples/steps_per_second, epoch, total_flos
```

For the full artifact-layout and registry-manifest schemas, see
[Data contracts §5](../spec/02-data-contracts.md).

## Key behavior worth knowing

**Every disqualifying condition is checked before any model is
downloaded** — deliberately, per the code's own comments, so a misconfigured
run fails in milliseconds rather than after minutes of downloading a base
model it was never going to be allowed to train:

1. `MLFLOW_TRACKING_URI` unset → exit 2 (checked before the adapter is even
   resolved, so a missing tracking URI can't surface only after training
   already ran, the same class of fix the Judge needed).
2. Unknown `model.adapter` → exit 2.
3. `train.method: full` against an adapter whose `supports_full_ft` is
   `False` → exit 2. The shipped default adapter, `gemma-e4b`, is exactly
   such an adapter — verified directly by reading
   `src/tuner/trainer/cli.py`:
   ```python
   if config.train.method == "full" and not adapter.supports_full_ft:
       ...
       return 2
   ```
4. `train.method: qlora` with no CUDA device visible → exit 2. This check is
   specific to `qlora` — `bitsandbytes`' 4-bit quantization fundamentally
   needs a CUDA device — and does *not* gate `method: full`, which runs on
   whatever device torch/accelerate find, GPU or CPU. This is exactly why
   this repository's own CPU-fast path uses `method: full`.
5. An `index_map.adapter` that doesn't match `model.adapter` → exit 2 — the
   tensors were tokenized for a different model. Verified directly:
   ```
   $ uv run tuner train --run-id <id> --config <config selecting tiny-test, but index_map says gemma-e4b>
   train: tokens were built for adapter 'gemma-e4b', config selects 'tiny-test' -- tensors were built for a different model, re-run tokenize first
   ```
   See the [Tokenizer page](tokenizer.md) for why this situation can arise
   at all — the Tokenizer itself doesn't check for it.

**bf16 is conditional on more than just "is there a GPU."** Training uses
bf16 only when `torch.cuda.is_available() and torch.cuda.is_bf16_supported()`
both hold — not CUDA presence alone. An earlier version used bf16
unconditionally whenever `method: full` ran, which failed outright on a
CPU-only device (no bf16/GPU support at all) and would have failed the same
way on a CUDA device lacking bf16 tensor cores; everything else — CPU, or a
GPU without bf16 support — trains in fp32.

**The seed is fixed and always logged**: `SEED = 42` in
`src/tuner/trainer/cli.py`, passed to `TrainingArguments(seed=...)` and
logged as an MLflow param on every run, regardless of method.

**Idempotent re-runs clean up both possible output directories, not just the
current one.** Before uploading its own output, the Trainer deletes *both*
`{run_id}/model/` and `{run_id}/adapter/` under the run's artifact prefix,
even though only one of them is this run's actual output subdirectory. This
matters if `train.method` ever flips between two invocations sharing the
same run ID — without deleting both, the *other* method's stale output would
be orphaned under the run prefix forever, silently disagreeing with what the
registry manifest's `weights_uri` actually points at.

**CUDA out-of-memory gets a specific, actionable message**, not a raw
traceback: `train: CUDA out of memory -- reduce
train.hyperparameters.per_device_batch_size or tokenize.max_seq_len: ...` —
naming the two knobs that actually shrink memory use, rather than a generic
failure.

**A traceback is captured as an MLflow artifact before the run fails.** Any
exception raised during training (after the MLflow run has started) is
written to `traceback.txt`, logged as an artifact on the still-open run, and
then re-raised — which is what lets `mlflow.start_run`'s own context-manager
exit mark the run `FAILED` rather than leaving it dangling in `RUNNING`
state.

**No `.bin`/pickle files anywhere, ever** — verified directly: every object
under this run's `tuner-artifacts` prefix after a real `method: full` run
was `.safetensors`/`.json`. That the saved directory is genuinely loadable,
not just correctly named, was proven by the same run's own Smoke-test stage,
which loads it back via `AutoModelForCausalLM.from_pretrained` and generated
real completions from it (see [smoke-test.md](smoke-test.md)).

## Running it

Standalone: `tuner train --run-id <RUN_ID> --config <path>`. As the fifth
stage of `tuner run`, right before the Smoke-test. Needs `HF_TOKEN` and
`MLFLOW_TRACKING_URI`; needs a CUDA device only for `method: qlora` — see the
host-venv fallback in [Getting started §5](../00-getting-started.md) if
Docker GPU passthrough isn't set up. See
[CLI reference — `tuner train`](../02-cli-reference.md#tuner-train) for exact
flags and exit codes.

## Configuring it

`train.method`, `train.hyperparameters`, and `train.mlflow_experiment`,
along with the adapter-defaults-then-config-then-CLI precedence rule that
governs hyperparameter merging, are documented in
[Configuration reference — `train`](../03-configuration.md#train) and
[— precedence](../03-configuration.md#precedence-adapter-defaults--config-file--cli-flags).
