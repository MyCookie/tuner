# Tuner Code Review Workflow

How work reaches `main` from T05 on. The core invariant of [09-git-workflow.md](09-git-workflow.md) — **`main` is always green** — is unchanged; this document adds the second invariant: **nothing merges on its author's own say-so.**

Every build task is implemented by one agent and merged by a different one, with fresh context, that re-derives the verdict from the branch and the specs rather than from the implementer's report.

## 1. Roles

| Role | Model | Context | Owns |
| :--- | :--- | :--- | :--- |
| **Implementer** | Sonnet 5 (high) | One build task per session | The code. Branch, implement, local gate, push, open the PR. |
| **Reviewer** | Opus 5 (high) | **Fresh per PR** — spawned via `Agent(subagent_type: "code-reviewer")` | The merge. Independently re-run the gate, analyse against the specs, post the verdict, merge or reject. |

**The reviewer never edits code.** If it fixed what it found, it would be reviewing its own work — which is exactly the T03/T04 pattern this replaces. Its permitted actions are: read, run tests, post a review, apply a label, merge, delete the branch. A reviewer that wants a change files a finding and rejects.

**The implementer never merges its own PR.** Not after a green gate, not for a one-line fix, not "to save a round".

## 2. Lifecycle

```
implementer:  branch  →  implement  →  ./scripts/gate.sh  →  git push -u  →  gh pr create
                                                                                  ↓
                                                          spawn fresh Opus reviewer (own worktree)
                                                                                  ↓
                                                  detached checkout of origin/<branch> → gate.sh → semantic analysis
                                                                                  ↓
                                                            gh pr review --comment  (verdict + findings)
                                                                 ↓                            ↓
                                                          Verdict: APPROVE          Verdict: REQUEST_CHANGES
                                                                 ↓                            ↓
                                          gh pr merge --merge --delete-branch     implementer fixes on the branch,
                                                                 ↓                pushes, spawns a NEW reviewer (round 2)
                                             implementer: git switch main               ↓
                                                          git pull --ff-only     still rejected after round 2
                                                                                        ↓
                                                                                 stop, leave the PR open, report
```

## 3. Implementer steps

```bash
# 1. branch from up-to-date main
git fetch origin && git switch main && git pull --ff-only
git switch -c feat/tNN-<slug>

# 2. implement the task per 07-build-plan.md; atomic Conventional commits (09 §2–§3)

# 3. the gate — the same script the reviewer will run
./scripts/gate.sh

# 4. publish
git push -u origin feat/tNN-<slug>
gh pr create --base main --title "TNN — <task title>" --body-file <filled-in template>
```

The PR body follows [.github/pull_request_template.md](../.github/pull_request_template.md): which task, which suite cases, the gate transcript, any spec decision made along the way, and the areas the implementer most wants scrutinised. Claims in the PR body are *hints* for the reviewer, never evidence — the reviewer re-derives everything.

Then spawn the reviewer:

```
Agent(subagent_type: "code-reviewer", isolation: "worktree",
      description: "Review PR #N",
      prompt: "Review PR #<N> — branch feat/tNN-<slug>, build task TNN.")
```

## 4. Reviewer runbook

Runs cold, in its own git worktree, against **what is on `origin`** — not the implementer's working tree.

```bash
git fetch origin
git checkout --detach origin/feat/tNN-<slug>   # detached: the branch is checked out in the implementer's tree
cp /home/ashish/Projects/tuner/.env .env       # git-ignored, so absent from a fresh worktree
uv sync                                        # a fresh .venv also proves the lockfile resolves
set -a; . ./.env; set +a                       # tests read credentials from the environment
./scripts/gate.sh
```

The compose stack is a **single shared instance** on fixed ports (`9000`/`9001`/`5000`). `docker-compose.yaml` pins `name: tuner`, so commands issued from a worktree address that same project rather than starting a second, port-colliding stack. If the stack is down, bring it up with `docker compose up -d minio minio-init mlflow` — from any directory.

## 5. Semantic analysis

The gate proves the tests pass. The review exists for what a green gate cannot show. Work through all six:

**a. Hard rules ([CLAUDE.md](../CLAUDE.md)).** `import boto3` only inside `src/tuner/core/storage.py` (`CORE-U-046`); no `pickle`, no `torch.save`/`torch.load`, no `.bin` weights; secrets only via the `TUNER_*`/`MLFLOW_*`/`HF_TOKEN` env vars — never in configs, code, logs, or compose; stages stateless and idempotent (delete own output prefix → rewrite → manifest written **last**); exit codes 0 ok / 1 error / 2 config-or-validation / 3 zero-records; no stage branching on a model adapter's name.

**b. Spec conformance.** Read the task's **Spec** sections yourself. Every claim on its **Accept** line must trace to a real assertion, not a plausible-looking one. Canonical names — buckets, env vars, config keys, run-ID format ([01 §4](01-architecture.md)) — must be used verbatim, with no invented variants.

