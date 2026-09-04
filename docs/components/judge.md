# Judge

The Judge is the pipeline's quality gate: it scores every Silver record with
an LLM against a fixed rubric and only promotes the records that clear a
configurable threshold to Gold. It's the one stage that talks to an external
model, and the one place non-determinism enters the pipeline — see
[Architecture overview](../01-architecture-overview.md) for where it sits
between the Cleaner and Tokenizer. Everything below comes from reading
`src/tuner/judge/cli.py`, `client.py`, and `prompts.py`, and from running
`tuner judge`/`tuner run` against the `mock-judge` sidecar this repository
ships for exactly this purpose.

## What it does

For each Silver record, the Judge renders the conversation into a rubric
prompt, sends it to whatever OpenAI-compatible endpoint `TUNER_JUDGE_BASE_URL`
names, and parses the reply for an integer score 1–10. Scores are normalized
to `[0.0, 1.0]`; records at or above `judge.threshold` are written to Gold
with `evaluation` populated, everything else is dropped. Because the judge
model itself is fully pluggable — any OpenAI-compatible chat-completions
endpoint works — this stage is the one part of the pipeline whose output
isn't bit-for-bit reproducible across environments; the manifest and MLflow
run record which judge model and rubric version produced it, so Gold tiers
stay comparable rather than reproducible.

## Input and output

| | Bucket | Path |
| :--- | :--- | :--- |
| Reads | `tuner-silver` | `{run_id}/` |
| Writes | `tuner-gold` | `{run_id}/records-00000.jsonl` + `{run_id}/manifest.json` (never sharded, same as the Cleaner — verified against `src/tuner/judge/cli.py`), plus an MLflow run |

A Silver record before, and the same record in Gold after, captured from a
real run against the `mock-judge` sidecar (`configs/pipeline.e2e.yaml`):

```json
// Silver: {"id": "6e01dad9-...", "conversation": [...], "evaluation": null}
```
```json
{
  "id": "6e01dad9-bcbf-4bac-80a0-89e4dc766adc",
  "conversation": [ /* unchanged */ ],
  "evaluation": {
    "score": 0.8,
    "judge_model": "mock-judge",
    "reasoning": "mock judge deterministic reasoning",
    "evaluated_at": "2026-09-04T00:23:57Z"
  }
}
```

The full manifest from that run — 99 Silver records in, 94 Gold out, all 5
drops `below_threshold` (`judge.threshold: 0.7`, i.e. a raw score below 7):

```json
"counts": {"read": 99, "written": 94, "dropped": 5},
"drops": [{"reason": "below_threshold", "count": 5}]
```

For the Gold schema (identical to Silver's, `evaluation` now required), see
[Data contracts §2](../spec/02-data-contracts.md).

## Key behavior worth knowing

**The rubric asks for one thing: a JSON object.** The prompt
(`src/tuner/judge/prompts.py`, versioned as `RUBRIC_V1`) scores four
dimensions holistically — instruction adherence, factual plausibility,
completeness, tone/formatting — as one integer 1–10, and instructs the model
to reply with *only* `{"score": <int>, "reasoning": "..."}`. The request
always sends `temperature: 0` and `response_format: {type: json_object}` —
it doesn't probe or degrade based on what the endpoint claims to support; an
endpoint that ignores or rejects `response_format` is on its own (the parser
below is the actual safety net, not endpoint negotiation).

**Reply parsing is stricter than "find some JSON in the text."** The parser
scans for the *first* balanced `{...}` span that both parses as JSON and has
a `score` key — not just the first `{`, since a reply can have brace-shaped
prose before the real object (`"Consider the set {a, b}. Verdict: {\"score\":
7,...}"`). Once a span qualifies as "the first JSON object," an invalid score
there (a string, a float, a bool, an out-of-range int) is a parse failure,
full stop — the parser does not fall through to scan for a second,
better-looking object elsewhere in the reply. `reasoning` gets the same
strictness: a non-string value there is also a parse failure. Either way,
this counts as one retryable attempt, not an immediate fatal error.

**Retries follow a specific retryable/fatal split, not "retry everything."**
HTTP 429 and any 5xx status, plus any transport-level failure (timeout,
connection refused), are retryable; every other 4xx (400, 401, 404, ...) is
treated as the endpoint telling you the request itself is wrong, and fails
immediately without burning through retries. Backoff is exponential with
full jitter — base 2 seconds, doubling per attempt, capped at 60 seconds —
and a record that exhausts `judge.max_retries` extra attempts is dropped as
`judge_error`, not retried indefinitely.

**A degraded endpoint aborts the whole run, not just the affected records.**
If `judge_error` drops exceed 10% of records read, the Judge exits 1 rather
than promoting a partial, endpoint-biased Gold tier — verified by reading
`JUDGE_ERROR_ABORT_FRACTION = 0.10` directly in `src/tuner/judge/cli.py`.
This is a deliberate policy choice worth knowing: a flaky endpoint that fails
on, say, 15% of your records doesn't quietly ship you a smaller-but-usable
Gold tier — it kills the whole run.

**Two things are checked before any record is scored, and before Gold is
even touched:** `judge.model` must be non-empty and `TUNER_JUDGE_BASE_URL`
must be set, and separately, `MLFLOW_TRACKING_URI` must be set — verified
directly:

```
$ uv run tuner judge --run-id <id> --config configs/pipeline.yaml   # ships with judge.model: ""
judge: judge.model must be non-empty and TUNER_JUDGE_BASE_URL must be set
```

The `MLFLOW_TRACKING_URI` check exists specifically so a missing tracking URI
can't surface *after* Gold has already been written — without it, the
failure would only appear during MLflow logging, once the Gold shard and
manifest were already committed, misreporting an actually-successful run as
a failure.

**Zero promotions is exit 3** — either every record scored below threshold,
or too many `judge_error`s to promote anything: `judge: zero records
promoted to Gold`.

**MLflow logging is per-run, not per-record.** One MLflow run per Judge
invocation (tag `tuner.stage: judge`), logging `judge_model`, `threshold`,
and `rubric_version` as params; `mean_score`, `median_score`,
`promotion_rate`, and `judge_error_rate` as metrics; and a plain-text
10-bucket score histogram as an artifact (MLflow has no native histogram
artifact type, so this is a `.txt` file, not a chart).

## Running it

Standalone: `tuner judge --run-id <RUN_ID> --config <path>`. As the third
stage of `tuner run`, between the Cleaner and Tokenizer. Both need
`TUNER_JUDGE_BASE_URL`/`TUNER_JUDGE_API_KEY` pointed at a real judge endpoint
(or the `mock-judge` sidecar for local/CI use — see
[Getting started §4](../00-getting-started.md)). See
[CLI reference — `tuner judge`](../02-cli-reference.md#tuner-judge) for exact
flags and exit codes.

## Configuring it

`judge.model`, `judge.threshold`, `judge.max_concurrency`, and
`judge.max_retries` are documented in
[Configuration reference — `judge`](../03-configuration.md#judge). The
endpoint itself (which server, which credential) is environment-only
(`TUNER_JUDGE_BASE_URL`/`TUNER_JUDGE_API_KEY`), never a config key — see
[Configuration reference — environment variables](../03-configuration.md#environment-variables-env).
