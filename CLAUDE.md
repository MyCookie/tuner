# Tuner — Conventions for Implementing Agents

You are building the Enterprise Fine-Tuning Pipeline from the specs in `docs/`. The specs are normative; do not improvise architecture. If a spec is ambiguous or wrong, stop and say so rather than guessing.

## Read this first

- Your task comes from [docs/07-build-plan.md](docs/07-build-plan.md). Do exactly one task per session, and finish it against that document's ten-point **Definition of done**: its "Suite" + "Accept" + "Verify" lines, clean lint, and a review gate that ends at *an independent reviewer merged it* — not at *my tests pass* ([docs/10-code-review.md](docs/10-code-review.md)).
- Tests are specified, not improvised: implement every case in your task's suite exactly as listed in [docs/08-test-specs/](docs/08-test-specs/README.md), docstring-tagged with its case ID. Coverage gates: ≥90 % branch globally, 100 % on the listed pure-logic modules.
- Before touching **any** record, manifest, or schema code, read [docs/02-data-contracts.md](docs/02-data-contracts.md). It wins over code.
- All canonical names (buckets, env vars, config keys, run-ID format, exit codes) live in [docs/01-architecture.md §4](docs/01-architecture.md). Never invent a name variant.

## Hard rules

1. **Object storage only via `tuner.core.storage.StorageClient`.** No direct `boto3` imports outside it.
2. **No pickle, ever.** No `torch.load`/`torch.save` of raw tensors, no `.bin` weights — SafeTensors only. CI greps for violations.
3. **Secrets via env vars only** (the `TUNER_*`/`MLFLOW_*`/`HF_TOKEN` set). Never in configs, code, logs, or compose files.
4. **Stages are stateless and idempotent:** delete own output prefix for the run ID, rewrite, write the manifest last. Never write outside your stage's output bucket (the IAM matrix will reject it anyway).
5. **Validate inputs fail-fast** with the pydantic models in `tuner/core/schemas.py`; exit codes: 0 ok / 1 error / 2 config-or-validation / 3 zero-records.
6. **Model specifics live only in model adapters** (`docs/04-model-adapters.md`). A stage branching on an adapter's name is a bug.
7. **Never weaken a test to make it pass.** A test that looks wrong is a spec question — check [docs/08-test-specs/](docs/08-test-specs/README.md) and the component spec, and flag conflicts instead of editing the test.

## Git & review (full rules: [docs/09-git-workflow.md](docs/09-git-workflow.md), [docs/10-code-review.md](docs/10-code-review.md))

- Never commit to `main`. One branch per build task (`feat/tNN-<slug>`), branched from up-to-date `main`.
- Atomic commits, Conventional Commits format (`feat(cleaner): ...`), code + its tests in the same commit, unit tests passing at every commit.
- The gate is one command: `./scripts/gate.sh` (ruff, pickle ban, unit, integration, coverage). Red and unfixable this session ⇒ leave the branch, report honestly. Never merge-then-fix, never force-push shared branches.
- **Green is not done.** Push the branch, open a PR, then spawn a fresh reviewer — `Agent(subagent_type: "code-reviewer", isolation: "worktree")`. It re-runs the gate itself, reviews against the specs, and merges on `APPROVE`. **You never merge your own PR**, and you never report a review as approval it did not give. Keep iterating while each round finds new defects; stop and report when a finding is re-argued, when the spec itself is disputed, or at five rounds ([docs/10 §8](docs/10-code-review.md)).

## Tooling (fixed — do not churn)

- Python 3.11+, **uv** for env/deps (`uv sync --extra dev` — the test toolchain is an extra, so a bare `uv sync` uninstalls ruff and pytest; then `uv run ...`), src-layout single package `tuner`.
- **ruff** for lint + format (`uv run ruff check --fix . && uv run ruff format .`).
- **pytest**; markers: default = unit, `-m integration` needs `docker compose up -d minio minio-init mlflow`, `-m e2e` is the full steel thread.
- CLI framework: **click**, single `tuner` entrypoint.

## Running things locally

```bash
cp .env.example .env                 # fill in HF_TOKEN, judge endpoint
docker compose up -d minio minio-init mlflow
uv run tuner run --config configs/pipeline.yaml     # full pipeline, prints run ID
set -a; . ./.env; set +a && ./scripts/gate.sh        # the full merge gate
# MinIO console: http://localhost:9001  ·  MLflow: http://localhost:5000
```

GPU stages (`train`, `smoke`) may run from a host venv if Docker GPU passthrough isn't set up — same commands, same env vars (docs/05 §3).

## Style

- Match existing code; comments only for non-obvious constraints.
- Type hints everywhere; pydantic v2 models for anything that crosses a process/storage boundary.
- Tests assert contracts (schemas, manifests, counts, exit codes) per docs/06 — prefer table-driven cases.
- Fixture data is synthetic only; never commit real data or credentials.
