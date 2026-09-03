# CLI reference

Everything below is taken from the actual CLI code
(`src/tuner/cli.py` and each stage's `src/tuner/<stage>/cli.py`) and from
running `uv run tuner ... --help` against this repository, not from
paraphrasing the specification. Where the spec and the running code would
ever disagree, the code is what ships — this page follows the code; see
[docs/spec/01-architecture.md §4.4](spec/01-architecture.md) for the
normative version if you need to cite one.

## Global shape

```
$ uv run tuner --help
Usage: tuner [OPTIONS] COMMAND [ARGS]...

  Tuner — Enterprise Fine-Tuning Pipeline.

Options:
  --help  Show this message and exit.

Commands:
  clean     Convert Bronze envelopes into scrubbed, filtered,...
  ingest    Convert configured sources into Bronze envelopes.
  judge     Score Silver records with an LLM and promote passing ones to...
  registry  Model registry operations.
  run       Run the full pipeline: ingest -> clean -> judge -> tokenize...
  smoke     Generate before/after transcripts proving the trained model...
  tokenize  Map Gold records to the target model's vocabulary; write...
  train     Fine-tune the selected adapter's base model on tokenized Gold...
```

Every per-stage subcommand (`ingest`, `clean`, `judge`, `tokenize`, `train`,
`smoke`) takes exactly two options:

| Option | Required | Default | Notes |
| :--- | :-: | :--- | :--- |
| `--run-id` | yes | — | must match `^run-\d{8}-\d{6}-[0-9a-f]{6}$` ([run-ID format](spec/01-architecture.md#42-identifiers)); an invalid value fails immediately with `Invalid value for '--run-id': must match ...` (exit `2`, verified) |
| `--config` | no | `configs/pipeline.yaml` | pipeline config path — see [03-configuration.md](03-configuration.md) |

`tuner run` is the one exception: it takes only `--config` (it generates its
own run ID) and no `--run-id`.

`tuner registry`'s own subcommand (`list`) takes **neither** option — it
operates on the whole `tuner-registry` bucket across every run, not one run's
config-scoped output.

`train` and `smoke` are loaded lazily: importing them pulls in
torch/transformers/peft/accelerate (the `train` extra), so the module isn't
touched at all until you actually invoke one of those two subcommands — even
`tuner --help` never imports it. Calling either one without that extra
installed fails fast with a message naming the fix, verified against this
repository's own `.venv` before the extra was installed:

```
$ uv run tuner train --help
Error: 'train' needs the `train` extra (torch/transformers/peft/accelerate) --
run `uv sync --extra train` (05-infrastructure.md §3). Underlying import
error: No module named 'accelerate'
```

## Exit codes

The same four codes apply to every subcommand, including `run`
([docs/spec/01-architecture.md §4.4](spec/01-architecture.md)):

| Code | Meaning | Verified trigger |
| :-: | :--- | :--- |
| `0` | success | a completed `tuner run` / stage invocation |
| `1` | unexpected error | object store unreachable: `ingest: Could not connect to the endpoint URL: "http://localhost:1/tuner-bronze?..."` |
| `2` | config or input-schema validation failure | missing config file (`run: config file not found: configs/does-not-exist.yaml`); missing env vars (`ingest: missing required env var(s): TUNER_S3_ACCESS_KEY, TUNER_S3_SECRET_KEY`); a malformed `--run-id`; an unknown config key or invalid config value; an upstream tier's manifest missing or invalid |
| `3` | zero records survived the stage | e.g. `ingest: zero records ingested across all sources`, `clean: zero records survived cleaning`, `judge: zero records promoted to Gold` — the pipeline should abort, not continue on an empty tier |

`tuner run` propagates the first non-zero exit code its stages produce
verbatim for `2` and `3` (`run: stage 'clean' failed (exit 3)` ⇒ `tuner run`
itself exits `3`, printed as `run: pipeline empty at clean`); anything
outside `{0, 1, 2, 3}` (e.g. a signal-killed subprocess) is normalized to `1`
rather than propagated raw, so `tuner run`'s own exit code is always in that
four-value contract regardless of what a stage subprocess actually returned.

## `tuner run`

```
Usage: tuner run [OPTIONS]

  Run the full pipeline: ingest -> clean -> judge -> tokenize -> train ->
  smoke.

Options:
  --config TEXT  Pipeline config path.  [default: configs/pipeline.yaml]
  --help         Show this message and exit.
```

Generates one run ID, then runs `ingest → clean → judge → tokenize → train →
smoke` in that fixed order, each as a `python -m tuner <stage> --run-id
<id> --config <path>` subprocess with stdout/stderr inherited straight
through — so you see each stage's own output live. It aborts on the first
non-zero exit and prints nothing further. On success it prints the run ID,
the trained model/adapter's storage URI, the smoke transcript's URI, and a
direct MLflow run URL — verified output (run ID and URLs will differ per
run):

```
run: starting pipeline run run-20260903-194852-acde5a
...
run: run_id: run-20260903-194852-acde5a
run: model/adapter: s3://tuner-artifacts/run-20260903-194852-acde5a/model/
run: transcript: s3://tuner-artifacts/run-20260903-194852-acde5a/smoke/transcript.json
run: mlflow run: http://localhost:5000/#/experiments/1/runs/4faa205d2aa44b3f8441f3766e36ce33
```

## `tuner ingest`

```
Usage: tuner ingest [OPTIONS]

  Convert configured sources into Bronze envelopes.

Options:
  --run-id TEXT  Run ID shared across the pipeline.  [required]
  --config TEXT  Pipeline config path.  [default: configs/pipeline.yaml]
  --help         Show this message and exit.
```

Reads every source under `ingest.sources` in the config (`csv`/`jsonl` in the
MVP), writes `tuner-bronze/{run_id}/records-*.jsonl` + manifest. Exit `3` if
every configured source together yields zero ingestable records.

## `tuner clean`

```
Usage: tuner clean [OPTIONS]

  Convert Bronze envelopes into scrubbed, filtered, deduplicated Silver
  records.

Options:
  --run-id TEXT  Run ID shared across the pipeline.  [required]
  --config TEXT  Pipeline config path.  [default: configs/pipeline.yaml]
  --help         Show this message and exit.
```

Reads `tuner-bronze/{run_id}/` (manifest first — absent ⇒ exit `2`), applies
`clean.min_chars`/`max_chars`/`pii` scrubbing/filtering/dedup, writes
`tuner-silver/{run_id}/`. Exit `3` if zero records survive.

## `tuner judge`

```
Usage: tuner judge [OPTIONS]

  Score Silver records with an LLM and promote passing ones to Gold.

Options:
  --run-id TEXT  Run ID shared across the pipeline.  [required]
  --config TEXT  Pipeline config path.  [default: configs/pipeline.yaml]
  --help         Show this message and exit.
```

Reads `tuner-silver/{run_id}/`, scores each record against the
OpenAI-compatible endpoint at `TUNER_JUDGE_BASE_URL`, promotes records
scoring at or above `judge.threshold` to `tuner-gold/{run_id}/`, and opens an
MLflow run (tag `tuner.stage: judge`) logging the score distribution,
promotion rate, and judge model name. Two failure modes worth calling out
specifically, both read from the code (`src/tuner/judge/cli.py`):

- If the fraction of records the judge endpoint fails to score at all
  (`judge_error`) exceeds 10% of records read, the Judge aborts the whole
  run rather than silently promoting a partial, endpoint-degraded batch.
- Exit `3` if zero records reach Gold (every record scored below threshold,
  or too many judge errors to promote anything).

## `tuner tokenize`

```
Usage: tuner tokenize [OPTIONS]

  Map Gold records to the target model's vocabulary; write SafeTensors +
  index_map.

Options:
  --run-id TEXT  Run ID shared across the pipeline.  [required]
  --config TEXT  Pipeline config path.  [default: configs/pipeline.yaml]
  --help         Show this message and exit.
```

Reads `tuner-gold/{run_id}/`, uses the selected `model.adapter`'s tokenizer
and chat template to build `train.safetensors`/`eval.safetensors` (labels
masked to `-100` outside assistant turns) plus `index_map.json` for
row-to-record lineage, split by `tokenize.eval_fraction`. Exit `3` if the
train split ends up empty.

## `tuner train`

```
Usage: tuner train [OPTIONS]

  Fine-tune the selected adapter's base model on tokenized Gold data.

Options:
  --run-id TEXT  Run ID shared across the pipeline.  [required]
  --config TEXT  Pipeline config path.  [default: configs/pipeline.yaml]
  --help         Show this message and exit.
```

Reads `tuner-artifacts/{run_id}/tokens/` (its `index_map.json`'s recorded
`adapter` must match the config's `model.adapter`, else exit `2` — the
tensors were built for a different model), fine-tunes per `train.method`
(`qlora` or `full`), and writes `tuner-artifacts/{run_id}/adapter/` (or
`model/` for `method: full`) plus a registry manifest at
`tuner-registry/{adapter_name}-{run_id}/manifest.json`. Opens an MLflow run
tagged `tuner.stage: trainer` logging all effective hyperparameters, the loss
curve, and the Gold manifest URI (dataset version).

Verified failure mode: requesting `train.method: full` against an adapter
whose `supports_full_ft` is `False` (the shipped default, `gemma-e4b`, is one
such adapter) exits `2` before any storage access — read directly from
`src/tuner/trainer/cli.py`:

```python
if config.train.method == "full" and not adapter.supports_full_ft:
    ...
    return 2
```

QLoRA's 4-bit quantization (`bitsandbytes`) needs a CUDA device; that
requirement gates `method: qlora` specifically. `method: full` runs on
whatever device torch/accelerate find, CPU included — which is what lets the
`tiny-test` CPU-fast path in [00-getting-started.md](00-getting-started.md)
run without a GPU at all.

## `tuner smoke`

```
Usage: tuner smoke [OPTIONS]

  Generate before/after transcripts proving the trained model changed
  behavior.

Options:
  --run-id TEXT  Run ID shared across the pipeline.  [required]
  --config TEXT  Pipeline config path.  [default: configs/pipeline.yaml]
  --help         Show this message and exit.
```

Loads the base model with and without the trained adapter/weights, runs
`smoke.num_prompts` held-out (eval-split) prompts through both, and writes
`tuner-artifacts/{run_id}/smoke/transcript.json` — also attached as an
artifact to the Trainer's MLflow run (found by filtering on **both**
`tuner.run_id` and `tuner.stage: trainer`; exactly one match required, else
exit `2`). Exit `3` if there are zero eval-split records to sample from.
Shares the Trainer's CUDA requirement for `method: qlora` only, for the same
`bitsandbytes` reason.

## `tuner registry list`

```
Usage: tuner registry list [OPTIONS]

  List every registered model version (candidate/promoted/retired).

Options:
  --help  Show this message and exit.
```

Lists every `{model_version}/manifest.json` object under `tuner-registry`,
newest-first, one row per model version: `MODEL_VERSION`, `ADAPTER`,
`CREATED_AT`, `STATUS`, `FINAL_EVAL_LOSS`. Verified output shape:

```
MODEL_VERSION                                 ADAPTER         CREATED_AT             STATUS     FINAL_EVAL_LOSS
tiny-test-run-20260903-194852-acde5a          tiny-test       2026-09-03T19:49:19Z   candidate  1.4921988248825073
```

A manifest that fails schema validation is reported by its object key
(`<key> INVALID`) rather than silently dropped — `list` is a diagnostic tool
and, by design, always exits `0` (per
[docs/spec/03-components/registry.md](spec/03-components/registry.md), "must
not die on one bad object"). `show`, `promote`, and `rollback` are specified
in the same document but are explicitly out of MVP scope — not implemented
in this CLI yet.
