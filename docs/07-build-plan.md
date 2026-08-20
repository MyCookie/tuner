# Tuner MVP Build Plan

Ordered task breakdown for the MVP slice, sized for one-task-per-session execution by Sonnet-class implementers. Each task is independently completable and verifiable; no task depends on a later one. Read [CLAUDE.md](../CLAUDE.md) before any task; the referenced spec sections are normative.

**Definition of done for every task:** the listed files exist, the task's test-spec suite ([08-test-specs](08-test-specs/README.md), see the suite→task table there) is fully implemented and green, ruff is clean, and the verification command runs as stated. Every task is executed on its own branch and merged only through the gate in [09-git-workflow.md](09-git-workflow.md).

---

### T01 — Repo scaffold & core config
**Goal:** installable `tuner` package with config loading.
**Files:** `pyproject.toml` (uv-managed; deps: pydantic, boto3, pyyaml, click; extras `train`, `dev` incl. pytest-cov + hypothesis; coverage config per [08 README](08-test-specs/README.md)), `.gitignore` + `scripts/pre-commit` hook ([09 §6](09-git-workflow.md)), `src/tuner/core/config.py`, `src/tuner/core/ids.py`, `src/tuner/cli.py` (subcommand skeleton, all stages stubbed exit 1 "not implemented"), `configs/pipeline.yaml` (defaults from [01 §6](01-architecture.md)), `tests/unit/test_config.py`, `tests/unit/test_ids.py`.
**Spec:** [01 §3, §4.2, §4.4, §6](01-architecture.md).
**Accept:** config file round-trips through the pydantic model; unknown key rejected; run-ID/record-ID formats match §4.2 regexes.
**Verify:** `uv run tuner --help` lists all subcommands; `uv run pytest tests/unit`.

### T02 — Schemas
**Goal:** executable data contracts.
**Files:** `src/tuner/core/schemas.py`, `src/tuner/core/manifest.py`, `tests/unit/test_schemas.py` (doc-02 examples embedded as fixtures).
**Spec:** [02](02-data-contracts.md) entire.
**Accept:** every doc-02 example validates; the rejection cases of [06 §1](06-testing.md) fail as specified.
**Verify:** `uv run pytest tests/unit/test_schemas.py`.

### T03 — StorageClient
**Goal:** the single object-store access path.
**Files:** `src/tuner/core/storage.py`, `tests/integration/test_storage.py`.
**Spec:** [01 §5.1](01-architecture.md).
**Accept:** jsonl round-trip across shards, dir upload/download, delete-prefix; all against MinIO.
**Verify:** `docker compose up -d minio minio-init && uv run pytest -m integration tests/integration/test_storage.py`.
**Depends:** T01; T04 for compose (run T04 first if starting fresh — T03/T04 may be built in either order but verified together).

### T04 — Compose infra & MinIO bootstrap
**Goal:** local environment per [05 §1–§2, §5](05-infrastructure.md).
**Files:** `docker-compose.yaml`, `docker/base.Dockerfile`, `scripts/bootstrap_minio.py` (buckets incl. `tuner-mlflow`, per-stage users + policies from the IAM matrix), `.env.example`.
**Accept:** fresh `docker compose up -d` yields healthy MinIO + MLflow; INF-I-001..005 and INF-U-006..007 pass ([08 infra.md](08-test-specs/infra.md)) — including the full IAM matrix sweep and the MLflow proxied-artifact round-trip. INF-I-005 is scoped to `StorageClient` at this task (see the footnote on that case in [08 infra.md](08-test-specs/infra.md)) since no stage CLI does real storage I/O until T06.
**Verify:** `python scripts/check_iam.py` prints the full matrix result; `uv run pytest -m integration tests/integration/test_infra.py`.

### T05 — Fixtures & mock judge
**Goal:** test data + judge test double, before any stage exists.
**Files:** `scripts/make_fixtures.py`, `fixtures/*` (committed output incl. `expected_counts.json`), `tests/mock_judge/app.py`.
**Spec:** [06 §2, §4](06-testing.md).
**Accept:** fixture defect counts match `expected_counts.json` by construction; mock judge honors all markers.
**Verify:** `uv run pytest tests/unit/test_mock_judge.py`.

### T06 — Ingestor
**Files:** `src/tuner/ingestor/` (`sources.py`, `cli.py`), `tests/unit/test_sources.py`, `tests/integration/test_ingestor.py`.
**Spec:** [ingestor.md](03-components/ingestor.md) — implement its acceptance criteria as the integration test.
**Also:** add the deferred CLI-level companion case for `INF-I-005` in `tests/integration/test_infra.py` (real `tuner ingest` against an unreachable object store → exit 1, connection-error message, no partial manifest) — see the footnote on that case in [08 infra.md](08-test-specs/infra.md).
**Verify:** `uv run tuner ingest --run-id $(uv run python -m tuner.core.ids) --config configs/pipeline.yaml` then inspect `tuner-bronze` in the MinIO console.

