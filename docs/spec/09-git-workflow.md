# Tuner Git Workflow

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

From the feature branch, after rebasing onto latest `main` (`git fetch && git rebase origin/main` — rebase, don't back-merge), run the whole gate as one command:

```bash
./scripts/gate.sh
```

It reads `.env` itself — there is no export step — and runs ruff check + format, the pickle ban, unit tests, then unit + integration with the ≥ 90 % branch coverage gate, and — from T14 on — `check_test_ids.py`, `check_coverage.py`, `check_docs.py`. Every step runs even after an earlier one fails, so one round surfaces the whole picture, and it prints a summary table to paste into the PR. Anything red ⇒ fix on the branch; never merge-then-fix.

**A green gate is necessary but no longer sufficient.** From T05 on, `main` advances only through a reviewed pull request: push the branch, open the PR, and a fresh Opus 5 reviewer agent independently re-runs this same gate in its own worktree, analyses the change against the specs, and merges it — [10-code-review.md](10-code-review.md) is normative. **The author never merges their own work.** The merge happens on GitHub via `gh pr merge --merge`, which produces the same merge-commit shape `--no-ff` did; local `main` then fast-forwards with `git pull --ff-only`.

CI (T14) can attach the same checks to the PR, but nothing yet *requires* them: branch protection is available on this (now public) repository but currently unset ([10 §9](10-code-review.md) — that section also records the one time it briefly wasn't, mid-review, from a rule left over from the repo's private era). Until the owner turns it on, the gate is enforced by discipline rather than by the server. The local gate stays mandatory regardless, because CI has no GPU.

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

1. `git fetch origin && git switch main && git pull --ff-only`, then `git switch -c feat/tNN-<slug>`.
2. Implement the task per [07-build-plan.md](07-build-plan.md); commit atomically as you go (§2–§3). Code and its spec'd test cases land in the same commit.
3. Run the gate (§4): `./scripts/gate.sh`. Red and unfixable this session ⇒ **stop, push the branch, report honestly** — never merge, never weaken a test to pass it.
4. Green ⇒ publish:
   ```bash
   cp .github/pull_request_template.md /tmp/pr-body.md   # fill it in; gh will not
   ./scripts/open-pr.sh "TNN — <task title>" /tmp/pr-body.md
   ```
   `open-pr.sh` pushes the branch and opens the PR, refusing on a dirty tree, on `main`, on a detached HEAD, or on a body file that is missing or still the unmodified template. `gh pr create` will not run non-interactively without `--body`/`--body-file` and never applies the template outside a terminal, which is why the copy is explicit.
5. Spawn a **fresh** reviewer — `Agent(subagent_type: "code-reviewer", isolation: "worktree")` — with the PR number and branch. It re-runs the gate itself, reviews against the specs, posts a verdict, and merges on `APPROVE` ([10-code-review.md](10-code-review.md)). Do not merge it yourself, and do not summarise the review as approval it did not give.
6. `REQUEST_CHANGES` ⇒ fix every `blocker` and `major` on the branch, push, and spawn a **new** reviewer for the next round. Keep going while each round finds *new* defects and the previous round's findings verify as fixed — that is the process working. Stop and report to the user when a round re-raises an already-argued finding, when a reviewer disputes what the spec requires, or at five rounds ([10 §8](10-code-review.md)).
7. Merged ⇒ `git switch main && git pull --ff-only`; report the task done with the gate output and the PR URL.
8. A test that seems wrong is a spec question: check [08-test-specs](08-test-specs/) and the component spec; if they conflict, flag it and record the resolution **in the docs**, instead of "fixing" the test.
