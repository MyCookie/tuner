#!/usr/bin/env bash
# The merge gate (09-git-workflow.md §4). One script so that what the implementer
# ran and what the reviewer ran cannot drift (10-code-review.md §3, §4).
#
# Every step runs even after an earlier one fails — a reviewer wants the whole
# picture, not the first thing that broke. Exits non-zero if any step failed.
#
# Needs: the compose stack up (`docker compose up -d minio minio-init mlflow`)
# and the .env vars exported (`set -a; . ./.env; set +a`).
set -uo pipefail

cd "$(dirname "$0")/.."

results=()
failed=0

step() {
    local name="$1"
    shift
    printf '\n\033[1m── %s ─────────────────────────────\033[0m\n' "$name"
    if "$@"; then
        results+=("$name|pass")
    else
        results+=("$name|FAIL")
        failed=1
    fi
}

skip() {
    results+=("$1|skipped — $2")
}

# -- preflight: fail with an actionable message, not 50 confusing test errors --

if [ -z "${TUNER_S3_ENDPOINT:-}" ]; then
    echo "gate: TUNER_S3_ENDPOINT is unset — integration tests read credentials from the" >&2
    echo "      environment. Run:  set -a; . ./.env; set +a" >&2
    exit 2
fi

if ! curl -fsS --max-time 5 "${TUNER_S3_ENDPOINT}/minio/health/live" >/dev/null 2>&1; then
    echo "gate: no MinIO at ${TUNER_S3_ENDPOINT} — start the stack:" >&2
    echo "      docker compose up -d minio minio-init mlflow" >&2
    exit 2
fi

# -- lint & format ---------------------------------------------------------

step "ruff check" uv run ruff check .
step "ruff format --check" uv run ruff format --check .

# -- the pickle ban (05 §6, 06 §6) — CI greps for this; so does pre-commit --

pickle_ban() {
    if grep -rnE 'pickle\.(dump|load)|torch\.(save|load)\(' src/ scripts/ tests/ --include='*.py'; then
        echo "gate: banned pickle/torch.save/load reference above" >&2
        return 1
    fi
    return 0
}
step "pickle ban" pickle_ban

# -- tests: unit first (fast, no services), then unit+integration with coverage --

step "unit tests" uv run pytest
step "unit + integration, coverage" \
    uv run pytest -m 'not e2e and not gpu and not slow' \
    --cov=src/tuner --cov-report=term-missing

# -- T14 scripts, once they exist -------------------------------------------

if [ -f scripts/check_test_ids.py ]; then
    step "spec↔test traceability" uv run python scripts/check_test_ids.py
else
    skip "spec↔test traceability" "scripts/check_test_ids.py not built until T14"
fi

if [ -f scripts/check_coverage.py ]; then
    step "per-module coverage gate" uv run python scripts/check_coverage.py
else
    skip "per-module coverage gate" "scripts/check_coverage.py not built until T14"
fi

if [ -f scripts/check_docs.py ]; then
    step "docs links & IDs" uv run python scripts/check_docs.py
else
    skip "docs links & IDs" "scripts/check_docs.py not built until T14"
fi

# -- transcript: paste this into the PR body / review comment ----------------

printf '\n\033[1m── gate summary ─────────────────────────────\033[0m\n\n'
echo '| check | result |'
echo '| :--- | :--- |'
for row in "${results[@]}"; do
    echo "| ${row%%|*} | ${row#*|} |"
done
echo

if [ "$failed" -ne 0 ]; then
    echo "gate: FAILED — do not push for review, do not merge." >&2
    exit 1
fi
echo "gate: green."
