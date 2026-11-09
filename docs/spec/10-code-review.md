# Tuner Code Review Workflow

How work reaches `main` from T05 on. The core invariant of [09-git-workflow.md](09-git-workflow.md) — **`main` is always green** — is unchanged; this document adds the second invariant: **nothing merges on its author's own say-so.**

Review and merge are run by the [teamflow](https://github.com/Ribhu-Labs/teamflow) plugin (`teamflow@teamflow`, installed in `.claude/settings.json`, configured in `.claude/teamflow.json`). teamflow's own rules — roles, handoffs, `tf` exit codes, the claim/gate/verdict/merge steps — are the `teamflow:workflow` skill (`skills/workflow/SKILL.md` upstream) and are **not restated here**; this page says what is specific to Tuner: what the gate is, what reviewers check against, and how findings are graded. Where the two differ on mechanics, the skill wins; where they differ on what is checked, this page and the specs win — but see §5 for how little of that the plugin enforces. How to start the team: [05-agent-team-setup.md](../05-agent-team-setup.md).

Every build task is implemented by one agent and merged by a different one, with fresh context, that re-derives the verdict from the pushed head and the specs rather than from the implementer's report.

## 1. Roles

| Role | Context | Owns |
| :--- | :--- | :--- |
| **Implementer** (`teamflow:implementer`, spawned by `impl-lead`) | One unit of work, one worktree | The code. Branch, implement, local gate, open the PR with `tf pr create`. Reworks on `REQUEST_CHANGES`. |
| **review-lead** (`teamflow:review-lead`) | **A fresh instance per PR per round**, spawned by the manager with only the PR number, branch and Issue | The merge. Claims the PR, checks the gate, spawns the five specialists, records the one verdict, arms or runs the merge. |
| **Specialists** (`teamflow:architect-reviewer`, `security-reviewer`, `quality-reviewer`, `docs-reviewer`, `simplicity-reviewer`) | Five fresh instances per round, each in its own worktree | The findings. Each checks out the PR head, reviews it, and reports graded findings to review-lead. |

**review-lead is the only actor that arms or runs a merge.** The specialists never claim, release, review or merge, and neither do the implementer, `impl-lead` or the manager. The implementer never merges its own PR: not after a green gate, not for a one-line fix, not "to save a round".

**Reviewers never edit code.** If they fixed what they found, they would be reviewing their own work. A reviewer that wants a change files a finding and rejects. The teamflow review agents withhold Edit and Write for this reason — a guard rail, not a wall: they have `Bash`, and `Bash` can write files. Removing the easy path is not the same as making it impossible, so the rule is what holds the line, exactly as for the server-side enforcement in §9.

## 2. Lifecycle

```
implementer:  branch  →  implement  →  ./scripts/gate.sh  →  tf pr create   (pushes the branch, opens the PR)
                                                                  ↓
                                   manager spawns a fresh review-lead (per-PR mode)
                                                                  ↓
          tf pr claim → tf pr checkout → tf pr gate (CI `gate` check, §4) → five fresh specialists (§5)
                                                                  ↓
                                   tf pr review --verdict APPROVE | REQUEST_CHANGES   (one per head, §6)
                                         ↓                                      ↓
                  APPROVE: review-lead's review arms auto-merge       impl-lead's implementer fixes on the
                  (or runs tf pr merge); GitHub merges once           branch and pushes; the manager spawns a
                  `gate` and `teamflow/code-review` are green (§7)    NEW review-lead for the next round
                                         ↓                                      ↓
                              main advances (no local steps)        finding re-argued, spec disputed,
                                                                    or 5 rounds (§8): stop, PR stays open
```

## 3. Implementer steps

```bash
# 1. branch from up-to-date main
git fetch origin && git switch main && git pull --ff-only
git switch -c <type>/<issue#>-<slug>

# 2. implement the task per 07-build-plan.md; atomic Conventional commits (09 §2–§3)

# 3. the gate — the checks CI's `gate` job repeats
./scripts/gate.sh

# 4. publish — pushes the branch and opens the PR
tf pr create --title "TNN — <task title>" --body-file - <<'EOF_BODY'
...the filled-in .github/pull_request_template.md...
EOF_BODY
```

`tf pr create` refuses a dirty tree, the base branch, a detached HEAD and an unedited template, and pushes the branch first. A human outside a teamflow session can use `./scripts/open-pr.sh "TNN — <task title>" <body-file>` instead: same refusals, same result.

The PR body follows [.github/pull_request_template.md](../../.github/pull_request_template.md): which task, which suite cases, the gate transcript, any spec decision made along the way, and the areas the implementer most wants scrutinised. It carries `Closes #<n>` for each resolved Issue. Claims in the PR body are *hints* for the reviewers, never evidence — they re-derive everything.

**Red/green.** `.claude/teamflow.json` sets `tdd: required`: for a testable change the implementer writes the test first, keeps the failing output, then makes it pass, and the PR body records per test its name, the command, the red output and that it now passes. A change that cannot be tested (prose, configuration) says so and why. Missing red/green evidence on a testable change is a `major` (§6).

**Who spawns whom.** The implementer is a subagent of `impl-lead` and cannot spawn reviewers. It opens the PR and reports it; `impl-lead` returns the PR-ready report to the manager, and the manager spawns review-lead. A PR has at most one round in flight: review-lead's `tf pr claim` refuses a second. The prompt review-lead gives each specialist is deliberately bare (PR number, branch, Issue, the pinned head SHA); §9 records why.

## 4. The gate at review, and running things locally

**The gate is `./scripts/gate.sh`** (ruff, the pickle ban, unit, integration, coverage, the docs and test-ID checks). It stays the single definition of green: the implementer runs it before opening a PR, and `.github/workflows/gate.yml` runs those checks, plus the secret scans, as the CI job `gate` on every PR. The job id is the check name; the teamflow ruleset requires that context (§9). The local run is mandatory before opening a PR because teamflow's implementer rule requires it and it surfaces a red gate before a review round is spent; it covers no tests that CI skips. Both select `-m 'not e2e and not gpu and not slow'` (`gate.sh` lines 107–110, the `addopts` in `pyproject.toml`, and `gate.yml`), so no PR-time run covers `gpu` tests (run manually in T15) or `slow` tests (the nightly lane).

**At review**, review-lead runs `tf pr checkout <N>` in its own worktree, then `tf pr gate <N>`. On GitHub that waits for CI's required checks on the exact head and does not run the script a second time. Red is a `blocker`; the round continues so the implementer gets the whole picture. A gate that could not run, or whose result is unreadable, is a STOP: no verdict is recorded on an unverified gate. `tf` exit codes are in the workflow skill.

A specialist that needs to run the suite itself — to re-run a new test against the base branch's code for red/green, to reproduce a failure — does it in its own worktree:

```bash
./scripts/review-setup.sh <branch-under-review>
./scripts/gate.sh
rm -f .env                                  # when you finish
```

`review-setup.sh` fetches, checks out `origin/<branch>` **detached** — reviewing origin's copy is what makes this a review of what was published — copies `.env` in from the main worktree, and installs the toolchain with `uv sync --extra dev --extra train`, since `dev` is an extra and a bare `uv sync` uninstalls ruff and pytest, and `train` is needed from T11 on so `tests/integration/test_trainer.py` (which imports torch/peft/accelerate at collection time) doesn't fail the whole run on a collection error. It refuses to run in the main worktree, because detaching the tree someone else is working in would be destructive.

`gate.sh` reads `.env` itself, so there is no export step to forget — and it refuses, naming the missing variables, when credentials are incomplete rather than proceeding on a partial set. Both scripts exit 2 naming the fix when a precondition is unmet. If the main worktree has no `.env`, or the compose stack cannot be brought up, the local run cannot happen: say so and stop. Never invent credentials.

Delete `.env` when you finish. It is gitignored, so this is not about avoiding a stray commit — it is a real credentials file, and copies left in disposable worktrees are how one eventually outlives its worktree.

This is a script rather than a list of commands for a reason worth keeping: **no shell state persists between an agent's commands** — not variables, and not even the working directory. A runbook that assigns a variable in one step and uses it in the next silently does the wrong thing. A script runs as one command, and `gate.sh` syntax-checks every shell script in `scripts/` — identified by shebang, not by a `*.sh` glob.

The compose stack is a **single shared instance** on fixed ports (`9000`/`9001`/`5000`). `docker-compose.yaml` pins `name: tuner`, so commands issued from a worktree address that same project rather than starting a second, port-colliding stack. If the stack is down, bring it up with `docker compose up -d minio minio-init mlflow` — from any directory.

`tf` reads `.claude/teamflow.json` from the **main worktree**, which must stay on `main`; every `tf` command except `tf setup` stops (exit 2) otherwise. All work happens in separate worktrees. A PR that edits `.claude/teamflow.json` changes the rules its own review runs under only after it merges, because `tf` reads it from `main`. A PR that edits `.github/workflows/`, or any script or config a workflow step invokes (read `gate.yml` for the current list; it includes the pytest and coverage settings in `pyproject.toml`), is different: a `pull_request` workflow runs from the PR's own copy and checkout (PR #105's head got a `gate` check while `main` still had the old workflow), so its green `gate` was produced by the gate it modified and proves nothing about that change. The reviewer must read the gate change itself, as a checker change under §5, and not rely on the green result. A change to `scripts/gate.sh` is not in that set: CI does not run `gate.sh`, since `gate.yml` runs the checks itself, so it affects only the local run. Review it by checking it against `gate.yml` for drift.

