# Tuner — Enterprise Fine-Tuning Pipeline

A modular, cloud-native pipeline that turns raw enterprise data into fine-tuned LLMs with full auditability: every model version traces back to the exact scored dataset, cleaning rules, and experiment that produced it. Text-first, multimodal-ready. The fine-tune target model is pluggable via a model-adapter layer; the initial default is Google Gemma E4B.

```
Data Source ──> Ingestor ──> Cleaner ──> Judge ──> Tokenizer ──> Trainer ──> Registry ──> Inference Engine
                (Bronze)     (Silver)    (Gold)    (Artifact)    (Artifact)  (Registry)   (serving)
```

Each stage is a stateless CLI that reads from one object-storage tier (MinIO locally, S3-compatible in the cloud) and writes to the next, coordinated by run ID. See [docs/spec/01-architecture.md](docs/spec/01-architecture.md) for the full system design.

## Status

The MVP slice is implemented and passing its own test suite end to end (T01–T14 of the build plan); one hardening task remains (T15: a real-GPU training run plus the nightly slow lane) before the pipeline is considered fully verified. See [docs/spec/07-build-plan.md](docs/spec/07-build-plan.md) for exactly what that covers.

## Documentation

`docs/` holds two things side by side — see [docs/README.md](docs/README.md) for the full map:

- **Top-level `docs/*.md`** — the user/operator guide: how to install, configure, run, and troubleshoot Tuner. Use it if you want to *use* the pipeline. Not yet written beyond this map — see `docs/README.md`'s "forthcoming" list.
- **[docs/spec/](docs/spec/00-product-scope.md)** — the normative engineering specification the implementation is built from: architecture, data contracts, per-component specs, the build plan, and the git/review workflow. Start there if you want to *extend* the pipeline.

The original architecture spec this doc set derives from is [SAS.md](SAS.md).

## MVP scope

One command, `tuner run`, ingests fixture data and ends with a trained QLoRA adapter plus a smoke-test transcript, with the run logged in MLflow — reproducibly, under the IAM matrix, with zero pickle artifacts. Out of MVP scope: model serving/canary, Kubernetes/Kubeflow, registry lifecycle beyond `candidate`, SQL/PDF/API ingestion, multimodal. See [docs/spec/00-product-scope.md §3](docs/spec/00-product-scope.md) for the full exclusion list.

## Quick start

```bash
cp .env.example .env                 # fill in HF_TOKEN, judge endpoint
docker compose up -d minio minio-init mlflow
uv run tuner run --config configs/pipeline.yaml     # full pipeline, prints run ID
# MinIO console: http://localhost:9001  ·  MLflow: http://localhost:5000
```

GPU stages (`train`, `smoke`) may run from a host venv if Docker GPU passthrough isn't set up — same commands, same env vars ([docs/spec/05-infrastructure.md §3](docs/spec/05-infrastructure.md)).

## Contributing

If you're implementing a build-plan task, read [CLAUDE.md](CLAUDE.md) first — it fixes the conventions (spec authority, hard rules, tooling, git workflow) for anyone or anything writing code in this repo.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
