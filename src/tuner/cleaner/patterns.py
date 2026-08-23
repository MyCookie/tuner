"""PII regex patterns (docs/03-components/cleaner.md core logic 4), unit-tested in isolation.

Each pattern is deliberately narrow rather than maximally permissive: a false negative
(PII that slips through) is a privacy bug, but a false positive (a version string or a zip
code mangled into `[PHONE]`) silently corrupts training data on every run. The negative
cases pinned in CLN-U-002/004 are the actual contract; a "more thorough" regex that breaks
one of them is a regression, not an improvement.
"""

from __future__ import annotations

import re

# Local part, then a domain that must itself contain a dot and a >=2-letter TLD -- this is
# what rejects `user@localhost` (no dot in the domain) without special-casing it, and
# requiring an alnum immediately before `@` is what rejects `@handle` (nothing to anchor
# a local part to).
EMAIL_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._%+-]*[A-Za-z0-9])?"
    r"@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,}"
)

# Four shapes, each anchored to the exact digit-group layout of a real phone number so
# structurally-similar non-phone strings don't match:
#   +1 (555) 123-4567   -- country code, parenthesized area code, hyphenated local number
#   (555) 123-4567       -- same, without the country code (PR #7 review round 1 finding 4)
#   +44 20 7946 0958     -- country code, then 2-4 space-separated digit groups
#   555-123-4567         -- bare 3-3-4 hyphenated groups
# None of these accept `.` as a separator, which is what keeps a version string like
# `v2.10.3` out. The 3-3-4 shape is what distinguishes a hyphenated phone number from an
# ISO date (4-2-2, e.g. `2026-07-20`) using the same hyphen separator. A bare digit run
# with no separator at all (a zip code, `port 8080`) never matches any of the four shapes.
PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:"
    r"\+\d{1,3}[ ]?\(\d{3}\)[ ]?\d{3}[-\s]?\d{4}"
    r"|\(\d{3}\)[ ]?\d{3}[-\s]?\d{4}"
    r"|\+\d{1,3}(?:[ ]\d{2,4}){2,4}"
    r"|\d{3}-\d{3}-\d{4}"
    r")"
    r"(?!\d)"
)
