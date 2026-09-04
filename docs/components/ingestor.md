# Ingestor

The Ingestor is the first stage of the pipeline and the only one that touches
the outside world — see [Architecture overview](../01-architecture-overview.md)
for where it sits relative to the rest of the medallion flow. Its whole job is
narrow: take whatever the configured sources hand it and wrap each record in a
metadata envelope, byte-faithfully, with nothing cleaned, normalized, or
dropped. Everything below was verified either by reading
`src/tuner/ingestor/cli.py` and `src/tuner/ingestor/sources.py` directly, or by
running `tuner ingest`/`tuner run` against this repository's own fixtures.

## What it does

For each source listed under `ingest.sources`, the Ingestor reads every
record the source yields and writes it into Bronze unchanged, plus a small
envelope of metadata about where it came from. No structure mapping, no PII
scrubbing, no filtering happens here — that's the Cleaner's job. If a source
produces something malformed, the Ingestor fails the whole run rather than
silently skip it; if it succeeds, whatever went in comes out byte-identical
inside `raw`.

## Input and output

| | Bucket | Path |
| :--- | :--- | :--- |
| Reads | *(external)* | whatever `ingest.sources[].uri` names — a local path today (see below: `s3://` is spec'd but not yet implemented) |
| Writes | `tuner-bronze` | `{run_id}/records-{NNNNN}.jsonl` + `{run_id}/manifest.json` |

A CSV row before and after, taken from an actual run against
`fixtures/support_dialogs.csv` (config: `configs/pipeline.e2e.yaml`):

```jsonc
// fixtures/support_dialogs.csv, row 1:
// question,answer,system
// "How do I reset my password for your account?","Go to Settings...","You are a helpful support agent."
```

```json
{
  "id": "6e01dad9-bcbf-4bac-80a0-89e4dc766adc",
  "run_id": "run-20260904-002347-19e725",
  "source": {
    "uri": "fixtures/support_dialogs.csv",
    "type": "csv",
    "locator": "row:1",
    "ingested_at": "2026-09-04T00:23:49Z",
    "ingestor_version": "0.1.0"
  },
  "content_hash": "sha256:7f1851ab784ba76d694389f8afb5b26f646123672654cd3ac96c6c53e9d76f13",
  "raw": {
    "question": "How do I reset my password for your account?",
    "answer": "Go to Settings, select Security, then choose Reset Password. You'll receive a confirmation email within a few minutes.",
    "system": "You are a helpful support agent."
  }
}
```

Note that `raw` is the entire CSV row, including the `system` column, even
though this particular run's `ingest.sources[].mapping` never names a
`system_column` for it — the Ingestor doesn't look at `mapping` at all beyond
using it to fail fast at startup (below); interpreting *which* columns matter
is entirely the Cleaner's job, downstream. See
[Data contracts §1](../spec/02-data-contracts.md) for the full Bronze schema
and [Configuration reference — `ingest`](../03-configuration.md#ingest) for
`mapping`'s exact keys.

For the full schema and the tier-manifest shape, see
[Data contracts §1 and §3](../spec/02-data-contracts.md) rather than
re-deriving it here.

## Key behavior worth knowing

**Two source types exist, and that's all.** `csv` and `jsonl` are the only
registered source types — `sql`, `pdf`, and `api` are reserved names for later
phases. Naming one of those today doesn't fail with a generic schema error;
it fails with a message that says exactly what's missing, verified directly:

```
$ uv run tuner ingest --run-id run-20260904-000000-000000 --config <a config naming type: sql>
ingest: unknown source type 'sql'; known types: ['csv', 'jsonl']
```

**`s3://` source URIs are spec'd but not implemented.** The component spec
([spec/03-components/ingestor.md](../spec/03-components/ingestor.md)) lists
`s3://` URIs as valid alongside local paths. Reading the actual
`CsvSource`/`JsonlSource` constructors shows they only ever call
`Path(cfg.uri).open(...)` — there is no branch that recognizes an `s3://`
prefix, and no `StorageClient` primitive yet for reading an arbitrary object
by key. If you point a source `uri` at `s3://...` today, it fails as an
unreadable local path, not as "not yet supported" — worth knowing before you
assume the spec's claim holds.

**A CSV `mapping`'s columns are checked before any row is read, at source
construction time** — a `prompt_column`/`response_column`/`system_column`
naming a column that isn't in the CSV header fails immediately (exit 2), not
partway through ingestion. Note this validates *that the column exists*, not
that it maps sensibly; a column existing but always empty is a Cleaner-side
`unmappable` drop, not an Ingestor-side failure.

**Malformed input never leaves a partial shard, but it can leave earlier
*complete* shards behind.** A CSV row with more fields than the header (an
unescaped comma, typically) or a JSONL line that isn't valid JSON raises
immediately and exits 2. The manifest (the commit marker) is only written
after every record from every source has streamed through successfully, so
it's always absent on this kind of abort. But each shard
(`_write_shard` in `src/tuner/ingestor/cli.py`) is uploaded the moment it
fills, inside the same loop that reads records -- so on a large, multi-shard
run, any shard that already filled *before* the bad record survives the
abort in storage, manifest or no manifest. This repository's own fixtures
all fit in a single shard, so a fixture-scale abort really does leave Bronze
completely empty; it's the code path, not a run of this scale, that proves
the multi-shard case.

**Zero records across every configured source is exit 3**, verified:

```
$ uv run tuner ingest --run-id run-20260904-000000-000000 --config <a config whose one CSV source has header-only rows>
ingest: zero records ingested across all sources
```

**Sharding exists but fixture-scale data never touches it.** Records stream
into `records-{NNNNN}.jsonl` shards of ≤ 50,000 records each
(`SHARD_SIZE` in `src/tuner/ingestor/cli.py`); every fixture and test config
in this repository produces far fewer records than that, so in practice
you'll only ever see `records-00000.jsonl`.

**Multiple sources merge into one Bronze tier**, distinguished by
`source.uri`. Running `configs/pipeline.e2e.yaml` (one CSV source, 100 rows,
one JSONL source, 20 lines) against this repository's fixtures produced
exactly 120 Bronze envelopes — `counts: {read: 120, written: 120, dropped:
0}` — with `dropped` always `0` at this tier: the Ingestor preserves
everything it can parse; only the Cleaner filters.

**Re-running with the same run ID is a clean overwrite, not an accumulation**
— verified directly: re-running `tuner clean` (and by the same code pattern,
`tuner ingest`) against an existing run ID reproduced byte-identical manifest
counts, because every stage deletes its own output prefix before writing
anything new.

## Running it

Standalone: `tuner ingest --run-id <RUN_ID> --config <path>`. As part of the
full pipeline: the first stage `tuner run` invokes. See
[CLI reference — `tuner ingest`](../02-cli-reference.md#tuner-ingest) for the
exact flags and every exit code, and
[Getting started](../00-getting-started.md) for a full first-run walkthrough.

## Configuring it

Everything an operator can set for this stage — `ingest.sources`, each
source's `type`/`uri`/`mapping` — is documented in
[Configuration reference — `ingest`](../03-configuration.md#ingest); nothing
here duplicates it.
