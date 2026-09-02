#!/usr/bin/env python3
"""Secret scan (docs/08-test-specs/README.md "Secret scanning": "gitleaks (or
equivalent grep script) in pre-commit and CI over the full history of the branch
being merged"). This is the "equivalent grep script" -- no external gitleaks binary
dependency, so it runs anywhere `uv run` does, at the cost of being pattern-based
rather than entropy-based.

Usage:
  check_secrets.py [FILE ...]             working-tree scan (pre-commit's own use:
                                           staged files only). With none, scans
                                           every git-tracked file -- `.env` itself is
                                           never tracked (.gitignore + pre-commit's
                                           own filename block), so a full-tree scan
                                           never needs a special case for it.
  check_secrets.py --since-merge-base REF  branch-history scan (CI's own use): every
                                           line ever *added* since REF, across every
                                           commit on the branch individually -- a
                                           value introduced in one commit and removed
                                           in a later one on the same branch still
                                           leaked into history and a plain REF..HEAD
                                           diff would hide it (git only shows the net
                                           change); `git log -p` walks each commit's
                                           own patch instead.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# AWS-shaped access key ID -- low false-positive risk, no placeholder exemption needed.
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
# A private-key file's own header line -- .pem/.key content pasted somewhere it shouldn't be.
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
# A credential-shaped variable assigned a long literal -- deliberately not restricted
# to base64/hex (unlike tests/unit/test_infra_static.py's own narrower _TOKEN_SHAPED
# check, which only ever needs to rule out AWS-style keys in .env.example): real
# secrets come in many shapes (Stripe's "sk_live_...", GitHub's "ghp_...", JWTs, ...),
# so the charset stays broad and the "isn't a placeholder" check below is what keeps
# false positives down. `${...}` compose interpolation (the sanctioned pattern,
# CLAUDE.md hard rule 3) and common placeholder markers are excluded so the scanner
# doesn't fail on its own codebase's env-var *names* or documented example values.
_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![a-zA-Z-])(secret|password|api[_-]?key|access[_-]?key|token)(?![a-zA-Z])\w*"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9+/_.-]{20,})['\"]?"
)
_PLACEHOLDER_RE = re.compile(
    r"(?i)changeme|example|dummy|unused|placeholder|xxx|your[_-]|<.*>|\$\{"
)

# Extensions never worth scanning: binary or generated, would just produce noise.
_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".lock", ".safetensors"}


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


def _scan_line(line: str, where: str) -> list[str]:
    """The three pattern checks, applied to one line of text -- shared by the
    working-tree scan (a real line in a real file) and the history scan (an added
    line in a diff hunk); `where` is whatever locator makes sense for the caller."""
    problems = []
    if _AWS_KEY_RE.search(line):
        problems.append(f"{where}: AWS-shaped access key ID")
    if _PRIVATE_KEY_RE.search(line):
        problems.append(f"{where}: private key header")
    for match in _ASSIGNMENT_RE.finditer(line):
        value = match.group(2)
        if _PLACEHOLDER_RE.search(line):
            continue
        # A real secret's value is essentially always a letter/digit mix (AWS
        # keys, Stripe/GitHub tokens, JWTs, ...); a plain Python identifier or
        # attribute chain (e.g. `hf_tokenizer.eos_token_id`) never contains a
        # digit. Cheap stand-in for real entropy analysis (gitleaks' own
        # approach) -- catches the secret shapes this project actually uses
        # without flagging every long dotted name in the codebase.
        if not any(c.isdigit() for c in value):
            continue
        problems.append(f"{where}: {match.group(1)}-shaped variable assigned a real-looking value")
    return problems


def _scan_file(path: Path) -> list[str]:
    if path.suffix in _SKIP_SUFFIXES or not path.is_file():
        return []
    try:
        text = path.read_text()
    except (UnicodeDecodeError, OSError):
        return []

    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        where = f"{path.relative_to(REPO_ROOT)}:{lineno}"
        problems.extend(_scan_line(line, where))
    return problems


def _scan_history(base_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "log", "-p", "--no-color", f"{base_ref}..HEAD", "--", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"check_secrets: `git log -p {base_ref}..HEAD` failed:\n{result.stderr}",
            file=sys.stderr,
        )
        sys.exit(2)

    problems: list[str] = []
    commit = "?"
    current_file = "?"
    for line in result.stdout.splitlines():
        if line.startswith("commit "):
            commit = line.split(maxsplit=1)[1][:12]
        elif line.startswith("+++ "):
            # "+++ b/path/to/file" for an add/modify, "+++ /dev/null" for a deletion
            # (a pure deletion has no added lines to scan anyway).
            tail = line[len("+++ ") :]
            current_file = tail[2:] if tail.startswith("b/") else tail
        elif line.startswith("+") and not line.startswith("+++"):
            if Path(current_file).suffix in _SKIP_SUFFIXES:
                continue
            problems.extend(_scan_line(line[1:], f"{commit}:{current_file}"))
    return problems


def _report(problems: list[str], scanned_description: str) -> int:
    if problems:
        print("check_secrets: FAILED -- possible secret(s) found:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "  If this is a false positive, adjust check_secrets.py's placeholder "
            "pattern rather than committing anyway.",
            file=sys.stderr,
        )
        return 1

    print(f"check_secrets: {scanned_description}, nothing found.")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--since-merge-base":
        base_ref = argv[1]
        problems = _scan_history(base_ref)
        return _report(problems, f"branch history since {base_ref}")

    # .resolve(): pre-commit passes repo-relative paths (`git diff --name-only`), and
    # _scan_file's own `path.relative_to(REPO_ROOT)` needs an absolute path to compare.
    paths = [Path(p).resolve() for p in argv] if argv else _tracked_files()

    problems = []
    for path in paths:
        problems.extend(_scan_file(path))
    return _report(problems, f"{len(paths)} file(s) scanned")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
