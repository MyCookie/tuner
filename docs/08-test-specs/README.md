# EFTP Test Suite Specifications

Executable-precision test specs for the MVP slice. [06-testing.md](../06-testing.md) is the strategy; these documents are the **normative case lists** — implement every case in the suite that matches your build task, exactly as specified. A build task is not done until its suite is green ([07-build-plan.md](../07-build-plan.md) definition of done, [09-git-workflow.md](../09-git-workflow.md) merge gate).

## Suites

| Suite | Prefix | File | Built in task |
| :--- | :--- | :--- | :--- |
| Core (config, ids, schemas, manifest, storage) | `CORE` | [core.md](core.md) | T01–T03 |
| Ingestor | `ING` | [ingestor.md](ingestor.md) | T06 |
| Cleaner | `CLN` | [cleaner.md](cleaner.md) | T07 |
| Judge (+ mock judge) | `JDG` | [judge.md](judge.md) | T05, T08 |
| Model adapters | `ADP` | [adapters.md](adapters.md) | T09 |
| Tokenizer | `TOK` | [tokenizer.md](tokenizer.md) | T10 |
| Trainer | `TRN` | [trainer.md](trainer.md) | T11 |
| Smoke-test | `SMK` | [smoke.md](smoke.md) | T12 |
| CLI, driver, registry list | `CLI` | [cli.md](cli.md) | T13 |
| End-to-end steel thread | `E2E` | [e2e.md](e2e.md) | T14 |

## Conventions

- **Case IDs:** `<PREFIX>-<U|I|E|G>-<NNN>` — `U` unit (no services), `I` integration (compose MinIO ± mock judge ± MLflow), `E` end-to-end, `G` GPU-only (manual, T15). IDs are stable; never renumber, append instead.
- **Traceability:** the first line of every test function's docstring is its case ID (`"""CLN-U-004: phone regex ignores version strings."""`). `scripts/check_test_ids.py` (built in T14) greps specs vs. code both ways and fails CI on: a spec case with no test, a test with no spec case, or a duplicated ID.
- **Markers:** unit tests unmarked; `@pytest.mark.integration`, `@pytest.mark.e2e`, `@pytest.mark.gpu` (real-GPU-only paths; skipped in CI, run in T15).
- **Table-driven by default:** cases within one ID that differ only by data use `pytest.mark.parametrize`; the ID covers the whole table.
- **Determinism:** no network except the in-process mock judge; no wall-clock assertions (inject/freeze time where a timestamp is asserted); integration tests create their own run IDs and clean their prefixes; tests never share state.
- **Shared fixtures** (`tests/conftest.py`): `storage` (StorageClient against compose MinIO), `run_id` (fresh per test), `seed_tier(tier, records)` (writes records + valid manifest directly, for testing a stage in isolation), `mock_judge` (ASGI app + env pointing at it), `fixture_counts` (parsed `expected_counts.json`), `tiny_adapter` (the `tiny-test` adapter).
- **Exit-code assertions** invoke stage CLIs in-process via `click.testing.CliRunner` and assert `result.exit_code` — never subprocess (keeps coverage measurable).

## Coverage policy

Tooling: `pytest-cov` (add to the `dev` extra in T01), **branch coverage on**, configured in `pyproject.toml`:

- **Global gate (CI-enforced): ≥ 90 % branch coverage** over `src/eftp`, unit + integration combined. The aspirational target is 100 %; treat every uncovered line as a question to answer, not a gap to accept.
- **100 % required (per-module `fail_under`, enforced by `scripts/check_coverage.py` in T14):** `eftp/core/*`, `eftp/cleaner/rules.py`, `eftp/cleaner/patterns.py`, `eftp/judge/prompts.py`, judge reply-parsing, `eftp/models/*`, `eftp/tokenizer/split.py`, `eftp/tokenizer/masking.py` — pure logic has no excuse.
- **Exclusions:** `# pragma: no cover` is allowed **only** on hardware-dependent branches (CUDA/bitsandbytes load paths, GPU OOM handlers) and each pragma carries a same-line justification comment naming the T15 manual check that covers it. CI greps that every pragma has a justification.
- E2E runs are not counted toward the gate (they'd mask unit gaps); they exist to prove integration, not coverage.

## Suite spec format

Each suite doc has: **Setup** (fixtures/services needed), a **case table** (`ID · Scenario · Expected`), and **notes** only where a case needs arrange/assert detail beyond the table. Expected outcomes reference contracts by doc-02/component-spec section, never restate schemas.
