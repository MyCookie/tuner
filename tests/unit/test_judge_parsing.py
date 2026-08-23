"""Unit tests for tuner.judge.client/prompts (JDG suite, docs/08-test-specs/judge.md)."""

from __future__ import annotations

import json
import random
import time

import httpx
import pytest

from tuner.judge.client import (
    BASE_DELAY_SECONDS,
    MAX_DELAY_SECONDS,
    ParseError,
    backoff_delay,
    build_http_client,
    is_retryable,
    normalize_score,
    parse_reply,
    score_record,
)
from tuner.judge.prompts import RUBRIC_V1, render_rubric_prompt

_CONVERSATION = [
    {"role": "user", "content": [{"type": "text", "value": "hi"}]},
    {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
]


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


def _ok_response(score: int) -> _FakeResponse:
    content = json.dumps({"score": score, "reasoning": "fine"})
    return _FakeResponse(200, {"choices": [{"message": {"content": content}}]})


class _FakeClient:
    """A minimal stand-in for httpx.Client: score_record only ever calls `.post()`."""

    def __init__(self, results: list) -> None:
        self._results = iter(results)
        self.calls = 0

    def post(self, path: str, json: dict) -> _FakeResponse:
        self.calls += 1
        item = next(self._results)
        if isinstance(item, Exception):
            raise item
        return item


def test_clean_json_reply_extracted():
    """JDG-U-010: clean JSON reply -> (7, "reasoning") extracted."""
    score, reasoning = parse_reply('{"score": 7, "reasoning": "Solid, complete answer."}')
    assert (score, reasoning) == (7, "Solid, complete answer.")


@pytest.mark.parametrize(
    "reply, expected_score",
    [
        ('Sure, here is my evaluation: {"score": 9, "reasoning": "Excellent."}', 9),
        ('```json\n{"score": 6, "reasoning": "Adequate."}\n```', 6),
        ('Let me think...\n```json\n{"score": 8, "reasoning": "Good."}\n```\nDone.', 8),
        # Two JSON objects in one reply: the FIRST one wins, not the last.
        ('{"score": 3, "reasoning": "first"} then: {"score": 9, "reasoning": "second"}', 3),
        # A brace inside the reasoning string itself must not truncate extraction --
        # a naive non-greedy `\{.*?\}` regex stops at *a* `}`, not necessarily the
        # object's own closing one (PR #8 review round 1 finding 4).
        ('{"score": 7, "reasoning": "the answer used {curly braces} correctly"}', 7),
        # A genuinely nested object: the inner `}` must not end extraction early.
        ('{"score": 7, "reasoning": "ok", "meta": {"nested": true}}', 7),
        # An escaped quote inside the string must not be mistaken for the string's end
        # (which would then miscount a later structural brace as inside/outside a string).
        (r'{"score": 7, "reasoning": "she said \"hi\" back"}', 7),
        # Brace-shaped prose BEFORE the real JSON object: "{a, b}" balances into a
        # complete-but-non-JSON group. A single-shot scan would stop there and never
        # reach the real object (PR #8 review round 2 finding 1).
        ('Consider the set {a, b}. Verdict: {"score": 7, "reasoning": "ok"}', 7),
        ('**{score}**: {"score": 7, "reasoning": "ok"}', 7),
        # A stray, unescaped quote in surrounding prose (not JSON) throws off in-string
        # tracking for a candidate starting before it -- the scanner must still recover
        # by trying the next `{` with fresh state, not give up on the whole reply.
        ('He said "use { for objects" then {"score": 7, "reasoning": "ok"}', 7),
        # An unclosed brace group, followed by a real, complete one.
        ('{"score": broken, then: {"score": 7, "reasoning": "ok"}', 7),
    ],
    ids=[
        "leading-prose",
        "json-fence",
        "leading-prose-and-fence",
        "two-objects-first-wins",
        "brace-inside-reasoning-string",
        "nested-object",
        "escaped-quote-inside-string",
        "brace-shaped-prose-before-real-object",
        "brace-shaped-markdown-before-real-object",
        "stray-quote-in-prose-before-real-object",
        "unclosed-object-before-real-one",
    ],
)
def test_json_embedded_in_prose_extracted(reply, expected_score):
    """JDG-U-011: JSON embedded in prose (```json fences, leading text, a second JSON
    object later in the reply, a brace inside the reasoning string itself) -- the first
    complete JSON object is extracted, exactly."""
    score, _ = parse_reply(reply)
    assert score == expected_score


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all, just prose.",
        "{}",
        '{"reasoning": "no score field"}',
        "{not valid json}",
        '{"score": 7, "reasoning": "unterminated',
    ],
    ids=[
        "no-json",
        "empty-object",
        "json-without-score",
        "brace-shaped-but-invalid",
        "unclosed-brace",
    ],
)
def test_garbage_or_missing_score_raises_parse_error(reply):
    """JDG-U-012: garbage / no JSON / JSON without score -> ParseError (counts as a
    retryable attempt, per score_record's handling)."""
    with pytest.raises(ParseError):
        parse_reply(reply)


