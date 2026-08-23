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

- `support_dialogs.csv` — 100 rows (`question,answer,system`, `system` populated on a quarter of the clean rows, empty elsewhere): 80 clean + 4 with emails/phones (kept, scrubbed — not a drop reason) = 84 written; 16 dropped: 3 exact duplicates, 5 under-length, 2 over-length, 3 with empty answer + 3 with blank question (both `unmappable`, per [cleaner.md](03-components/cleaner.md) core logic 3).
- `extra_dialogs.jsonl` — **20 lines**: 10 contract-shaped, 5 flat `prompt`/`response`, 5 arbitrary-shape (unmappable) — 15 written, 5 dropped. The 3 malformed-for-Bronze-abort lines live entirely in a separate `bad_lines.jsonl` (used by `ING-U-004`), not counted in this file's 20, so the happy-path file ingests cleanly.
- `expected_counts.json` — the authoritative drop-count expectations consumed by integration/E2E asserts, generated from the same counters `make_fixtures.py` uses to decide what to write (so it matches "by construction", never recomputed by a second implementation of cleaner logic). Shape: `{"ingest": {"<file>": {"read": N}, "combined": {...}}, "clean": {"<file>": {"read", "written", "dropped", "drops": {<reason>: N}}, "combined": {...}}, "judge": {"threshold_score": N, "<file>": {"read", "written", "dropped", "drops": {"below_threshold": N}}, "combined": {...}}}`, one entry per source file plus a `"combined"` entry for the two-source configs (`ING-I-013`, `CLN-I-030`).
- A handful of the written (surviving-Cleaner) rows carry an explicit `[[score=N]]` mock-judge marker (§4) — most below the config's judge threshold, one above it — so a real Judge run against the committed fixtures (T08 Verify) and the E2E steel thread (`E2E-E-003`, "marker-derived judge expectations") see a mix of `below_threshold`/promoted outcomes instead of every record silently defaulting to the mock judge's score-8 default. The `"judge"` block above is that mix's expected counts; `JDG-I-*` cases are unaffected since they seed Silver directly rather than reading these files ([judge.md Setup](08-test-specs/judge.md)).

All content is synthetic English support-desk chatter; no real data, no real PII. `bad_lines.jsonl` and `expected_counts.json` are also written by `make_fixtures.py`, alongside the two files above.

## 3. Integration tests (compose MinIO up)

One test module per stage, each following the pattern: seed the input tier by writing records + manifest directly via `StorageClient` → run the stage via its CLI entrypoint in-process → assert output records, manifest counts vs. `expected_counts.json`, idempotent re-run, and refusal behaviors (missing upstream manifest ⇒ exit 2, invalid record ⇒ exit 2).

## 4. Mock judge

`tests/mock_judge/` — a FastAPI app implementing `POST /v1/chat/completions`, behavior keyed by a marker embedded in the record text: `[[score=N]]` ⇒ return that score, `[[garbage]]` ⇒ non-JSON reply, `[[fail=429]]` ⇒ error status (every call), `[[fail_once=429]]` ⇒ error status on the first call only, then scores normally (T08). Fixture records carry markers, making judge outcomes deterministic. Runs in-process via the ASGI test client for integration tests, and as a compose sidecar for E2E. Every judge acceptance criterion ([judge.md](03-components/judge.md)) maps to a marker scenario.

- **Precedence:** the mock checks markers in a fixed order — `[[fail_once=...]]` first, then `[[fail=...]]`, then `[[garbage]]`, then `[[score=N]]`, else the score-8 default — so a record can't accidentally suppress an injected failure by also matching another marker.
- **Marker scope:** the mock scans the *entire* rendered request (every message's content) for a marker, not just the record's own text. This is why `RUBRIC_V1` (`src/tuner/judge/prompts.py`)'s instructional text must never contain anything shaped like `[[...]]` (e.g. a worked example using that bracket style) — every call would otherwise match it as an unintended marker, regardless of the record's own content.
- **State (T08):** the mock is a module-level singleton carrying `app.state.call_counts` (calls per distinct request text), `app.state.fail_once_consumed` (which `[[fail_once=...]]` content has used its one failure), and `app.state.peak_in_flight` (highest concurrently in-flight request count, behind a small artificial delay so concurrent calls actually overlap). `reset_state()` clears all three; tests call it between cases sharing the same app instance. This closes the gap T05 deferred here: `JDG-I-022` (retried-call counting), `JDG-I-024` (fail-once-then-succeed), and `JDG-I-025` (peak in-flight count) all need this state, and T05's stateless marker vocabulary didn't cover them.

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
