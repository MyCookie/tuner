# Tuner Architecture

Authoritative architecture reference for the Enterprise Fine-Tuning Pipeline (Tuner). This document also fixes the **glossary** — the canonical names for buckets, environment variables, config keys, CLI commands, and identifiers. Every other document uses these names verbatim; if a name here conflicts with a name elsewhere, this document wins.

Related docs: [Product scope](00-product-scope.md) · [Data contracts](02-data-contracts.md) · [Component specs](03-components/) · [Model adapters](04-model-adapters.md) · [Infrastructure](05-infrastructure.md)

---

## 1. System overview

Tuner is a decoupled pipeline of stateless stages. Each stage:

1. Is invoked as a CLI (`tuner <stage> --run-id <RUN_ID> --config <path>`).
2. Reads its input **only** from object storage (one medallion tier), validates it against the schema in [02-data-contracts.md](02-data-contracts.md).
3. Writes its output **only** to object storage (the next tier), plus a tier **manifest**.
4. Holds no state between invocations. All coordination happens through storage and the orchestrator.

This shape is deliberately Kubeflow-compatible: a stage that works as a local CLI against MinIO becomes a Kubeflow Pipelines (KFP) component against S3 without code changes — only packaging changes ([05-infrastructure.md §4](05-infrastructure.md)).

### 1.1 Full pipeline flow

```
Data Source ──> Ingestor ──> Cleaner ──> Judge ──> Tokenizer ──> Trainer ──> Registry ──> Inference Engine
                (Bronze)     (Silver)    (Gold)    (Artifact)    (Artifact)  (Registry)   (serving)
                                                                     │
                                                                     └──> MLflow (params, metrics, loss, dataset version)
```

Post-training validation (MVP) is performed by the **Smoke-test** component, which reads the trained adapter from the Artifact tier and writes a before/after transcript back to it.

### 1.2 Stage-to-tier map

| Stage | Reads | Writes | Spec |
| :--- | :--- | :--- | :--- |
| Ingestor | external sources | `tuner-bronze` | [ingestor.md](03-components/ingestor.md) |
| Cleaner | `tuner-bronze` | `tuner-silver` | [cleaner.md](03-components/cleaner.md) |
| Judge | `tuner-silver` | `tuner-gold` | [judge.md](03-components/judge.md) |
| Tokenizer | `tuner-gold` | `tuner-artifacts` | [tokenizer.md](03-components/tokenizer.md) |
| Trainer | `tuner-artifacts` | `tuner-artifacts`, `tuner-registry` | [trainer.md](03-components/trainer.md) |
| Smoke-test | `tuner-artifacts` | `tuner-artifacts` | [smoke-test.md](03-components/smoke-test.md) |
| Registry ops | `tuner-registry` | `tuner-registry` | [registry.md](03-components/registry.md) |
| Inference Engine | `tuner-registry` | — (serves traffic) | [inference.md](03-components/inference.md) |

The IAM matrix in [05-infrastructure.md §5](05-infrastructure.md) enforces exactly this table: a stage gets read on its input bucket and write on its output bucket, nothing else.

---

## 2. Orchestration

Two orchestrators drive the same stage CLIs; stages never know which one invoked them.

- **MVP — local driver:** `tuner run --config configs/pipeline.yaml` executes the stages in order as subprocesses, aborting on the first non-zero exit code. It generates the run ID, passes it to every stage, and prints the final artifact locations. The driver is intentionally dumb: no retries, no partial resume in MVP (re-running a stage manually with the same run ID is the recovery path — stages are idempotent, see §5.3).
- **Production (Phase 3) — Kubeflow Pipelines:** each stage image becomes a KFP component; the DAG mirrors §1.1. Specified in [05-infrastructure.md §4](05-infrastructure.md); not built in MVP.

---

## 3. Repository layout

