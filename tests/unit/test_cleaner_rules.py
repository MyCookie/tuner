"""Unit tests for tuner.cleaner.rules/patterns (CLN suite, docs/08-test-specs/cleaner.md)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from hypothesis import given
from hypothesis import strategies as st

from tuner.cleaner.cli import clean_command
from tuner.cleaner.patterns import EMAIL_RE, PHONE_RE
from tuner.cleaner.rules import Deduplicator, clean_record, scrub
from tuner.core.config import IngestSourceMapping

_MAPPING = IngestSourceMapping(prompt_column="q", response_column="a", system_column=None)


def _clean(
    raw, source_type="csv", mapping=_MAPPING, pii_scrubbers=(), min_chars=0, max_chars=32000
):
    return clean_record(
        source_type,
        raw,
        mapping,
        pii_scrubbers=pii_scrubbers,
        min_chars=min_chars,
        max_chars=max_chars,
    )


# --- Scrubbers (unit, table-driven) ------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "alice@example.com",
        "jane.doe+billing@example.com",
        "am-team@corp.example.com",
        "SECURITY@EXAMPLE.COM",
    ],
    ids=["plain", "plus-tag", "subdomain", "uppercase"],
)
def test_email_scrubbed(email):
    """CLN-U-001: email scrubber replaces plain/plus-tag/subdomain/uppercase addresses with
    [EMAIL]; surrounding text intact."""
    assert scrub(f"Contact us at {email} for help.") == "Contact us at [EMAIL] for help."


@pytest.mark.parametrize(
    "text",
    ["reach me at user@localhost please", "follow @handle for updates"],
    ids=["localhost-no-tld", "twitter-handle"],
)
def test_email_negatives_unchanged(text):
    """CLN-U-002: user@localhost (no TLD) and @handle (twitter-style) are not emails --
    pinned as unchanged, a chosen behavior, not an accident."""
    assert scrub(text) == text


@pytest.mark.parametrize(
    "phone",
    ["+1 (555) 123-4567", "(555) 123-4567", "555-123-4567", "+44 20 7946 0958"],
    ids=["us-parens-intl", "us-parens-no-country-code", "us-hyphen", "uk-intl"],
)
def test_phone_scrubbed(phone):
    """CLN-U-003: phone scrubber replaces each format with [PHONE], including a
    parenthesized area code with no country code (PR #7 review round 1 finding 4)."""
    assert scrub(f"Call {phone} anytime.") == "Call [PHONE] anytime."


@pytest.mark.parametrize(
    "text",
    ["running v2.10.3 now", "shipped on 2026-07-20", "zip code 90210", "listening on port 8080"],
    ids=["version-string", "iso-date", "zip-code", "port-number"],
)
def test_phone_negatives_unchanged(text):
    """CLN-U-004: version strings, ISO dates, 5-digit zips, port numbers are not phone
    numbers -- pinned as unchanged."""
    assert scrub(text) == text


def test_unicode_nfc_normalization():
    """CLN-U-005: decomposed and composed accented characters normalize identically --
    hash-equal for dedup, since dedup hashes the scrubbed text."""
    composed = "café"  # é = U+00E9
    decomposed = "café"  # e + combining acute accent (U+0301)
    assert composed != decomposed  # sanity: genuinely different code points to start
    assert scrub(composed) == scrub(decomposed) == "café"


def test_control_chars_stripped_newlines_tabs_preserved():
    """CLN-U-006: control chars stripped; \\n/\\t preserved; >=3 consecutive blank lines
    collapse to 2; leading/trailing whitespace trimmed."""
    text = "\x00\x01Hello\tworld\x07\n\n\n\nBye  "
    assert scrub(text) == "Hello\tworld\n\n\nBye"


def test_filter_too_short():
    """CLN-U-007: a 19-char conversation drops too_short at min_chars=20."""
    turns, reason = _clean({"q": "Hi", "a": "a" * 17}, min_chars=20, max_chars=32000)
    assert (turns, reason) == (None, "too_short")


def test_filter_too_long():
    """CLN-U-007: a conversation over max_chars drops too_long."""
    turns, reason = _clean({"q": "Hi", "a": "a" * 100}, min_chars=1, max_chars=50)
    assert (turns, reason) == (None, "too_long")


def test_filter_empty_turn():
    """CLN-U-007: a turn that's whitespace-only -- present at mapping time, but empty after
    scrub -- drops empty_turn (distinct from unmappable, which is missing/empty at the
    source before mapping even runs)."""
    raw = {"q": "   ", "a": "A real response here that is long enough."}
    turns, reason = _clean(raw, min_chars=1, max_chars=32000)
    assert (turns, reason) == (None, "empty_turn")


def test_filter_empty_turn_one_blank_part_among_non_blank_ones():
    """CLN-U-007 (PR #7 review round 1 finding 1): a turn with one blank text part
    alongside a non-blank one also drops empty_turn -- every ContentPart's text must be
    non-empty after trim (02-data-contracts.md §2), so "some parts blank" is just as
    unwritable as "all parts blank", not something that silently keeps only the good part."""
    conversation = [
        {
            "role": "user",
            "content": [
                {"type": "text", "value": "A perfectly good question."},
                {"type": "text", "value": "   "},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "value": "A perfectly good answer."}]},
    ]
    turns, reason = _clean(
        {"conversation": conversation}, source_type="jsonl", mapping=None, min_chars=1
    )
    assert (turns, reason) == (None, "empty_turn")


