"""Mock-judge self-tests (docs/spec/08-test-specs/judge.md — built in T05, before
the Judge exists)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from tests.mock_judge.app import DEFAULT_SCORE, app

client = TestClient(app)


def _request(content: str) -> dict:
    return {
        "model": "mock-judge-v1",
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
    }


def _assert_openai_shaped(body: dict, model: str) -> None:
    assert body["object"] == "chat.completion"
    assert body["model"] == model
    assert isinstance(body["id"], str) and body["id"]
    assert isinstance(body["created"], int)
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    assert isinstance(choice["message"]["content"], str)
    assert "usage" in body


def test_score_marker_returns_scored_json():
    """JDG-U-001: `[[score=7]]` in the user content -> 200, OpenAI-shaped response,
    message content is `{"score": 7, "reasoning": ...}` JSON."""
    response = client.post("/v1/chat/completions", json=_request("Evaluate: [[score=7]] please."))

    assert response.status_code == 200
    body = response.json()
    _assert_openai_shaped(body, "mock-judge-v1")

    content = json.loads(body["choices"][0]["message"]["content"])
    assert content["score"] == 7
    assert isinstance(content["reasoning"], str) and content["reasoning"]


def test_garbage_marker_returns_non_json_prose():
    """JDG-U-002: `[[garbage]]` marker -> 200 with non-JSON prose content."""
    response = client.post("/v1/chat/completions", json=_request("Evaluate: [[garbage]]"))

    assert response.status_code == 200
    body = response.json()
    _assert_openai_shaped(body, "mock-judge-v1")

    content = body["choices"][0]["message"]["content"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(content)


@pytest.mark.parametrize("code", [429, 500])
def test_fail_marker_returns_http_error(code: int):
    """JDG-U-003: `[[fail=429]]` -> HTTP 429; `[[fail=500]]` -> HTTP 500."""
    response = client.post("/v1/chat/completions", json=_request(f"Evaluate: [[fail={code}]]"))

    assert response.status_code == code


def test_no_marker_returns_deterministic_default_score():
    """JDG-U-004: no marker -> deterministic default score 8."""
    response = client.post("/v1/chat/completions", json=_request("Evaluate this response please."))

    assert response.status_code == 200
    body = response.json()
    _assert_openai_shaped(body, "mock-judge-v1")

    content = json.loads(body["choices"][0]["message"]["content"])
    assert content["score"] == DEFAULT_SCORE == 8