### T07 — Cleaner
**Files:** `src/tuner/cleaner/` (`patterns.py`, `rules.py`, `cli.py`), `tests/unit/test_cleaner_rules.py`, `tests/integration/test_cleaner.py`.
**Spec:** [cleaner.md](03-components/cleaner.md).
**Verify:** integration test asserts drop counts equal `expected_counts.json`.

### T08 — Judge
**Files:** `src/tuner/judge/` (`client.py`, `prompts.py`, `cli.py`), `tests/unit/test_judge_parsing.py`, `tests/integration/test_judge.py` (against mock judge).
**Spec:** [judge.md](03-components/judge.md), incl. the >10 % judge_error abort.
**Verify:** integration run promotes exactly the `[[score>=threshold]]` fixtures; MLflow shows the judge run.

### T09 — Model-adapter layer
**Files:** `src/tuner/models/` (`base.py`, `registry.py`, `gemma_e4b.py`, `tiny_test.py` — SmolLM2-135M-Instruct test adapter, [06 §5](06-testing.md)), `tests/unit/test_adapters.py`.
**Spec:** [04](04-model-adapters.md) — confirm the real Gemma E4B HF repo id available to the team and set it in `gemma_e4b.py`.
**Verify:** `uv run pytest tests/unit/test_adapters.py`.

### T10 — Tokenizer
**Files:** `src/tuner/tokenizer/` (`masking.py`, `split.py`, `cli.py`), `tests/unit/test_split.py`, `tests/unit/test_masking.py`, `tests/integration/test_tokenizer.py` (uses `tiny-test` adapter).
**Spec:** [tokenizer.md](03-components/tokenizer.md).
**Verify:** integration test asserts index-map lineage + label masking on a handcrafted record.

### T11 — Trainer
**Files:** `docker/trainer.Dockerfile`, `src/tuner/trainer/cli.py`, `tests/integration/test_trainer.py` (tiny-test adapter, 1 epoch, CPU-capable).
**Spec:** [trainer.md](03-components/trainer.md); MLflow logging per [01 §7](01-architecture.md).
**Verify:** integration run leaves adapter dir + registry manifest; MLflow run has params/loss/tags; `method: full` with `gemma-e4b` exits 2.

### T12 — Smoke-test
**Files:** `src/tuner/smoke/cli.py`, `tests/integration/test_smoke.py`.
**Spec:** [smoke-test.md](03-components/smoke-test.md).
**Verify:** transcript at `tuner-artifacts/{run_id}/smoke/transcript.json` and attached to the trainer's MLflow run.

### T13 — Driver + registry list
**Files:** `tuner run` implementation in `src/tuner/cli.py`, `src/tuner/registry_ops/cli.py` (`tuner registry list` only, [registry.md MVP scope](03-components/registry.md)).
**Spec:** [01 §2](01-architecture.md).
**Verify:** `uv run tuner run --config configs/pipeline.e2e.yaml` completes end-to-end; `uv run tuner registry list` shows the new candidate.

### T14 — E2E steel thread + CI
**Files:** `tests/e2e/test_steel_thread.py`, `configs/pipeline.e2e.yaml`, `scripts/check_test_ids.py` + `scripts/check_coverage.py` + `scripts/check_docs.py` ([08 README](08-test-specs/README.md)), CI workflows per [06 §6](06-testing.md) (push, PR with offline-HF cache seeding, weekly audit, nightly slow lane).
**Spec:** [06 §5–§6](06-testing.md), [08 e2e.md](08-test-specs/e2e.md).
**Verify:** `uv run pytest -m e2e` passes within budget; CI green on a PR.

### T15 — MVP hardening pass
**Goal:** close the gaps a single-task view misses.
**Do:** run the full pipeline on the real `gemma-e4b` adapter with fixtures on the GPU box (or host-venv fallback, [05 §3](05-infrastructure.md)) — this covers the `G`-marked cases (TRN-G-020) and every justified coverage pragma; run the slow lane (`pytest -m slow`: INF-S-020..021); review the smoke transcript by hand; verify IAM matrix once more via `check_iam.py`; confirm no `.bin`/pickle artifacts anywhere in MinIO; write `README.md` quick-start (compose up → `tuner run` → MLflow URL).
**Accept:** a teammate can go from clone to trained-adapter-with-transcript using only README.md.

---

## Dependency graph

```
T01 ─> T02 ─> T03 ─┐
        T04 ───────┼─> T06 ─> T07 ─> T08 ─┐
        T05 ───────┘                      ├─> T10 ─> T11 ─> T12 ─> T13 ─> T14 ─> T15
                              T09 ────────┘
```

## Next slices (post-MVP — build plans to be written from the same specs)

- **Slice 2 (Phase 2 completion):** registry `show`/`promote`/`rollback`; smoke regression scoring; trainer checkpoint resume; `SqlSource`/`PdfSource`.
- **Slice 3 (Phase 3):** KFP component wrappers + K8s manifests ([05 §4](05-infrastructure.md)); cloud store cutover; Inference Engine + canary ([inference.md](03-components/inference.md)).
- **Slice 4 (Phase 4):** `tuner-assets` + multimodal ingestion/cleaning/eval; multimodal adapter fields ([04 §5](04-model-adapters.md)); multimodal fine-tune.
