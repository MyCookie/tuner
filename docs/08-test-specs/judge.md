# Test Suite: Judge (`JDG`)

Spec under test: [judge.md](../03-components/judge.md). Files: `tests/unit/test_judge_parsing.py`, `tests/unit/test_mock_judge.py`, `tests/integration/test_judge.py`. Coverage target: **100 %** of prompts + parsing.

## Setup

Integration cases mount the `mock_judge` fixture (in-process ASGI, `TUNER_JUDGE_BASE_URL` pointed at it) and seed Silver records whose text carries behavior markers ([06 §4](../06-testing.md)). MLflow assertions use a file-backed tracking URI (`mlflow.set_tracking_uri` to a temp dir) — no server needed.

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
| JDG-U-011 | JSON embedded in prose (\`\`\`json fences, leading text, brace-shaped prose or a stray quote before the real object, an unclosed first object — parametrized) | First JSON object extracted, skipping only false starts that aren't JSON at all (or lack a `score` key) |
| JDG-U-012 | Garbage / no JSON / JSON without `score` | `ParseError` (counts as retryable attempt) |
| JDG-U-013 | `score` 0, 11, 7.5, `"7"` (parametrized) | 0/11/7.5 rejected (int 1–10 only); `"7"` policy: rejected — strict types |
| JDG-U-014 | Normalization | score 7 → `evaluation.score == 0.7` exactly |
| JDG-U-015 | Retryability classifier: 429→retry, every 5xx (incl. 504/599, not just 500/502/503)→retry, timeout→retry, 400/401/404→fatal (parametrized) | Per [judge.md core logic 3](../03-components/judge.md) |
| JDG-U-016 | Backoff sequence for 3 retries (jitter seeded) | Base 2 s exponential, capped 60 s (assert computed delays, no real sleeping) |
| JDG-U-017 | Rubric prompt rendering for a 3-turn conversation | Contains every turn's text, the JSON-only output instruction, and `RUBRIC_V1` is embedded in the module as a constant |
| JDG-U-018 | `build_http_client` wiring: base URL, bearer-token header, falsy `api_key` | Header present/absent as expected |
| JDG-U-019 | `score_record`: a transport-level failure (no HTTP response) | Retried like a timeout, not fatal; succeeds on the next attempt |
| JDG-U-020 | `score_record`: a fatal (non-retryable) status | Returns `None` immediately, without spending further retries |
| JDG-U-021 | `score_record`: a 200 reply whose body doesn't parse to a valid score | Retried, not fatal |
| JDG-U-022 | `score_record`: every attempt fails with a retryable status | Retries exhausted, returns `None` (`judge_error`) rather than raising |
| JDG-U-023 | A reply with two JSON objects, the first carrying an out-of-range score | `ParseError` — the first qualifying object (JSON + has a `score` key) is authoritative; an invalid score there is fatal, not a reason to look at the second object |
| JDG-U-024 | A `{"score": N}`-shaped fragment nested inside an outer object whose own quoting breaks it (invalid JSON) | `ParseError` — the nested fragment is not rescued from within already-broken JSON |
| JDG-U-025 | 20,000 unmatched `{` characters, no valid JSON anywhere | `ParseError`, in well under a second — not the O(n²) worst case over the full length |

JDG-U-018..022 were added in review round 1: `score_record`'s retry/transport-error/fatal-status branches, and `build_http_client`'s wiring, had no case IDs of their own even though they were the only untagged tests in the repo. JDG-U-023..025 were added in review round 3 alongside the fixes they regression-test (see Notes below).

## Pipeline behavior (integration, mock judge)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| JDG-I-020 | Silver seeded with markers `[[score=5..9]]`, threshold 0.7 | Exactly the ≥7 records in Gold; others dropped `below_threshold`; counts reconcile |
| JDG-I-021 | Promoted records | `evaluation` fully populated: normalized score, `judge_model` = config value, reasoning, `evaluated_at`; ids/lineage unchanged from Silver |
| JDG-I-022 | One `[[garbage]]` record among clean ones, `max_retries: 2` | That record retried exactly 2 extra times (mock counts calls), then dropped `judge_error`; rest promoted |
| JDG-I-023 | 6 of 10 records `[[fail=500]]` (>10 % judge_error) | Exit 1, no Gold manifest written |
| JDG-I-024 | One `[[fail=429]]` record that succeeds on 2nd attempt (mock: fail once then score) | Promoted; retry path proven end-to-end |
| JDG-I-025 | `max_concurrency: 3` with a mock that records concurrent in-flight count | Peak in-flight ≤ 3 |
| JDG-I-026 | MLflow: run exists tagged `tuner.run_id` **and** `tuner.stage: judge`; params judge model/threshold/rubric version; metrics mean/median/promotion_rate/judge_error_rate; histogram artifact present | Per [judge.md core logic 6](../03-components/judge.md) |
| JDG-I-027 | Empty `judge.model` / unset `TUNER_JUDGE_BASE_URL` (parametrized) | Exit 2 before any Silver read (mock records zero calls) |
| JDG-I-028 | All records score below threshold | Exit 3 |
| JDG-I-029 | Re-run same run ID | Gold prefix rebuilt; single manifest (idempotency) |
| JDG-I-030 | Missing Silver manifest | Exit 2 (upstream incomplete) |
| JDG-I-031 | Seeded invalid Silver record (schema-breaking) | Exit 2 naming record id — invalid input is an abort, not a drop |
| JDG-I-032 | Unset `MLFLOW_TRACKING_URI` | Exit 2 before any Silver read (mock records zero calls) |

JDG-I-030/031 mirror CLN-I-035/034 (Cleaner's own upstream-validation cases) — the original table above omitted the Judge's equivalent of "validate schema" in [judge.md core logic 1](../03-components/judge.md), even though the implementation handles it the same way Ingestor and Cleaner do. Added while implementing T08, as a spec gap noticed against the sibling stages' suites, rather than left as an untested code path with no case to justify it.

## Notes — fixes from review round 1

- **Retryability covers *every* 5xx, not a fixed set of three.** `is_retryable()` originally checked membership in `{429, 500, 502, 503}` — [judge.md core logic 3](../03-components/judge.md) says "429/5xx" unconditionally, and a 504/520/... from a real gateway was silently treated as fatal, dropping the record as `judge_error` on the first attempt instead of retrying. Fixed to `429 or 500 <= status < 600`.
- **JSON-object extraction is a proper balanced-brace scan, not a regex.** `parse_reply`'s original `\{.*?\}` regex stops at the first `}` it finds, which can be *inside* the JSON string content itself — `{"score": 7, "reasoning": "use {braces} here"}` truncated to an unparseable fragment and dropped as `judge_error`. `_find_first_json_object` now tracks string-literal state (respecting `\"` escapes) so a brace inside a quoted string never affects nesting depth.
- **`MLFLOW_TRACKING_URI` is validated at the same point as `judge.model`/`TUNER_JUDGE_BASE_URL`**, before touching Silver at all — it was previously only read inside `_log_to_mlflow`, so a missing value raised an uncaught `KeyError` *after* the Gold shard and manifest were already committed, reporting exit 1 (failure) on a run whose actual output had succeeded.

## Notes — fixes from review round 2

- **`JDG-I-032` regression-tests the `MLFLOW_TRACKING_URI` early-validation fix itself** — round 1 fixed the timing but round 2 noted the fix had no dedicated test of its own (only the pre-existing `JDG-I-027` cases for `judge.model`/`TUNER_JUDGE_BASE_URL`); added as its own case rather than folded into `JDG-I-027`'s parametrize table, since it's a different env var with its own rationale (docs comment above, `cli.py`).
- **The balanced-brace scanner still gave up too early.** Round 1's `_find_first_json_object` found the *first* `{` in the text, balanced it, and returned (or failed) from there. That fails whenever brace-shaped prose precedes the real JSON object — e.g. `"Consider the set {a, b}. Verdict: {\"score\": 7, ...}"` balances `{a, b}` into a complete-but-non-JSON group and stops there, never reaching the real object. It also fails when a stray, unescaped quote in prose before the real object throws off string-literal tracking enough that depth never returns to 0 from that start at all. Renamed to `_iter_balanced_braces` and rewritten as a generator that tries *every* `{` start position in the text, yielding each balanced substring it finds and always advancing past a start that never closes — `parse_reply` now walks these candidates in order and keeps going past any that don't validate as a score object, instead of committing to the first one found. `JDG-U-011` gained four regression cases for exactly these shapes.

## Notes — fixes from review round 3

- **Round 2's "keep going past any that don't validate" went one step too far.** It treated an *invalid* score the same as a *missing* one — both just fell through to the next brace group — so a reply with a genuine, well-formed score object that happened to be out of range (e.g. `{"score": 11, ...}`) got silently skipped in favor of a second object later in the text, when [judge.md](../03-components/judge.md) says to extract *the first* JSON object and validate its score: an invalid score there is the parse failure, not grounds to keep looking. `_iter_balanced_braces` (renamed `_iter_balanced_brace_spans`, now yielding index spans instead of substrings) is unchanged; `parse_reply` now raises immediately once a candidate is JSON *and* carries a `score` key, rather than continuing past a bad one. Only candidates that aren't JSON at all, or lack a `score` key entirely, are still treated as false starts to skip. `JDG-U-023` regression-tests this.
- **A `{"score": N}`-shaped fragment could be lifted out of an outer object's own broken quoting.** A reply like `{"score": 3, "reasoning": "example {"score": 10}"}` has an unescaped quote inside the `reasoning` value, making the *outer* object invalid JSON — but the *inner* `{"score": 10}` substring, scanned fresh from its own start position, parses fine on its own and was returned instead of the whole reply being treated as a parse failure. `parse_reply` now records every span that fails `json.loads` and skips any later span nested inside one of them, so a fragment from within already-broken JSON is never rescued as if it were the real answer. `JDG-U-024` regression-tests this.
- **The scan was quadratic in reply length.** Trying every `{` start position (round 2's fix) means a reply consisting of thousands of unmatched `{` characters re-scans from each one to the end of the text — O(n²) — and a 20,000-character adversarial reply measured ~6.6 s per attempt, multiplied by `max_retries` on a worker-pool thread. `parse_reply` now caps the text it scans at `_MAX_SCAN_CHARS` (4,000 characters): the rubric prompt instructs the judge to return *only* a JSON object, so a genuine score object sits at or near the start of a reply, and a reply that needs more than that to find one is already malformed. `JDG-U-025` regression-tests the bound (asserts sub-second, not just "eventually raises").
