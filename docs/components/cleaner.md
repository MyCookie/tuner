# Cleaner

The Cleaner turns raw Bronze envelopes into the pipeline's first
"conversation-shaped" tier — Silver — by mapping each source record into the
Multimodal Contract, scrubbing text, and dropping anything that fails a fixed
set of quality/PII/dedup rules. It's purely deterministic: no LLM calls, no
randomness, same input always yields the same output. See
[Architecture overview](../01-architecture-overview.md) for how Silver fits
between Bronze and Gold. Everything below comes from reading
`src/tuner/cleaner/cli.py`, `rules.py`, and `patterns.py`, and from running
`tuner clean`/`tuner run` against this repository's fixtures.

## What it does

For every Bronze record, the Cleaner does four things, in order: (1) maps the
source's raw shape into a `conversation` array (rules differ for `csv` vs
`jsonl` sources), (2) scrubs each text value (Unicode normalization, control
characters, PII patterns, blank-line collapsing), (3) filters records that
are too short, too long, structurally invalid, or have an empty turn after
scrubbing, and (4) drops exact duplicates (by hash of the scrubbed
conversation, first occurrence wins, scoped to this run only). The Bronze
`id` carries through unchanged; `evaluation` is always `null` at this tier —
that only gets populated by the Judge.

## Input and output

