#!/usr/bin/env bash
# The merge gate (09-git-workflow.md §4). One script so that what the implementer
# ran and what the reviewer ran cannot drift (10-code-review.md §3, §4).
#
# Every step runs even after an earlier one fails — a reviewer wants the whole
# picture, not the first thing that broke. Exits non-zero if any step failed.
#
# Needs the compose stack up: `docker compose up -d minio minio-init mlflow`.
# Credentials come from .env, which this script loads itself — nothing to export.
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

# Load .env ourselves rather than telling the caller to export it first. That
# instruction was wrong for agents anyway: no shell state survives between
# commands, so exporting in one call and running the gate in the next leaves the
# gate with nothing. Sourced only if any required var is missing, so a fully
# exported environment is left alone.
_missing_creds() {
    [ -z "${TUNER_S3_ENDPOINT:-}" ] || [ -z "${TUNER_S3_ACCESS_KEY:-}" ] ||
        [ -z "${TUNER_S3_SECRET_KEY:-}" ]
}

if _missing_creds && [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# Checked after sourcing, and all three: a partial environment used to slip past a
# single-variable check and surface as dozens of unexplained integration failures.
if _missing_creds; then
    echo "gate: incomplete object-store credentials. Missing:" >&2
    for v in TUNER_S3_ENDPOINT TUNER_S3_ACCESS_KEY TUNER_S3_SECRET_KEY; do
        [ -z "$(eval "printf %s \"\${$v:-}\"")" ] && echo "        $v" >&2
    done
    echo "      cp .env.example .env and fill it in, or export all three yourself." >&2
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

# CLAUDE.md hard rule 2 / 05 §6: SafeTensors only. Matches the serialisers themselves,
# disabled safe-serialization (what actually makes HF emit one), and any *named* weight
# file with the banned extension — including the multi-shard form a real checkpoint
# produces, which the previous revision missed. Requiring a name character before the
# extension is what keeps TRN-I-003's own suffix assertion out of the net: a bare quoted
# suffix has no filename in front of it. grep exits 0 = found, 1 = clean, 2 = error; only
# 1 passes, so a broken grep fails the gate instead of silently passing it.
#
# NB: this comment deliberately never spells out a banned filename. The scanner scans its
# own directory, and naming one here would make the gate fail on itself — as it did twice.
_BAN_RE='pickle\.(dump|load|Pickler|Unpickler)|torch\.(save|load)\(|joblib\.(dump|load)|^[[:space:]]*(import|from)[[:space:]]+[a-zA-Z_0-9,.[:space:]]*\b(_?pickle|cPickle|dill)\b|from[[:space:]]+joblib[[:space:]]+import[[:space:]]+.*\b(dump|load)\b|safe_serialization[[:space:]]*=[[:space:]]*False|[A-Za-z0-9_-]+\.bin\b'

pickle_ban() {
    grep -rnE "$_BAN_RE" src/ scripts/ tests/ docker/ configs/ \
        --include='*.py' --include='*.sh' --include='*.yaml' --include='*.yml' \
        --include='Dockerfile' --include='*.Dockerfile'
    local rc=$?
    case "$rc" in
        1) return 0 ;;
        0) echo "gate: banned serialiser/.bin reference above (CLAUDE.md hard rule 2)" >&2; return 1 ;;
        *) echo "gate: pickle-ban grep failed with status $rc" >&2; return 1 ;;
    esac
}
step "pickle ban" pickle_ban

# The runbook lives in scripts/ now (10-code-review.md §3, §4), so a broken one is
# a broken process. Cheapest possible guard: it must parse.
shell_syntax() {
    local rc=0 f
    for f in scripts/*; do
        [ -f "$f" ] || continue
        # By shebang, not by extension: scripts/pre-commit is bash with no suffix,
        # and a `scripts/*.sh` glob skipped it while claiming full coverage.
        case "$(head -1 "$f")" in
            '#!'*sh*) bash -n "$f" || { echo "gate: $f does not parse" >&2; rc=1; } ;;
        esac
    done
    return "$rc"
}
step "shell scripts parse" shell_syntax

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