## 5. Semantic analysis

The gate proves the tests pass. The review exists for what a green gate cannot show. This is Tuner's checklist, items **T-a to T-g** (prefixed so they cannot be confused with teamflow's own a to f).

**What is and is not enforced.** teamflow's review-lead records a verdict only once its *own* split of checks has been reported (upstream `agents/review-lead.md`, step 6): a hard rules (architect), b the Issue's acceptance criteria, c test integrity, d commits and red/green (quality), e decisions documented (docs), f changes to a checker (security), plus the gate. Nothing in the plugin reads this page, so nothing makes review-lead withhold a verdict for a missing T-item; coverage (T-d) and data contracts (T-e) have no counterpart in that split at all. This checklist reaches the reviewers only through what they read: `CLAUDE.md`, which every agent loads, tells reviewers to apply it. Whether a specialist follows that pointer is **not verified and not mechanically enforced**; a human or the manager reading a verdict should look for these items by their T-letters. A verdict's "item d" means teamflow's d (commits, red/green) unless written "T-d".

**T-a. Hard rules ([CLAUDE.md](../../CLAUDE.md)).** `import boto3` only inside `src/tuner/core/storage.py` (`CORE-U-046`); no `pickle`, no `torch.save`/`torch.load`, no `.bin` weights; secrets only via the `TUNER_*`/`MLFLOW_*`/`HF_TOKEN` env vars — never in configs, code, logs, or compose; stages stateless and idempotent (delete own output prefix → rewrite → manifest written **last**); exit codes 0 ok / 1 error / 2 config-or-validation / 3 zero-records; no stage branching on a model adapter's name.