**c. Test integrity.** Every case ID on the task's **Suite** line is present, docstring-tagged as the first line ([08 README](08-test-specs/README.md)), and genuinely asserts the specified expectation. No case silently skipped; no case invented outside the spec. Then:

```bash
git diff origin/main...HEAD -- tests/
```

Edits to **pre-existing** tests are the strongest signal that a test was bent to fit the code. Every such edit needs a justification in the PR body, and the honest resolution of a wrong-looking test is a spec question, not an edit.

**d. Coverage.** ≥90 % branch globally; 100 % on the modules listed in [08 README](08-test-specs/README.md). Every `# pragma: no cover` carries a same-line justification naming the T15 manual check that covers it.

**e. Data contracts.** Anything touching a record, manifest, or schema is checked against [02-data-contracts.md](02-data-contracts.md), which wins over code.

**f. Documentation of decisions.** A spec ambiguity resolved during implementation belongs **in the docs**, not in a code comment or a commit message. The precedent is T04's `INF-I-005` footnote in [08 infra.md](08-test-specs/infra.md): the decision, its reasoning, and the task that owns the follow-up.

## 6. Verdict

One review per round, posted with `gh pr review <N> --comment --body-file <file>`. The **first line of the body is exactly** one of:

```
**Verdict: APPROVE**
**Verdict: REQUEST_CHANGES**
```

Then the gate transcript as a table (check · result), then the findings, each:

```
#### N. [blocker] <one-line claim>
**Where:** path/to/file.py:123
**Why required:** <which rule or spec section this breaks, and the consequence>
**Suggested fix:** <concrete — not "consider refactoring">
```

| Severity | Meaning | Blocks merge |
| :--- | :--- | :--- |
| `blocker` | Hard-rule breach, wrong behavior, or a contract violation | yes |
| `major` | Spec'd case missing, a pre-existing test weakened, coverage gate missed | yes |
| `minor` | Clarity, dead code, a comment that misstates what the code does | no |
| `nit` | Style preference | no |

**APPROVE requires zero `blocker` and zero `major`.** `minor`/`nit` findings are posted and left to the implementer's judgement — they never hold up a merge on their own.

A review that finds nothing still posts: the verdict plus the gate transcript plus an explicit statement of what was checked. "Looks good" is not a review.

Apply the matching label so PR state is queryable (`gh pr list --label review:changes-requested`):

```bash
gh pr edit <N> --add-label review:approved          # or review:changes-requested
```

## 7. Merging

Only the reviewer, only after posting `**Verdict: APPROVE**`:

```bash
gh pr merge <N> --merge --delete-branch \
  --subject "Merge feat/tNN-<slug>: TNN <task title>" \
  --body "Refs: TNN

Reviewed-by: Opus 5 reviewer agent (round R)"
```

`--merge` produces a merge commit, preserving the feature boundary in history exactly as the local `--no-ff` merges of T01–T04 did. The implementer then updates its local trunk:

```bash
git switch main && git pull --ff-only
```

If that pull is not a fast-forward, something wrote to `main` outside this workflow — stop and report rather than reconciling.

## 8. Rework loop

Bounded at **two rounds**.

1. `REQUEST_CHANGES` on round 1 ⇒ the implementer addresses every `blocker` and `major` on the same branch, in atomic commits that reference the finding, re-runs `./scripts/gate.sh`, and pushes.
2. Round 2 is a **brand-new reviewer** with fresh context that re-runs the entire gate and the entire checklist. It is never asked to "just re-check the fix" — a targeted re-check inherits the first reviewer's blind spots.
3. Still `REQUEST_CHANGES` after round 2 ⇒ **stop**. Leave the PR open with both reviews attached and report to the user. Two independent rejections mean the disagreement is about what the spec requires, and that is a question for a human, not a third round.

The implementer never argues a finding into submission by re-explaining it. If a finding is wrong, say so in a PR comment with the spec citation that refutes it, and let the next reviewer weigh both.

## 9. GitHub constraints (already discovered — don't re-derive)

- **Self-approval is impossible.** Both agents authenticate as the same GitHub user, who is the PR author; GitHub rejects `APPROVE` and `REQUEST_CHANGES` reviews on your own PR with HTTP 422. `--comment` reviews are permitted and appear in the review timeline, which is why the verdict is a parseable line in the body rather than a native review state.
- **Branch protection is not settable with the current token.** `gh api repos/MyCookie/tuner/branches/main/protection` returns 403 "Resource not accessible by personal access token". The gate in this document is therefore enforced by agent discipline, not by the server. Enabling *Require a pull request before merging* in the GitHub UI would harden it today; required status checks become possible at T14, when CI exists ([06 §6](06-testing.md)).

## 10. Scope

Applies to build tasks **T05 onward**, and to any `fix/`, `refactor/`, or `docs/` branch of comparable size. T01–T04 predate the workflow and are not re-reviewed retroactively.

A change too small to justify a review round — a typo in a comment, a broken link — may still be merged directly by the implementer, but only on a branch, only with the gate green, and only when it touches no behavior and no test.
