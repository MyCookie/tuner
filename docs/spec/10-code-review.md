# Tuner Code Review Workflow

How work reaches `main` from T05 on. The core invariant of [09-git-workflow.md](09-git-workflow.md) — **`main` is always green** — is unchanged; this document adds the second invariant: **nothing merges on its author's own say-so.**

Every build task is implemented by one agent and merged by a different one, with fresh context, that re-derives the verdict from the branch and the specs rather than from the implementer's report.

## 1. Roles

| Role | Model | Context | Owns |
| :--- | :--- | :--- | :--- |
| **Implementer** | Sonnet 5 (high) | One build task per session | The code. Branch, implement, local gate, push, open the PR. |
| **Reviewer** | Opus 5 (high) | **Fresh per PR** — spawned via `Agent(subagent_type: "code-reviewer")` | The merge. Independently re-run the gate, analyse against the specs, post the verdict, merge or reject. |

**The reviewer never edits code.** If it fixed what it found, it would be reviewing its own work — which is exactly the T03/T04 pattern this replaces. Its permitted actions are: read, run tests, post a review, apply a label, merge, delete the branch. A reviewer that wants a change files a finding and rejects.

The `code-reviewer` agent definition withholds Edit and Write for this reason — but that is a guard rail, not a wall. The reviewer has `Bash`, and `Bash` can write files. Removing the easy path is not the same as making it impossible, so the rule is what actually holds the line, exactly as it is for branch protection in §9.

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
                                          gh pr merge (§7) + push --delete        implementer fixes on the branch,
                                                                 ↓                pushes, spawns a NEW reviewer
                                             implementer: git switch main               ↓
                                                          git pull --ff-only     finding re-argued, spec disputed,
                                                                                 or 5 rounds (§8)
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

# 4. publish — pushes the branch and opens the PR
cp .github/pull_request_template.md /tmp/pr-body.md   # gh will not apply it non-interactively
#   ...fill in /tmp/pr-body.md...
./scripts/open-pr.sh "TNN — <task title>" /tmp/pr-body.md
```

`open-pr.sh` refuses on a dirty tree, on `main`, on a detached HEAD, on a missing body file, and on a body that is still the unmodified template. It exists because the transcribed version of this sequence was broken in both documents that carried it.

The PR body follows [.github/pull_request_template.md](../../.github/pull_request_template.md): which task, which suite cases, the gate transcript, any spec decision made along the way, and the areas the implementer most wants scrutinised. Claims in the PR body are *hints* for the reviewer, never evidence — the reviewer re-derives everything.

Then spawn the reviewer:

```
Agent(subagent_type: "code-reviewer", isolation: "worktree",
      description: "Review PR #N",
      prompt: "Review PR #<N> — branch feat/tNN-<slug>, build task TNN.")
