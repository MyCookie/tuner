#!/usr/bin/env bash
# Push the current branch and open its PR (09-git-workflow.md §7, 10-code-review.md §3).
#
#   ./scripts/open-pr.sh "TNN — <task title>" <body-file>
#
# Exists because the prose version of this was broken in both documents: it set
# PR_BODY in one command and used it in the next three (shell state does not
# persist between an agent's commands), and one copy never defined it at all.
set -euo pipefail

title="${1:-}"
body="${2:-}"
template=".github/pull_request_template.md"

if [ -z "$title" ] || [ -z "$body" ]; then
    echo "usage: $0 \"TNN — <task title>\" <body-file>" >&2
    echo "       start from $template — gh does not apply it non-interactively." >&2
    exit 2
fi

if [ ! -f "$body" ]; then
    echo "open-pr: no such body file: $body" >&2
    echo "         cp $template <body-file>  and fill it in." >&2
    exit 2
fi

# An unedited template is a PR body that says nothing. gh would accept it.
if diff -q "$template" "$body" >/dev/null 2>&1; then
    echo "open-pr: $body is the unmodified template — fill it in first." >&2
    exit 2
fi

branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" = "HEAD" ]; then
    echo "open-pr: detached HEAD — run this from the feature branch." >&2
    exit 2
fi
if [ "$branch" = "main" ]; then
    echo "open-pr: refusing to open a PR from main (09 §1)." >&2
    exit 2
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "open-pr: working tree is dirty — commit or stash before publishing." >&2
    git status --short >&2
    exit 2
fi

git push -u origin "$branch"
gh pr create --base main --title "$title" --body-file "$body"