**T-b. Spec conformance.** Read the task's **Spec** sections yourself. Every claim on its **Accept** line must trace to a real assertion, not a plausible-looking one. Canonical names — buckets, env vars, config keys, run-ID format ([01 §4](01-architecture.md)) — must be used verbatim, with no invented variants.

*When the PR is not a build task* — a `docs/`, `fix/`, `refactor/` or `chore/` branch, which §10 also brings under this gate — there is no build-plan entry, no **Suite** line and no **Accept** line. Every other item still applies verbatim; this one becomes: **does the change do what its PR body and Issue claim, and does anything it asserts as fact actually hold?** Verify factual claims in prose exactly as you would an assertion in code — run the command, read the spec, check the API response. A document that confidently states something false is this repo's equivalent of wrong behavior, because the docs are what the next agent executes from.

**T-c. Test integrity.** Every case ID on the task's **Suite** line is present, docstring-tagged as the first line ([08 README](08-test-specs/README.md)), and genuinely asserts the specified expectation. No case silently skipped; no case invented outside the spec. Then:

```bash
git diff origin/main...HEAD -- tests/
```

Edits to **pre-existing** tests are the strongest signal that a test was bent to fit the code. Every such edit needs a justification in the PR body, and the honest resolution of a wrong-looking test is a spec question, not an edit. Red/green evidence for new tests is checked here too (§3).

