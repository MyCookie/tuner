"""Cleaner scrubbing, structure-mapping, filtering, and dedup rules
(docs/03-components/cleaner.md core logic 3-6).

Pure logic only — no storage, no CLI. `tuner.cleaner.cli` orchestrates I/O and calls
into this module per record.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from tuner.cleaner.patterns import EMAIL_RE, PHONE_RE
from tuner.core.config import IngestSourceMapping
from tuner.core.ids import canonical_hash

DROP_REASONS = frozenset(
    {"unmappable", "too_short", "too_long", "empty_turn", "bad_structure", "duplicate"}
)

_VALID_ROLES = frozenset({"system", "user", "assistant"})
_VALID_CONTENT_TYPES = frozenset({"text", "image", "audio"})
_SCRUBBERS: dict[str, re.Pattern[str]] = {"email": EMAIL_RE, "phone": PHONE_RE}
_BLANK_LINE_RUN_RE = re.compile(r"\n{4,}")

# JSONL flat shapes cleaner.md recognizes, mapped like a CSV row (03-components/cleaner.md
# core logic 3): exactly these keys, nothing else, or it's unmappable.
_FLAT_KEY_SETS: dict[frozenset[str], tuple[str, str]] = {
    frozenset({"prompt", "response"}): ("prompt", "response"),
    frozenset({"prompt", "response", "system"}): ("prompt", "response"),
    frozenset({"question", "answer"}): ("question", "answer"),
    frozenset({"question", "answer", "system"}): ("question", "answer"),
}


def scrub(text: str, pii_scrubbers: Iterable[str] = ("email", "phone")) -> str:
    """One text value through the full scrub chain, in order (core logic 4):
    NFC normalize -> strip control chars (keep \\n/\\t) -> PII placeholders -> collapse
    long blank-line runs -> trim.

    Idempotent (CLN-U-030): scrub(scrub(x)) == scrub(x). This holds because each step is
    individually a no-op on its own output, and PII scrubbing runs *after* control-char
    stripping — a control char sitting between digits could otherwise knit two
    non-matching fragments into something PII-shaped only after stripping, and this
    ordering guarantees that newly-formed pattern still gets caught in the same pass.
    """
    text = unicodedata.normalize("NFC", text)
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")
    for name in pii_scrubbers:
        pattern = _SCRUBBERS[name]
        text = pattern.sub(f"[{name.upper()}]", text)
    text = _BLANK_LINE_RUN_RE.sub("\n\n\n", text)
    return text.strip()


def _build_conversation(prompt: str, response: str, system: str | None) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    if system:
        turns.append({"role": "system", "content": [{"type": "text", "value": system}]})
    turns.append({"role": "user", "content": [{"type": "text", "value": prompt}]})
    turns.append({"role": "assistant", "content": [{"type": "text", "value": response}]})
    return turns


def map_csv_row(
    raw: dict[str, Any], mapping: IngestSourceMapping | None
) -> list[dict[str, Any]] | None:
    """CSV structure mapping (core logic 3). None (unmappable) if the row has no usable
    mapping, or a missing/empty prompt or response."""
    if mapping is None or not mapping.prompt_column or not mapping.response_column:
        return None
    prompt = raw.get(mapping.prompt_column)
    response = raw.get(mapping.response_column)
    if not prompt or not response:
        return None
    system = raw.get(mapping.system_column) if mapping.system_column else None
    return _build_conversation(prompt, response, system)


def map_jsonl_raw(raw: dict[str, Any]) -> list[dict[str, Any]] | None:
    """JSONL structure mapping (core logic 3): adopt an already contract-shaped
    `conversation` as-is, map a recognized flat shape like a CSV row, or unmappable."""
    conversation = raw.get("conversation")
    if isinstance(conversation, list):
        return conversation

    keys = frozenset(raw.keys())
    columns = _FLAT_KEY_SETS.get(keys)
    if columns is None:
        return None
    prompt_key, response_key = columns
    prompt, response = raw.get(prompt_key), raw.get(response_key)
    if not prompt or not response:
        return None
    return _build_conversation(prompt, response, raw.get("system"))


def build_conversation(
    source_type: str, raw: dict[str, Any], mapping: IngestSourceMapping | None
) -> list[dict[str, Any]] | None:
    """Dispatch structure mapping by Bronze `source.type`. Unknown/future types (sql, pdf,
    api) are unmappable here -- the Cleaner only knows how to map what the Ingestor
    produces in MVP."""
    if source_type == "csv":
        return map_csv_row(raw, mapping)
    if source_type == "jsonl":
        return map_jsonl_raw(raw)
    return None


def _is_well_structured(turns: list[Any]) -> bool:
    """Shape rules of the Multimodal Contract (02-data-contracts.md §2), checked defensively
    since a JSONL `conversation` is untrusted, attacker- or typo-controlled input, not
    something the Cleaner already knows is well-formed. `turns` is always an actual list by
    the time this is called -- both `map_csv_row` and `map_jsonl_raw` only ever return a
    real list or None, and `clean_record` short-circuits on None before calling this -- but
    its *elements* are exactly what an untrusted "contract-shaped" `raw.conversation` says
    they are, so nothing below that assumes a particular shape."""
    if len(turns) < 2:
        return False

    roles = []
    for turn in turns:
        if not isinstance(turn, dict):
            return False
        role = turn.get("role")
        content = turn.get("content")
        if role not in _VALID_ROLES:
            return False
        if not isinstance(content, list) or len(content) < 1:
            return False
        for part in content:
            if not isinstance(part, dict) or set(part.keys()) != {"type", "value"}:
                return False
            if part["type"] not in _VALID_CONTENT_TYPES:
                return False
            if not isinstance(part["value"], str):
                return False
        roles.append(role)

    if roles.count("system") > 1:
        return False
    if "system" in roles and roles.index("system") != 0:
        return False
    if "user" not in roles or "assistant" not in roles:
        return False
    return roles[-1] == "assistant"


def _scrub_turns(turns: list[dict[str, Any]], pii_scrubbers: Iterable[str]) -> list[dict[str, Any]]:
    scrubbed = []
    for turn in turns:
        content = [
            {**part, "value": scrub(part["value"], pii_scrubbers)}
            if part.get("type") == "text"
            else part
            for part in turn["content"]
        ]
        scrubbed.append({**turn, "content": content})
    return scrubbed


def _turn_is_empty(turn: dict[str, Any]) -> bool:
    """True if the turn has any text part that's blank after scrub. `any`, not `all`: the
    Multimodal Contract requires *every* ContentPart's text be non-empty after trim
    (02-data-contracts.md §2), so a turn with one blank text part alongside a non-blank one
    is just as unwritable as a turn that's blank all over -- keeping it would produce Silver
    output that fails its own schema."""
    text_values = [p["value"] for p in turn["content"] if p.get("type") == "text"]
    return any(not value.strip() for value in text_values)


def _total_chars(turns: list[dict[str, Any]]) -> int:
    return sum(
        len(part["value"])
        for turn in turns
        for part in turn["content"]
        if part.get("type") == "text"
    )


def clean_record(
    source_type: str,
    raw: dict[str, Any],
    mapping: IngestSourceMapping | None,
    *,
    pii_scrubbers: Iterable[str],
    min_chars: int,
    max_chars: int,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Map, scrub, and filter one Bronze `raw` payload into Silver `conversation` turns
    (core logic 3-5). Returns `(turns, None)` on success or `(None, reason)` on drop.
    Does not dedup -- that needs cross-record state the caller owns (see `Deduplicator`)."""
    turns = build_conversation(source_type, raw, mapping)
    if turns is None:
        return None, "unmappable"

    if not _is_well_structured(turns):
        return None, "bad_structure"

    scrubbed = _scrub_turns(turns, pii_scrubbers)

    if any(_turn_is_empty(turn) for turn in scrubbed):
        return None, "empty_turn"

    total_chars = _total_chars(scrubbed)
    if total_chars < min_chars:
        return None, "too_short"
    if total_chars > max_chars:
        return None, "too_long"

    return scrubbed, None


class Deduplicator:
    """Tracks scrubbed-conversation hashes seen so far in one Cleaner run (core logic 6):
    exact dedup on the sha256 of the canonical-JSON conversation, first occurrence wins."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def is_duplicate(self, turns: list[dict[str, Any]]) -> bool:
        digest = canonical_hash(turns)
        if digest in self._seen:
            return True
        self._seen.add(digest)
        return False
