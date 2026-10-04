# Standing up the agent team stack

This page is the runbook for bringing up the Tuner agent team on a fresh machine or
a fresh repo: what the stack is, what it needs, how to start it, and how to check
that it works. The day-to-day rules (file ownership, handoffs, the review gate) are
in [CONTRIBUTING.md](CONTRIBUTING.md) and [CLAUDE.md](../CLAUDE.md); this page does
not repeat them.

## 1. What the stack is

Four long-lived lead sessions and a pool of short-lived subagents. Leads talk to each
other by `@name` messages; leads spawn subagents; subagents report back to the lead that
spawned them.

| Session | Definition | Spawns | Writes files? |
| :--- | :--- | :--- | :--- |
| `manager` | `.claude/agents/manager.md` | nothing | only `.manager-state.json` |
| `research-lead` | `.claude/agents/research-lead.md` | `researcher` | no |
| `impl-lead` | `.claude/agents/impl-lead.md` | `implementer`, `code-reviewer` | no (runs `git worktree`) |
| `review-lead` | `.claude/agents/review-lead.md` | five reviewers, `code-reviewer` | no |

| Subagent | Spawned by | Isolation | Writes files? |
| :--- | :--- | :--- | :--- |
| `researcher` | `research-lead` | worktree | no |
| `implementer` | `impl-lead` | the worktree `impl-lead` assigns | yes, only files it owns |
| `architect-reviewer`, `security-reviewer`, `quality-reviewer`, `docs-reviewer`, `simplicity-reviewer` | `review-lead` | worktree | no |
| `code-reviewer` | `impl-lead`; `review-lead` in merge-review mode | worktree | no (merges PRs) |

Models and effort are not listed here on purpose: each agent's `model:` and `effort:`
lines are the only record, so a change is one line in one file. To see them all:
`grep -H -E '^(model|effort):' .claude/agents/*.md`.

`code-reviewer` is spawned fresh for each review round and is the only actor allowed to
merge. Which lead spawns it, and how a lead claims a PR so it never has two reviewers,
is in [spec/10 §3](spec/10-code-review.md).

Messaging is by `@name` between the four sessions. `code-reviewer` messages its lead only to escalate (missing
`.env`, branch protection, a disputed spec, a critical security finding); the verdict
itself always goes on the PR.

## 2. Prerequisites

| Need | Why | Check |
| :--- | :--- | :--- |
| Claude Code with agent teams and cross-session messaging | leads address each other by `@name` | `claude --version` |
| `gh` authenticated with `repo` scope | every agent files Issues, opens PRs, or reads them | `gh auth status` |
| `uv`, Docker, a filled-in `.env` | the merge gate runs the integration tests against MinIO and MLflow | `cp .env.example .env`, then see [00-getting-started.md](00-getting-started.md) |
| Push access to the remote, branch protection on `main` | the reviewer merges; nobody else should | repo settings |

## 3. Files that define the stack

Everything is checked in except per-person settings.

| Path | Role |
| :--- | :--- |
| `.claude/agents/*.md` | one file per agent: frontmatter (model, tools, limits, isolation) plus the system prompt |
| `.claude/settings.json` | sets `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and `crossSessionInbound: accept` (lets sessions deliver each other's messages); declares the `ponytail` and `mattpocock` plugin marketplaces and enables both plugins |
| `.claude/settings.local.json` | per-person overrides; gitignored |
| `CLAUDE.md` | conventions, routing table, loop bounds; loaded by every agent |
| `AGENTS.md` | behavioural rules, Issue filing format, research output format; loaded by every agent |
| `scripts/gate.sh` | the merge gate the implementer and `code-reviewer` both run |
| `scripts/review-setup.sh` | builds the `code-reviewer` worktree (fetch, detached checkout, `.env` copy, `uv sync`) |
| `.manager-state.json` | the manager's loop state; created on first run, see step 5 |

Frontmatter fields the stack relies on:

- `tools` is an allowlist. A lead lists the subagents it may spawn as `Agent(name, ...)`; a
  subagent that must not spawn lists `disallowedTools: Agent` or omits `Agent`. Messaging
  tools (`SendMessage`, `ListAgents`) are granted the same way, by name in `tools:`.
- `disallowedTools: Edit, Write, MultiEdit` is what makes a role read-only. It is a guard
  rail, not a wall: any agent with `Bash` can still write. `AGENTS.md` states the rule.
- `isolation: worktree` gives the subagent its own checkout.
- `maxTurns` bounds a runaway agent.

## 4. Setup

### Step 1. Clone and check the tree

```bash
git clone git@github.com:MyCookie/tuner.git
cd tuner
git fetch origin && git status
ls .claude/agents          # expect 12 files
```

### Step 2. Install the toolchain and start the local stack

```bash
cp .env.example .env       # fill in HF_TOKEN and the judge endpoint
uv sync --extra dev --extra train   # the gate's integration tests need both extras
docker compose up -d minio minio-init mlflow
./scripts/gate.sh          # must be green on a clean main before any agent runs
```

Do not start the team on a red `main`. The reviewer will fail the gate on every PR and the
loop will spin.

### Step 3. Create the labels

The agents file Issues with these labels and `gh issue create` fails if one is missing.
The Tuner repo already has them. For a new repo:

```bash
for l in severity:high severity:medium severity:low \
         area:architecture area:security area:quality area:docs area:simplicity area:research \
         type:feature type:bug type:task \
         needs-discussion review:approved review:changes-requested review:in-progress; do
  gh label create "$l" 2>/dev/null || true