@pytest.mark.parametrize(
    "score",
    [0, 11, 7.5, "7"],
    ids=["zero", "eleven", "float", "string"],
)
def test_out_of_range_or_wrong_type_score_rejected(score):
    """JDG-U-013: score 0, 11, 7.5, "7" all rejected -- int 1-10 only, no coercion
    (strict types, not "close enough")."""
    with pytest.raises(ParseError):
        parse_reply(f'{{"score": {score!r}, "reasoning": "x"}}'.replace("'", '"'))


def test_invalid_first_score_object_is_fatal_not_skipped():
    """JDG-U-023: once a candidate parses as JSON and carries a `score` key, it IS "the
    first JSON object" -- an invalid score there is a parse failure, not a reason to
    keep scanning for a second, better-looking object (PR #8 review round 3 finding 1:
    an earlier version returned the *second* object's valid score instead of raising)."""
    reply = '{"score": 11, "reasoning": "a"} and {"score": 5, "reasoning": "b"}'
    with pytest.raises(ParseError):
        parse_reply(reply)


def test_score_nested_inside_malformed_object_not_rescued():
    """JDG-U-024: a `{"score": N}`-shaped fragment sitting *inside* a span that itself
    fails to parse as JSON (here, the outer object's own reasoning string breaks its
    quoting) must not be rescued and treated as the real answer (PR #8 review round 3
    finding 2: an earlier version returned the nested fragment's score, 10, instead of
    treating the whole malformed reply as a parse failure)."""
    reply = '{"score": 3, "reasoning": "example {"score": 10}"}'
    with pytest.raises(ParseError):
        parse_reply(reply)


def test_pathological_unmatched_braces_bounded_scan_time():
    """JDG-U-025: a reply consisting of thousands of unmatched `{` characters (no valid
    JSON anywhere) is rejected quickly, not scanned at the O(n^2) worst case over the
    full length -- PR #8 review round 3 finding 3 measured ~6.6s for 20,000 unclosed
    braces; the scan is length-capped so a pathological reply can't stall a worker
    thread that long."""
    reply = "{" * 20_000
    start = time.monotonic()
    with pytest.raises(ParseError):
        parse_reply(reply)
    assert time.monotonic() - start < 1.0


def test_normalization_exact():
    """JDG-U-014: score 7 -> evaluation.score == 0.7 exactly."""
    assert normalize_score(7) == 0.7


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (429, True),
        (500, True),
        (502, True),
        (503, True),
        # 504/599 are not in any small fixed set of "known" 5xx codes -- PR #8 review
        # round 1 found an earlier version narrowed to {429, 500, 502, 503} exactly,
        # silently treating every other 5xx as fatal. "429/5xx" means *every* 5xx.
        (504, True),
        (599, True),
        ("timeout", True),
        (400, False),
        (401, False),
        (404, False),
    ],
)
def test_retryability_classifier(outcome, expected):
    """JDG-U-015: 429/any 5xx/timeout retryable; 400/401/404 fatal (core logic 3)."""
    assert is_retryable(outcome) is expected