**T-d. Coverage.** ≥90 % branch globally; 100 % on the modules listed in [08 README](08-test-specs/README.md). Every `# pragma: no cover` carries a same-line justification naming the T15 manual check that covers it.

**T-e. Data contracts.** Anything touching a record, manifest, or schema is checked against [02-data-contracts.md](02-data-contracts.md), which wins over code.

**T-f. Commits.** Atomic and Conventional ([09 §2–§3](09-git-workflow.md)); code and its tests in the same commit; nothing committed that `.gitignore` should have caught.

**T-g. Documentation of decisions.** A spec ambiguity resolved during implementation belongs **in the docs**, not in a code comment or a commit message. The precedent is T04's `INF-I-005` footnote in [08 infra.md](08-test-specs/infra.md): the decision, its reasoning, and the task that owns the follow-up.

### Reviewing a checker

This applies to any change that greps, bans, validates or filters — and to changes to the *guidance* about such checks, which are judged by whether they would have caught the most recent defect found in that check's own history.

Running the gate proves nothing about a change *to* the gate. Anything that greps, bans, validates or filters needs its own controls, built by the reviewer — never reused from the implementer, since a check validated only against its author's examples is validated against its author's blind spots. Build true positives covering every form the check exists to catch (including awkward real-world ones), and **negative controls of legitimate code that superficially resembles what is banned** — that is where the defects are. A false positive in a gate blocks every future task and fails loudly on correct work, which is worse than failing open. Extract the pattern from the file under review rather than retyping it, and prove failure propagation by breaking something deliberately rather than by reasoning about shell semantics.

- **When a fix *widens* a check, ask what it now catches that it did not before.** "Does it still catch X, does it still allow Y" only tests properties someone already thought of; a widening fix's characteristic failure is a new false positive nobody was looking for. Recover the previous pattern with `git show origin/main:<file>`, run old and new over the same corpus, and read the difference — the lines the new one matches and the old one did not are the entire risk surface of the change. Prove the corpus is live before trusting the delta: at least one control must match the **old** pattern. A corpus that matches neither yields a clean, plausible, empty delta indistinguishable from "no new risk" — this failure mode looks exactly like success.

## 6. Verdict

**Exactly one `tf pr review` per head per round**, made by review-lead after all five specialists have reported, against the full head SHA it pinned after the claim (`tf` refuses a head that moved):

```bash
tf pr review <N> --verdict APPROVE|REQUEST_CHANGES --sha <full head SHA> --body-file - <<'EOF_BODY'
...
EOF_BODY
```

On GitHub this posts the verdict as a PR comment and sets the `teamflow/code-review` status on that SHA. The first `APPROVE` on a ruleset repository arms auto-merge and the last status per head wins, so a second call, or a call by a specialist, could merge a head nobody fully reviewed — hence one call, by one actor. The body carries the gate result as a table (check · result), each finding with its grade, and what was checked and how. Every round is a full re-review, never a spot-check of the fixes. A review that finds nothing still posts, with an explicit statement of what was checked: "looks good" is not a review.

Each finding:

```
**N. [blocker] <one-line claim>**
**Where:** path/to/file.py:123
**Why required:** <which rule or spec section this breaks, and the consequence>
**Suggested fix:** <concrete — not "consider refactoring">; say so if you have not tested it
```