done
```

### Step 4. Enable the plugins

`.claude/settings.json` declares both marketplaces. The first session in a new checkout
asks you to trust and install them. Accept, or install by hand:

```
/plugin marketplace add DietrichGebert/ponytail
/plugin marketplace add mattpocock/skills
/plugin install ponytail@ponytail
/plugin install mattpocock-skills@mattpocock
```

`simplicity-reviewer` needs `ponytail` (its startup runs `/ponytail ultra`, `/ponytail-audit`
and `/ponytail-debt`). `mattpocock-skills` supplies `/grilling`, which you can use at intake to
stress-test a goal before it reaches the manager.

### Step 5. Set the loop bounds

Bounds live in two places that must agree: `CLAUDE.md` (the human-readable source) and
`.manager-state.json` (what the manager reads at runtime). Edit them before the first run.

```json
{
  "goal": "",
  "phase": "intake",
  "iteration": 0,
  "max_iterations": 3,
  "exit_severity_threshold": "medium",
  "human_checkpoint": "every_cycle",
  "max_open_issues_to_continue": 0,
  "research_tracking_issue": null,
  "impl_prs": [],
  "review_issues_per_cycle": [],
  "sessions_ready": []
}
```

The manager creates this file itself if it is missing. It is gitignored.

### Step 6. Start the sessions

One terminal per lead, from the repo root. Start the three working leads first and the
manager last, so the manager finds them running.

```bash
claude --agent research-lead --name research-lead
claude --agent impl-lead     --name impl-lead
claude --agent review-lead   --name review-lead
claude --agent manager       --name manager
```

Start each lead fresh from the repo root. If one was started from the wrong directory,
exit it and start it again: `claude --resume` restores the session's original working
directory, so it cannot fix this, and from outside the repo `.claude/agents/` is not
found and the lead cannot spawn its subagents.

If your Claude Code version does not accept `--agent`, start `claude --name <name>` and
tell the session "You are the <name> defined in `.claude/agents/<name>.md`; follow it."
Run `claude --help` to see which applies. Subagents are never started by hand; their
leads spawn them.

### Step 7. Give the manager a goal

In the `manager` session, state the goal in plain words. It routes per the table in
[CLAUDE.md](../CLAUDE.md#multi-agent-orchestration):

| You say | Goes to |
| :--- | :--- |
| a vague goal or feature idea | research |
| a specific task with no Issue | research |
| "fix #12, #15" | implementation |
| "review what we built" | review |
| "merge-review PR #40" (a PR the implementation team did not open) | review, merge-review mode |

The manager stops for your approval at two points: the `impl-lead` unit plan (before any
implementer is spawned), and each loop cycle when `human_checkpoint` is `every_cycle`.

## 5. Verify the stack

Run these in order. Each is cheap and each catches a distinct failure.

1. Agent definitions load. In any session, run `/agents`. All 12 names appear, with the
   models in their frontmatter.
2. Leads can see each other. In `manager`, use `ListAgents`. The three leads are listed.
   If they are not, the sessions were started from different directories or the
   `crossSessionInbound` setting in `.claude/settings.json` is not `accept`.
3. Read-only roles are read-only. Ask `manager` to edit a source file, such as
   `CLAUDE.md`. It must refuse. (It does write `.manager-state.json`; that is its job.)
4. Dry-run the review path. Tell `manager`: "Review what we built, diff-review mode, no PRs
   listed, stop after the report." A healthy run spawns five reviewers, files no duplicate
   Issues, and reports a count and highest severity.
5. Dry-run the implementation path on a trivial Issue (a typo in a doc). `impl-lead` must
   show a plan table and wait for approval, `implementer` must work inside its worktree, and
   the PR must reach exactly one `code-reviewer`, spawned by a lead, that re-runs
   `./scripts/gate.sh` itself. The PR carries `review:in-progress` while the reviewer
   runs and loses it when the verdict lands.

Clean up after step 5: `git worktree list`, then `git worktree remove` for each leftover,
delete the merged branch, delete the `worktree-agent-*` branches that `isolation: worktree`
subagents leave behind (`git branch --list 'worktree-agent-*'`), and confirm `origin/main`
is still green.

## 6. Known drift

None open. The mismatches found while writing this page (who spawns `code-reviewer`, models,
messaging tools, labels, the loop-bounds heading, commit format, the cross-session setting, the
`.manager-state.json` ignore, effort, the `impl-lead` label query) are fixed. If you find new
drift between an agent file and the docs, record it here until it is fixed.

## 7. Changing the stack

- Change a model: edit the `model:` line in the agent file. Effort is the `effort:`
  line below it.
- Add a subagent: create `.claude/agents/<name>.md`, then add `Agent(<name>)` to its
  lead's `tools:` line and to the lead's prompt. A lead cannot spawn what it does not list.
  Give it `SendMessage` in its own `tools:` if it needs to reach its lead.
- Add a lead: create its file, add it to the manager's phases and to the routing table in
  `CLAUDE.md`, and start a session for it.
- After any edit to `.claude/agents/`, restart the affected sessions. A running session keeps
  the definition it started with.
- Keep the agent files and the docs in step. A change to a label, a heading or a file name
  that an agent file references breaks the agent silently, which is how the drift in
  section 6 happened.
