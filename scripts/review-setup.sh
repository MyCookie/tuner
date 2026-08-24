#!/usr/bin/env bash
# Prepare a reviewer worktree to review one branch (10-code-review.md §4).
#
# Exists because this was prose for five review rounds and was wrong in three
# of them: shell state does not persist between an agent's commands, so any
# runbook that sets a variable in one step and uses it in the next silently
# does the wrong thing. A script is one command, and `gate.sh` syntax-checks it.
#
#   ./scripts/review-setup.sh <branch-under-review>
set -euo pipefail

branch="${1:-}"
if [ -z "$branch" ]; then
    echo "usage: $0 <branch-under-review>" >&2
    exit 2
fi

main_tree=$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')
here=$(git rev-parse --show-toplevel)

# Refuse to run in the implementer's checkout: this detaches HEAD, and doing
# that to the tree someone is working in would be destructive. Prose could ask;
# a script can refuse.
if [ "$here" = "$main_tree" ]; then
    echo "review-setup: refusing to run in the main worktree ($main_tree)." >&2
    echo "              Reviews run in their own worktree — see 10-code-review.md §4." >&2
    exit 2
fi

git fetch origin --quiet

if ! git rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null; then
    echo "review-setup: no branch '$branch' on origin." >&2
    echo "              Check the name; git's own error here is unhelpfully about paths." >&2
    exit 2
fi

# Detached on purpose: the branch is checked out in the implementer's tree, and
# reviewing origin's copy is what makes this a review of what was published.
git checkout --detach "origin/$branch" --quiet
echo "review-setup: detached at $(git rev-parse --short HEAD) (origin/$branch)"

if [ ! -f "$main_tree/.env" ]; then
    echo "review-setup: no .env in $main_tree — stop and report this." >&2
    echo "              Do not invent credentials or skip the integration tests." >&2
    exit 2
fi
cp "$main_tree/.env" .env
echo "review-setup: copied .env from the main worktree (delete it when you finish)"

# `dev` is an extra, so a bare `uv sync` uninstalls ruff, pytest and hypothesis.
# `train` is also needed from T11 on: tests/integration/test_trainer.py imports
# torch/peft/accelerate at collection time, so without it pytest fails to even
# collect the file rather than cleanly skipping it -- always synced together with
# `dev`, not conditionally, to keep this script the one thing that can't drift from
# what gate.sh itself needs (10-code-review.md §3, §4).
uv sync --extra dev --extra train --quiet
echo "review-setup: toolchain installed (uv sync --extra dev --extra train)"

echo
echo "Ready. Next:  ./scripts/gate.sh        # reads .env itself; no wrapper needed"
echo "When done:    rm -f .env               # a real credentials file"
