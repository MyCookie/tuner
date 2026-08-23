"""Judge HTTP client: reply parsing, retry/backoff policy, and the scoring call itself
(docs/03-components/judge.md scoring protocol + core logic 3).
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal

import httpx

from tuner.judge.prompts import render_rubric_prompt

BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 60.0

# 429 and any 5xx are transient-endpoint signals worth retrying; other 4xx are the
# endpoint telling us the request itself is wrong, so retrying changes nothing (judge.md
# core logic 3, literally "429/5xx"). A timeout or any other transport-level failure (no
# status code at all) is grouped with "timeout" -- there's no response to classify, so
# it's retried the same way. PR #8 review round 1 found an earlier version of this
# narrowed to a fixed set of three 5xx codes {500, 502, 503}, silently treating every
# other 5xx (504, 520, ...) as fatal.
_RETRYABLE_4XX = frozenset({429})

# A budget on total *work* (characters actually visited by the inner scan, summed
# across every start position tried), not on input length: PR #8 review round 3 capped
# `text` itself at a fixed length to bound the O(n^2) worst case of
# _iter_balanced_brace_spans trying every start position (20,000 unclosed `{` took
# ~6.6s per attempt) -- but that also truncates the *legitimate* case of one long, valid
# JSON object (a verbose `reasoning` field, or a reasoning model's chain-of-thought
# preceding its answer), rejecting a well-formed reply purely for length (round 4
# finding 1). A single long-but-valid object only ever gets scanned once, so it costs
# O(its own length) regardless of this budget; only a text with many *failing* start
# attempts (each rescanning toward the end without closing) burns through it quickly.
_MAX_SCAN_STEPS = 200_000


class ParseError(Exception):
    """The judge's reply had no valid `{"score": <int 1-10>, "reasoning": ...}` JSON object."""