The body starts with exactly `**Verdict: APPROVE**` or `**Verdict: REQUEST_CHANGES**`, and no body line starts with `#` (teamflow's review-lead is told to use bold headings).

| Severity | Meaning | Blocks merge |
| :--- | :--- | :--- |
| `blocker` | Hard-rule breach, wrong behavior, or a contract violation | yes |
| `major` | Spec'd case missing, a pre-existing test weakened, coverage gate missed, red/green evidence missing on a testable change | yes |
| `minor` | Clarity, dead code, a comment that misstates what the code does | no |
| `nit` | Style preference | no |

**Findings against documentation use the same ladder**, because the docs here are normative and the next agent executes from them. A doc that states a verifiable falsehood, or prescribes a command that does not work, is a `major`: it will mislead someone who cannot check it. A doc that is merely unclear, incomplete or stale is a `minor`. A documentation `major` blocks the merge; that is intended.

**APPROVE requires zero `blocker` and zero `major` from any specialist, and a green gate.** `minor`/`nit` findings are posted in the comment and not worked on yet; they never hold up a merge on their own. These findings live in the PR, not in Issues. A problem *outside the PR's diff* is filed once by review-lead as an Issue (after merging the specialists' duplicates), labelled per [CLAUDE.md](../../CLAUDE.md).

`**Where:**` takes a section reference when the finding is not about a line of code — `docs/spec/10-code-review.md §9` is a perfectly good anchor. Links in a review body or PR body are rendered by GitHub, not by a file tree: use a repo-root path or plain inline code, never `../`.

The verdict's state is the `teamflow/code-review` status on the head SHA, which is the queryable PR state; there are no review labels.

## 7. Merging

Only review-lead, only after recording `APPROVE`, and what is left to do depends on the output and exit code of `tf pr review`:

- **With the ruleset in place** (§9), the `APPROVE` itself arms GitHub auto-merge, pinned to the reviewed head; GitHub merges once the required checks pass. review-lead then confirms it: `tf pr gate <N>`, then `tf pr view <N>` for `state: merged`. A PR that is still open — `behind` the base, CI red or unreadable — stays unmerged and is reported to the manager, who escalates or starts a new round. The detail is in the workflow skill ("Queued merge").
- **Without the ruleset**, `tf pr review` arms nothing and review-lead runs `tf pr merge <N>`, which itself checks the CI `gate` check and the verdict before merging.
- Exit 4 (verdict recorded, auto-merge not armed) is the one non-zero exit that is not a STOP: run `tf pr merge <N>` once. A stacked PR (one that targets another branch than `main`) is not merged until it targets `main`.

The merge is a merge commit (`--merge`), preserving the feature boundary in history, and `tf pr merge` does not delete the branch. `tf setup --apply` turns on the repository's delete-branch-on-merge setting, so once it has run (§9, Issue #103) GitHub deletes merged branches; **until then** the setting is off and a merged branch stays on the remote, so delete it by hand (`git push origin --delete <branch>`). Afterwards anyone with a checkout updates with `git switch main && git pull --ff-only`; if that pull is not a fast-forward, something wrote to `main` outside this workflow — stop and report rather than reconciling.

**Never `--admin`, a direct `git push` to `main`, or a self-approval** to get past a blocked merge: `mergeStateStatus: BLOCKED` means the ruleset is doing its job, or was changed; that decision belongs to the repository owner, not to a reviewer. Stop and report.

A verdict is for one commit: a push after an approval needs a new round, and `tf` refuses to merge a head nobody approved.

## 8. Rework loop

**A reviewer's `Suggested fix:` is an argument, not a verified patch.** It was written by someone who had just found a defect, not by someone who then tested the remedy — reviewers do not run their own suggestions, and §1 forbids them from applying one. Adopting the wording verbatim is how a reviewer's blind spot becomes the next commit's defect. That is measured, not hypothetical: it happened twice on `docs/merge-authority`, the branch that added this rule — once producing a claim that was false *in the same direction the same review had just rejected*. Check a suggested fix exactly as a reviewer is required to check a claim in the PR body, then write your own.

`REQUEST_CHANGES` ⇒ the implementer (resumed by `impl-lead`) addresses every `blocker` and `major` on the same branch, in atomic commits that reference the finding, re-runs `./scripts/gate.sh`, and pushes. The next round is a **brand-new review-lead and five brand-new specialists** with fresh context, which re-run the entire gate check and the entire checklist. They are never asked to "just re-check the fix" — a targeted re-check inherits the previous round's blind spots, and in practice each fresh pass finds defects its predecessor missed.

**The manager stops and escalates to the human when the disagreement stops being about defects:**

