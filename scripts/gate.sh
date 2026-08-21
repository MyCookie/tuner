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

# The test toolchain lives in the `dev` extra, so a bare `uv sync` UNINSTALLS it and
# every check below fails for a reason that has nothing to do with the branch.
for tool in ruff pytest; do
    if ! uv run --no-sync "$tool" --version >/dev/null 2>&1; then
        echo "gate: $tool is not installed in .venv — the test toolchain is an extra:" >&2
        echo "      uv sync --extra dev" >&2
        exit 2
    fi
done

# -- lint & format ---------------------------------------------------------

step "ruff check" uv run ruff check .
step "ruff format --check" uv run ruff format --check .

# -- the pickle ban (05 §6, 06 §6) — CI greps for this; so does pre-commit --

# CLAUDE.md hard rule 2 / 05 §6: SafeTensors only. Covers the serialisers themselves,
# not just their call sites, and `.bin` weights. grep exits 0 = found, 1 = clean, 2 = error;
# only 1 is a pass, so a broken grep fails the gate instead of silently passing it.
pickle_ban() {
    grep -rnE "pickle\.(dump|load)|torch\.(save|load)\(|^[[:space:]]*(import|from)[[:space:]]+(pickle|cPickle|dill|joblib)\b|pytorch_model\.bin|\.bin[\"']" \
        src/ scripts/ tests/ docker/ configs/ --include='*.py' --include='*.sh' \
        --include='*.yaml' --include='*.yml' --include='Dockerfile' --include='*.Dockerfile'
    local rc=$?
    case "$rc" in
        1) return 0 ;;
        0) echo "gate: banned pickle/torch.save/.bin reference above (CLAUDE.md hard rule 2)" >&2; return 1 ;;
        *) echo "gate: pickle-ban grep failed with status $rc" >&2; return 1 ;;
    esac
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
