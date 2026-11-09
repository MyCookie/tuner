# Standing up the agent team stack

This page is the runbook for bringing up the Tuner agent team on a fresh machine or
a fresh repo: what the stack is, what it needs, how to start it, and how to check
that it works. The agents come from the [teamflow](https://github.com/Ribhu-Labs/teamflow)
plugin (`teamflow@teamflow`); teamflow's own install and run docs are linked rather than
repeated here: [docs/setup.md](https://github.com/Ribhu-Labs/teamflow/blob/main/docs/setup.md)
and [docs/running.md](https://github.com/Ribhu-Labs/teamflow/blob/main/docs/running.md).
The day-to-day rules (file ownership, handoffs, the review gate) are in
[CONTRIBUTING.md](CONTRIBUTING.md), [CLAUDE.md](../CLAUDE.md) and
[spec/10-code-review.md](spec/10-code-review.md); this page does not repeat them.

## 1. What the stack is

One session, started as the manager. The manager spawns three leads as subagents, the
research and implementation leads spawn their workers, and the review lead spawns five
specialist reviewers. Every handoff runs through the manager, including each PR's review
rounds.

| Agent | Spawned by | Spawns | Job |
| :--- | :--- | :--- | :--- |
| `teamflow:manager` | you (`claude --agent teamflow:manager`) | the three leads | routes goals, runs every handoff, counts each PR's rounds |
| `teamflow:research-lead` | manager | `teamflow:researcher` | decomposes goals into Issues; implements nothing |
| `teamflow:impl-lead` | manager | `teamflow:implementer` | one worktree and PR per non-overlapping unit; reworks on review findings |
| `teamflow:review-lead` | manager, fresh per PR per round | the five specialists | claims the PR, checks the gate, records one verdict, arms or runs the merge; the only actor that merges |
| `teamflow:architect-reviewer`, `security-reviewer`, `quality-reviewer`, `docs-reviewer`, `simplicity-reviewer` | review-lead | nothing | review one PR head each round and report graded findings |

Models, effort, tools and spawn limits are defined by the plugin, not by this repo, so they
are not listed here. A lead can also be started alone (`claude --agent teamflow:impl-lead`)
and then reports to you; a lone impl-lead has no manager to hand its PRs to, so it reports
them open and unreviewed. The review and merge steps are in
[spec/10-code-review.md](spec/10-code-review.md) and the plugin's `teamflow:workflow` skill.

There are no local agent definitions: `.claude/agents/` does not exist. A local unprefixed
copy of a teamflow agent would bypass the plugin's spawn hook, which only polices
`teamflow:`-prefixed callers.

## 2. Prerequisites

| Need | Why | Check |
| :--- | :--- | :--- |
| Claude Code 2.1.304 or later | teamflow's documented minimum | `claude --version` |
| `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` at 2 or more (3 is the default from v2.1.219) | manager, lead, worker is three layers; the manager stops at startup if it reads `1` | `echo $CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` |
| `CLAUDE_CODE_FORK_SUBAGENT` not set to `0` | headless (`-p`/SDK) runs need fork mode on for leads to wait for their workers; teamflow does not support `-p` with it off (interactive sessions don't depend on it) | `echo $CLAUDE_CODE_FORK_SUBAGENT` |
| `gh` authenticated with `repo` scope | `tf` (forge `github`) reaches every Issue and PR through it; with no login, `tf` stops with exit 2 and never falls back | `gh auth status` |
| `uv`, Docker, a filled-in `.env` | the gate runs the integration tests against MinIO and MLflow | `cp .env.example .env`, then see [00-getting-started.md](00-getting-started.md) |
| The main worktree on `main` | `tf` and `teamflow-config` read `.claude/teamflow.json` from the main worktree and stop (exit 2) on any other branch; all work happens in separate worktrees | `git worktree list` |
| Push access to the remote; the branch ruleset applied (step 4) | review-lead's merge is enforced server-side only with the ruleset | repo settings |

`.claude/settings.json` also sets `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. teamflow's docs
do not recommend that setting and have not tested it with teamflow: if the leads behave
oddly, set it to `0`.

## 3. Files that define the stack

Everything is checked in except per-person settings and the manager's state.

| Path | Role |
| :--- | :--- |
| `.claude/settings.json` | declares the `ponytail`, `mattpocock` and `teamflow` plugin marketplaces and enables all three plugins; allows `Bash(tf *)` (without it auto mode refuses `tf` writes); sets `crossSessionInbound: accept` and `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` |
| `.claude/teamflow.json` | teamflow's per-repository config: `forge` (`github`), `remote`, `base`, `tdd`, `teamflow_auto_approve` and the loop bounds. `teamflow-config` prints the effective values and rejects unknown keys with exit 2 |
| `.claude/settings.local.json` | per-person overrides; gitignored |
| `CLAUDE.md` | conventions, hard rules, routing table; loaded by every agent |
| `AGENTS.md` | behavioural rules, Issue filing format, research output format; loaded by every agent |
| `scripts/gate.sh` | the merge gate; the implementer runs it, and CI mirrors it |
| `.github/workflows/gate.yml` | CI: the job `gate`, the required check |
| `.github/pull_request_template.md` | the PR body `tf pr create` requires filled in |
| `scripts/review-setup.sh`, `scripts/open-pr.sh` | a reviewer worktree with `.env` and the toolchain; a PR opener for use outside teamflow |
| `.manager-state.json` | the manager's loop state; created on first run; gitignored |

`.claude/teamflow.json` sets `tdd: required`, `teamflow_auto_approve: low`,
`loop.exit_severity_threshold: medium` and `loop.max_open_issues_to_continue: 0`. There is
no iteration cap and no human checkpoint key: teamflow removed both, and a config that
sets either makes every `tf` command exit 2. What the keys mean is in teamflow's
[docs/setup.md](https://github.com/Ribhu-Labs/teamflow/blob/main/docs/setup.md).

## 4. Setup

### Step 1. Clone and check the tree

```bash
git clone git@github.com:MyCookie/tuner.git
cd tuner
git fetch origin && git status        # on main, clean
ls .claude/agents                      # expect: No such file or directory
```

### Step 2. Install the toolchain and start the local stack

```bash
cp .env.example .env       # fill in HF_TOKEN and the judge endpoint
uv sync --extra dev --extra train   # the gate's integration tests need both extras
docker compose up -d minio minio-init mlflow
./scripts/gate.sh          # must be green on a clean main before any agent runs
```

Do not start the team on a red `main`. Every PR's gate would fail and the rounds would spin.

### Step 3. Enable the plugins

`.claude/settings.json` declares the marketplaces. The first session in a new checkout
asks you to trust and install them. Accept, or install by hand:

```
/plugin marketplace add Ribhu-Labs/teamflow
/plugin install teamflow@teamflow
```

teamflow depends on `ponytail@ponytail` (the simplicity-reviewer's engine) and
`mattpocock-skills@mattpocock` and installs both from their own marketplaces; a copy synced
from claude.ai does not satisfy the dependency. `mattpocock-skills` also supplies `/grilling`,
which you can use at intake to stress-test a goal before it reaches the manager.

After a merged change reaches the plugin you need `claude plugin update teamflow@teamflow
--scope project` and a fresh session: never `--resume` a session from before an update.

### Step 4. Set up the repository for GitHub mode (once, by a human)

`tf setup` applies what a plugin cannot: the CI workflow, labels, repository settings (auto-merge,
delete-branch-on-merge) and the branch ruleset requiring `gate` and `teamflow/code-review` with
no bypass actors. It is a dry run unless given `--apply`, never overwrites a file, and never
commits. Run it from a separate worktree on a setup branch, not from the main worktree:

```bash
git worktree add ../tuner-setup -b chore/103-teamflow-setup
cd ../tuner-setup
tf setup --forge github               # dry run: read what it would change
tf setup --forge github --apply       # only after confirming the ruleset with the repository owner
```

The ruleset makes GitHub refuse a merge without a green `gate` check and a success
`teamflow/code-review` status on the current head. It does not tell reviewer from author: every
agent shares the owner's `gh` login and could set that status or edit the ruleset, so it is a
process guard, not an access control ([spec/10 §9](spec/10-code-review.md)). Applying it is
the owner's decision, not an agent's. This step is tracked in Issue #103; until it is
done, `tf` checks the `gate` check and the verdict itself before merging, and nothing on
GitHub enforces them ([spec/10 §9](spec/10-code-review.md)).

### Step 5. Start the session

From the repo root, with the main worktree on `main`:

```bash
claude --agent teamflow:manager
```

Start it fresh from the repo root. `claude --resume` restores the session's original working
directory, so it cannot repair a session started from the wrong one. If the manager reads
`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` it stops at startup, names the variable and spawns
nothing. The leads and workers are never started by hand.

### Step 6. Give the manager a goal

State the goal in plain words. It routes per the table in
[CLAUDE.md](../CLAUDE.md#multi-agent-orchestration):

| You say | Goes to |
| :--- | :--- |
| a vague goal or feature idea | research |
| a specific task with no Issue | research |
| "fix #12, #15" | implementation; each PR is then reviewed and merged before the batch is reported |
| "review what we built" | review, diff-review mode (post-merge; only on request) |
| "merge-review PR #40" (a PR the implementation team did not open) | review, per-PR mode |

The manager stops for your approval of the `impl-lead` unit plan before any implementer is
spawned, unless every Issue in the batch is `severity:low` and none is `needs-discussion`
(`teamflow_auto_approve: low`); the conditions are in the workflow skill.

## 5. Verify the stack

Run these in order. Each is cheap and each catches a distinct failure.

1. The agents load. In a fresh session, run `/agents`. Only `teamflow:*` agents appear, no
   unprefixed ones.
2. The config is valid. From the main worktree, `teamflow-config` prints
   `"forge": "github"` and the values above, and exits 0; `tf mode` prints `github`. If it
   exits 2, read the message: an unknown key, or the main worktree is not on `main`.
3. `gh` works for `tf`. `tf pr list` runs without error.
4. Dry-run the implementation path on a trivial Issue (a typo in a doc). `impl-lead` must
   return a plan table and wait for approval (or state why it auto-approved), the implementer
   must work inside its worktree, the PR must reach exactly one `review-lead` per round that
   claims it (a pending `teamflow/code-review` status), and the merge must come from
   review-lead's `APPROVE`, not from the implementer.
5. Dry-run the review path. Tell the manager: "Review what we built, diff-review mode, no PRs
   listed, stop after the report." A healthy run spawns five reviewers, files no duplicate
   Issues, and reports a count and highest severity.

Clean up after steps 4-5: `git worktree list`, then `git worktree remove` for each leftover,
delete the merged branch, delete the `worktree-agent-*` branches that `isolation: worktree`
subagents leave behind (`git branch --list 'worktree-agent-*'`), and confirm `origin/main`
is still green.

## 6. Changing the stack

- The agent definitions ship with the plugin. To change a model, a tool list or a prompt,
  change teamflow upstream (or fork its marketplace and point `extraKnownMarketplaces` at the
  fork); do not add a local copy under `.claude/agents/`.
- Change the config in `.claude/teamflow.json` on a branch. It takes effect only once merged
  and pulled onto `main`, because `tf` reads it from the main worktree, so a PR under review
  cannot change the rules its own review runs under.
- After a plugin update or an edit to `.claude/settings.json`, start a fresh session. A running
  session keeps what it started with.
- Keep these docs and `CLAUDE.md` in step with the plugin's behaviour. When they disagree on
  mechanics (`tf` exit codes, the claim/gate/verdict/merge steps), the plugin's
  `teamflow:workflow` skill wins; record the mismatch here until it is fixed.
