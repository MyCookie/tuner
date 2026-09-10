# Agent Behavioural Rules
# Loaded automatically by all Claude Code agents and teammates.

## Role bounds
- Every agent has exactly one job. If a task falls outside your defined
  scope, route it to the correct session via cross-session messaging rather
  than absorbing it.
- "Owns X exclusively" means read and write access to that path only.
  Any other path is off-limits unless listed in your role definition.
- "Read-only" means no Edit, Write, MultiEdit, or destructive Bash
  commands. This is stated here AND enforced via disallowedTools.

## Task completion vocabulary
- A task is **done** when: the defined output exists, tests pass (if
  applicable), and you have reported completion to your lead or @manager.
- A task is **blocked** when: a dependency is unmet or you have discovered
  a file-ownership conflict. Message your lead immediately; do not proceed.
- A task is **in-progress** — claim it in the task list before starting.
  Never work on an unclaimed task.

## Communication rules
- Keep messages terse and structured. Lead with status, follow with detail.
- Do not send progress updates mid-task unless blocked.
- Escalate to the human (not just your lead) if: a file ownership conflict
  cannot be resolved, a critical security vulnerability is found, or any
  loop iteration produces more high-severity Issues than it resolves.

## Issue filing format
Every GitHub Issue filed must include:
- **Severity**: high / medium / low / needs-discussion
- **Type label**: type:task (research) / area:* (review findings)
- **Location**: file path and line number(s) where applicable
- **Finding / Task**: what needs doing and why
- **Convention violated**: cite the specific rule, or state needs-discussion
- Never assign a severity that cannot be grounded in a documented convention
  or a clear correctness defect.

## Loop context — what counts as "new"
When the review team operates in diff-review mode (post-implementation):
- A finding is **new** if it was not present in a prior review cycle's
  Issues, OR if a prior Issue was closed but the root cause was not fixed.
- A finding is **duplicate** if the same file:line and defect type already
  has an open Issue. Do not file a duplicate — add a comment to the
  existing Issue instead.
- The manager uses the net new-Issue count per cycle to evaluate loop
  termination, not the gross count.

## Worktree discipline (implementation teammates only)
- cd into your assigned worktree as the very first command.
- Never read or modify files outside your worktree directory.
- Run `git status` before opening a PR to confirm no accidental out-of-scope
  changes are staged.

## Research output format (researcher teammates only)
Structure findings as:
- **Finding**: what was discovered
- **Confidence**: high / medium / low
- **Source**: file:line, URL, or document reference
- **Recommendation**: what this implies for the implementation task
- **Conflicts with**: any other finding this contradicts, if applicable
