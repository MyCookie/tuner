"""Judge HTTP client: reply parsing, retry/backoff policy, and the scoring call itself
(docs/03-components/judge.md scoring protocol + core logic 3).
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
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


class ParseError(Exception):
    """The judge's reply had no valid `{"score": <int 1-10>, "reasoning": ...}` JSON object."""


def _find_first_json_object(text: str) -> str | None:
    """The substring of the first balanced `{...}` in `text`, or None if there isn't one.

    Not a regex (`\\{.*?\\}` can't do this correctly): a non-greedy brace regex stops at
    the *first* closing brace, which can be inside the JSON string content itself --
    `{"score": 7, "reasoning": "use {braces} here"}` would extract just
    `{"score": 7, "reasoning": "use {braces}` and fail to parse. This scans character by
    character, tracking string-literal state (respecting `\\"` escapes) so a brace inside
    a quoted string never affects nesting depth (PR #8 review round 1 finding 4)."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
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
                return text[start : i + 1]
    return None


def parse_reply(text: str) -> tuple[int, str]:
    """Extract the first JSON object in `text`, validate `score` is a strict int in
    [1, 10] (no float/str coercion — JDG-U-013), and return `(score, reasoning)`.
    Raises `ParseError` otherwise (JDG-U-010..013); a parse failure counts as a
    retryable attempt, not a fatal one (docs/03-components/judge.md scoring protocol)."""
    candidate = _find_first_json_object(text)
    if candidate is None:
        raise ParseError(f"no JSON object found in reply: {text!r}")
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ParseError(f"reply is not valid JSON: {text!r}") from exc
    if not isinstance(obj, dict) or "score" not in obj:
        raise ParseError(f"reply JSON has no 'score' field: {text!r}")

    score = obj["score"]
    # `type(...) is int`, not `isinstance`: bool is an int subclass in Python, and a
    # float/str score (even "7" or 7.0) must be rejected, not coerced (JDG-U-013).
    if type(score) is not int or not (1 <= score <= 10):
        raise ParseError(f"score must be an integer in [1, 10], got {score!r}")

    reasoning = obj.get("reasoning", "")
    return score, reasoning


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
            return parse_reply(content)
        except (ParseError, KeyError, IndexError, TypeError, ValueError):
            continue  # malformed reply -- retryable (JDG-U-012)

    return None