```
tuner/
├── CLAUDE.md                  # conventions for implementing agents
├── SAS.md                     # original architecture specification
├── docs/                      # this document set
├── pyproject.toml             # single package: tuner (managed with uv)
├── docker-compose.yaml        # MinIO + MLflow + stage images (05-infrastructure.md)
├── docker/                    # Dockerfiles: docker/base.Dockerfile, docker/<stage>.Dockerfile
├── configs/
│   └── pipeline.yaml          # default pipeline config (§6)
├── fixtures/                  # committed synthetic test data (06-testing.md)
├── scripts/
│   └── bootstrap_minio.py     # creates buckets + per-stage credentials
├── src/tuner/
│   ├── core/
│   │   ├── config.py          # config loading & validation (§6)
│   │   ├── storage.py         # StorageClient — the only S3/MinIO access path (§5.1)
│   │   ├── schemas.py         # pydantic models for all contracts (02-data-contracts.md)
│   │   ├── manifest.py        # tier-manifest read/write helpers
│   │   └── ids.py             # run-ID/record-ID generation + canonical_hash() (02 §general rules)
│   ├── models/
│   │   ├── base.py            # ModelAdapter interface (04-model-adapters.md)
│   │   ├── registry.py        # adapter lookup by short name
│   │   └── gemma_e4b.py       # default adapter
│   ├── ingestor/              # one package per stage; each has cli.py with main()
│   ├── cleaner/
│   ├── judge/
│   ├── tokenizer/
│   ├── trainer/
│   ├── smoke/
│   ├── registry_ops/          # `tuner registry ...` subcommands
│   └── cli.py                 # `tuner` entrypoint dispatching to stage CLIs + `tuner run`
└── tests/
    ├── unit/
    ├── integration/           # requires MinIO (docker compose)
    └── e2e/                   # steel-thread test (06-testing.md)
```

---

## 4. Glossary — canonical names

### 4.1 Buckets

| Bucket | Tier | Content |
| :--- | :--- | :--- |
| `tuner-bronze` | Bronze | raw records in metadata envelope |
| `tuner-silver` | Silver | cleaned Multimodal Contract records |
| `tuner-gold` | Gold | judged/filtered records |
| `tuner-artifacts` | Artifact | tensors, adapters, transcripts |
| `tuner-registry` | Registry | model version manifests |
| `tuner-mlflow` | (infra) | MLflow artifact store — server-internal, no stage touches it directly |
| `tuner-assets` | (Phase 4) | binary media referenced by multimodal records |

Object layout inside each tier bucket: `{run_id}/records-{NNNNN}.jsonl` (5-digit zero-padded shard index, `records-00000.jsonl` when unsharded) and `{run_id}/manifest.json`. Artifact/registry layouts are defined in [02-data-contracts.md §5](02-data-contracts.md).

### 4.2 Identifiers

- **Run ID**: `run-{YYYYMMDD}-{HHMMSS}-{6 lowercase hex}` (UTC), e.g. `run-20260720-142201-a3f9c2`. Generated once by the orchestrator, passed to every stage as `--run-id`, and threading through every manifest, MLflow run tag (`tuner.run_id`), and registry entry.
- **Record ID**: UUIDv4, assigned by the Ingestor at Bronze creation and **preserved unchanged** through Silver and Gold. Lineage from tensor row back to source record flows: `index_map` → record ID → Bronze envelope → source URI.
- **Model version**: `{adapter_name}-{run_id}`, e.g. `gemma-e4b-run-20260720-142201-a3f9c2`.

### 4.3 Environment variables

Secrets and endpoints only — everything else lives in the config file (§6).

| Variable | Used by | Meaning |
| :--- | :--- | :--- |
| `TUNER_S3_ENDPOINT` | all stages | object-store endpoint URL (MinIO locally; unset ⇒ AWS default) |
| `TUNER_S3_ACCESS_KEY` / `TUNER_S3_SECRET_KEY` | all stages | per-stage scoped credentials |
| `TUNER_S3_REGION` | all stages | region (default `us-east-1`) |
| `MLFLOW_TRACKING_URI` | judge, trainer, smoke | MLflow server URL |
| `TUNER_JUDGE_BASE_URL` | judge | OpenAI-compatible endpoint base URL |
| `TUNER_JUDGE_API_KEY` | judge | key for that endpoint (dummy value ok for local servers) |
| `HF_TOKEN` | tokenizer, trainer, smoke | Hugging Face token for gated models |

### 4.4 CLI