- a round **re-raises a finding already argued** — the implementer explained why it does not hold and the review restates it without new evidence; or
- a review **disputes what the spec requires**, rather than whether the code matches it; or
- **five rounds** have passed on one PR without a merge, whatever the reason (teamflow counts rounds per PR and keeps the same limit).

Those are the signals that a human is needed. A round that finds *new, reproducible* defects on a branch that is otherwise converging — every prior finding verified fixed — is the process working, and it continues. Counting rounds measures how much work remains; it does not measure whether the process is stuck. (This document once capped the loop at two rounds; round 2 of the first PR reviewed under it found three new majors and confirmed all eight of round 1's findings fixed, which falsified the cap.)

When it stops: the PR stays open with every review attached, nothing is merged, and the report to the human says which of the three conditions fired.

**Being the round that would trigger the stop must not change anyone's grading.** A defect suppressed so something can merge is worse than one more rejection. What the last round *should* do is state, for each finding, whether it genuinely blocks or is something the owner could reasonably accept as-is — that is the judgement the escalation needs, and withholding a finding is not a substitute for making it.

A PR the implementation team did not open (a human's or an external contributor's) gets the same per-PR review, routed by the manager as merge-review. There is no implementer to resume, so a `REQUEST_CHANGES` goes to the human and the author reworks.

## 9. What enforces this

- **The ruleset.** `tf setup --forge github --apply` installs a branch ruleset on `main` that requires both the CI check `gate` and an approving `teamflow/code-review` status on the current head, with no bypass actors, and turns on auto-merge and delete-branch-on-merge. With it, no merge happens without the gate and a verdict on the head, and a push after approval always needs a new review. **Until it has been applied** (tracked in Issue #103; check `gh api repos/{owner}/{repo}/rulesets`), `main` has no server-side protection, and `tf` itself refuses to merge without the `gate` check and an approving verdict — enforcement by the tool and by discipline, not by GitHub. Do not describe the ruleset as in force without checking.
- **Self-approval and `--admin` are still wrong for any agent to invoke.** Branch protection was once found (PR #4) still configured from the repository's private era, putting an approved PR into `mergeStateStatus: BLOCKED`; the owner removed it. The lesson holds under the new setup: a reviewer that meets `BLOCKED` neither routes around it nor assumes the workflow is broken — it stops and asks the owner.
- **It is a process control, not an access control, and must never be described as though it were.** Every agent authenticates as the same GitHub user and runs as the same OS user. Nothing on GitHub distinguishes reviewer from author, and any agent could set the `teamflow/code-review` status. The separation is real — separate context windows, separate worktrees, a fresh review-lead and fresh specialists each round, a gate that CI re-runs on the exact head, and a verdict trail that stays on the PR — and it has caught defects the implementer would have shipped, but it rests partly on convention. Recorded as a deliberate choice by the repository owner on 2026-08-21, and carried over to the teamflow model.

*What compensates:* CI runs the gate on the head itself; every round is a fresh instance rather than a continuation; and only review-lead merges, once, after all five specialists report.

*What does not, stated at full strength:*
- review-lead is spawned by the manager with only the PR number, branch and Issue, and the specialists are given only those plus the pinned SHA. That keeps the *implementer's* reasoning out of the review — in the old flow the implementer wrote the reviewer's prompt and chose which risks to name, the largest channel undermining independence. The PR body is still a channel, which is why §3 calls it a hint rather than evidence.
- The implementer's and the reviewers' sessions share a machine and a user, so abstaining from each other's working trees and transcripts is a convention, not a boundary.
- Later rounds can see earlier rounds' verdict comments on the PR, so blind spots are correlated across rounds, not independent.
- The PR trail is editable by the same account that merges.
- Nothing stops a determined human or agent with the shared login from merging their own work if no ruleset is in force.

Revisit when a stage begins producing artifacts whose defects are expensive to discover late.

## 10. Scope

Applies to build tasks **T05 onward**, and to any `fix/`, `refactor/`, `docs/` or `chore/` branch. T01–T04 predate the workflow and are not re-reviewed retroactively.

There is deliberately no size-based exemption. A trivial change still goes on a branch, still runs the gate, and still gets a review — the review is simply fast.