```

Agent definitions in `.claude/agents/` are read when a session starts, so a newly added or edited one is **not** available as a `subagent_type` in the session that changed it. There, spawn a general-purpose agent and point it at `.claude/agents/code-reviewer.md` as its instructions instead — which also tests whether that file is self-sufficient.

Keep that prompt short. §9 records it as the largest channel undermining the reviewer's independence: every risk it names, and the order it names them in, is the implementer deciding what gets looked at. Give the PR number, the branch, the task, and the facts the reviewer would otherwise waste time discovering — not a ranked list of what you think is risky.

## 4. Reviewer runbook

Runs cold, in its own git worktree, against **what is on `origin`** — not the implementer's working tree.

```bash
./scripts/review-setup.sh <branch-under-review>
./scripts/gate.sh
rm -f .env                                  # when you finish
```

`review-setup.sh` fetches, checks out `origin/<branch>` **detached** — the branch is checked out in the implementer's tree, and reviewing origin's copy is what makes this a review of what was published — copies `.env` in from the main worktree, and installs the toolchain with `uv sync --extra dev --extra train`, since `dev` is an extra and a bare `uv sync` uninstalls ruff and pytest, and `train` is needed from T11 on so `tests/integration/test_trainer.py` (which imports torch/peft/accelerate at collection time) doesn't fail the whole gate on a collection error. It refuses to run in the main worktree, because detaching the tree someone else is working in would be destructive.

`gate.sh` reads `.env` itself, so there is no export step to forget — and it refuses, naming the missing variables, when credentials are incomplete rather than proceeding on a partial set. Both scripts exit 2 naming the fix when a precondition is unmet, rather than failing in a way that looks like a bad branch.

Delete `.env` when you finish. It is gitignored, so this is not about avoiding a stray commit — it is a real credentials file, and copies left in disposable worktrees are how one eventually outlives its worktree.

This is a script rather than a list of commands for a reason worth keeping: **no shell state persists between an agent's commands** — not variables, and not even the working directory. A runbook that assigns a variable in one step and uses it in the next silently does the wrong thing; that defect shipped here three times, in prose four reviewers had already read. A script runs as one command, and `gate.sh` syntax-checks every shell script in `scripts/` — identified by shebang, not by a `*.sh` glob, which silently skipped `scripts/pre-commit`.

If the PR modifies `.claude/agents/code-reviewer.md` itself — several have — the reviewer's own instructions are part of the diff, and it does **not** get the branch's version for free. A worktree starts at `main`, so a stand-in reads `main`'s copy, and a registered `code-reviewer` subagent is running whatever `main` had when its session began. Either way: **after `review-setup.sh` checks the branch out, re-read the file and work from the branch's version**, treating what differs from the instructions actually in force as part of what is being judged. Nothing about this is automatic. And while a branch is the one introducing `scripts/review-setup.sh`, that script does not exist on `main` — run it by absolute path from the main worktree, or do its steps by hand, until it has merged.

The compose stack is a **single shared instance** on fixed ports (`9000`/`9001`/`5000`). `docker-compose.yaml` pins `name: tuner`, so commands issued from a worktree address that same project rather than starting a second, port-colliding stack. If the stack is down, bring it up with `docker compose up -d minio minio-init mlflow` — from any directory.

## 5. Semantic analysis

The gate proves the tests pass. The review exists for what a green gate cannot show. Work through all seven:

The lettering below is normative; [.claude/agents/code-reviewer.md](../../.claude/agents/code-reviewer.md) restates it and must not diverge.

**a. Hard rules ([CLAUDE.md](../../CLAUDE.md)).** `import boto3` only inside `src/tuner/core/storage.py` (`CORE-U-046`); no `pickle`, no `torch.save`/`torch.load`, no `.bin` weights; secrets only via the `TUNER_*`/`MLFLOW_*`/`HF_TOKEN` env vars — never in configs, code, logs, or compose; stages stateless and idempotent (delete own output prefix → rewrite → manifest written **last**); exit codes 0 ok / 1 error / 2 config-or-validation / 3 zero-records; no stage branching on a model adapter's name.

**b. Spec conformance.** Read the task's **Spec** sections yourself. Every claim on its **Accept** line must trace to a real assertion, not a plausible-looking one. Canonical names — buckets, env vars, config keys, run-ID format ([01 §4](01-architecture.md)) — must be used verbatim, with no invented variants.

*When the PR is not a build task* — a `docs/`, `fix/`, `refactor/` or `chore/` branch, which §10 also brings under this gate — there is no build-plan entry, no **Suite** line and no **Accept** line. Every other item still applies verbatim; this one becomes: **does the change do what its PR body claims, and does anything it asserts as fact actually hold?** Verify factual claims in prose exactly as you would an assertion in code — run the command, read the spec, check the API response. A document that confidently states something false is this repo's equivalent of wrong behavior, because the docs are what the next agent executes from.

**c. Test integrity.** Every case ID on the task's **Suite** line is present, docstring-tagged as the first line ([08 README](08-test-specs/README.md)), and genuinely asserts the specified expectation. No case silently skipped; no case invented outside the spec. Then:

```bash
git diff origin/main...HEAD -- tests/
```

Edits to **pre-existing** tests are the strongest signal that a test was bent to fit the code. Every such edit needs a justification in the PR body, and the honest resolution of a wrong-looking test is a spec question, not an edit.

**d. Coverage.** ≥90 % branch globally; 100 % on the modules listed in [08 README](08-test-specs/README.md). Every `# pragma: no cover` carries a same-line justification naming the T15 manual check that covers it.

**e. Data contracts.** Anything touching a record, manifest, or schema is checked against [02-data-contracts.md](02-data-contracts.md), which wins over code.

**f. Commits.** Atomic and Conventional ([09 §2–§3](09-git-workflow.md)); code and its tests in the same commit; nothing committed that `.gitignore` should have caught.

