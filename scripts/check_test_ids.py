#!/usr/bin/env python3
"""Spec <-> test traceability (docs/spec/08-test-specs/README.md "Conventions" /
"Traceability"). Fails on:

- a spec case (a table row's own ID column, in `docs/spec/08-test-specs/*.md`) with no
  test function tagging it,
- a test function's docstring tagging an ID with no matching spec case,
- an ID defined twice within the spec tables themselves (a doc authoring mistake,
  e.g. a copy-pasted row whose number wasn't bumped).

A case ID matches `<PREFIX>-<U|I|E|G|S>-<NNN>` (spec/08 README "Conventions"). One ID
legitimately tagging *several* test functions is not flagged: "table-driven by
default" (08 README) covers grouping several data-driven facets of one case under
several named functions, not just literal `pytest.mark.parametrize` -- e.g.
CORE-I-042's "round-trip / missing-key / other-error" split across three functions.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SPEC_DIR = REPO_ROOT / "docs" / "spec" / "08-test-specs"
TEST_DIRS = (
    REPO_ROOT / "tests" / "unit",
    REPO_ROOT / "tests" / "integration",
    REPO_ROOT / "tests" / "e2e",
)

ID_RE = re.compile(r"[A-Z]+-[UIEGS]-\d{3}")
# A table row's own ID column: "| CLN-U-004 | ..." or "| TRN-G-020 *(gpu)* | ..." --
# anchored at the cell's start (not required to be the *whole* cell, since a G/S row
# may append an inline annotation) so a cross-reference to another suite's ID inside
# some other row's Scenario/Expected/Notes prose (very common: "mirrors TRN-I-009's
# own generic-exit-1 path") is never mistaken for a fresh definition -- prose
# references are never the first thing after the row's opening `|`.
SPEC_ROW_RE = re.compile(r"^\|\s*(" + ID_RE.pattern + r")\b")
DOCSTRING_ID_RE = re.compile(r"^(" + ID_RE.pattern + r"):")

# Cases whose own spec row says they land in a later task (docs/07-build-plan.md's
# T15 Suite line: "TRN-G-020 ...; INF-S-020..021") -- G/S-marked, GPU-only or
# nightly-slow-lane, not yet built at T14. Explicit and individually justified
# (mirrors the coverage policy's own "# pragma: no cover ... names the T15 manual
# check" discipline) rather than a blanket "skip every G/S case" rule, so a *future*
# G/S case introduced within an already-built task still has to have a real test.
_DEFERRED = {
    "TRN-G-020": 'docs/spec/08-test-specs/trainer.md -- "executed in T15"',
    "INF-S-020": "docs/spec/07-build-plan.md T15 Suite line",
    "INF-S-021": "docs/spec/07-build-plan.md T15 Suite line",
}


def _spec_ids() -> dict[str, list[str]]:
    """ID -> list of "file:line" locations it's defined at (table rows only)."""
    locations: dict[str, list[str]] = {}
    for path in sorted(SPEC_DIR.glob("*.md")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            match = SPEC_ROW_RE.match(line)
            if match:
                where = f"{path.relative_to(REPO_ROOT)}:{lineno}"
                locations.setdefault(match.group(1), []).append(where)
    return locations


def _test_ids() -> set[str]:
    """Every ID tagged by at least one test function's docstring."""
    found: set[str] = set()
    for test_dir in TEST_DIRS:
        for path in sorted(test_dir.rglob("test_*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if not node.name.startswith("test_"):
                    continue
                docstring = ast.get_docstring(node)
                if not docstring:
                    continue
                first_line = docstring.strip().splitlines()[0]
                match = DOCSTRING_ID_RE.match(first_line)
                if match:
                    found.add(match.group(1))
    return found


def main() -> int:
    spec_ids = _spec_ids()
    test_ids = _test_ids()

    problems: list[str] = []
    deferred_notes: list[str] = []

    for case_id, where in sorted(spec_ids.items()):
        if len(where) > 1:
            problems.append(f"duplicated spec case {case_id}: {', '.join(where)}")

    untested = sorted(set(spec_ids) - test_ids)
    for case_id in untested:
        if case_id in _DEFERRED:
            deferred_notes.append(f"{case_id} (deferred: {_DEFERRED[case_id]})")
            continue
        problems.append(f"spec case with no test: {case_id} ({spec_ids[case_id][0]})")

    unspecced = sorted(test_ids - set(spec_ids))
    for case_id in unspecced:
        problems.append(f"test with no spec case: {case_id}")

    if problems:
        print("check_test_ids: FAILED", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"check_test_ids: {len(spec_ids)} spec cases, all tagged, no orphan test tags.")
    if deferred_notes:
        print(f"  ({len(deferred_notes)} deferred to a later task: {', '.join(deferred_notes)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
