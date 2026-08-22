"""Mock judge (docs/06-testing.md §4) — a deterministic OpenAI-compatible
`/v1/chat/completions` test double for the Judge stage (docs/03-components/judge.md).

Behavior is selected by a marker embedded anywhere in the request's message
content, so fixture/seed records carry the marker and outcomes are
deterministic without a real LLM endpoint:

    [[score=N]]   -> 200; message content is `{"score": N, "reasoning": ...}`
    [[garbage]]   -> 200; message content is non-JSON prose
    [[fail=CODE]] -> HTTP CODE (e.g. 429, 500)
    (no marker)   -> 200; deterministic default score (DEFAULT_SCORE)

`[[fail=...]]` is checked first so a record can't accidentally suppress a
failure by also matching another marker. Runs in-process via the ASGI test
client for unit (JDG-U) and integration (JDG-I) cases, and as a compose
sidecar for the T14 E2E steel thread.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

DEFAULT_SCORE = 8

_FAIL_RE = re.compile(r"\[\[fail=(\d+)\]\]")
_GARBAGE_RE = re.compile(r"\[\[garbage\]\]")
_SCORE_RE = re.compile(r"\[\[score=(\d+)\]\]")


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