**g. Documentation of decisions.** A spec ambiguity resolved during implementation belongs **in the docs**, not in a code comment or a commit message. The precedent is T04's `INF-I-005` footnote in [08 infra.md](08-test-specs/infra.md): the decision, its reasoning, and the task that owns the follow-up.

### Reviewing a checker

This applies to any change that greps, bans, validates or filters — and to changes to the *guidance* about such checks, which are judged by whether they would have caught the most recent defect found in that check's own history.

Running the gate proves nothing about a change *to* the gate. Anything that greps, bans, validates or filters needs its own controls, built by the reviewer — never reused from the implementer, since a check validated only against its author's examples is validated against its author's blind spots. Build true positives covering every form the check exists to catch (including awkward real-world ones), and **negative controls of legitimate code that superficially resembles what is banned** — that is where the defects are. A false positive in a gate blocks every future task and fails loudly on correct work, which is worse than failing open. Extract the pattern from the file under review rather than retyping it, and prove failure propagation by breaking something deliberately rather than by reasoning about shell semantics.

- **When a fix *widens* a check, ask what it now catches that it did not before.** "Does it still catch X, does it still allow Y" only tests properties someone already thought of; a widening fix's characteristic failure is a new false positive nobody was looking for. Recover the previous pattern with `git show origin/main:<file>`, run old and new over the same corpus, and read the difference — the lines the new one matches and the old one did not are the entire risk surface of the change. Prove the corpus is live before trusting the delta: at least one control must match the **old** pattern. A corpus that matches neither yields a clean, plausible, empty delta indistinguishable from "no new risk" — this failure mode looks exactly like success.

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
**Suggested fix:** <concrete — not "consider refactoring">; say so if you have not tested it
```

| Severity | Meaning | Blocks merge |
| :--- | :--- | :--- |
| `blocker` | Hard-rule breach, wrong behavior, or a contract violation | yes |
| `major` | Spec'd case missing, a pre-existing test weakened, coverage gate missed | yes |
| `minor` | Clarity, dead code, a comment that misstates what the code does | no |
| `nit` | Style preference | no |

**Findings against documentation use the same ladder**, because the docs here are normative and the next agent executes from them. A doc that states a verifiable falsehood, or prescribes a command that does not work, is a `major`: it will mislead someone who cannot check it. A doc that is merely unclear, incomplete or stale is a `minor`. A documentation `major` blocks the merge; that is intended.

`**Where:**` takes a section reference when the finding is not about a line of code — `docs/spec/10-code-review.md §9` is a perfectly good anchor. Links in a review body or PR body are rendered by GitHub, not by a file tree: use a repo-root path or plain inline code, never `../`.

**APPROVE requires zero `blocker` and zero `major`.** `minor`/`nit` findings are posted and left to the implementer's judgement — they never hold up a merge on their own.

A review that finds nothing still posts: the verdict plus the gate transcript plus an explicit statement of what was checked. "Looks good" is not a review.

Write the body to a scratch file first — the harness names a scratchpad directory; it is not exported as a variable. A single bare heredoc carrying a long review body is refused, so build the file with one `>` then successive `>>` appends, each its own command. Discovering that at the moment there is nothing left to do but post is a bad time to discover it.

A `--comment` review posts with state `COMMENTED`, which does **not** appear under `gh pr view <N> --json comments` — read it back with `gh api repos/{owner}/{repo}/pulls/<N>/reviews` instead.

Apply the matching label so PR state is queryable (`gh pr list --label review:changes-requested`):

```bash
gh pr edit <N> --add-label review:approved --remove-label review:changes-requested
# or, rejecting:
gh pr edit <N> --add-label review:changes-requested --remove-label review:approved
```

Always remove the other one. On a multi-round PR you inherit the previous round's label, and a PR carrying both tells `gh pr list --label` nothing.

## 7. Merging

Only the reviewer, only after posting `**Verdict: APPROVE**`:

```bash
gh pr merge <N> --merge \
  --subject "Merge feat/tNN-<slug>: TNN <task title>" \
  --body "Refs: TNN

Reviewed-by: Opus 5 reviewer agent (round R)"

