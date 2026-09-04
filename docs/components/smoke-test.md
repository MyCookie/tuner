# Smoke-test

The Smoke-test is the MVP's validation gate: it proves the trained model
actually changed behavior by running the same held-out prompts through the
base model and the fine-tuned one, side by side, and writing the result as a
human-readable transcript. It's explicitly not a benchmark or an automatic
pass/fail check — there's no scoring in the MVP, just a transcript for a
human to read. See [Architecture overview](../01-architecture-overview.md)
for where it sits after the Trainer. Everything below comes from reading
`src/tuner/smoke/cli.py` and from running `tuner smoke`/`tuner run` on the
`tiny-test` adapter.

## What it does

Takes the first `smoke.num_prompts` eval-split record IDs from
`index_map.json` (eval-split specifically, so the model never trained on
these), fetches their Gold records, and for each one generates a completion
from the base model and from the trained model — greedy decoding,
`temperature 0`, `max_new_tokens` from config. The prompt given to both
models is exactly `to_chat_messages(conversation)` minus its final message
(the "prompt_messages"); that final message's content becomes `reference` —
the ground-truth answer the base and tuned outputs are meant to be compared
against. Everything is written to one `transcript.json`, which is also
attached as an artifact to the Trainer's MLflow run.

## Input and output

| | Bucket | Path |
| :--- | :--- | :--- |
| Reads | `tuner-artifacts` | `{run_id}/adapter/` (or `{run_id}/model/` for `method: full`), `{run_id}/tokens/index_map.json` |
| Reads | `tuner-gold` | `{run_id}/` (to fetch prompt/reference text for the sampled eval record IDs) |
| Writes | `tuner-artifacts` | `{run_id}/smoke/transcript.json`, plus an MLflow artifact on the Trainer's run |

One sample from a real transcript, `method: full`, `tiny-test` adapter:

```json
{
  "record_id": "fbadfece-9d61-4532-be70-265d8d99066f",
  "prompt_messages": [{"role": "user", "content": "Why was I charged twice for your account this month?"}],
  "reference": "Duplicate charges are usually a temporary authorization hold that clears within 3-5 business days. If it persists, contact billing support with your invoice number.",
  "base_output": "I'm sorry for the confusion, but as a chatbot, I don't have the ability to access personal data or account information. I'm designed to provide assistance and support, not to answer questions about your account. I recommend contacting your bank or credit card company for assistance.",
  "tuned_output": "You've been charged twice for the same offense. You can dispute the charges and get a new one charged."
}
```

This is a small (135M-parameter, one-epoch, tiny fixture-scale) model, and it
shows — `tuned_output` clearly diverges from `base_output` (proving the
weights changed) without being a *good* answer, which is exactly what the
Smoke-test is for: showing that training had an effect, not certifying
quality. All 8 samples in this run were confirmed to come from the eval
split (`smoke.num_prompts: 8` for this config), verified directly against
`index_map.json`. See [Data contracts §5.3](../spec/02-data-contracts.md) for
the full transcript schema.

## Key behavior worth knowing

**`train.method` decides which directory this stage reads from, and it's the
same config key the Trainer itself branches on** — `{run_id}/adapter/` for
`qlora`, `{run_id}/model/` for `full`. There's no independent flag for this;
the Smoke-test infers it from `train.method` the same way the Trainer decided
where to write in the first place.

**A missing weights directory fails with an operator-actionable message, not
a storage error**, and — like the Trainer's own adapter/full-FT gate — this
is checked before any model download happens:

```
$ uv run tuner smoke --run-id <a run with no completed Trainer output> --config configs/pipeline.e2e.yaml
smoke: trainer has not completed for this run ID (missing s3://tuner-artifacts/<run_id>/model/)
```

**`method: full` has no PEFT adapter to attach — the saved weights *are* the
tuned model**, loaded directly via `AutoModelForCausalLM.from_pretrained` in
place of attaching a `PeftModel` on top of the base model. This is a real
branch in `src/tuner/smoke/cli.py`, not just a config difference: `qlora`
attaches a PEFT adapter to the already-loaded base model; `full` loads a
second, independent model instance from the saved weights.

**Exactly one Trainer MLflow run must exist for this run ID, or the stage
refuses to proceed** — checked *before* anything is written to storage, so a
bad MLflow lookup never leaves a transcript committed without its attachment.
The filter is on the pair of tags `tuner.run_id` + `tuner.stage: trainer`,
not `tuner.run_id` alone, since multiple stages across one pipeline run each
open their own MLflow run under the same run-ID tag (see
[Architecture overview — object storage + MLflow](../01-architecture-overview.md#object-storage--mlflow-two-systems-two-jobs)).
Zero or more-than-one match exits 2.

**Fewer eval records than requested degrades gracefully; zero is a hard
stop.** If `index_map.json`'s eval split has fewer entries than
`smoke.num_prompts`, the stage warns and uses all available records rather
than failing. If there are *no* eval records at all, it exits 3 — there's
nothing held-out to sample from.

**QLoRA's CUDA requirement is inherited, not re-derived** — the exact same
`bitsandbytes`-needs-a-GPU reasoning as the Trainer, gating `method: qlora`
specifically and checked before any storage or model access:

```
smoke: method qlora requires a CUDA device (bitsandbytes 4-bit quantization); none found -- see the host-venv fallback in 05-infrastructure.md §3
```

`method: full` runs on CPU or GPU, same as the Trainer.

**Generation is deterministic**, per the spec's own acceptance criterion:
greedy decoding with `temperature 0` means two Smoke-test runs against the
same trained artifacts produce identical `base_output`/`tuned_output` text,
not just similar text.

## Running it

Standalone: `tuner smoke --run-id <RUN_ID> --config <path>`. As the sixth and
final stage of `tuner run`. Needs `HF_TOKEN` and `MLFLOW_TRACKING_URI`; a CUDA
device only for `method: qlora`. See
[CLI reference — `tuner smoke`](../02-cli-reference.md#tuner-smoke) for exact
flags and exit codes.

## Configuring it

`smoke.num_prompts` and `smoke.max_new_tokens` are documented in
[Configuration reference — `smoke`](../03-configuration.md#smoke).
