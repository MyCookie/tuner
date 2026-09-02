#!/usr/bin/env bash
# The pickle ban (CLAUDE.md hard rule 2, 05-infrastructure.md §6, 06-testing.md §6).
# Extracted from scripts/gate.sh (T14) so its own "every push" CI job can run the
# identical check without duplicating the regex -- one script, so what gate.sh runs
# locally and what CI runs on every push can't drift (mirrors gate.sh's own reason
# for existing, 09-git-workflow.md §4/10-code-review.md §3-4).
set -uo pipefail

cd "$(dirname "$0")/.."

# Matches the serialisers themselves, disabled safe-serialization (what actually
# makes HF emit one), and any *named* weight file with the banned extension --
# including the multi-shard form a real checkpoint produces. Requiring a name
# character before the extension keeps TRN-I-003's own suffix assertion out of the
# net: a bare quoted suffix has no filename in front of it. grep exits 0 = found,
# 1 = clean, 2 = error; only 1 passes, so a broken grep fails this instead of
# silently passing it.
#
# NB: this comment deliberately never spells out a banned filename. The scanner
# scans its own directory, and naming one here would make it fail on itself.
_BAN_RE='pickle\.(dump|load|Pickler|Unpickler)|torch\.(save|load)\(|joblib\.(dump|load)|^[[:space:]]*(import|from)[[:space:]]+[a-zA-Z_0-9,.[:space:]]*\b(_?pickle|cPickle|dill)\b|from[[:space:]]+joblib[[:space:]]+import[[:space:]]+.*\b(dump|load)\b|safe_serialization[[:space:]]*=[[:space:]]*False|[A-Za-z0-9_-]+\.bin\b'

grep -rnE "$_BAN_RE" src/ scripts/ tests/ docker/ configs/ \
    --include='*.py' --include='*.sh' --include='*.yaml' --include='*.yml' \
    --include='Dockerfile' --include='*.Dockerfile'
rc=$?
case "$rc" in
    1) exit 0 ;;
    0)
        echo "check_pickle_ban: banned serialiser/.bin reference above (CLAUDE.md hard rule 2)" >&2
        exit 1
        ;;
    *)
        echo "check_pickle_ban: grep failed with status $rc" >&2
        exit 1
        ;;
esac
