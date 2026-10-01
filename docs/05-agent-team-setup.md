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

| Session | Definition | Model | Spawns | Writes files? |
| :--- | :--- | :--- | :--- | :--- |
| `manager` | `.claude/agents/manager.md` | `claude-opus-5-5` | nothing | no |
| `research-lead` | `.claude/agents/research-lead.md` | `claude-opus-5-5` | `researcher` | no |
| `impl-lead` | `.claude/agents/impl-lead.md` | `claude-opus-5-5` | `implementer`, `code-reviewer` | no (runs `git worktree`) |
| `review-lead` | `.claude/agents/review-lead.md` | `claude-opus-5-5` | five reviewers, `code-reviewer` | no |

| Subagent | Spawned by | Model | Isolation | Writes files? |
| :--- | :--- | :--- | :--- | :--- |
| `researcher` | `research-lead` | `claude-sonnet-5-5` | worktree | no |
| `implementer` | `impl-lead` | `claude-sonnet-5-5` | the worktree `impl-lead` assigns | yes, only files it owns |
| `architect-reviewer`, `security-reviewer`, `quality-reviewer`, `docs-reviewer`, `simplicity-reviewer` | `review-lead` | `claude-sonnet-5-5` | worktree | no |
| `code-reviewer` | `impl-lead` or `review-lead`, per PR | `opus` (effort `high`) | worktree | no (merges PRs) |

`code-reviewer` is spawned fresh for each review round of each PR and is the only actor
allowed to merge ([spec/10](spec/10-code-review.md)). The implementer has no `Agent` tool, so
in a team run a lead spawns it: both `impl-lead` and `review-lead` list it in their
`Agent(...)` allowlist. The lead that spawns it owns that round, and a PR never has two
reviewers running at once.

Every subagent a lead spawns (`researcher`, `implementer`, the five specialist reviewers) has
`SendMessage` and `ListAgents` in its `tools:` line, so it can message its lead or another
session and look up exact session names. `code-reviewer` does not have them.

Model policy: leads and the manager on Opus 5.5, subagents on Sonnet 5.5. The model is a
single `model:` line in each agent's frontmatter, so a change is one line per file.

## 2. Prerequisites

| Need | Why | Check |
| :--- | :--- | :--- |
| Claude Code with agent teams and cross-session messaging | leads address each other by `@name` | `claude --version` |
| `gh` authenticated with `repo` scope | every agent files Issues, opens PRs, or reads them | `gh auth status` |
| A GitHub MCP server named `github` | reviewers, `implementer` and `research-lead` list `mcp__github` in `tools:` | `claude mcp list` |
| `uv`, Docker, a filled-in `.env` | the merge gate runs the integration tests against MinIO and MLflow | `cp .env.example .env`, then see [00-getting-started.md](00-getting-started.md) |
| Push access to the remote, branch protection on `main` | the reviewer merges; nobody else should | repo settings |

## 3. Files that define the stack

Everything is checked in except per-person settings.

| Path | Role |
| :--- | :--- |
| `.claude/agents/*.md` | one file per agent: frontmatter (model, tools, limits, isolation) plus the system prompt |
| `.claude/settings.json` | sets `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`; declares the `ponytail` and `mattpocock` plugin marketplaces and enables both plugins |
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
uv sync --extra dev
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
         needs-discussion review:approved review:changes-requested; do
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

The manager creates this file itself if it is missing. It is not gitignored, so add
`.manager-state.json` to `.gitignore` or it will end up in someone's commit.

### Step 6. Start the sessions

One terminal per lead, from the repo root. Start the three working leads first and the
manager last, so the manager finds them running.

```bash
claude --agent research-lead --name research-lead
claude --agent impl-lead     --name impl-lead
claude --agent review-lead   --name review-lead
claude --agent manager       --name manager
```

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

The manager stops for your approval at two points: the `impl-lead` unit plan (before any
implementer is spawned), and each loop cycle when `human_checkpoint` is `every_cycle`.

