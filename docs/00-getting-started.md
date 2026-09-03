# Getting started

This page takes you from a fresh clone to a completed pipeline run: fixture data
in, a trained model and a before/after transcript out, both visible in MLflow.
It assumes you know roughly what a fine-tuning pipeline does, are comfortable
with Docker and a CLI, but have never touched Tuner before.

Every command below was actually run against this repository while writing
this page (see "What we verified versus what we read" in §5 for the one
part — the real-model, real-GPU path — that goes further than that) —
nothing here is inferred from the spec alone.

## Prerequisites

| Tool | Why | Checked against |
| :--- | :--- | :--- |
| Python 3.11+ | Tuner's runtime | — |
| [`uv`](https://docs.astral.sh/uv/) | dependency/environment management — the only supported way to install and run Tuner | `uv 0.12.9` |
| Docker + the `docker compose` CLI plugin (not the legacy standalone `docker-compose`) | local MinIO (object storage) + MLflow | `Docker 29.2.1` / Compose plugin `v5.0.2` |
| An NVIDIA GPU + drivers (optional) | only needed for `train`/`smoke` against the real default model, `gemma-e4b`; the fixture-scale path below runs entirely on CPU | — |

You do **not** need a GPU to complete this page. You need one only if you go
on to fine-tune the real default model (`gemma-e4b`) instead of the tiny
CPU-sized test model this page uses.

## 1. Install

```bash
git clone <this repository> tuner && cd tuner
uv sync --extra dev
```

`uv sync` alone would leave you without `ruff`/`pytest` — the test toolchain
is an opt-in extra (`dev`), not a default dependency, so CI and local runs
stay lean. `uv sync --extra dev` installs the package plus that extra into a
project-local `.venv`; every command below is then run through `uv run`,
which uses that environment without you having to activate it.

`uv run tuner --help` should now list every subcommand:

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
  registry  Model registry operations (docs/spec/03-components/registry.md).
  run       Run the full pipeline: ingest -> clean -> judge -> tokenize...
  smoke     Generate before/after transcripts proving the trained model
            changed behavior.
  tokenize  Map Gold records to the target model's vocabulary; write...
  train     Fine-tune the selected adapter's base model on tokenized Gold
            data.
```

(`smoke` and `train` skip click's usual summary truncation entirely — their
listing text comes from a static string used precisely because their real
modules aren't imported just to list them, explained just below — so it
wraps to a second line instead of being cut off with `...`. `registry`'s
one-liner happens to fit inside the truncation limit exactly, so truncation
runs but changes nothing.)

You'll see a one-line warning above that (`[transformers] PyTorch was not
found...`) — harmless at this point. `train` and `smoke` need real
torch/transformers/peft/accelerate, gated behind a separate `train` extra
(§5 below) precisely so that `tuner --help`, `ingest`, `clean`, `judge`, and
`tokenize` never have to pull in a GPU-oriented dependency stack just to run.
Calling `train` or `smoke` before installing that extra fails fast with a
clear message naming the fix, rather than a raw `ModuleNotFoundError`:

```
$ uv run tuner train --help
Error: 'train' needs the `train` extra (torch/transformers/peft/accelerate) -- run `uv sync --extra train` (05-infrastructure.md §3). Underlying import error: No module named 'accelerate'
```

(click prints this as one line — wrapped above only for this page's width.)

## 2. Configure environment

```bash
cp .env.example .env
```

`.env` is git-ignored — it never gets committed, and nothing in
`docker-compose.yaml`, `configs/*.yaml`, or the source ever hard-codes a
credential (a project hard rule; see [CLAUDE.md](../CLAUDE.md)). Open `.env`
and fill in real (even if just locally-invented) values. What each block is
for:

| Block | Used for |
| :--- | :--- |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | MinIO admin bootstrap only — `scripts/bootstrap_minio.py` uses these once to create the per-stage buckets and users below; no stage ever sees them |
| `INGESTOR_S3_*` … `MLFLOW_S3_*` | one scoped credential pair per stage, created by that bootstrap step and wired into each stage's *container* by `docker-compose.yaml` (the IAM matrix — see [01-architecture-overview.md](01-architecture-overview.md)) |
| `TUNER_S3_ENDPOINT` / `TUNER_S3_ACCESS_KEY` / `TUNER_S3_SECRET_KEY` / `TUNER_S3_REGION` | the credentials a stage picks up when you run it **from the host** instead of inside its container — which is exactly what the quick-start path below does. `.env.example`'s own comment says to point these at the `MINIO_ROOT_USER`/`PASSWORD` pair for a local test run, since a single host process walks every tier and no one stage's scoped credentials can do that |
| `MLFLOW_TRACKING_URI` | where the Judge, Trainer, and Smoke-test log to (`http://localhost:5000` locally) |
| `TUNER_JUDGE_BASE_URL` / `TUNER_JUDGE_API_KEY` | your OpenAI-compatible judge endpoint (a local Ollama/vLLM box, a hosted compatible endpoint, etc.) — there is no built-in judge model; you point Tuner at one you run or subscribe to |
| `HF_TOKEN` | Hugging Face token, needed by the Tokenizer/Trainer/Smoke-test to pull the base model |

We never put the actual values we used anywhere in this repo or this guide —
only what each variable is for, per this project's own secrets-in-env-vars-only rule.

## 3. Start local infrastructure

```bash
docker compose up -d minio minio-init mlflow
```

This starts MinIO (S3-compatible object storage), runs `minio-init` once to
create the seven canonical buckets and per-stage users from your `.env`, then
starts MLflow. Confirmed behavior, run twice in a row on the same machine
while writing this page:

- `minio` and `mlflow` come up and pass their healthchecks.
- `minio-init` runs to completion and **exits** (`Container tuner-minio-init-1
  Exited`) — that's success, not a crash; it's a one-shot bootstrap job, not a
  resident service. Its bucket/user creation is idempotent (re-running it is
  safe), but a second `docker compose up -d` still visibly restarts the
  existing `minio-init` container (`Starting` / `Started` / `Exited`) each
  time rather than skipping it silently — it just changes nothing at the end.

Once it settles, two UIs are reachable on `localhost`:

- **MinIO console:** <http://localhost:9001> — log in with `MINIO_ROOT_USER`
  / `MINIO_ROOT_PASSWORD` from your `.env`. This is where you can browse the
  `tuner-bronze` / `tuner-silver` / `tuner-gold` / `tuner-artifacts` /
  `tuner-registry` buckets object-by-object after a run (plus `tuner-mlflow`,
  MLflow's own backing store, and `tuner-assets`, reserved for a future
  multimodal phase and unused today).
- **MLflow UI:** <http://localhost:5000> — every Judge and Trainer run gets
  logged here (params, metrics, the loss curve, and — attached to the
  Trainer's run — the smoke-test transcript).

Both returned HTTP 200 when we checked them directly against this compose
stack.

## 4. Your first run — the CPU-fast path

`tuner run` drives the whole pipeline (ingest → clean → judge → tokenize →
train → smoke) as one command, generating a fresh run ID and aborting on the
first stage that fails. The default `configs/pipeline.yaml` targets the real
default model (`gemma-e4b`, a ~4B-parameter model) and expects a real judge
endpoint — good for production use, not for a first run on a laptop.

For a first run — and for this page, since we ran this path ourselves,
end-to-end, to verify it — use `configs/pipeline.e2e.yaml` instead. It's the
same config the project's own E2E test suite runs
(`tests/e2e/test_steel_thread.py`): it selects the `tiny-test` model adapter
(a 135M-parameter model, CPU-capable, no gating) and `train.method: full`
(skips QLoRA/`bitsandbytes`, which need a CUDA device), so the whole pipeline
runs on CPU alone — no GPU required. Measured end-to-end wall time with
`CUDA_VISIBLE_DEVICES=` unset (no GPU visible): a little over two minutes on
this page's test hardware; expect it to vary with your own CPU and whether
the HF cache is already warm. It's test infrastructure, not a
product config — a real run uses `configs/pipeline.yaml` (or your own copy of
it) with a real judge endpoint and, ordinarily, the real default adapter.

This path also needs the `mock-judge` sidecar — a canned-response
OpenAI-compatible server that exists only for tests/CI, never a real
deployment — and the `train` extra, since `train`/`smoke` are part of `tuner
run`:

```bash
uv sync --extra train
docker compose --profile e2e up -d minio minio-init mlflow mock-judge
```

Then, with your `.env` values exported into the shell (or set inline as
below — `mock-judge` needs its own judge URL/key, distinct from whatever real
endpoint you put in `.env`):

```bash
TUNER_S3_ENDPOINT=http://localhost:9000 \
TUNER_S3_ACCESS_KEY=<your TUNER_S3_ACCESS_KEY from .env> \
TUNER_S3_SECRET_KEY=<your TUNER_S3_SECRET_KEY from .env> \
TUNER_S3_REGION=us-east-1 \
MLFLOW_TRACKING_URI=http://localhost:5000 \
TUNER_JUDGE_BASE_URL=http://localhost:8088 \
TUNER_JUDGE_API_KEY=unused-mock-key \
HF_TOKEN=<your HF_TOKEN from .env> \
uv run tuner run --config configs/pipeline.e2e.yaml
```

### What success looks like

Exit code `0`, and output ending with four lines naming exactly where
everything landed. From an actual run against this stack:

```
run: starting pipeline run run-20260903-194852-acde5a
...
run: run_id: run-20260903-194852-acde5a
run: model/adapter: s3://tuner-artifacts/run-20260903-194852-acde5a/model/
run: transcript: s3://tuner-artifacts/run-20260903-194852-acde5a/smoke/transcript.json
run: mlflow run: http://localhost:5000/#/experiments/1/runs/4faa205d2aa44b3f8441f3766e36ce33
```

(The run ID is generated fresh each time —
`run-{YYYYMMDD}-{HHMMSS}-{6 lowercase hex}`, UTC — so yours will differ. In
between those two lines you'll see each stage's own stdout inherited
straight through: MLflow's own "View run"/"View experiment" links from the
Judge, then the Trainer's training-loss table, then Hugging Face's
`transformers` progress bars for model loading/saving. All of that is normal
and expected.)

Afterward:

- **`uv run tuner registry list`** shows your new candidate model version.
  Verified output shape:

  ```
  MODEL_VERSION                                 ADAPTER         CREATED_AT             STATUS     FINAL_EVAL_LOSS
  tiny-test-run-20260903-194852-acde5a          tiny-test       2026-09-03T19:49:19Z   candidate  1.4921988248825073
  ```
- **MinIO console** (<http://localhost:9001>) — browse `tuner-bronze`,
  `tuner-silver`, `tuner-gold`, `tuner-artifacts`, `tuner-registry` under your
  run ID (or `{adapter}-{run_id}` for the registry) to see each tier's
  records and manifests directly.
- **MLflow UI** (<http://localhost:5000>) — the `run: mlflow run:` URL above
  opens the Trainer's run directly: hyperparameters, the loss curve, and (in
  its artifacts) `smoke/transcript.json`, the before/after transcript. A
  second run in the same experiment, tagged `tuner.stage: judge`, carries the
  score distribution and promotion rate.

Re-running the exact same command is safe — every stage deletes and rewrites
only its own output for that run ID before it writes anything new (the
idempotency contract in
[01-architecture-overview.md](01-architecture-overview.md)); a fresh run ID
is generated each time you invoke `tuner run`, so back-to-back runs simply
coexist rather than collide.

## 5. Running the real pipeline (`gemma-e4b`, GPU)

`uv run tuner run --config configs/pipeline.yaml` is the actual product path:
default adapter `gemma-e4b`, QLoRA training, a real judge endpoint. As shipped,
`configs/pipeline.yaml` has `judge.model: ""` — the Judge stage exits `2`
immediately on an empty model name (`src/tuner/judge/cli.py`), before any
GPU work, so the file isn't runnable until you set a real one. It needs:

- `judge.model` in the config set to a real model name, plus
  `TUNER_JUDGE_BASE_URL` pointed at a real OpenAI-compatible endpoint (not
  `mock-judge`), with a working `TUNER_JUDGE_API_KEY`.
- A CUDA-capable GPU for `train`/`smoke` (QLoRA's 4-bit quantization,
  `bitsandbytes`, requires one) — either via `docker compose`'s
  `trainer`/`smoke` services (which request `nvidia` GPU passthrough), or, if
  that passthrough isn't set up on your box, the sanctioned fallback: run
  those two stages from a host `uv` venv (`uv sync --extra train`, then
  `uv run tuner train ...` / `uv run tuner smoke ...`, same env vars, no code
  or command changes otherwise).
- A valid `HF_TOKEN` (the repository itself is public/ungated, but the
  download still goes through the Hugging Face Hub).

**What we verified versus what we read:** this repository's sandbox does
have a GPU, but no real judge endpoint reachable from it, and a full
`gemma-e4b` fine-tune is a materially bigger, longer job than this page's
CPU-fast path — so we did not run this path end-to-end. What we did verify by
reading the actual source: `tuner.models.registry` resolves `gemma-e4b` to
`GemmaE4BAdapter` (`src/tuner/models/gemma_e4b.py`); the Trainer's
`method: full` + `supports_full_ft: False` combination (which `gemma-e4b`
has) exits `2` before touching storage; and the `TUNER_S3_*`/`MLFLOW_*`/
`HF_TOKEN` env-var contract and CLI flags are identical between this path and
the one above — we exercised those mechanics directly, just against the
smaller model. Treat everything in this section as spec- and code-verified,
not run-verified, until someone runs it on real GPU hardware against a real
judge endpoint (tracked as build-plan task T15,
[docs/spec/07-build-plan.md](spec/07-build-plan.md)).

## Exit codes

Every `tuner` subcommand (including `run`) uses the same four exit codes:

| Code | Meaning | Example we triggered |
| :-: | :--- | :--- |
| `0` | success | the run above |
| `1` | unexpected error | object store unreachable mid-stage: `Could not connect to the endpoint URL: "http://localhost:1/tuner-bronze?..."` |
| `2` | config or input-schema validation failure | missing config file: `run: config file not found: configs/does-not-exist.yaml`; missing env vars: `ingest: missing required env var(s): TUNER_S3_ACCESS_KEY, TUNER_S3_SECRET_KEY` |
| `3` | zero records survived the stage | e.g. every source produced zero ingestable records, or every Silver record was filtered by the Judge |

See [02-cli-reference.md](02-cli-reference.md) for the exit-code behavior of
each individual subcommand.

## Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| `ingest: missing required env var(s): TUNER_S3_ACCESS_KEY, TUNER_S3_SECRET_KEY` (or similar, any stage) | `.env` not created, not filled in, or not exported into your shell | `cp .env.example .env`, fill it in, and export it (or prefix each command as shown above) |
| `...: Could not connect to the endpoint URL: "http://localhost:.../..."` (exit `1`) | MinIO isn't running, or `TUNER_S3_ENDPOINT` points somewhere unreachable | `docker compose up -d minio minio-init mlflow`, then re-check `TUNER_S3_ENDPOINT` |
| `gate: no MinIO at ... — start the stack: docker compose up -d minio minio-init mlflow` | same, surfaced by `./scripts/gate.sh`'s own preflight check | same fix |
| `Error: 'train' needs the \`train\` extra ...` | you called `train`/`smoke` without installing torch/transformers/peft/accelerate | `uv sync --extra train` |
| `train`/`smoke` hang or fail looking for a CUDA device under `method: qlora` | no GPU, and no passthrough into Docker | either enable NVIDIA Docker passthrough, or run just `train`/`smoke` from a host venv per §5 above (`method: full`, as the CPU-fast path uses, needs no GPU at all) |
| `run: pipeline empty at <stage>` (exit `3`) | every record was dropped at that stage (e.g. all filtered by cleaning rules or judge threshold) | check that stage's drop counts in its tier manifest (`{bucket}/{run_id}/manifest.json` in the MinIO console) |

## Next steps

- [Architecture overview](01-architecture-overview.md) — how the pipeline
  fits together and why it's shaped this way.
- [CLI reference](02-cli-reference.md) — every subcommand, flag, and exit
  code.
- [Configuration reference](03-configuration.md) — every `pipeline.yaml` key.
