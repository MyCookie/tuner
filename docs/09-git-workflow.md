# EFTP Git Workflow

Rules for all work in this repo, human or agent. The core invariant: **`main` is always green** — every commit on `main` passes the full local gate. Nothing merges on hope.

## 1. Branch model

- `main` — protected trunk. Never commit to it directly; it only advances by merging a green feature branch.
- One branch per unit of work, named `<type>/<scope>-<slug>`:
  - Build-plan tasks: `feat/t06-ingestor`, `feat/t10-tokenizer` (one branch per task — tasks are already sized to be atomic features)
  - Bug fixes: `fix/cleaner-phone-regex`
  - Docs-only: `docs/test-specs`
  - Refactors/chores: `refactor/...`, `chore/...`
- Branch from up-to-date `main`, keep branches short-lived (a task-sized branch should live hours-to-days, not weeks). Delete the branch after merge.
- The existing `draft` branch holds the documentation set; once approved it merges to `main` under the same gate (link check + traceability read in place of pytest).

## 2. Atomic commits

- **One logical change per commit** — a commit should be revertable without collateral damage. "Add phone scrubber + fix unrelated typo in config" is two commits.
- **Code and its tests land in the same commit.** A commit that adds behavior without its spec'd test cases ([08-test-specs](08-test-specs/)) is incomplete; a commit that only fixes tests to match broken code is a red flag.
- Every commit should leave the tree in a state where `uv run pytest` (unit) passes — bisectability is the payoff.
- Don't squash away meaningful history, and don't commit "wip"/"fixes" noise; use `git commit --amend` or `git rebase -i` **on your own unpushed branch only** to tidy before review.

## 3. Commit messages — Conventional Commits

```
<type>(<scope>): <imperative summary ≤ 72 chars>

Body: what and why, not how. Reference the build task and any
spec sections that constrain the change.

Refs: T07, docs/03-components/cleaner.md
```

- `type` ∈ `feat`, `fix`, `test`, `docs`, `refactor`, `chore`, `ci`. `scope` = stage/module (`ingestor`, `core`, `judge`, …).
- Examples: `feat(cleaner): add PII scrubbers and length filters`, `test(judge): cover retry/backoff cases JDG-U-015..016`, `fix(tokenizer): mask both assistant spans in multi-turn (TOK-U-011)`.
- Agent-authored commits keep the `Co-Authored-By` trailer the harness specifies.

## 4. Merge gate — run before every merge to `main`

From the feature branch, after rebasing onto latest `main` (`git fetch && git rebase origin/main` — rebase, don't back-merge):

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                          # unit
uv run pytest -m integration           # compose services up
python scripts/check_test_ids.py       # spec ↔ test traceability (from T14 on)
python scripts/check_coverage.py       # ≥90% global / 100% listed modules (from T14 on)
```

All green ⇒ merge with `git merge --no-ff <branch>` (preserves the feature boundary in history) and delete the branch. Anything red ⇒ fix on the branch; never merge-then-fix.

Where a GitHub remote exists, encode this as branch protection + the CI workflow (T14) as required checks, and merge via PR; the local gate stays mandatory regardless because CI has no GPU.

## 5. Releases

- Tag on `main` only, after the **E2E steel thread passes** ([08 e2e.md](08-test-specs/e2e.md)): annotated tag `mvp-0.1.0` style, the E2E run ID in the annotation. T15's real-model run gates the `mvp-1.0` tag.
- Never re-tag; a bad release gets a new patch tag.

## 6. Hygiene rules

- **Never force-push** `main` or any branch someone else may have pulled; `--force-with-lease` on your own unshared branch only.
- **Never commit:** `.env`, credentials, HF tokens, model weights, tensors, MinIO data dirs, `mlruns/`, caches. Keep `.gitignore` ahead of these (add it in T01: `.env`, `*.safetensors`, `mlruns/`, `.venv/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `.coverage*`, `data/`).
- Fixtures are the deliberate exception: `fixtures/**` is committed and changes to it must update `expected_counts.json` in the same commit.
- If a secret ever lands in a commit: rotate the credential immediately, then rewrite; rotation is the fix, rewriting is cleanup.
- One pipeline concern per PR/branch; if a change needs edits across many stages, that's usually a `core/` abstraction change — do it as its own branch first.
- Install the pre-commit hook (T01 adds `scripts/pre-commit` → `.git/hooks/`): runs `ruff check`, `ruff format --check`, unit tests, and blocks commits touching `.env` or matching the pickle-ban grep.

## 7. Agent session protocol (one build task per session)

1. `git fetch && git switch main && git pull` (or `git switch -c` from current `main` if no remote), then `git switch -c feat/tNN-<slug>`.
2. Implement the task per [07-build-plan.md](07-build-plan.md); commit atomically as you go (§2–§3).
3. Run the merge gate (§4). Green: merge `--no-ff` to `main`, delete the branch, report the task done with the gate output. Red and unfixable this session: **stop, push/leave the branch, report honestly** — never merge, never weaken a test to pass it.
4. A test that seems wrong is a spec question: check [08-test-specs](08-test-specs/) and the component spec; if they conflict, flag it instead of "fixing" the test.