@pytest.mark.parametrize(
    "conversation",
    [
        [
            {"role": "user", "content": [{"type": "text", "value": "hi"}]},
            {"role": "user", "content": [{"type": "text", "value": "again"}]},
        ],
        [
            {"role": "user", "content": [{"type": "text", "value": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
            {"role": "user", "content": [{"type": "text", "value": "bye"}]},
        ],
        [
            {"role": "system", "content": [{"type": "text", "value": "sys1"}]},
            {"role": "system", "content": [{"type": "text", "value": "sys2"}]},
            {"role": "user", "content": [{"type": "text", "value": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        [
            {"role": "user", "content": [{"type": "text", "value": "hi"}]},
            {"role": "system", "content": [{"type": "text", "value": "not first"}]},
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        [{"role": "user", "content": [{"type": "text", "value": "only one turn"}]}],
        [
            "not a dict",
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        [
            {"role": "user", "content": "not array-wrapped"},
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        [
            {"role": "user", "content": ["not a dict"]},
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        [
            {"role": "user", "content": [{"type": "text"}]},  # missing "value"
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        [
            {"role": "narrator", "content": [{"type": "text", "value": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        [
            {"role": "user", "content": [{"type": "video", "value": "hi"}]},  # invalid type
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        [
            {"role": "user", "content": [{"type": "text", "value": 42}]},  # value not a str
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        [
            # extra key beyond type/value -- ContentPart is `extra="forbid"`
            {"role": "user", "content": [{"type": "text", "value": "hi", "lang": "en"}]},
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
    ],
    ids=[
        "no-assistant-turn",
        "last-turn-user",
        "double-system",
        "system-not-first",
        "single-turn",
        "turn-not-a-dict",
        "content-not-array-wrapped",
        "content-part-not-a-dict",
        "content-part-missing-value",
        "invalid-role",
        "content-part-invalid-type",
        "content-part-value-not-a-string",
        "content-part-extra-key",
    ],
)
def test_bad_structure_dropped_not_crashed(conversation):
    """CLN-U-008: no assistant turn, last turn user, double system, and (extending the
    table) every other way a contract-shaped `raw.conversation` can be malformed while
    still being a JSON value -- all drop bad_structure. The Cleaner drops, not crashes,
    since Bronze `raw` is untrusted."""
    turns, reason = _clean(
        {"conversation": conversation}, source_type="jsonl", mapping=None, min_chars=0
    )
    assert (turns, reason) == (None, "bad_structure")


def _turns(response_suffix: str = "") -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "value": "How do I reset my password?"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "value": f"Go to Settings{response_suffix}."}],
        },
    ]


def test_dedup_identical_dropped_near_identical_kept():
    """CLN-U-009: identical scrubbed conversations (as would come from different Bronze
    ids) -- first kept, second dropped duplicate; near-identical (one-fragment diff)
    conversations both kept."""
    dedup = Deduplicator()
    assert dedup.is_duplicate(_turns()) is False
    assert dedup.is_duplicate(_turns()) is True
    assert dedup.is_duplicate(_turns(" now")) is False


def test_dedup_runs_on_scrubbed_text():
    """CLN-U-010: two records identical only after PII scrub -- the second is dropped,
    proving dedup runs on scrubbed text, not raw text."""
    raw_a = {"q": "Contact me", "a": "Email alice@example.com for help with your account."}
    raw_b = {"q": "Contact me", "a": "Email bob@example.com for help with your account."}
    dedup = Deduplicator()

    turns_a, reason_a = _clean(raw_a, pii_scrubbers=("email",), min_chars=1)
    turns_b, reason_b = _clean(raw_b, pii_scrubbers=("email",), min_chars=1)

    assert reason_a is None
    assert reason_b is None
    assert dedup.is_duplicate(turns_a) is False
    assert dedup.is_duplicate(turns_b) is True


# --- Structure mapping (unit) ------------------------------------------------------------


@pytest.mark.parametrize(
    "system_column, raw, expected_turns",
    [
        (
            "sys",
            {
                "q": "How do I reset my password?",
                "a": "Go to Settings.",
                "sys": "You are helpful.",
            },
            3,
        ),
        (None, {"q": "How do I reset my password?", "a": "Go to Settings."}, 2),
    ],
    ids=["system-column-present", "system-column-null"],
)
def test_csv_mapping_system_column_present_or_null(system_column, raw, expected_turns):
    """CLN-U-020: CSV mapping with system_column present/null -> 3-turn/2-turn conversation;
    content values array-wrapped."""
    mapping = IngestSourceMapping(
        prompt_column="q", response_column="a", system_column=system_column
    )
    turns, reason = _clean(raw, mapping=mapping)
    assert reason is None
    assert len(turns) == expected_turns
    for turn in turns:
        assert isinstance(turn["content"], list)
        assert turn["content"][0]["type"] == "text"


@pytest.mark.parametrize(
    "raw, mapping",
    [
        ({"q": "hi", "a": ""}, _MAPPING),
        ({"q": "hi", "a": "fine"}, None),
    ],
    ids=["empty-response-column", "no-mapping-at-all"],
)
def test_csv_unmappable(raw, mapping):
    """CLN-U-021: a CSV row with an empty response_column, or a CSV source with no mapping
    at all, both drop unmappable."""
    assert _clean(raw, mapping=mapping) == (None, "unmappable")


def test_jsonl_contract_shaped_adopted_as_is():
    """CLN-U-022: JSONL raw already contract-shaped is adopted as-is (then scrubbed),
    including a non-text (image) content part -- adopted as-is per the Multimodal
    Contract's forward-compat fields, and not judged "empty" the way a blank text turn
    would be, since there's no text to be empty."""
    conversation = [
        {"role": "user", "content": [{"type": "image", "value": "s3://tuner-assets/x.jpg"}]},
        {"role": "assistant", "content": [{"type": "text", "value": "  hi  "}]},
    ]
    turns, reason = _clean({"conversation": conversation}, source_type="jsonl", mapping=None)
    assert reason is None
    assert turns[0]["content"][0] == {"type": "image", "value": "s3://tuner-assets/x.jpg"}
    assert turns[1]["content"][0]["value"] == "hi"  # scrubbed (trimmed)


@pytest.mark.parametrize(
    "raw",
    [
        {"prompt": "hi", "response": "hello"},
        {"question": "hi", "answer": "hello"},
        {"prompt": "hi", "response": "hello", "system": "be nice"},
        {"question": "hi", "answer": "hello", "system": "be nice"},
    ],
    ids=["prompt-response", "question-answer", "prompt-response-system", "question-answer-system"],
)
def test_jsonl_flat_shapes_mapped_like_csv(raw):
    """CLN-U-023: JSONL flat shapes prompt/response and question/answer (+ optional system)
    are mapped like a CSV row."""
    turns, reason = _clean(raw, source_type="jsonl", mapping=None)
    assert reason is None
    assert turns[-2]["role"] == "user"
    assert turns[-1]["role"] == "assistant"
    if "system" in raw:
        assert turns[0]["role"] == "system"
        assert len(turns) == 3
    else:
        assert len(turns) == 2


@pytest.mark.parametrize(
    "source_type, raw",
    [
        ("jsonl", {"note": "internal memo"}),
        ("jsonl", {"prompt": "hi", "response": ""}),
        ("sql", {"question": "hi", "answer": "fine"}),
    ],
    ids=["arbitrary-object", "flat-shape-empty-value", "unknown-source-type"],
)
def test_jsonl_and_dispatch_unmappable(source_type, raw):
    """CLN-U-024: a JSONL arbitrary object (no recognizable shape), a recognized flat shape
    with an empty value, and a Bronze `source.type` the Cleaner doesn't know how to map
    (sql/pdf/api, reserved for later phases) all drop unmappable."""
    assert _clean(raw, source_type=source_type, mapping=None) == (None, "unmappable")


@given(st.text(max_size=200))
def test_scrub_property_idempotent_no_pii_bounded_length(text):
    """CLN-U-030: property -- scrub is idempotent, output never matches the PII patterns,
    and output length is bounded relative to the input.

    The length bound is deliberately loose (8x + 8, not "input + a few chars per match"):
    counting matches on the *raw* input and comparing against the scrubbed output isn't
    sound, because control-char stripping runs before PII scrubbing and could in principle
    join two non-matching fragments into a new match that only exists post-strip. The
    bound here only needs to catch a scrub() that blows up catastrophically (e.g.
    quadratic growth, a runaway loop) -- "some slack for placeholder growth" is not a
    precise contract, per docs/08-test-specs/cleaner.md.
    """
    once = scrub(text)
    twice = scrub(once)

    assert once == twice
    assert EMAIL_RE.search(once) is None
    assert PHONE_RE.search(once) is None
    assert len(once) <= 8 * len(text) + 8


def test_clean_command_missing_run_id_is_a_usage_error():
    """PR #7 review round 1 finding 5: `clean_command` is exercised at least once via
    CliRunner, matching the exit-code-assertion convention (docs/08-test-specs/README.md) --
    every other case in this file calls `clean_record`/`scrub` directly, which never
    touches the click wiring (the `--run-id`/`--config` options, `sys.exit`) at all."""
    result = CliRunner().invoke(clean_command, [])
    assert result.exit_code != 0
    assert "run-id" in result.output.lower()