git push origin --delete feat/tNN-<slug>      # NOT `gh pr merge --delete-branch`
```

If `gh pr merge` reports `mergeStateStatus: BLOCKED` (distinct from the known `--delete-branch` false-failure below, which happens *after* a successful merge, not instead of one), branch protection has been (re-)enabled server-side (§9) — do not reach for `--admin` or a self-approving review to get past it; that decision belongs to the repository owner, not to the reviewer. Stop and report instead.

**Do not use `--delete-branch`.** §4 puts you on a detached HEAD, and that flag makes `gh` resolve the *local* current branch to switch away from it — so it exits 1 with `could not determine current branch: failed to run git: not on any branch`, **after** the merge has already succeeded server-side. The result is an error that mentions no merge, a merge that happened, and a branch still on the remote. `git push origin --delete` needs no current branch and works from a detached worktree.

For a branch that is not a build task — `docs/`, `fix/`, `refactor/`, `chore/`, which §10 brings under this gate too — use the branch's own name and reason in place of the task form: `--subject "Merge <branch>: <what it does>"`, `--body "Refs: <PR # or task it follows up>"`, keeping the `Reviewed-by:` line.

`--merge` produces a merge commit, preserving the feature boundary in history exactly as the local `--no-ff` merges of T01–T04 did. The implementer then updates its local trunk:

```bash
git switch main && git pull --ff-only
```

If that pull is not a fast-forward, something wrote to `main` outside this workflow — stop and report rather than reconciling.

## 8. Rework loop

**A reviewer's `Suggested fix:` is an argument, not a verified patch.** It was written by someone who had just found a defect, not by someone who then tested the remedy — reviewers do not run their own suggestions, and §1 forbids them from applying one. Adopting the wording verbatim is how a reviewer's blind spot becomes the next commit's defect. That is measured, not hypothetical: it happened twice on `docs/merge-authority`, the branch that added this rule — once producing a claim that was false *in the same direction the same review had just rejected*. Check a suggested fix exactly as the reviewer is required to check a claim in the PR body, then write your own.

`REQUEST_CHANGES` ⇒ the implementer addresses every `blocker` and `major` on the same branch, in atomic commits that reference the finding, re-runs `./scripts/gate.sh`, and pushes. The next round is a **brand-new reviewer** with fresh context that re-runs the entire gate and the entire checklist. It is never asked to "just re-check the fix" — a targeted re-check inherits the previous reviewer's blind spots, and in practice each fresh pass finds defects its predecessor missed.

**Stop and escalate to the user when the disagreement stops being about defects:**

- a round **re-raises a finding already argued** — the implementer explained why it does not hold and the reviewer restates it without new evidence; or
- a reviewer **disputes what the spec requires**, rather than whether the code matches it; or
- **five rounds** have passed, whatever the reason.

Those are the signals that a human is needed. A round that finds *new, reproducible* defects on a branch that is otherwise converging — every prior finding verified fixed — is the process working, and it continues.

The distinction is deliberate and was learned the hard way. This document originally capped the loop at two rounds, reasoning that "a second independent rejection means a spec disagreement." Round 2 of the very first PR reviewed under it falsified that: it found three new majors, none of them a re-litigation, while confirming all eight of round 1's findings fixed. Counting rounds measures how much work remains; it does not measure whether the process is stuck.

When you do stop: leave the PR open with every review attached, merge nothing, and report to the user — including which of the three conditions fired.

**Being the round that would trigger the stop must not change a reviewer's grading.** A defect suppressed so something can merge is worse than one more rejection, and the reviewer is not responsible for the escalation being inconvenient. What the last round *should* do is state, for each finding, whether it genuinely blocks or is something the owner could reasonably accept as-is — that is the judgement the escalation needs, and withholding a finding is not a substitute for making it.

## 9. GitHub constraints (already discovered — don't re-derive)

- **Self-approval is assumed impossible — this one is inferred, not measured.** Both agents authenticate as the same GitHub user, who is the PR author, and GitHub is documented to reject `APPROVE` and `REQUEST_CHANGES` reviews on your own PR with HTTP 422. **No reviewer has verified it, and none should:** confirming it requires the author submitting a real approving review on their own PR, which is precisely what the rule exists to prevent. No review round has tested it, and none should — a standing, deliberate abstention rather than an oversight. `--comment` reviews are permitted and appear in the review timeline, which is why the verdict is a parseable line in the body rather than a native review state — and that design costs nothing even if the 422 assumption is wrong. Treat it as the one claim in this section that rests on documentation rather than on a command someone ran.
- **Branch protection is available, but currently unset — this has already changed once.** This bullet originally read "unavailable": `MyCookie/tuner` was a private repo on a free personal account, and `gh api repos/MyCookie/tuner/branches/main/protection` returned 403 *"Upgrade to GitHub Pro or make this repository public to enable this feature."* The repository is now public, which lifts that gate — the same call now returns 404 *"Branch not protected"*, meaning no rule exists, not that one can't. **The state in between is the reason this note exists at all:** while T05's PR #4 was already `APPROVE`d, the repository briefly still carried a protection rule (`required_approving_review_count: 1`, `enforce_admins: true`) that put the PR into `mergeStateStatus: BLOCKED`. Per the repository owner — this is owner testimony, not independently re-derivable now that the rule and any record of it are gone — it had been configured while the repository was private and simply forgotten across the visibility change; that sits in tension with the 403 evidence two sentences up (protection documented as gated for private free-plan repos), a discrepancy this document notes rather than resolves. The owner removed the rule and then merged normally: PR #4's two reviews are both `COMMENTED`, not `APPROVE`, which confirms the merge did not go through by satisfying the review requirement — it went through because the requirement no longer existed by the time of the merge. Nothing on the branch itself was wrong. A reviewer that hits `mergeStateStatus: BLOCKED` on an approved PR should not assume the workflow is broken and should not try to route around it — self-approval and `--admin` bypass are still wrong for a reviewer to invoke on its own judgment (§1, §7) — it should stop and ask the repository owner, exactly as happened here. Server-side enforcement, if wanted, is now a real toggle (*Require a pull request before merging*, required status checks once T14's CI exists — [06 §6](06-testing.md)); until the owner turns it on, the gate in this document rests on agent discipline alone, which is why §1's role boundaries are written as rules rather than inferred from tooling.

- **`main` advances on agent judgement alone, and that is a deliberate choice — not an oversight.** Recorded 2026-08-21 by the repository owner, having been shown: both agents authenticate as the same GitHub user, so nothing on GitHub distinguishes reviewer from author; branch protection is currently unset (above); and the reviewer is a subagent that the implementer spawns, briefs, and whose output the implementer relays. The separation is real — a separate context window, its own worktree, and it has caught defects the implementer would otherwise have shipped — but it is a **process control, not an access control**, and must never be described as though it were. Concretely: the reviewer runs as the same OS user as the implementer. It *can* read the implementer's working tree — §4 requires exactly that, to copy `.env` — and it could read the session transcripts under `~/.claude/projects/`, which are readable by that user. It does not, and that abstention is a convention, not a boundary. Nothing here withholds access from anyone; it only asks.

  *What compensates:* the reviewer re-runs the gate itself and re-derives the claims that are checkable, rather than trusting the PR body; each round is a fresh instance rather than a continuation of the last; and the verdict, findings and rebuttals stay on the PR, auditable afterwards.

  *What does not compensate, stated at full strength:*
  - **The implementer writes the reviewer's spawn prompt.** It chooses which risks are named, in what order, and with what framing — so the implementer's reasoning reaches the reviewer intact, through a channel the reviewer cannot audit and this document cannot constrain. The PR body is a second such channel, which is why §3 calls it a hint rather than evidence. Of everything on this list, this is the one that most undercuts the word "independent".
  - Later rounds read earlier rounds' findings, so blind spots are correlated across rounds, not independent.
  - The PR trail is editable by the same account that merges.
  - Not every claim is checkable: bullet 1 above is an assumption no reviewer should test, so it is trusted afresh by every round and verified by none.
  - Nothing stops a determined implementer from merging its own work.

  Revisit if branch protection is (re-)enabled on `main` — it would require a human-approval step this section's agent-judgment model does not have — or when a stage begins producing artifacts whose defects are expensive to discover late.

## 10. Scope

Applies to build tasks **T05 onward**, and to any `fix/`, `refactor/`, or `docs/` branch of comparable size. T01–T04 predate the workflow and are not re-reviewed retroactively.

There is deliberately no size-based exemption. An earlier draft let the implementer self-merge changes "too small to justify a review round", which contradicted §1 and §3's unqualified rule that the author never merges. A trivial change still goes on a branch, still runs the gate, and still gets a review — the review is simply fast.
