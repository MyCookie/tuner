# Configuration reference

Every key `configs/pipeline.yaml` accepts, sourced directly from the pydantic
models in `src/tuner/core/config.py` — the actual schema, not a
paraphrase — and cross-checked against the two real config files in this
repository, `configs/pipeline.yaml` (the product default) and
`configs/pipeline.e2e.yaml` (the CPU-fast test/CI config used in
[00-getting-started.md](00-getting-started.md)). If a key here and the
running code ever disagree, trust `config.py` and file it as a doc bug.

## File format and validation

A single YAML file, default path `configs/pipeline.yaml`, loaded and
validated by `PipelineConfig.model_validate(...)` on every pipeline-stage
invocation. Every model in this schema sets `extra="forbid"` — **an unknown
key anywhere in the file is a validation error**, not a silently-ignored
typo. A missing file or a schema violation raises `ConfigError`, which every
CLI entry point turns into exit code `2`, verified directly:

```
$ uv run tuner run --config configs/does-not-exist.yaml
run: config file not found: configs/does-not-exist.yaml
```

## Top-level shape

```yaml
model: {...}       # required
ingest: {...}      # required
clean: {...}       # optional -- defaults shown below
judge: {...}       # optional
tokenize: {...}    # optional
train: {...}       # optional
smoke: {...}       # optional
```

Only `model` and `ingest` are required; every other section falls back to
its own defaults if omitted entirely (each is `Field(default_factory=...)`
in `config.py`). Within a present section, any field you don't set falls
back to that field's own default — sections are not all-or-nothing.

## `model`

| Key | Type | Required | Meaning |
| :--- | :--- | :-: | :--- |
| `adapter` | string | yes | model-adapter registry key — see [01-architecture-overview.md](01-architecture-overview.md#the-model-adapter-abstraction-what-makes-the-fine-tune-target-pluggable) and [docs/spec/04-model-adapters.md](spec/04-model-adapters.md). Ships with `gemma-e4b` (the product default) and `tiny-test` (CPU-sized, test-only). An unknown name fails at stage start with exit `2` and the list of known adapters. |

```yaml
model:
  adapter: gemma-e4b
```

## `ingest`

| Key | Type | Required | Meaning |
| :--- | :--- | :-: | :--- |
| `sources` | list of source configs | yes | one entry per data source, concatenated |

Each entry in `sources`:

| Key | Type | Meaning |
| :--- | :--- | :--- |
| `type` | string | `csv` or `jsonl` in the MVP (`sql`/`pdf`/`api` are reserved names for later phases — naming one today fails with a message naming what's actually supported, not a generic schema error, since this field is validated by the source registry rather than a fixed enum) |
| `uri` | string | source location — a local path (e.g. `fixtures/support_dialogs.csv`) or an `s3://` URI readable via `StorageClient` |
| `mapping` | object, optional | CSV only: column → conversation role. `prompt_column`, `response_column`, `system_column` (all optional strings; `system_column: null` if the source has none) |

```yaml
ingest:
  sources:
    - type: csv
      uri: fixtures/support_dialogs.csv
      mapping:
        prompt_column: question
        response_column: answer
        system_column: null
    - type: jsonl
      uri: fixtures/extra_dialogs.jsonl
```

(That two-source shape is taken directly from `configs/pipeline.e2e.yaml` —
multiple sources of different types combine into one Bronze output.)

## `clean`

| Key | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `min_chars` | int (≥0) | `20` | drop conversations with fewer total text characters across all turns |
| `max_chars` | int (≥0) | `32000` | drop conversations with more |
| `pii` | list of `"email"` \| `"phone"` | `["email", "phone"]` | enabled PII scrubbers. Any other string is rejected at config-load time (exit `2`) rather than reaching the cleaner and failing later — the code comment in `config.py` is explicit that this is deliberately a closed `Literal` set, not an open `list[str]`, precisely so a typo'd or not-yet-implemented scrubber name fails fast |

```yaml
clean:
  min_chars: 20
  max_chars: 32000
  pii: [email, phone]
```

## `judge`

| Key | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `model` | string | `""` | judge model name to request at `TUNER_JUDGE_BASE_URL`. An empty string fails at Judge startup by design — there is no default judge model |
| `threshold` | float, `0.0`–`1.0` | `0.7` | minimum normalized score for Gold promotion |
| `max_concurrency` | int (>0) | `4` | concurrent in-flight judge requests |
| `max_retries` | int (≥0) | `3` | retries per record before counting it as a `judge_error` |

```yaml
judge:
  model: ""              # must be set for a real run
  threshold: 0.7
  max_concurrency: 4
  max_retries: 3
```

The judge endpoint itself (which server, which credential) is **not** a
config key — it's environment-only (`TUNER_JUDGE_BASE_URL`,
`TUNER_JUDGE_API_KEY`, below), so a judge model *name* can live in
version-controlled config while the endpoint it's fetched from stays a
per-environment secret.

## `tokenize`

| Key | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `max_seq_len` | int or `null` | `null` | `null` ⇒ use the selected adapter's own default (e.g. `gemma-e4b`'s is `4096`) |
| `eval_fraction` | float, `0.0`–`1.0` | `0.1` | fraction of records held out for the eval split — assigned deterministically by hashing each record's ID, so the same run ID always produces the same split |

