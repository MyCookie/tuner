# Tuner — Conventions for Implementing Agents

You are building the Enterprise Fine-Tuning Pipeline from the specs in `docs/spec/`. The specs are normative; do not improvise architecture. If a spec is ambiguous or wrong, stop and say so rather than guessing.

`docs/` is split in two: `docs/spec/` is this normative engineering specification (implementer-facing — everything below points into it); the top-level `docs/*.md` files are the user/operator guide (how to run and operate the built pipeline). See [docs/README.md](docs/README.md) for the map of both.

## Read this first

- Your task comes from [docs/spec/07-build-plan.md](docs/spec/07-build-plan.md). Do exactly one task per session, and finish it against that document's ten-point **Definition of done**: its "Suite" + "Accept" + "Verify" lines, clean lint, and a review gate that ends at *the review team merged it* — not at *my tests pass* ([docs/spec/10-code-review.md](docs/spec/10-code-review.md)).
- Tests are specified, not improvised: implement every case in your task's suite exactly as listed in [docs/spec/08-test-specs/](docs/spec/08-test-specs/README.md), docstring-tagged with its case ID. Coverage gates: ≥90 % branch globally, 100 % on the listed pure-logic modules.
- Before touching **any** record, manifest, or schema code, read [docs/spec/02-data-contracts.md](docs/spec/02-data-contracts.md). It wins over code.
- All canonical names (buckets, env vars, config keys, run-ID format, exit codes) live in [docs/spec/01-architecture.md §4](docs/spec/01-architecture.md). Never invent a name variant.

## Hard rules

1. **Object storage only via `tuner.core.storage.StorageClient`.** No direct `boto3` imports outside it.
2. **No pickle, ever.** No `torch.load`/`torch.save` of raw tensors, no `.bin` weights — SafeTensors only. CI greps for violations.
3. **Secrets via env vars only** (the `TUNER_*`/`MLFLOW_*`/`HF_TOKEN` set). Never in configs, code, logs, or compose files.
4. **Stages are stateless and idempotent:** delete own output prefix for the run ID, rewrite, write the manifest last. Never write outside your stage's output bucket (the IAM matrix will reject it anyway).
5. **Validate inputs fail-fast** with the pydantic models in `tuner/core/schemas.py`; exit codes: 0 ok / 1 error / 2 config-or-validation / 3 zero-records.
6. **Model specifics live only in model adapters** (`docs/spec/04-model-adapters.md`). A stage branching on an adapter's name is a bug.
7. **Never weaken a test to make it pass.** A test that looks wrong is a spec question — check [docs/spec/08-test-specs/](docs/spec/08-test-specs/README.md) and the component spec, and flag conflicts instead of editing the test.

## Git & review (full rules: [docs/spec/09-git-workflow.md](docs/spec/09-git-workflow.md), [docs/spec/10-code-review.md](docs/spec/10-code-review.md))

- Never commit to `main`. One branch per unit of work, named `<type>/<issue#>-<slug>` (`feat/`, `fix/`, `docs/`, `refactor/`…), branched from up-to-date `main`. Every branch ties to an Issue — build-plan tasks get an Issue too.
- Atomic commits, Conventional Commits format (`feat(cleaner): ...`), code + its tests in the same commit, unit tests passing at every commit.
- The gate is one command: `./scripts/gate.sh` (ruff, pickle ban, unit, integration, coverage). Red and unfixable this session ⇒ leave the branch, report honestly. Never merge-then-fix, never force-push shared branches.
- **Review checklist.** Reviewers (`review-lead` and the five specialists) apply [docs/spec/10 §5](docs/spec/10-code-review.md) (items T-a to T-g) in addition to the plugin's own checks, naming coverage (T-d) and data contracts (T-e), which the plugin's split has no counterpart for. The plugin does not read that page, so this pointer is the only thing that reaches them; whether a specialist follows it is not mechanically enforced.
- **Green is not done.** Push the branch, open a PR with `tf pr create`, and stop. The review team reviews it against the specs and merges on `APPROVE` ([spec/10](docs/spec/10-code-review.md)). **You never merge your own PR**, and you never report a review as approval it did not give. Keep iterating while each round finds new defects; when to stop and escalate is [spec/10 §8](docs/spec/10-code-review.md).

## Multi-agent orchestration

The agents come from the [teamflow](https://github.com/Ribhu-Labs/teamflow) plugin (`teamflow@teamflow`, forge `github`), installed in `.claude/settings.json` and configured in `.claude/teamflow.json`, which is the only place the loop bounds, `tdd` and auto-approve values live (`teamflow-config` rejects unknown keys and every `tf` command then exits 2). Start one session with `claude --agent teamflow:manager`. Roles, routing, handoffs, the claim/gate/verdict/merge steps, `tf` exit codes, Issue labels and messaging are the plugin's `teamflow:workflow` skill and are **not restated in this repo**; where a repo doc disagrees with it on mechanics, the skill wins. What is specific to Tuner (the gate, the review checklist, verdict format) is [docs/spec/10-code-review.md](docs/spec/10-code-review.md). Setup and verification: [docs/05-agent-team-setup.md](docs/05-agent-team-setup.md); handoff and ownership detail for this repo: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).

Agents reach Issues and PRs with `tf issue ...` and `tf pr ...`, not raw `gh`. Every PR body carries `Closes #<n>` for each resolved Issue. The `ponytail@ponytail` (the simplicity-reviewer's engine) and `mattpocock-skills@mattpocock` (`/grilling` for stress-testing a goal at intake) plugins are declared in `.claude/settings.json`; per-person defaults belong in `.claude/settings.local.json`.

## Tooling (fixed — do not churn)

- Python 3.11+, **uv** for env/deps (`uv sync --extra dev --extra train` — the test toolchain is an extra, so a bare `uv sync` uninstalls ruff and pytest; then `uv run ...`), src-layout single package `tuner`.
- **ruff** for lint + format (`uv run ruff check --fix . && uv run ruff format .`).
- **pytest**; markers: default = unit, `-m integration` needs `docker compose up -d minio minio-init mlflow`, `-m e2e` is the full steel thread.
- CLI framework: **click**, single `tuner` entrypoint.

## Running things locally

```bash
cp .env.example .env                 # fill in HF_TOKEN, judge endpoint
docker compose up -d minio minio-init mlflow
uv run tuner run --config configs/pipeline.yaml     # full pipeline, prints run ID
./scripts/gate.sh                                    # the full merge gate (reads .env itself)
# MinIO console: http://localhost:9001  ·  MLflow: http://localhost:5000
```

GPU stages (`train`, `smoke`) may run from a host venv if Docker GPU passthrough isn't set up — same commands, same env vars (docs/spec/05 §3).

## Style

- Match existing code; comments only for non-obvious constraints.
- Type hints everywhere; pydantic v2 models for anything that crosses a process/storage boundary.
- Tests assert contracts (schemas, manifests, counts, exit codes) per docs/spec/06 — prefer table-driven cases.
- Fixture data is synthetic only; never commit real data or credentials.
