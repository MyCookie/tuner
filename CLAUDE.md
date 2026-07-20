# EFTP — Conventions for Implementing Agents

You are building the Enterprise Fine-Tuning Pipeline from the specs in `docs/`. The specs are normative; do not improvise architecture. If a spec is ambiguous or wrong, stop and say so rather than guessing.

## Read this first

- Your task comes from [docs/07-build-plan.md](docs/07-build-plan.md). Do exactly one task per session; its "Accept" + "Verify" lines are the definition of done, together with tests and clean lint.
- Before touching **any** record, manifest, or schema code, read [docs/02-data-contracts.md](docs/02-data-contracts.md). It wins over code.
- All canonical names (buckets, env vars, config keys, run-ID format, exit codes) live in [docs/01-architecture.md §4](docs/01-architecture.md). Never invent a name variant.

## Hard rules

1. **Object storage only via `eftp.core.storage.StorageClient`.** No direct `boto3` imports outside it.
2. **No pickle, ever.** No `torch.load`/`torch.save` of raw tensors, no `.bin` weights — SafeTensors only. CI greps for violations.
3. **Secrets via env vars only** (the `EFTP_*`/`MLFLOW_*`/`HF_TOKEN` set). Never in configs, code, logs, or compose files.
4. **Stages are stateless and idempotent:** delete own output prefix for the run ID, rewrite, write the manifest last. Never write outside your stage's output bucket (the IAM matrix will reject it anyway).
5. **Validate inputs fail-fast** with the pydantic models in `eftp/core/schemas.py`; exit codes: 0 ok / 1 error / 2 config-or-validation / 3 zero-records.
6. **Model specifics live only in model adapters** (`docs/04-model-adapters.md`). A stage branching on an adapter's name is a bug.

## Tooling (fixed — do not churn)

- Python 3.11+, **uv** for env/deps (`uv sync`, `uv run ...`), src-layout single package `eftp`.
- **ruff** for lint + format (`uv run ruff check --fix . && uv run ruff format .`).
- **pytest**; markers: default = unit, `-m integration` needs `docker compose up -d minio minio-init mlflow`, `-m e2e` is the full steel thread.
- CLI framework: **click**, single `eftp` entrypoint.

## Running things locally

```bash
cp .env.example .env                 # fill in HF_TOKEN, judge endpoint
docker compose up -d minio minio-init mlflow
uv run eftp run --config configs/pipeline.yaml     # full pipeline, prints run ID
# MinIO console: http://localhost:9001  ·  MLflow: http://localhost:5000
```

GPU stages (`train`, `smoke`) may run from a host venv if Docker GPU passthrough isn't set up — same commands, same env vars (docs/05 §3).

## Style

- Match existing code; comments only for non-obvious constraints.
- Type hints everywhere; pydantic v2 models for anything that crosses a process/storage boundary.
- Tests assert contracts (schemas, manifests, counts, exit codes) per docs/06 — prefer table-driven cases.
- Fixture data is synthetic only; never commit real data or credentials.
