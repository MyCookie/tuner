# Tuner Testing Strategy

Testing philosophy: the pipeline's value is **auditability and determinism**, so tests assert contracts (schemas, manifests, counts, lineage) more than implementations. Every build-plan task lands with its tests; the E2E steel-thread test is the MVP's definition of done.

Test layout: `tests/unit/` (no services), `tests/integration/` (requires compose MinIO + mock judge), `tests/e2e/` (full pipeline). Runner: `pytest`; markers `integration`, `e2e`, and `gpu` keep default runs fast (`pytest` alone runs unit only).

This document is the strategy. The **normative, case-by-case test specifications** — including the coverage policy (≥90 % branch globally, 100 % on pure-logic modules) and the spec↔test traceability scheme — live in [08-test-specs/](08-test-specs/README.md); implementers work from those.

---

## 1. Unit tests (per module, no I/O)

- **Schemas:** every contract model accepts its doc-02 example verbatim and rejects mutations (missing field, bad role order, flat-string content, score out of range). The doc-02 JSON examples are embedded as fixtures — doc/code drift fails tests.
- **Cleaner rules:** table-driven cases per scrubber and filter in `tests/unit/test_cleaner_rules.py`; PII regex suite includes tricky negatives (version strings that look like phones).
- **Adapters:** `to_chat_messages` golden outputs, incl. system-turn folding; `training_defaults` completeness ([04 §4](04-model-adapters.md) checklist item).
- **Split & masking:** deterministic split hash values pinned; label-masking verified token-by-token on a tiny handcrafted conversation with a stub tokenizer.
- **Judge parsing:** reply-parsing cases — clean JSON, JSON in prose, garbage, out-of-range score.

## 2. Fixture dataset (`fixtures/`)

Committed, synthetic, generated once by `scripts/make_fixtures.py` (kept for regeneration, output committed for stability):

- `support_dialogs.csv` — 100 rows (`question,answer` + a `system` column on some): ~80 clean, planted defects with **known counts**: 3 exact duplicates, 5 under-length, 2 over-length, 4 with emails/phones, 3 with empty answer, 3 unmappable (blank question).
- `extra_dialogs.jsonl` — 20 lines: 10 contract-shaped, 5 flat `prompt`/`response`, 3 malformed-for-Bronze-abort tests (kept in a separate `bad_lines.jsonl` so the happy-path file ingests cleanly), 5 arbitrary-shape (unmappable).
- `expected_counts.json` — the authoritative drop-count expectations consumed by integration/E2E asserts.

All content is synthetic English support-desk chatter; no real data, no real PII.

## 3. Integration tests (compose MinIO up)

One test module per stage, each following the pattern: seed the input tier by writing records + manifest directly via `StorageClient` → run the stage via its CLI entrypoint in-process → assert output records, manifest counts vs. `expected_counts.json`, idempotent re-run, and refusal behaviors (missing upstream manifest ⇒ exit 2, invalid record ⇒ exit 2).

## 4. Mock judge

`tests/mock_judge/` — a ~100-line FastAPI app implementing `POST /v1/chat/completions`, behavior keyed by a marker embedded in the record text: `[[score=N]]` ⇒ return that score, `[[garbage]]` ⇒ non-JSON reply, `[[fail=429]]` ⇒ error status. Fixture records carry markers, making judge outcomes deterministic. Runs in-process via the ASGI test client for integration tests, and as a compose sidecar for E2E. Every judge acceptance criterion ([judge.md](03-components/judge.md)) maps to a marker scenario.

## 5. E2E steel-thread test

`tests/e2e/test_steel_thread.py`, config `configs/pipeline.e2e.yaml`:

- Adapter `tiny-test` — a permanently registered adapter over **`HuggingFaceTB/SmolLM2-135M-Instruct`** (135M params, chat-templated, ungated; pin `hf_revision` to its current commit at implementation time), `supports_full_ft: True`. Test infrastructure, not a product model — it exists so CPU-runnable tests exercise real tokenizer/trainer code paths. This is also the model whose cache CI pre-seeds for offline mode (INF-I-012).
- Runs `tuner run` end-to-end against fixtures + mock judge: asserts the full lineage chain — Bronze→Silver→Gold manifest links resolve, every Gold id maps to a tensor row or a recorded drop, adapter dir exists, registry manifest validates, MLflow run has params/metrics/transcript, smoke transcript has the configured sample count.
- Budget: ≤10 min on the dev-box GPU; also runnable on CPU (tiny model) for a slow nightly.

## 6. CI (GitHub Actions or equivalent, no GPU)

- Every push: ruff (lint + format check), unit tests, the pickle-ban grep ([05 §6](05-infrastructure.md)), secret scan (gitleaks), docs check (`scripts/check_docs.py`: link resolution + test-ID traceability).
- Every PR: integration tests with MinIO + mock judge as service containers, HF access in offline mode with a pre-seeded tiny-model cache (INF-I-012), coverage gate (`scripts/check_coverage.py`).
- On lockfile change + weekly: dependency audit (`uvx pip-audit`).
- Nightly: `pytest -m slow` (container structure, scale smoke — [08 infra.md](08-test-specs/infra.md)) + CPU E2E.
- GPU E2E: not in CI; it is the **manual release gate** — run before tagging and record the run ID in the PR/tag description.

## MVP scope

Everything above. **Later phases add:** serving canary-gate tests (regression-score thresholds, [inference.md](03-components/inference.md)) in Phase 3; multimodal fixtures (tiny images + asset-validation cases) in Phase 4.
