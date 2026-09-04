# Tuner documentation map

This directory holds two document sets, kept side by side on purpose:

| | Audience | Where |
| :--- | :--- | :--- |
| **User guide** | Someone installing, running, or operating Tuner | this directory (`docs/*.md`) |
| **Engineering specification** | Someone implementing or extending Tuner | [`docs/spec/`](spec/00-product-scope.md) |

If you only want to run the pipeline, you should not need to open `docs/spec/` at
all. If you're changing how a stage works, the specification is normative — it
wins over code, and over this guide, if the two ever disagree.

## User guide

<!-- Each page below is added to this list by the task that writes it; an
     unlinked bullet is a page not yet written. -->

- [Getting started](00-getting-started.md) (installation, first run)
- [Architecture overview](01-architecture-overview.md) (how the pipeline fits together)
- [CLI reference](02-cli-reference.md)
- [Configuration reference](03-configuration.md)
- Component guides (ingestor, cleaner, judge, tokenizer, trainer, smoke-test,
  registry, inference) — forthcoming
- Operations & troubleshooting — forthcoming

## Engineering specification

Start at [spec/00-product-scope.md](spec/00-product-scope.md) and read in order:

1. [00-product-scope.md](spec/00-product-scope.md) — product definition, delivery phases, SAS traceability
2. [01-architecture.md](spec/01-architecture.md) — system design and the canonical glossary (buckets, env vars, config keys, run-ID format)
3. [02-data-contracts.md](spec/02-data-contracts.md) — Bronze/Silver/Gold/Artifact schemas (normative — wins over code)
4. [03-components/](spec/03-components/) — per-stage specs (ingestor, cleaner, judge, tokenizer, trainer, smoke-test, registry, inference)
5. [04-model-adapters.md](spec/04-model-adapters.md) — pluggable fine-tune target model layer
6. [05-infrastructure.md](spec/05-infrastructure.md) — Docker Compose, MinIO, IAM matrix, container hardening
7. [06-testing.md](spec/06-testing.md) — test strategy, fixtures, CI lanes
8. [07-build-plan.md](spec/07-build-plan.md) — ordered, one-task-per-session implementation plan
9. [08-test-specs/](spec/08-test-specs/README.md) — normative test cases, tagged by ID and traced to build tasks
10. [09-git-workflow.md](spec/09-git-workflow.md) — branching, commit, and merge-gate rules
11. [10-code-review.md](spec/10-code-review.md) — the independent-review merge gate

The original architecture spec this doc set derives from is [SAS.md](../SAS.md).