```yaml
tokenize:
  max_seq_len: null
  eval_fraction: 0.1
```

## `train`

| Key | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `method` | `"qlora"` \| `"full"` | `"qlora"` | fine-tuning method. `full` is rejected with exit `2` unless the selected adapter's `supports_full_ft` is `True` (`gemma-e4b`'s is `False`; `tiny-test`'s is `True`) |
| `hyperparameters` | object | `{}` | overrides merged field-by-field onto the adapter's own `training_defaults` (precedence below) |
| `mlflow_experiment` | string | `"tuner"` | MLflow experiment name this run's Judge/Trainer runs are logged under |

```yaml
train:
  method: qlora
  hyperparameters: {}
  mlflow_experiment: tuner
```

`hyperparameters` is validated against the adapter's own default field names
at merge time, not just accepted as a free-form dict: an override key that
isn't one of the adapter's `TrainingDefaults` fields (a typo, most often)
raises `ConfigError` naming the unknown key(s), rather than merging in
silently and being ignored. `configs/pipeline.e2e.yaml` overrides just one
field this way:

```yaml
train:
  method: full
  hyperparameters:
    epochs: 1
```

## `smoke`

| Key | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `num_prompts` | int (>0) | `8` | eval-split prompts to run through the before/after comparison |
| `max_new_tokens` | int (>0) | `256` | generation length cap for both the base and tuned outputs |

```yaml
smoke:
  num_prompts: 8
  max_new_tokens: 256
```

## Precedence: adapter defaults < config file < CLI flags

Three layers can determine an effective value, lowest priority first:

1. **Adapter defaults** — each `ModelAdapter` ships a `training_defaults`
   (`TrainingDefaults`, e.g. learning rate, LoRA rank, batch size) and a
   `max_seq_len`. These are the model-specific starting points (documented
   per-adapter in
   [docs/spec/04-model-adapters.md §3](spec/04-model-adapters.md)).
2. **Config file** — `train.hyperparameters` and `tokenize.max_seq_len`
   override the adapter defaults field-by-field when set; leaving
   `max_seq_len: null` or `hyperparameters: {}` means "use the adapter's
   value as-is."
3. **CLI flags** — outrank both. In the current CLI this only applies to
   `--run-id`/`--config` themselves (there's no `--learning-rate`-style flag
   yet); the precedence rule exists in the architecture spec for the general
   case and is exercised today mainly through layers 1–2.

Nothing below the adapter layer is ever silently invented: an unset
`tokenize.max_seq_len` doesn't mean "some pipeline-wide default," it means
"defer to whichever adapter `model.adapter` names" — so the same config
section behaves differently, correctly, across different adapters.

## Environment variables (`.env`)

Secrets and per-environment endpoints live in environment variables only,
never in `pipeline.yaml` or any other committed file — see
[CLAUDE.md](../CLAUDE.md)'s hard rule 3 and
[docs/spec/01-architecture.md §4.3](spec/01-architecture.md). `.env.example`
documents every variable name; copy it to `.env` (git-ignored) and fill in
real values — **this page names only what each variable is for, never a
value**, including our own local dev values while verifying these pages.

| Variable | Used by | Meaning |
| :--- | :--- | :--- |
| `TUNER_S3_ENDPOINT` | all stages | object-store endpoint URL (MinIO locally; unset ⇒ AWS default) |
| `TUNER_S3_ACCESS_KEY` / `TUNER_S3_SECRET_KEY` | all stages | credentials for that endpoint — per-stage scoped when a stage runs in its own container, or the admin pair when running a whole pipeline from one host process (see [00-getting-started.md §2](00-getting-started.md)) |
| `TUNER_S3_REGION` | all stages | region (default `us-east-1`) |
| `MLFLOW_TRACKING_URI` | judge, trainer, smoke | MLflow server URL |
| `TUNER_JUDGE_BASE_URL` | judge | OpenAI-compatible chat-completions endpoint base URL |
| `TUNER_JUDGE_API_KEY` | judge | key for that endpoint (a dummy value is fine for a local, unauthenticated server) |
| `HF_TOKEN` | tokenizer, trainer, smoke | Hugging Face token, needed to pull the base model |

The per-stage credential pairs in `.env.example`
(`INGESTOR_S3_ACCESS_KEY`/`_SECRET_KEY` through `MLFLOW_S3_*`) aren't read by
the CLI directly — `docker-compose.yaml` maps each one into its matching
container's `TUNER_S3_ACCESS_KEY`/`TUNER_S3_SECRET_KEY`, so each stage
container only ever holds the one credential its own IAM policy grants (the
matrix in [docs/spec/05-infrastructure.md §5](spec/05-infrastructure.md)).
`MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` are bootstrap-only, consumed once by
`scripts/bootstrap_minio.py` to create everything else.
