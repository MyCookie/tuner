# Tokenizer

The Tokenizer maps Gold records to the target model's vocabulary and writes
the SafeTensors tensors the Trainer trains on, plus an `index_map.json` that
lets you trace any tensor row back to the exact Gold record it came from.
Everything model-specific — which tokenizer, which chat template, what
sequence length — comes from the selected [model adapter](../spec/04-model-adapters.md);
see [Architecture overview — the model-adapter abstraction](../01-architecture-overview.md#the-model-adapter-abstraction-what-makes-the-fine-tune-target-pluggable).
Everything below comes from reading `src/tuner/tokenizer/cli.py`,
`masking.py`, and `split.py`, and from running `tuner tokenize`/`tuner run`
against the `tiny-test` adapter on this repository's fixtures.

## What it does

For each Gold record: assign it deterministically to the train or eval split
(before any tokenization happens), convert the conversation to the adapter's
chat-message format, tokenize the full sequence, and build `labels` — a copy
of `input_ids` with everything outside the assistant's own turns masked to
`-100` so the loss is only ever computed on tokens the model is actually
supposed to generate. Records that are too long, use an unsupported modality,
or hit a tokenizer edge case get dropped, not truncated or silently kept —
truncation would destroy assistant answers, which the tokenizer.md spec is
explicit about avoiding.

## Input and output

| | Bucket | Path |
| :--- | :--- | :--- |
| Reads | `tuner-gold` | `{run_id}/` |
| Writes | `tuner-artifacts` | `{run_id}/tokens/train.safetensors`, `eval.safetensors`, `index_map.json` |

Real output from a run against `tiny-test` (`configs/pipeline.e2e.yaml`, 94
Gold records in):

```
$ mc ls (or StorageClient.download_dir) tuner-artifacts/{run_id}/tokens/
train.safetensors    153616 bytes
eval.safetensors      19760 bytes
index_map.json          6453 bytes
```

The `index_map.json` header (splits/dropped lists trimmed here — see
[Data contracts §4](../spec/02-data-contracts.md) for the full shape):

```json
{
  "run_id": "run-20260904-002347-19e725",
  "adapter": "tiny-test",
  "tokenizer_id": "HuggingFaceTB/SmolLM2-135M-Instruct",
  "max_seq_len": 2048,
  "gold_manifest_uri": "s3://tuner-gold/run-20260904-002347-19e725/manifest.json",
  "splits": { "train": [ /* 83 entries */ ], "eval": [ /* 11 entries */ ] },
  "dropped": []
}
```

(94 Gold records → 83 train + 11 eval + 0 dropped, for this fixture-scale
run — every Gold record fits comfortably under `tiny-test`'s 2048-token
`max_seq_len`, so nothing hit the length or masking drop paths here. The
exact train/eval split size varies slightly run to run even against the same
fixtures, since `assign_split` hashes each record's freshly-generated UUID —
see below.)

## Key behavior worth knowing

**Split assignment is a pure hash function, not a shuffle.** `assign_split`
(`src/tuner/tokenizer/split.py`) computes `int(sha256(record_id)[:8], 16) /
0xFFFFFFFF` and compares against `eval_fraction` — nothing else feeds in, no
run-specific seed, no ordering dependency. That means the same record ID
always lands in the same split regardless of which run produced it, which
machine ran the Tokenizer, or what order Gold's file lists records in. It
also means changing `eval_fraction` reshuffles which specific records fall on
which side of the new boundary (records near the old cut point can flip),
rather than just resizing the split while keeping every prior assignment
intact.

**Label masking works by re-tokenizing incrementally, not by counting special
tokens.** `build_labels` (`src/tuner/tokenizer/masking.py`) locates each
assistant turn's span by tokenizing three variants — the full conversation,
the prefix before this turn plus the template's generation-prompt tokens, and
the prefix through this turn as a finished turn — and taking the length
difference. Two "prefix property" assertions guard this: a longer prefix's
tokenization must literally start with the shorter one's tokens, or the
turn's boundary can't be trusted. This is what protects against tokenizer
merge effects at turn boundaries silently shifting which tokens get
unmasked — a real risk with BPE-style tokenizers, not a hypothetical.

**A masking failure drops the record, it doesn't crash the run** — a
deliberate fix over an earlier version, per the code's own comment
(`src/tuner/tokenizer/masking.py`): some chat templates outright refuse to
render an empty prefix (e.g. a conversation that opens with an assistant
turn), which used to escape as an uncaught exception and abort the entire
stage over one oddly-shaped record. Now it's caught and counted as
`masking_mismatch`, same as a genuine prefix-property violation.

**Two implementation-level abort thresholds exist, and they're not in the
spec's own core-logic listing** — verified directly from
`src/tuner/tokenizer/cli.py`: if more than 50% of records drop `over_max_len`,
the stage exits 1 with a message suggesting you raise `tokenize.max_seq_len`
(a config problem, not a data-quality one); if more than 1% drop
`masking_mismatch`, it exits 1 saying the adapter's template interaction is
broken, not the data. Below those thresholds, affected records are simply
dropped and accounted for in `index_map.dropped`, and the stage completes
normally.

**Step ordering is deliberately reversed from the component spec's own
numbering** — the spec (`spec/03-components/tokenizer.md`) lists
over-length-drop after masking; the implementation checks length *first*,
skipping the extra incremental-prefix tokenization calls entirely for a
record that's getting dropped either way. The spec's own footnote 2 confirms
this is an accepted implementation choice: a record that would fail *both*
checks is reported as `over_max_len`, not `masking_mismatch`, and nothing is
silently kept under one ordering that the other would have dropped.

**Adapter mismatch is not checked here — only at Trainer time.** Verified
directly, by accident, while writing this page: re-running `tuner tokenize`
against an existing run ID with a *different* `model.adapter` in the config
happily deletes and rewrites that run's `tokens/` prefix for the new adapter,
with no complaint that a previous tokenization existed for a different model.
It's the Trainer (see [trainer.md](trainer.md)) that refuses to proceed if
`index_map.adapter` doesn't match `model.adapter` — so if you ever change
`model.adapter` for a run ID you've already tokenized, re-run `tuner
tokenize` again before `tuner train`, or the Trainer will exit 2 rather than
silently training on tensors built for the wrong tokenizer.

**Unknown adapter name exits 2** before any Gold record is even read, from
`get_adapter`: `tokenize: "unknown model adapter 'nonexistent-adapter';
known adapters: gemma-e4b, tiny-test"` — verified directly.

**Zero-row splits are valid, not an error.** `pad_and_stack` explicitly
handles an empty split by writing a well-shaped `(0, 0)` tensor rather than
failing — a small `eval_fraction` against a small Gold tier can legitimately
produce zero eval rows without that being a problem (though zero *train* rows
is exit 3, since there'd be nothing to train on).

**SafeTensors are written via `safetensors.numpy`, not `safetensors.torch`**
— `torch` isn't part of the Tokenizer's own dependency set, and the
on-disk format is framework-agnostic: a `numpy`-written int64 array is
byte-identical, header and all, to what `safetensors.torch` would write for
the same array. The Trainer, which does depend on `torch`, reads these files
with `safetensors.torch.load_file` without any conversion step.

## Running it

Standalone: `tuner tokenize --run-id <RUN_ID> --config <path>`. As the fourth
stage of `tuner run`, right before the Trainer. Needs `HF_TOKEN` to pull the
adapter's tokenizer. See
[CLI reference — `tuner tokenize`](../02-cli-reference.md#tuner-tokenize) for
exact flags and exit codes.

## Configuring it

`tokenize.max_seq_len` and `tokenize.eval_fraction` are documented in
[Configuration reference — `tokenize`](../03-configuration.md#tokenize),
including the adapter-default-vs-config-override precedence rule that
governs `max_seq_len: null`.