def test_backoff_sequence_base_2s_exponential_capped_60s():
    """JDG-U-016: backoff sequence for 3 retries (seeded jitter) -- base 2s exponential,
    capped 60s. No real sleeping: the delays are computed and asserted directly."""
    delays_a = [backoff_delay(attempt, random.Random(1234)) for attempt in (1, 2, 3)]

    for attempt, delay in zip((1, 2, 3), delays_a, strict=True):
        assert 0 <= delay <= BASE_DELAY_SECONDS * (2 ** (attempt - 1))

    # Two independent RNGs seeded identically and called through the same sequence of
    # attempts reproduce the same delays -- this is what "seeded" determinism means here,
    # not that a single call is repeatable in isolation.
    rng_a, rng_b = random.Random(1234), random.Random(1234)
    sequence_a = [backoff_delay(attempt, rng_a) for attempt in (1, 2, 3)]
    sequence_b = [backoff_delay(attempt, rng_b) for attempt in (1, 2, 3)]
    assert sequence_a == sequence_b

    # Cap: an attempt far enough out that 2 * 2**(attempt-1) would exceed 60s is still <= 60.
    assert backoff_delay(10, random.Random(1234)) <= MAX_DELAY_SECONDS


def test_rubric_prompt_rendering_three_turn_conversation():
    """JDG-U-017: rubric prompt rendering for a 3-turn conversation contains every turn's
    text, the JSON-only output instruction, and RUBRIC_V1 is a module constant."""
    conversation = [
        {"role": "system", "content": [{"type": "text", "value": "You are helpful."}]},
        {"role": "user", "content": [{"type": "text", "value": "How do I reset my password?"}]},
        {"role": "assistant", "content": [{"type": "text", "value": "Go to Settings."}]},
    ]

    prompt = render_rubric_prompt(conversation)

    assert "You are helpful." in prompt
    assert "How do I reset my password?" in prompt
    assert "Go to Settings." in prompt
    assert '{"score"' in prompt and '"reasoning"' in prompt
    assert isinstance(RUBRIC_V1, str) and RUBRIC_V1


def test_build_http_client_configures_base_url_and_auth_header():
    """JDG-U-018: build_http_client wires the base URL and bearer-token header a real
    endpoint needs; a falsy api_key sends no Authorization header at all."""
    with build_http_client("http://example.test", "secret-key") as client:
        assert str(client.base_url) == "http://example.test"
        assert client.headers["authorization"] == "Bearer secret-key"

    with build_http_client("http://example.test", "") as client:
        assert "authorization" not in client.headers


def test_score_record_transport_error_is_retried():
    """JDG-U-019: a transport-level failure (no HTTP response at all) is retried like a
    timeout, not treated as fatal -- the second attempt succeeds."""
    client = _FakeClient([httpx.ConnectError("boom"), _ok_response(8)])

    result = score_record(client, "m", _CONVERSATION, max_retries=1, sleep=lambda s: None)

    assert result == (8, "fine")
    assert client.calls == 2


def test_score_record_fatal_status_returns_none_without_further_retries():
    """JDG-U-020: a fatal (non-retryable) status stops the attempt loop immediately --
    it doesn't spend the rest of max_retries retrying something that will never succeed."""
    client = _FakeClient([_FakeResponse(404)])

    result = score_record(client, "m", _CONVERSATION, max_retries=3, sleep=lambda s: None)

    assert result is None
    assert client.calls == 1


def test_score_record_malformed_reply_body_is_retried():
    """JDG-U-021: a 200 response whose body doesn't parse to a valid score (missing
    'choices', or parse_reply itself raising ParseError) is retried, not fatal."""
    client = _FakeClient([_FakeResponse(200, {"unexpected": "shape"}), _ok_response(6)])

    result = score_record(client, "m", _CONVERSATION, max_retries=1, sleep=lambda s: None)

    assert result == (6, "fine")
    assert client.calls == 2


def test_score_record_exhausts_retries_returns_none():
    """JDG-U-022: every attempt failing with a retryable status exhausts max_retries and
    reports judge_error (None) rather than raising."""
    client = _FakeClient([_FakeResponse(500), _FakeResponse(500), _FakeResponse(500)])

    result = score_record(client, "m", _CONVERSATION, max_retries=2, sleep=lambda s: None)

    assert result is None
    assert client.calls == 3