def _iter_balanced_brace_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield the `(start, end)` index (end inclusive) of every balanced `{...}` span in
    `text`, in the order their opening brace appears, trying every possible start
    position -- not just the first `{`.

    Not a regex (`\\{.*?\\}` can't do this correctly): a non-greedy brace regex stops at
    the *first* closing brace, which can be inside the JSON string content itself --
    `{"score": 7, "reasoning": "use {braces} here"}` would extract just
    `{"score": 7, "reasoning": "use {braces}` and fail to parse. This tracks
    string-literal state (respecting `\\"` escapes) so a brace inside a quoted string
    never affects nesting depth (PR #8 review round 1 finding 4).

    Trying every start position, not just the first `{` in the text, is what round 2
    finding 1 added: brace-shaped prose *before* the real JSON object -- e.g.
    "Consider the set {a, b}. Verdict: {\"score\": 7, ...}" -- balances into a complete
    but non-JSON group (`{a, b}`) that a single-shot scan would return and stop at,
    never reaching the real object that follows.

    Bounded by `_MAX_SCAN_STEPS` total characters visited across every start position
    tried (round 4 finding 1) -- not by `len(text)`, so one long, genuinely valid object
    is scanned in full (it costs one pass over its own length) while a text full of
    failing start attempts (each re-scanning toward the end without closing) exhausts
    the budget and gives up quickly instead of retrying from every remaining `{`.
    """
    search_from = 0
    steps_used = 0
    while True:
        start = text.find("{", search_from)
        if start == -1:
            return

        depth = 0
        in_string = False
        escaped = False
        end = None
        for i in range(start, len(text)):
            steps_used += 1
            if steps_used > _MAX_SCAN_STEPS:
                return
            char = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        # Always advance and keep trying, even when this start never closes: stray
        # quote characters in surrounding prose (not JSON) can throw off `in_string`
        # tracking enough that depth never returns to 0 from *this* start, while a
        # later `{` -- scanning with fresh, uncorrupted string-tracking state -- finds
        # a perfectly good close. Giving up here would treat a confused first attempt
        # as proof no valid object exists anywhere in the text.
        search_from = start + 1
        if end is not None:
            yield start, end


def parse_reply(text: str) -> tuple[int, str]:
    """Extract the first JSON object in `text` -- the first balanced `{...}` span that
    both parses as JSON and carries a `score` key -- and validate it (strict int in
    [1, 10], no float/str coercion — JDG-U-013; `reasoning` must be a string if present
    — JDG-U-029), returning `(score, reasoning)`.

    Once a span qualifies as "the first JSON object" (valid JSON, has a `score` key),
    an invalid score there is a parse failure, full stop -- it does not fall through to
    a later brace group. `docs/03-components/judge.md`'s parsing rule is "extract the
    first JSON object; validate score"; scanning past a genuine-but-invalid object to
    find a second, better-looking one would silently launder a bad reply into a valid
    score (PR #8 review round 3 finding 1). Spans that fail `json.loads` outright are
    true false starts (not-JSON prose, e.g. round 2's brace-shaped text) and are simply
    skipped -- but any later span *nested inside* one of those failed spans is skipped
    too, without even attempting it: a `{"score": N}`-shaped fragment sitting inside a
    reply's own broken quoting must not be rescued and treated as the real answer
    (PR #8 review round 3 finding 2).

    Raises `ParseError` if no candidate in the reply qualifies (JDG-U-010..013); a parse
    failure counts as a retryable attempt, not a fatal one (docs/03-components/judge.md
    scoring protocol). The error message reports only the reply's length, not its full
    text -- an arbitrarily long judge reply has no place being echoed whole into a log
    line (PR #8 review round 4 finding 3)."""
    failed_spans: list[tuple[int, int]] = []
    for start, end in _iter_balanced_brace_spans(text):
        if any(fs <= start and end <= fe for fs, fe in failed_spans):
            continue

        candidate = text[start : end + 1]
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            failed_spans.append((start, end))
            continue
        if not isinstance(obj, dict) or "score" not in obj:
            continue

        score = obj["score"]
        # `type(...) is int`, not `isinstance`: bool is an int subclass in Python, and a
        # float/str score (even "7" or 7.0) must be rejected, not coerced (JDG-U-013).
        # The message reports only the value's type, not the value itself -- an invalid
        # score could in principle be an arbitrarily long string, and round 4 finding 3
        # already established that unbounded judge output has no place in a log line.
        if type(score) is not int or not (1 <= score <= 10):
            raise ParseError(
                f"first score object had an invalid score of type {type(score).__name__}"
            )

        # `reasoning` gets the same strictness as `score`, not a free pass:
        # `evaluation.reasoning` is a required `str` field (docs/02-data-contracts.md §2,
        # tuner.core.schemas.Evaluation) and this is the one place an untrusted external
        # LLM value lands directly in a contract-typed field. A non-string reasoning
        # (missing entirely defaults to "", which is fine) would otherwise reach Gold
        # unvalidated and only fail downstream, after judge() already exited 0
        # (PR #8 review round 5 finding 1).
        reasoning = obj.get("reasoning", "")
        if not isinstance(reasoning, str):
            raise ParseError(
                f"first score object had a non-string reasoning of type {type(reasoning).__name__}"
            )

        return score, reasoning

    raise ParseError(f"no valid score JSON object found in reply of length {len(text)}")


def normalize_score(score: int) -> float:
    """Raw 1-10 integer score -> normalized [0.0, 1.0] (JDG-U-014)."""
    return score / 10.0


def is_retryable(outcome: int | Literal["timeout"]) -> bool:
    """JDG-U-015: 429, any 5xx, and "timeout" are retryable; every other status
    (400/401/404, ...) is fatal (docs/03-components/judge.md core logic 3)."""
    if outcome == "timeout":
        return True
    return outcome in _RETRYABLE_4XX or 500 <= outcome < 600


def backoff_delay(attempt: int, rng: random.Random) -> float:
    """Exponential backoff with full jitter: base 2s, doubling per attempt, capped at
    60s (docs/03-components/judge.md core logic 3). `attempt` is 1 for the first retry,
    2 for the second, and so on. Deterministic given a seeded `rng` (JDG-U-016)."""
    cap = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    return rng.uniform(0, cap)


def build_http_client(base_url: str, api_key: str) -> httpx.Client:
    """The real (or ASGI-in-process, for tests) client `score_record` posts through."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.Client(base_url=base_url, headers=headers, timeout=httpx.Timeout(30.0))


def score_record(
    http_client: httpx.Client,
    model: str,
    conversation: list[dict[str, Any]],
    max_retries: int,
    rng: random.Random | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, str] | None:
    """Score one record, retrying on retryable failures up to `max_retries` extra
    attempts (docs/03-components/judge.md core logic 3). Returns `(score, reasoning)`
    on success, or `None` if every attempt failed or a fatal error was returned
    (the caller records this as a `judge_error` drop)."""
    rng = rng or random.Random()
    prompt = render_rubric_prompt(conversation)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    for attempt in range(max_retries + 1):
        if attempt > 0:
            sleep(backoff_delay(attempt, rng))

        try:
            response = http_client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError:
            continue  # transport-level failure -- always retryable (is_retryable("timeout"))

        if response.status_code != 200:
            if is_retryable(response.status_code):
                continue
            return None  # fatal status -- no point retrying further

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            continue  # malformed reply shape -- retryable (JDG-U-012)

        # A non-string content -- `null` (the standard shape for a refusal or a
        # tool-call-only message) or a content-parts list -- isn't something
        # parse_reply can scan; passing it through raised an uncaught AttributeError
        # (`.find` on a non-str) that escaped score_record entirely and aborted the
        # whole run with exit 1 instead of being retried like any other malformed reply
        # (PR #8 review round 6 finding 1).
        if not isinstance(content, str):
            continue  # retryable, same as any other malformed reply (JDG-U-030)

        try:
            return parse_reply(content)
        except ParseError:
            continue  # malformed reply -- retryable (JDG-U-012)

    return None
