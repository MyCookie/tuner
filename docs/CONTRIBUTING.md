# Contributing — workflow for agent teams

This page is for a **team of agents** working Tuner together: a lead that splits
work across implementer agents, the implementers, and the reviewer that merges.
It summarizes the workflow and routes you to the engineering docs that are
normative. It does **not** restate the rules those docs already own — read them.

Single agent, one task? You mostly want [CLAUDE.md](../CLAUDE.md) and
[docs/spec/07-build-plan.md](spec/07-build-plan.md); the team mechanics below
still apply the moment a second agent joins.

## Read these first

| You need… | Go to |
| :--- | :--- |
| Conventions + the 7 hard rules | [CLAUDE.md](../CLAUDE.md) |
| Map of all docs (user guide vs. spec) | [docs/README.md](README.md) |
| Your task, one per session | [docs/spec/07-build-plan.md](spec/07-build-plan.md) |
| System design + **canonical names** (buckets, env vars, exit codes) | [docs/spec/01-architecture.md §4](spec/01-architecture.md) |
| Record/manifest/schema contracts (**normative — wins over code**) | [docs/spec/02-data-contracts.md](spec/02-data-contracts.md) |
| Per-stage specs | [docs/spec/03-components/](spec/03-components/) |
| Test cases, tagged by ID | [docs/spec/08-test-specs/](spec/08-test-specs/README.md) |
| Branch / commit / merge-gate rules | [docs/spec/09-git-workflow.md](spec/09-git-workflow.md) |
| The independent-review merge gate | [docs/spec/10-code-review.md](spec/10-code-review.md) |

The specs are normative. If one is ambiguous or wrong, stop and say so — don't
improvise architecture or invent a name variant.

## Working as a team

**1. One canonical clone. Fetch before you touch anything.** The remote is the
source of truth. Before reviewing or grepping code, `git fetch && git status`
against `origin/main` (or read the PR ref) — a stale local checkout will make you
"find" bugs that were fixed commits ago. Don't keep parallel clones of the repo
lying around; if one exists, confirm it's current before trusting it.

**2. Split work into units by the files they touch.** Two issues that edit the
**same file** are one unit and go to **one agent** — never split a shared file
across agents, or their branches collide. Issues on different files that depend
on each other (a refactor another fix builds on) are separate units with an
explicit dependency. Produce the unit list — issue numbers, files owned,
dependencies — and get it approved before spawning anyone.

**3. One worktree per unit, exclusive ownership.** Each agent works in its own
`git worktree` on its own branch and edits **only inside that directory**. Keep
unit file-sets disjoint so branches merge cleanly; run independent units in
parallel, stack or sequence dependent ones (branch the dependent unit off the
one it needs, not off `main`).

**4. Branch, commit, PR per [09-git-workflow.md](spec/09-git-workflow.md).**
Never commit to `main`. One branch per unit, named `<type>/<issue#>-<slug>`
(`feat/`, `fix/`, `docs/`, `refactor/`…) — every unit is tracked by an Issue and
its number is the branch scope. Conventional Commits, code + its tests in the
same commit. Open a PR that closes its issues (`Closes #NN`).

**5. "Done" means the full gate passed — `./scripts/gate.sh`, not just
`pytest`.** The gate runs ruff, the pickle ban, unit + integration, coverage,
**and** the docs/traceability checks. Running only `pytest` will report green
while `check_test_ids` / `check_docs` are red. Run the whole gate before you
claim a unit is done, and state honestly which checks passed.

**6. New or removed test IDs must move with their spec case.** Every test is
docstring-tagged with a case ID in [08-test-specs/](spec/08-test-specs/README.md);
`check_test_ids` fails on any test tag without a spec row **or** any spec row
without a test. Add a test → add its spec-case row. Remove a test → retire or
re-point its spec case (and any `mirrors <ID>` references). This is enforced in
the gate.

**7. Green is not done — the reviewer merges, not you.** Push, open the PR, then
spawn a fresh independent reviewer
(`Agent(subagent_type: "code-reviewer", isolation: "worktree")`); it re-runs the
gate, reviews against the specs, and merges on `APPROVE`
([10-code-review.md](spec/10-code-review.md)). **You never merge your own PR**,
and never report a review as approval it did not give. **CI is the source of
truth — never merge a red PR** (branch protection should enforce this); never
merge-then-fix.

**8. Report faithfully.** If tests fail, say so with the output; if a step was
skipped, say that. A test that looks wrong is a spec question — flag it, never
weaken it (hard rule 7).

## Team orchestration & handoffs

Roles and routing are summarized in [CLAUDE.md](../CLAUDE.md#multi-agent-orchestration);
this is the operational detail behind them. A **manager** session coordinates
three sub-teams — **research** (files Issues), **implementation** (worktrees +
PRs), **review** (diff-reviews PRs, files follow-up Issues) — and governs the
review-implement loop.

**Handoff protocols** (address sessions by `@name`, lead with status):
- **Research → Implementation.** Research-lead → `@manager`: tracking-Issue URL,
  Issue count, recommended priority order. Manager → `@impl-lead`: "Begin planning
  pass on the open Issues. Show the unit plan before spawning."
- **Implementation → Review.** Manager → `@review-lead`: "diff-review mode. Review
  PRs: [list]. File new Issues for anything that needs fixing."
- **Review → Implementation (loop).** Manager checks loop bounds; if continuing,
  sends `@impl-lead` the new Issue list; if stopping, reports to the human.
- **Review → Done.** Review reports zero new Issues (or bounds reached); manager
  reports final state to the human.

**Loop bounds** (manager persists them in `.manager-state.json`; edit before
starting the manager): `max_iterations: 3` · `exit_severity_threshold: medium`
(stop when no open Issue above it remains) · `human_checkpoint: every_cycle` ·
`max_open_issues_to_continue: 0` (stop only when clean).

**Issues & labels.** Every Issue carries an `area:` (`architecture`, `security`,
`quality`, `docs`, `simplicity`, `research`), a `severity:` (`high|medium|low`),
and a `type:` (`feature|bug|task`). Research Issues use `type:task`; review
findings use their `area:` label. `needs-discussion` marks a call for a human;
the reviewer agent sets `review:approved` / `review:changes-requested`.

**Messaging.** Don't poll — use idle notifications when waiting on a phase.
`crossSessionInbound: accept` is set project-wide.

## When a unit is finished

Remove its worktree (`git worktree remove …`), delete the merged branch, and
shut down the agent that owned it. The lead confirms `origin/main` is green and
that no orphan worktrees, branches, or agents are left behind.
