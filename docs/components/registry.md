# Registry

The Registry is not a service or a database — it *is* the `tuner-registry`
bucket. One manifest object per trained model version links that model's
weights back to the exact Gold dataset and MLflow experiment that produced
it; `tuner registry` is a thin CLI over reading and listing those objects.
See [Architecture overview — run IDs](../01-architecture-overview.md#run-ids-the-only-thing-that-ties-a-pipeline-execution-together)
for how a model version string (`{adapter_name}-{run_id}`) ties back to
everything else. Everything below comes from reading
`src/tuner/registry_ops/cli.py` and `src/tuner/models/registry.py`, and from
running `tuner registry list` against a real registry populated by several
Trainer runs.

## What it does

`tuner registry list` downloads every `{model_version}/manifest.json` object
under `tuner-registry` and prints one row per version, newest first. That's
the entire MVP scope of this component — the manifests themselves are
written by the Trainer (see [trainer.md](trainer.md)), not by anything under
`registry_ops`; this stage only reads.

## Input and output

| | Bucket | Path |
| :--- | :--- | :--- |
| Reads | `tuner-registry` | every `{model_version}/manifest.json` |
| Writes | — | `list` is read-only |

Real output, from a registry with several prior `tiny-test` runs plus the
one this page's other examples are drawn from:

```
$ uv run tuner registry list
MODEL_VERSION                                 ADAPTER         CREATED_AT             STATUS     FINAL_EVAL_LOSS
tiny-test-run-20260904-002347-19e725          tiny-test       2026-09-04T00:24:14Z   candidate  1.7918636798858643
tiny-test-run-20260903-235031-646d90          tiny-test       2026-09-03T23:51:59Z   candidate  2.1498844623565674
tiny-test-run-20260903-232937-10c126          tiny-test       2026-09-03T23:30:53Z   candidate  1.5302120447158813
```

For the full registry-manifest schema, see
[Data contracts §5.2](../spec/02-data-contracts.md).

## Key behavior worth knowing

**Only `list` exists.** `tuner registry show/promote/rollback` are specified
in [spec/03-components/registry.md](../spec/03-components/registry.md) —
`promote`/`rollback`'s single-promoted-version-per-adapter semantics, the
`history` append-log, the rollback safety valve — but none of it is
implemented yet; they're explicitly Phase 2. Calling one today doesn't fail
with "not implemented," it fails the way click fails on any unknown
subcommand, verified directly:

```
$ uv run tuner registry show tiny-test-run-20260904-002347-19e725
Usage: tuner registry [OPTIONS] COMMAND [ARGS]...
Try 'tuner registry --help' for help.

Error: No such command 'show'.
```

That means **every registered model's `status` is always `candidate`** —
there's no promotion path yet to move it to `promoted`, so nothing in this
MVP ever gets served preferentially over anything else; a human reads
`registry list` and the Smoke-test transcript to decide what a candidate is
worth.

**A broken manifest is reported, not fatal.** `list` is explicitly designed,
per the spec's own words, to "not die on one bad object": a manifest that
fails JSON parsing, schema validation, or even UTF-8 decoding is printed as
`<key> INVALID` in the listing rather than aborting the whole command.
Verified directly, by writing a deliberately corrupt `manifest.json` and
re-running `list`:

```
$ uv run tuner registry list
MODEL_VERSION                                 ADAPTER         CREATED_AT             STATUS     FINAL_EVAL_LOSS
tiny-test-run-...                             tiny-test       ...                    candidate  ...
zz-broken-test/manifest.json                  INVALID
```

**`list` always exits 0**, by design — it's a diagnostic tool, and a bad
object in the bucket is something to report, not something that should make
the command itself fail. Don't script around a non-zero exit code to detect
registry problems; check the printed output for `INVALID` rows instead.

**Sort order is lexicographic on `created_at`, which is chronological here**
only because `created_at` is a zero-padded ISO-8601 UTC string — the sort key
is the string itself, not a parsed timestamp, which happens to produce the
right order for this format without needing a datetime dependency in the
listing path.

**Registry-ops credentials are scoped independently of the Trainer's own.**
Per the IAM matrix, the `registry-ops` principal gets read/write on
`tuner-registry` and read on `tuner-artifacts` (to verify `weights_uri`
objects exist, once `show` lands) — it never gets Bronze/Silver/Gold access,
matching the same "each stage's blast radius is fixed by bucket ACLs, not
code discipline" design described in
[Architecture overview](../01-architecture-overview.md#the-medallion-tiers-and-why-each-stage-sees-only-one).

## Running it

`tuner registry list` — no `--run-id`, no `--config`; it operates on the
whole bucket across every run, not one run's config-scoped output. It is
never part of `tuner run`'s own stage sequence — it's a query tool you run
afterward. See
[CLI reference — `tuner registry list`](../02-cli-reference.md#tuner-registry-list)
for the exact column shape.

## Configuring it

Nothing in `pipeline.yaml` affects this stage — its only inputs are whatever
is already sitting in `tuner-registry`. Its credentials come from the
`REGISTRY_OPS_S3_*` environment pair (see
[Configuration reference — environment variables](../03-configuration.md#environment-variables-env)).
