#!/usr/bin/env python3
"""CI workflow pinning check (docs/spec/09-git-workflow.md §8): every workflow under
.github/workflows/ must pin each action to a full 40-character commit SHA, set
`persist-credentials: false` on every `actions/checkout`, and declare `permissions`
(at workflow level, or on every job). Run by scripts/gate.sh.

Usage: check_workflow_pins.py [WORKFLOW_DIR]   (default: .github/workflows)
Exits 1 on any violation, and also when the directory holds no workflow, so a
moved directory fails the gate instead of passing it empty.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _check(path: Path) -> list[str]:
    wf = yaml.safe_load(path.read_text()) or {}
    jobs = wf.get("jobs") or {}
    problems: list[str] = []
    if "permissions" not in wf:
        missing = [j for j, body in jobs.items() if "permissions" not in body]
        if missing or not jobs:
            problems.append(f"{path}: no `permissions:` at workflow level or on job(s) {missing}")
    for job, body in jobs.items():
        # A reusable-workflow call has `uses` on the job itself, not in a step.
        for step in [body, *body.get("steps", [])]:
            uses = step.get("uses")
            if uses is None or uses.startswith("./"):
                continue
            ref = uses.rpartition("@")[2] if "@" in uses else ""
            if not SHA_RE.match(ref):
                problems.append(f"{path}: job {job}: `{uses}` is not pinned to a full SHA")
            if uses.startswith("actions/checkout@") and (
                (step.get("with") or {}).get("persist-credentials") is not False
            ):
                problems.append(f"{path}: job {job}: `{uses}` lacks `persist-credentials: false`")
    return problems


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".github/workflows")
    files = sorted([*root.glob("*.yml"), *root.glob("*.yaml")])
    if not files:
        print(f"check_workflow_pins: no workflow files under {root}", file=sys.stderr)
        return 1
    problems = [p for f in files for p in _check(f)]
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
