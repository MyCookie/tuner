# Component Spec: Judge

**Purpose:** Score Silver records with an LLM and promote only those meeting the threshold to Gold (SAS §3.1). The judge model is fully pluggable: the Judge speaks the **OpenAI-compatible chat-completions API**, so any endpoint works — local Ollama/vLLM, a cloud provider's compat endpoint, or an internal gateway — selected purely by environment/config.

## CLI

```
eftp judge --run-id <RUN_ID> [--config configs/pipeline.yaml]
```

Env: `EFTP_S3_*`, `EFTP_JUDGE_BASE_URL`, `EFTP_JUDGE_API_KEY`, `MLFLOW_TRACKING_URI`. Exit 3 if zero records reach Gold.

## Input / Output

- **Input:** `eftp-silver/{run_id}/`.
- **Output:** `eftp-gold/{run_id}/records-*.jsonl` + tier manifest; an MLflow run (experiment `train.mlflow_experiment`, tag `eftp.run_id`) logging score distribution and promotion rate.

## Config (`judge.*`)

| Key | Meaning |
| :--- | :--- |
| `model` | model name sent in the API request (required non-empty ⇒ else exit 2) |
| `threshold` | minimum normalized score for promotion (default 0.7) |
| `max_concurrency` | parallel in-flight requests (default 4) |
| `max_retries` | per-record retries on API/parse failure (default 3) |

## Scoring protocol

One API call per record. The request renders the record's conversation into a rubric prompt (`src/eftp/judge/prompts.py`, versioned constant `RUBRIC_V1`) that instructs the judge to return **only** a JSON object:

```json
{"score": 7, "reasoning": "..."}
```

- Rubric dimensions: instruction adherence, factual plausibility, completeness, tone/formatting — integer score 1–10 overall.
- Request uses `temperature: 0` and, when the endpoint supports it, JSON response format.
- Parsing: extract the first JSON object in the reply; validate `score` ∈ [1,10] integer. Parse failure counts as a retry.
- Normalization: `evaluation.score = score / 10.0`; `evaluation.judge_model` = config `judge.model`; `evaluation.reasoning` from the reply; `evaluation.evaluated_at` = now.

## Core logic

1. Read Silver manifest + records; validate schema.
2. Delete `eftp-gold/{run_id}/` (idempotency).
3. Score records with a worker pool of `max_concurrency`; retries with exponential backoff + jitter (base 2 s, cap 60 s); HTTP 429/5xx and timeouts are retryable, 4xx (other than 429) is not.
4. A record that exhausts retries is dropped with reason `judge_error`. If `judge_error` drops exceed **10 %** of records read, abort with exit 1 (endpoint is unhealthy; partial Gold would silently bias the dataset).
5. Records with `score >= threshold` are written to Gold with `evaluation` populated; below ⇒ drop `below_threshold`.
6. Write tier manifest (drop reasons: `below_threshold`, `judge_error`), then log to MLflow (own run, tags `eftp.run_id` + `eftp.stage: judge` per [01 §7](../01-architecture.md)): `params` (judge model, threshold, rubric version), `metrics` (mean/median score, promotion_rate, judge_error_rate), and a score histogram artifact.

## Error handling

- Missing `EFTP_JUDGE_BASE_URL` or empty `judge.model` ⇒ exit 2 before reading data.
- Determinism caveat: judge output is inherently nondeterministic across endpoints; the manifest records rubric version and judge model so Gold tiers are comparable, not reproducible bit-for-bit.

## Acceptance criteria

- With the mock judge server ([06-testing.md §4](../06-testing.md)) returning canned scores, promotion matches the threshold exactly and `evaluation` blocks are fully populated.
- A mock returning garbage for one record drops exactly that record as `judge_error` after `max_retries` attempts.
- A mock failing 50 % of records aborts with exit 1.
- MLflow run exists with `eftp.run_id` tag and the specified metrics.

## MVP scope

All of the above, text-only rubric.

## Future phases

**Phase 4:** modality-specific evaluators run **before** the LLM rubric and can veto promotion — e.g. CLIP image–text alignment scoring for `image` parts (SAS Phase 4); evaluator scores are added under `evaluation.modality_scores` (additive schema change to contract §2).