One console script, `tuner`, with subcommands: `ingest`, `clean`, `judge`, `tokenize`, `train`, `smoke`, `run`. Common options on every subcommand: `--run-id` (required except `run`, which generates it), `--config` (default `configs/pipeline.yaml`).

**Exit codes (all stages):** `0` success · `1` unexpected error · `2` config or input-schema validation failure · `3` zero records survived the stage (pipeline should abort).

---

## 5. Core abstractions

### 5.1 StorageClient (`tuner/core/storage.py`)

The only path to object storage. Wraps `boto3` with: endpoint/credentials from the §4.3 env vars, and helpers `read_jsonl(bucket, prefix)` (iterator over all record shards), `write_jsonl(bucket, key, records)`, `read_json` / `write_json`, `upload_dir` / `download_dir` (for adapter directories). Because configuration is entirely env-driven, the same code targets MinIO locally and S3/compatible stores in the cloud (SAS §4.1). Stages never import `boto3` directly.

### 5.2 Schemas (`tuner/core/schemas.py`)

Pydantic v2 models are the executable form of [02-data-contracts.md](02-data-contracts.md). Each stage validates every input record on read (fail fast, exit 2 on first invalid record) and constructs outputs through these models. Doc 02 is normative; `schemas.py` implements it.

### 5.3 Idempotency contract

Re-running a stage with the same run ID first deletes `{run_id}/` under its **output** prefix, then rewrites it. A stage never touches any other run's data or any tier other than its designated output. This makes "re-run the failed stage" the universal recovery procedure.

---

## 6. Configuration

Single YAML file (default `configs/pipeline.yaml`), validated by a pydantic model in `tuner/core/config.py` (unknown keys are errors). Canonical keys:

```yaml
model:
  adapter: gemma-e4b            # model-adapter registry key (04-model-adapters.md)

ingest:
  sources:                      # list; each item is one source
    - type: csv                 # csv | jsonl (MVP); sql | pdf | api (later phases)
      uri: fixtures/support_dialogs.csv
      mapping:                  # csv only: column -> conversation role
        prompt_column: question
        response_column: answer
        system_column: null     # optional

clean:
  min_chars: 20                 # drop conversations with fewer total text chars
  max_chars: 32000
  pii: [email, phone]           # enabled scrubbers

judge:
  model: ""                     # judge model name at TUNER_JUDGE_BASE_URL (empty ⇒ fail at startup)
  threshold: 0.7                # min normalized score for Gold promotion
  max_concurrency: 4
  max_retries: 3

tokenize:
  max_seq_len: null             # null ⇒ use adapter default
  eval_fraction: 0.1            # deterministic split by record-ID hash

train:
  method: qlora                 # qlora | full (full only if adapter allows, 04 §2)
  hyperparameters: {}           # overrides merged onto adapter defaults (04 §3)
  mlflow_experiment: tuner

smoke:
  num_prompts: 8                # eval-split prompts to run
  max_new_tokens: 256
```

Precedence: adapter defaults < config file < CLI flags. Secrets never appear in config files.

---

## 7. MLflow integration

- One MLflow **run** per pipeline run, created by the Trainer, named by run ID, tagged `tuner.run_id`, `tuner.model_version`, `tuner.adapter`, `tuner.stage: trainer`.
- **Every** Tuner-created MLflow run carries `tuner.run_id` and `tuner.stage` (`judge` | `trainer`); since multiple stages share a run ID, the pair `(tuner.run_id, tuner.stage)` is the unique lookup key — anything that must find a specific stage's run (e.g. the Smoke-test attaching to the Trainer's run) filters on **both** tags, never on `tuner.run_id` alone.
- Trainer logs: all effective hyperparameters, loss curve, Gold manifest URI (dataset version), adapter artifact path.
- Judge logs (to the same experiment, as its own run tagged with the run ID): score distribution, promotion rate, judge model name.
- Smoke-test appends its transcript as an artifact to the Trainer's run.

**MVP scope:** everything in this document except §2's KFP orchestrator and the Inference Engine row of §1.2 is in the MVP. Phase 4 adds `tuner-assets` and multimodal fields, which are already reserved in the schemas.