## 5. Verify the stack

Run these in order. Each is cheap and each catches a distinct failure.

1. Agent definitions load. In any session, run `/agents`. All 12 names appear, with the
   models from section 1.
2. Leads can see each other. In `manager`, run `/list-agents`. The three leads are listed.
   If they are not, the sessions were started from different directories or the
   cross-session setting is off (see section 6).
3. Read-only roles are read-only. Ask `manager` to create a file. It must refuse.
   Also confirm a spawned subagent can call `ListAgents`; if it cannot, the lead was
   started before the `tools:` edit and needs a restart.
4. Dry-run the review path. Tell `manager`: "Review what we built, diff-review mode, no PRs
   listed, stop after the report." A healthy run spawns five reviewers, files no duplicate
   Issues, and reports a count and highest severity.
5. Dry-run the implementation path on a trivial Issue (a typo in a doc). `impl-lead` must
   show a plan table and wait for approval, `implementer` must work inside its worktree, and
   the PR must reach exactly one `code-reviewer`, spawned by a lead, that re-runs
   `./scripts/gate.sh` itself.

Clean up after step 5: `git worktree list`, then `git worktree remove` for each leftover,
delete the merged branch, and confirm `origin/main` is still green.

## 6. Known drift to fix or work around

These are mismatches between the agent files and the docs, found while writing this page.
None blocks standing the stack up, but the first three will bite.

| Where | Problem | Effect | Fix |
| :--- | :--- | :--- | :--- |
| `CLAUDE.md`, `docs/CONTRIBUTING.md` say `crossSessionInbound: accept` is set project-wide | it is not in `.claude/settings.json`, and no other file sets it | leads may not receive each other's messages | confirm the setting for your Claude Code version and commit it to `.claude/settings.json` |
| `impl-lead.md` and `review-lead.md` prompts | both leads may spawn `code-reviewer`, but neither prompt says when or which lead does | the permission is unused, or both spawn a reviewer for the same PR | add one step to `impl-lead` ("after the implementer reports a PR, spawn `code-reviewer`") and tell `review-lead` not to unless asked |
| `.claude/agents/architect-reviewer.md` files Issues with `area:architect` | the label is `area:architecture` | `gh issue create` fails or files an unlabeled Issue, and `impl-lead` never picks it up | change the label in the agent file |
| `.claude/agents/manager.md` tells the manager to read "Review-implement loop bounds" | the `CLAUDE.md` heading is "Loop bounds" | manager cannot find the section and falls back to the JSON defaults | rename one of them |
| `docs/spec/09-git-workflow.md` steps 5 and 6, `docs/spec/07-build-plan.md` | describe the single-agent flow where the implementer spawns the reviewer | contradicts the team flow, where the implementer cannot spawn | add a team-run note, as `spec/10` now has |
| `.claude/agents/implementer.md` commits as `fix: <description> (closes #N)` | [spec/09](spec/09-git-workflow.md) requires `<type>(<scope>): ...` and `Refs:` | reviewer flags the commit format | have the implementer follow spec/09 |
| `.manager-state.json` | not gitignored | state committed by accident | add it to `.gitignore` |

## 7. Changing the stack

- Change a model: edit the `model:` line in the agent file. Effort goes in an `effort:`
  line; only `code-reviewer` sets one today.
- Add a subagent: create `.claude/agents/<name>.md`, then add `Agent(<name>)` to its
  lead's `tools:` line and to the lead's prompt. A lead cannot spawn what it does not list.
  Give it `SendMessage, ListAgents` in its own `tools:` if it needs to reach its lead.
- Add a lead: create its file, add it to the manager's phases and to the routing table in
  `CLAUDE.md`, and start a session for it.
- After any edit to `.claude/agents/`, restart the affected sessions. A running session keeps
  the definition it started with.
- Keep the agent files and the docs in step. A change to a label, a heading or a file name
  that an agent file references breaks the agent silently, which is how the drift in
  section 6 happened.
