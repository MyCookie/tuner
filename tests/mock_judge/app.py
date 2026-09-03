"""Mock judge (docs/spec/06-testing.md §4) — a deterministic OpenAI-compatible
`/v1/chat/completions` test double for the Judge stage (docs/spec/03-components/judge.md).

Behavior is selected by a marker embedded anywhere in the request's message
content, so fixture/seed records carry the marker and outcomes are
deterministic without a real LLM endpoint:

    [[score=N]]        -> 200; message content is `{"score": N, "reasoning": ...}`
    [[garbage]]        -> 200; message content is non-JSON prose
    [[fail=CODE]]      -> HTTP CODE (e.g. 429, 500), every call for this content
    [[fail_once=CODE]] -> HTTP CODE on the first call for this content, then scores
                          normally on every later call (proves a retry path recovers)
    (no marker)        -> 200; deterministic default score (DEFAULT_SCORE)

`[[fail=...]]`/`[[fail_once=...]]` are checked first so a record can't accidentally
suppress a failure by also matching another marker. Runs in-process via the ASGI test
client for unit (JDG-U) and integration (JDG-I) cases, and as a compose sidecar for the
T14 E2E steel thread.

State (JDG-I-022/024/025, added T08 — see the T05 footnote deferring these in
docs/spec/06-testing.md §4): `app.state.call_counts` counts calls per distinct request text,
`app.state.fail_once_consumed` tracks which `[[fail_once=...]]` content has already used
its one failure, and `app.state.peak_in_flight` records the highest number of
concurrently in-flight requests seen. `reset_state()` clears all of it between tests —
call it in a fixture/teardown, since the app is a module-level singleton shared across
whichever tests import it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

DEFAULT_SCORE = 8

# A small artificial delay so concurrent requests actually overlap in wall-clock time --
# without it, a fast in-process round trip could complete before the next one starts even
# under a worker pool, making peak_in_flight understate real concurrency (JDG-I-025).
_CONCURRENCY_DELAY_SECONDS = 0.05

_FAIL_RE = re.compile(r"\[\[fail=(\d+)\]\]")
_FAIL_ONCE_RE = re.compile(r"\[\[fail_once=(\d+)\]\]")
_GARBAGE_RE = re.compile(r"\[\[garbage\]\]")
_SCORE_RE = re.compile(r"\[\[score=(\d+)\]\]")


def reset_state() -> None:
    """Clear per-test call-count/fail-once/concurrency state (JDG-I-022/024/025)."""
    app.state.call_counts = {}
    app.state.fail_once_consumed = set()
    app.state.in_flight = 0
    app.state.peak_in_flight = 0


reset_state()


def _content_text(content: object) -> str:
    """A chat message's `content` is a plain string or a list of content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _marker_text(payload: dict) -> str:
    """Concatenate every message's content into one string to search for markers."""
    messages = payload.get("messages", [])
    return "\n".join(_content_text(m.get("content")) for m in messages)


def _chat_completion(model: str, content: str) -> dict:
    """An OpenAI-shaped `chat.completion` response wrapping `content`."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """JDG-U-001..004: dispatch on the first marker found in the request text."""
    payload = await request.json()
    text = _marker_text(payload)
    model = payload.get("model", "mock-judge")

    app.state.call_counts[text] = app.state.call_counts.get(text, 0) + 1
    app.state.in_flight += 1
    app.state.peak_in_flight = max(app.state.peak_in_flight, app.state.in_flight)
    try:
        await asyncio.sleep(_CONCURRENCY_DELAY_SECONDS)

        fail_once_match = _FAIL_ONCE_RE.search(text)
        if fail_once_match and text not in app.state.fail_once_consumed:
            app.state.fail_once_consumed.add(text)
            code = int(fail_once_match.group(1))
            return JSONResponse(status_code=code, content={"error": f"mock failure {code} (once)"})

        fail_match = _FAIL_RE.search(text)
        if fail_match:
            code = int(fail_match.group(1))
            return JSONResponse(status_code=code, content={"error": f"mock failure {code}"})

        if _GARBAGE_RE.search(text):
            return JSONResponse(content=_chat_completion(model, "not json at all, just prose."))

        score_match = _SCORE_RE.search(text)
        score = int(score_match.group(1)) if score_match else DEFAULT_SCORE
        body = json.dumps({"score": score, "reasoning": "mock judge deterministic reasoning"})
        return JSONResponse(content=_chat_completion(model, body))
    finally:
        app.state.in_flight -= 1