| | Bucket | Path |
| :--- | :--- | :--- |
| Reads | `tuner-bronze` | `{run_id}/` (manifest read first — its absence means Bronze isn't done) |
| Writes | `tuner-silver` | `{run_id}/records-00000.jsonl` + `{run_id}/manifest.json` (unlike the Ingestor, the Cleaner never shards — always one file, verified against `src/tuner/cleaner/cli.py`) |

Two before/after examples, captured from an actual `tuner run` against
`configs/pipeline.e2e.yaml` and `fixtures/support_dialogs.csv` — both show
the same PII scrub chain, on different pattern types:

**Email**, Bronze row 94 → Silver:

```json
// Bronze raw: {"question": "...", "answer": "Please email SECURITY@EXAMPLE.COM immediately; it's monitored 24/7.", "system": ""}
```
```json
{
  "conversation": [
    {"role": "user", "content": [{"type": "text", "value": "What's the best way to reach the security team urgently?"}]},
    {"role": "assistant", "content": [{"type": "text", "value": "Please email [EMAIL] immediately; it's monitored 24/7."}]}
  ],
  "evaluation": null
}
```

**Phone**, a different row:

```json
// Bronze raw answer: "Yes, call +1 (555) 123-4567 and an agent will assist you."
```
```json
{"role": "assistant", "content": [{"type": "text", "value": "Yes, call [PHONE] and an agent will assist you."}]}
```

Both scrubbers ran with the default config (`clean.pii: [email, phone]`).
Note the CSV's own `system` column is present in `raw` but never appears in
either Silver record above — `configs/pipeline.e2e.yaml`'s mapping for this
source names only `prompt_column`/`response_column`, so no system turn is
built regardless of what the source data contains; a `system_column` has to
be named explicitly (see [Configuration reference — `ingest`](../03-configuration.md#ingest)).

For the full Silver/Gold schema, see
[Data contracts §2](../spec/02-data-contracts.md); it's the same schema for
both tiers, differing only by whether `evaluation` is populated.

## Key behavior worth knowing

**Drop reasons are fixed and exhaustive**: `unmappable`, `too_short`,
`too_long`, `empty_turn`, `bad_structure`, `duplicate`. A real run's manifest,
verified end to end (120 Bronze in, 99 Silver out):

```json
"counts": {"read": 120, "written": 99, "dropped": 21},
"drops": [
  {"reason": "duplicate", "count": 3},
  {"reason": "too_long", "count": 2},
  {"reason": "too_short", "count": 5},
  {"reason": "unmappable", "count": 11}
]
```

**Mapping differs by source type, and JSONL is more permissive than you might
expect.** For `csv`, the row is mapped using that source's own `mapping`
config (`prompt_column`/`response_column`/`system_column`) — a missing or
empty prompt/response is `unmappable`. For `jsonl`, the Cleaner first checks
whether `raw` already has a contract-shaped `conversation` array (adopted
as-is) — this repo's `fixtures/extra_dialogs.jsonl` uses exactly this to seed
already-Gold-shaped test data — and otherwise falls back to recognizing one
of four flat key-sets: `{prompt, response}`, `{prompt, response, system}`,
`{question, answer}`, or `{question, answer, system}`, mapped the same way a
CSV row would be. *Any other shape* — extra or missing keys, even ones that
look conversational — is `unmappable`; five of this repo's own
`extra_dialogs.jsonl` lines (a "note", a bare `id`/`payload`, a log line, a
tags object, a `raw_text` blob) are `unmappable` for exactly this reason,
deliberately, as a fixture case.

**Scrubbing is idempotent, and the ordering matters.** Each text value goes
through NFC normalization → control-character stripping → PII placeholder
substitution → blank-line collapsing → trim, in that fixed order. PII
scrubbing runs *after* control-character stripping specifically so a stray
control character sitting between two PII-shaped fragments can't hide a match
by breaking it in two — an ordering choice that isn't obvious just from
reading the config.

**The regexes are deliberately narrow, not maximal.** `src/tuner/cleaner/patterns.py`'s
own comment states the philosophy directly: a false negative (PII that slips
through) is a privacy bug, but a false positive (a version string like
`v2.10.3`, or a zip code) silently corrupting training data on every run is
treated as the worse failure mode. Concretely, the phone regex requires one
of four specific digit-group shapes (e.g. `555-123-4567`, `(555) 123-4567`)
and never matches on a bare digit run or a `.`-separated string — so a
version number or a raw port number is never mistaken for a phone number,
and conversely an oddly-formatted real phone number (unusual separators)
could slip through unscrubbed.

**Dedup is exact and run-scoped only.** Two records anywhere in this run
whose *scrubbed* conversation hashes identically are collapsed to the first
occurrence — this is not near-dedup (no MinHash/embedding similarity; that's
explicitly out of MVP scope) and it does not look across runs, so re-running
the same source data under a new run ID does not dedup against a prior run's
Silver output.

**Invalid Bronze input is a hard failure, not a drop.** A Bronze record that
fails schema validation on read exits 2 immediately — that's an upstream
bug, distinct from a record that's *readable but fails cleaning policy* (a
`drop`). A missing Bronze manifest is the same exit 2, verified directly:

```
$ uv run tuner clean --run-id run-20260904-000000-000000 --config configs/pipeline.e2e.yaml
clean: missing manifest: s3://tuner-bronze/run-20260904-000000-000000/manifest.json
```

**Zero survivors is exit 3.** If every Bronze record gets dropped for any
combination of reasons, the Cleaner refuses to write an empty Silver tier and
propagate it downstream: `clean: zero records survived cleaning`.

**Re-running is byte-identical.** Cleaning has no randomness anywhere, so
re-running `tuner clean` against the same run ID reproduces the exact same
manifest counts and drop reasons — verified directly against a real run:
identical `{read: 120, written: 99, dropped: 21}` and the same four drop
counts on a second invocation.

## Running it

Standalone: `tuner clean --run-id <RUN_ID> --config <path>`. As the second
stage of `tuner run`, right after the Ingestor. See
[CLI reference — `tuner clean`](../02-cli-reference.md#tuner-clean) for exact
flags and exit codes.

## Configuring it

`clean.min_chars`, `clean.max_chars`, and `clean.pii` (the enabled scrubber
list) are documented in
[Configuration reference — `clean`](../03-configuration.md#clean); the
per-source `mapping` block that feeds this stage's CSV structure mapping is
under [`ingest`](../03-configuration.md#ingest) since it's set alongside the
source, not under `clean` itself.
