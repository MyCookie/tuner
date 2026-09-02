#!/usr/bin/env python3
"""Per-module 100% coverage gate (docs/08-test-specs/README.md "Coverage policy":
"100% required (per-module fail_under...): tuner/core/*, tuner/cleaner/rules.py,
tuner/cleaner/patterns.py, tuner/judge/prompts.py, judge reply-parsing,
tuner/models/*, tuner/tokenizer/split.py, tuner/tokenizer/masking.py -- pure logic
has no excuse").

Reads the `.coverage` data file gate.sh's own "unit + integration, coverage" step
already wrote (via `coverage json`, not by re-running pytest) and checks every
required module's own `percent_covered` (statements + branches combined, since
`branch = true` in `[tool.coverage.run]`) is exactly 100.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# The doc's own dir globs (tuner/core/*, tuner/models/*) are expanded for real via
# Path.glob below, not hardcoded -- a hardcoded list would let a future file added to
# either directory silently escape the 100% gate (PR #14 round 1 review finding 6).
# The individual-file entries genuinely are single files named in the doc, so they
# stay literal. "judge reply-parsing" isn't a literal path in the doc -- it names the
# score/reasoning parsing logic in judge/client.py, so that whole file is the
# concrete target here.
_REQUIRED_100_PERCENT_GLOBS = (
    "src/tuner/core/*.py",
    "src/tuner/models/*.py",
)
_REQUIRED_100_PERCENT_FILES = (
    "src/tuner/cleaner/rules.py",
    "src/tuner/cleaner/patterns.py",
    "src/tuner/judge/client.py",
    "src/tuner/judge/prompts.py",
    "src/tuner/tokenizer/split.py",
    "src/tuner/tokenizer/masking.py",
)


def _required_100_percent() -> tuple[str, ...]:
    globbed = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for pattern in _REQUIRED_100_PERCENT_GLOBS
        for p in REPO_ROOT.glob(pattern)
        if p.name != "__init__.py"  # empty, always 100%, adds nothing to the gate
    )
    return tuple(globbed) + _REQUIRED_100_PERCENT_FILES


def main() -> int:
    data_file = REPO_ROOT / ".coverage"
    if not data_file.exists():
        print(
            "check_coverage: no .coverage data file -- run the coverage step first:\n"
            "      uv run pytest -m 'not e2e and not gpu and not slow' --cov=src/tuner",
            file=sys.stderr,
        )
        return 2

    result = subprocess.run(
        ["uv", "run", "coverage", "json", "-o", "-"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"check_coverage: `coverage json` failed:\n{result.stderr}", file=sys.stderr)
        return 1

    files = json.loads(result.stdout)["files"]
    required = _required_100_percent()

    failures = []
    for rel_path in required:
        file_report = files.get(rel_path)
        if file_report is None:
            failures.append(f"{rel_path}: not present in the coverage report (never imported?)")
            continue
        summary = file_report["summary"]
        percent = summary["percent_covered"]
        if percent < 100.0:
            failures.append(
                f"{rel_path}: {percent:.2f}% (missing {summary['missing_lines']} line(s), "
                f"{summary['missing_branches']} branch(es))"
            )

    if failures:
        print("check_coverage: FAILED -- not at 100%:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"check_coverage: all {len(required)} required modules at 100%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
