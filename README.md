# Tuner — Enterprise Fine-Tuning Pipeline

A modular, cloud-native pipeline that turns raw enterprise data into fine-tuned LLMs with full auditability: every model version traces back to the exact scored dataset, cleaning rules, and experiment that produced it. Text-first, multimodal-ready. The fine-tune target model is pluggable via a model-adapter layer; the initial default is Google Gemma E4B.

```
Data Source ──> Ingestor ──> Cleaner ──> Judge ──> Tokenizer ──> Trainer ──> Registry ──> Inference Engine
                (Bronze)     (Silver)    (Gold)    (Artifact)    (Artifact)  (Registry)   (serving)
```

Each stage is a stateless CLI that reads from one object-storage tier (MinIO locally, S3-compatible in the cloud) and writes to the next, coordinated by run ID. See [docs/spec/01-architecture.md](docs/spec/01-architecture.md) for the full system design.

## Status

The MVP slice is implemented and fully verified (T01–T15 of the build plan): its test suite passes end to end, and T15's hardening pass ran the real product path for real — `google/gemma-4-E4B-it` QLoRA fine-tuned on a genuine CUDA GPU, the adapter reloaded independently via PEFT, the nightly slow lane (container structure + a 120,000-record scale smoke) green, and the IAM matrix and pickle-free artifact guarantees reverified against the live stack. See [docs/spec/07-build-plan.md](docs/spec/07-build-plan.md) for exactly what that covers.

## Documentation

`docs/` holds two things side by side — see [docs/README.md](docs/README.md) for the full map:

- **Top-level `docs/*.md`** — the user/operator guide: how to install, configure, run, and troubleshoot Tuner. Use it if you want to *use* the pipeline. Start at [docs/00-getting-started.md](docs/00-getting-started.md); [docs/README.md](docs/README.md) links every page (architecture overview, CLI reference, configuration, a guide per pipeline stage, and operations/troubleshooting).
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

This is the real product path — default adapter `gemma-e4b`, QLoRA — and needs a
CUDA GPU (`train`/`smoke` exit 2 without one, naming the fallback below). For a
CPU-only, fixture-scale dry run first, see
[docs/00-getting-started.md](docs/00-getting-started.md) instead.

GPU stages (`train`, `smoke`) ran via the host-`uv`-venv fallback for T15's own
real-GPU verification (`uv sync --extra train`, then the same `tuner run` command
above — no Docker GPU passthrough needed on this box). `docker compose`'s own
`trainer`/`smoke` services (which request `nvidia` GPU passthrough) are spec'd but
not yet run-verified end to end; either path uses the same commands and env vars
([docs/spec/05-infrastructure.md §3](docs/spec/05-infrastructure.md)).

## Contributing

If you're implementing a build-plan task, read [CLAUDE.md](CLAUDE.md) first — it fixes the conventions (spec authority, hard rules, tooling, git workflow) for anyone or anything writing code in this repo.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
