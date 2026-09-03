"""Deterministic train/eval split assignment (docs/spec/03-components/tokenizer.md core
logic 4)."""

from __future__ import annotations

import hashlib
from typing import Literal

# The max value an 8-hex-digit number can hold -- the divisor that turns the digest
# prefix into a fraction in [0.0, 1.0).
_MAX_8_HEX_DIGITS = 0xFFFFFFFF


def assign_split(record_id: str, eval_fraction: float) -> Literal["train", "eval"]:
    """A record goes to eval iff `int(sha256(record_id)[:8], 16) / 0xFFFFFFFF <
    eval_fraction`, else train (docs/spec/03-components/tokenizer.md core logic 4).

    Depends only on `record_id` and `eval_fraction` -- no shuffling, no run-specific
    state -- so it is stable across re-runs, across machines, and independent of the
    order records are processed in (TOK-U-001..004)."""
    digest_prefix = hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:8]
    fraction = int(digest_prefix, 16) / _MAX_8_HEX_DIGITS
    return "eval" if fraction < eval_fraction else "train"
