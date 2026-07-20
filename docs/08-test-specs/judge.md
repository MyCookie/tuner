# Test Suite: Judge (`JDG`)

Spec under test: [judge.md](../03-components/judge.md). Files: `tests/unit/test_judge_parsing.py`, `tests/unit/test_mock_judge.py`, `tests/integration/test_judge.py`. Coverage target: **100 %** of prompts + parsing.

## Setup

Integration cases mount the `mock_judge` fixture (in-process ASGI, `EFTP_JUDGE_BASE_URL` pointed at it) and seed Silver records whose text carries behavior markers ([06 §4](../06-testing.md)). MLflow assertions use a file-backed tracking URI (`mlflow.set_tracking_uri` to a temp dir) — no server needed.

## Mock judge self-tests (unit — built in T05, before the Judge exists)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| JDG-U-001 | POST `/v1/chat/completions` with `[[score=7]]` in the user content | 200; OpenAI-shaped response; message content is `{"score": 7, "reasoning": ...}` JSON |
| JDG-U-002 | `[[garbage]]` marker | 200 with non-JSON prose content |
| JDG-U-003 | `[[fail=429]]` marker | HTTP 429; `[[fail=500]]` → HTTP 500 |
| JDG-U-004 | No marker | Deterministic default score 8 |

## Reply parsing & client (unit)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| JDG-U-010 | Clean JSON reply | `(7, "reasoning")` extracted |
| JDG-U-011 | JSON embedded in prose (\`\`\`json fences, leading text — parametrized) | First JSON object extracted |
| JDG-U-012 | Garbage / no JSON / JSON without `score` | `ParseError` (counts as retryable attempt) |
| JDG-U-013 | `score` 0, 11, 7.5, `"7"` (parametrized) | 0/11/7.5 rejected (int 1–10 only); `"7"` policy: rejected — strict types |
| JDG-U-014 | Normalization | score 7 → `evaluation.score == 0.7` exactly |
| JDG-U-015 | Retryability classifier: 429→retry, 500/502/503→retry, timeout→retry, 400/401/404→fatal (parametrized) | Per [judge.md core logic 3](../03-components/judge.md) |
| JDG-U-016 | Backoff sequence for 3 retries (jitter seeded) | Base 2 s exponential, capped 60 s (assert computed delays, no real sleeping) |
| JDG-U-017 | Rubric prompt rendering for a 3-turn conversation | Contains every turn's text, the JSON-only output instruction, and `RUBRIC_V1` is embedded in the module as a constant |

## Pipeline behavior (integration, mock judge)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| JDG-I-020 | Silver seeded with markers `[[score=5..9]]`, threshold 0.7 | Exactly the ≥7 records in Gold; others dropped `below_threshold`; counts reconcile |
| JDG-I-021 | Promoted records | `evaluation` fully populated: normalized score, `judge_model` = config value, reasoning, `evaluated_at`; ids/lineage unchanged from Silver |
| JDG-I-022 | One `[[garbage]]` record among clean ones, `max_retries: 2` | That record retried exactly 2 extra times (mock counts calls), then dropped `judge_error`; rest promoted |
| JDG-I-023 | 6 of 10 records `[[fail=500]]` (>10 % judge_error) | Exit 1, no Gold manifest written |
| JDG-I-024 | One `[[fail=429]]` record that succeeds on 2nd attempt (mock: fail once then score) | Promoted; retry path proven end-to-end |
| JDG-I-025 | `max_concurrency: 3` with a mock that records concurrent in-flight count | Peak in-flight ≤ 3 |
| JDG-I-026 | MLflow: run exists tagged `eftp.run_id`; params judge model/threshold/rubric version; metrics mean/median/promotion_rate/judge_error_rate; histogram artifact present | Per [judge.md core logic 6](../03-components/judge.md) |
| JDG-I-027 | Empty `judge.model` / unset `EFTP_JUDGE_BASE_URL` (parametrized) | Exit 2 before any Silver read (mock records zero calls) |
| JDG-I-028 | All records score below threshold | Exit 3 |
| JDG-I-029 | Re-run same run ID | Gold prefix rebuilt; single manifest (idempotency) |
