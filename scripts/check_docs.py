#!/usr/bin/env python3
"""Docs verification (docs/08-test-specs/README.md "Beyond the suites" -- "Docs
verification"): relative-link resolution across `docs/`, plus the spec<->test
traceability checks of `check_test_ids.py` (imported and re-run directly here, not
reimplemented, so the two can never drift apart).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import check_test_ids  # noqa: E402 -- needs sys.path set first

REPO_ROOT = Path(__file__).parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# [text](target) -- every link in this repo's docs is a plain relative path to
# another doc; there are no external http(s) URLs and no #fragment anchors anywhere
# in docs/ (confirmed by grep at implementation time). A target with a URI scheme
# (http:, mailto:, ...) is skipped, not resolved -- not ours to check.
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _check_links() -> list[str]:
    problems: list[str] = []
    for path in sorted(DOCS_DIR.rglob("*.md")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for match in LINK_RE.finditer(line):
                target = match.group(1)
                if _SCHEME_RE.match(target):
                    continue
                target_path = (path.parent / target).resolve()
                # .exists(), not .is_file(): a directory link (e.g. "03-components/")
                # is valid too.
                if not target_path.exists():
                    rel = path.relative_to(REPO_ROOT)
                    problems.append(f"{rel}:{lineno}: broken link -> {target}")
    return problems


def main() -> int:
    link_problems = _check_links()
    if link_problems:
        print("check_docs: FAILED -- broken links:", file=sys.stderr)
        for problem in link_problems:
            print(f"  - {problem}", file=sys.stderr)
    else:
        print("check_docs: every relative link in docs/ resolves.")

    ids_exit_code = check_test_ids.main()

    return 1 if link_problems else ids_exit_code


if __name__ == "__main__":
    sys.exit(main())
