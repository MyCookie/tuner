# Tuner — Enterprise Fine-Tuning Pipeline

A modular, cloud-native pipeline that turns raw enterprise data into fine-tuned LLMs with full auditability: every model version traces back to the exact scored dataset, cleaning rules, and experiment that produced it. Text-first, multimodal-ready. The fine-tune target model is pluggable via a model-adapter layer; the initial default is Google Gemma E4B.

```
Data Source ──> Ingestor ──> Cleaner ──> Judge ──> Tokenizer ──> Trainer ──> Registry ──> Inference Engine
                (Bronze)     (Silver)    (Gold)    (Artifact)    (Artifact)  (Registry)   (serving)
```

Each stage is a stateless CLI that reads from one object-storage tier (MinIO locally, S3-compatible in the cloud) and writes to the next, coordinated by run ID. See [docs/01-architecture.md](docs/01-architecture.md) for the full system design.

## Status

This repository currently contains the **engineering specification**, not yet the implementation. The doc set in `docs/` is normative and complete; code is built one task at a time from [docs/07-build-plan.md](docs/07-build-plan.md), each task gated by its test suite. Nothing in `src/` exists until a task adds it — until then this README describes the target shape, not the current tree.

## Documentation

Start at [docs/00-product-scope.md](docs/00-product-scope.md) and read in order:

1. [00-product-scope.md](docs/00-product-scope.md) — product definition, delivery phases, SAS traceability
2. [01-architecture.md](docs/01-architecture.md) — system design and the canonical glossary (buckets, env vars, config keys, run-ID format)
3. [02-data-contracts.md](docs/02-data-contracts.md) — Bronze/Silver/Gold/Artifact schemas (normative — wins over code)
4. [03-components/](docs/03-components/) — per-stage specs (ingestor, cleaner, judge, tokenizer, trainer, smoke-test, registry, inference)
5. [04-model-adapters.md](docs/04-model-adapters.md) — pluggable fine-tune target model layer
6. [05-infrastructure.md](docs/05-infrastructure.md) — Docker Compose, MinIO, IAM matrix, container hardening
7. [06-testing.md](docs/06-testing.md) — test strategy, fixtures, CI lanes
8. [07-build-plan.md](docs/07-build-plan.md) — ordered, one-task-per-session implementation plan
9. [08-test-specs/](docs/08-test-specs/README.md) — normative test cases, tagged by ID and traced to build tasks
10. [09-git-workflow.md](docs/09-git-workflow.md) — branching, commit, and merge-gate rules

The original architecture spec this doc set derives from is [SAS.md](SAS.md).

## MVP scope

One command, `tuner run`, will ingest fixture data and end with a trained QLoRA adapter plus a smoke-test transcript, with the run logged in MLflow — reproducibly, under the IAM matrix, with zero pickle artifacts. Out of MVP scope: model serving/canary, Kubernetes/Kubeflow, registry lifecycle beyond `candidate`, SQL/PDF/API ingestion, multimodal. See [docs/00-product-scope.md §3](docs/00-product-scope.md) for the full exclusion list.

## Quick start (once implemented)

```bash
cp .env.example .env                 # fill in HF_TOKEN, judge endpoint
docker compose up -d minio minio-init mlflow
uv run tuner run --config configs/pipeline.yaml     # full pipeline, prints run ID
# MinIO console: http://localhost:9001  ·  MLflow: http://localhost:5000
```

GPU stages (`train`, `smoke`) may run from a host venv if Docker GPU passthrough isn't set up — same commands, same env vars ([docs/05-infrastructure.md §3](docs/05-infrastructure.md)).

## Contributing

If you're implementing a build-plan task, read [CLAUDE.md](CLAUDE.md) first — it fixes the conventions (spec authority, hard rules, tooling, git workflow) for anyone or anything writing code in this repo.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
