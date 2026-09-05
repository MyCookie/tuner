# Test Suite: Infrastructure & Tooling (`INF`)

Tests for the tooling *around* the pipeline: MinIO bootstrap + IAM, the MLflow server, Docker images, compose config, Hugging Face interaction, and failure behavior when infrastructure is absent. Specs under test: [05-infrastructure.md](../05-infrastructure.md), [04-model-adapters.md](../04-model-adapters.md). Files: `tests/integration/test_infra.py`, `tests/unit/test_infra_static.py`, `tests/slow/test_containers.py`, `tests/slow/test_scale.py`. Built in T04 (001–007), T09 (010–011), T14 (012), T15 exercises the slow lane.

## Setup

Integration cases read MinIO root credentials (`MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`) and every per-stage keypair from the environment; `.env.example` documents all of them. The canonical bare `TUNER_S3_ACCESS_KEY`/`TUNER_S3_SECRET_KEY` pair must be set to the **root** credentials for a local run: the suite drives `StorageClient` across every tier, and no single per-stage principal holds that many grants. `scripts/gate.sh` loads `.env` itself; running pytest directly needs the vars exported first.

## Object store & IAM (integration)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| INF-I-001 | `bootstrap_minio.py` against fresh MinIO | All 7 buckets exist (`tuner-bronze/silver/gold/artifacts/registry/assets/mlflow`); every per-stage user from the [05 §5](../05-infrastructure.md) matrix exists with its policy attached |
| INF-I-002 | Bootstrap re-run on an initialized store | Exit 0, idempotent: no duplicate policies, existing bucket contents untouched |
| INF-I-003 | **Full IAM matrix sweep** — parametrized over every principal×bucket cell of 05 §5, both directions | Every granted op succeeds and every ungranted op raises AccessDenied, per the 05 §5 legend (R = list+get; W = R + put+delete). The test's expectation table is a literal transcription of the doc table, so doc↔policy drift fails here |
| INF-I-004 | MLflow server round-trip (real compose `mlflow`, not file-backed) | Param/metric/artifact logged via `MLFLOW_TRACKING_URI` are readable back; artifact bytes appear under `tuner-mlflow` (proxied artifacts working); a stage credential attempting direct `tuner-mlflow` access is denied |
| INF-I-005 | Object store unreachable (endpoint pointed at a closed port) | `StorageClient` fails fast with a connection error — no hang (bounded retries/timeout)¹ |

¹ **Scope decision (T04):** at T04, no stage CLI does real storage I/O yet — every stage in `tuner/cli.py` is still T01's `sys.exit(1)` stub, so "any stage CLI fails fast on an unreachable store" isn't a buildable/testable claim until a stage genuinely talks to `StorageClient`. T04 implements and tests the shared mechanism directly against `StorageClient` (the layer every stage CLI will inherit this behavior from). The CLI-level half of this scenario — invoking a real stage CLI (starting with `tuner ingest`) against an unreachable store and asserting exit 1 + a connection-error message + no partial manifest — is deferred to **T06**, added as a companion case under this same ID (`INF-I-005`) once the Ingestor CLI exists, per the case-ID convention in [08 README](README.md) ("IDs are stable; never renumber, append instead").

## Static config checks (unit)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| INF-U-006 | `.env.example` completeness | Contains every env var named in [01 §4.3](../01-architecture.md), placeholder values only (a regex asserts nothing token-shaped) |
| INF-U-007 | Compose validity | `docker compose config -q` succeeds; services/ports/profiles match the [05 §1](../05-infrastructure.md) table; no service defines a credential literal |

## Hugging Face interaction

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| INF-U-010 | Gated model, missing/invalid `HF_TOKEN` (HF auth error mocked at the hub-client boundary) | Actionable `HFAuthError` naming `HF_TOKEN` and the model id — not a raw traceback¹ |
| INF-U-011 | Revision pinning | Every adapter in `ADAPTERS` has `hf_revision` set to a commit hash or tag — never `"main"`/`None` ([04 §1](../04-model-adapters.md)); reproducibility depends on it |
| INF-I-012 | Offline CI mode | With `HF_HUB_OFFLINE=1` and the pre-seeded tiny-test cache, the TOK integration suite passes with zero network calls. Implemented as a property of the CI job (cache-seed step + offline env), spec'd here so it can't be silently dropped |

¹ **Scope decision (T09, round 1 review on PR #9):** at T09, no stage CLI calls `load_tokenizer`/`load_base_model` yet — `tuner.models.base.HFAuthError` exists and is tested directly against the adapter methods that raise it, but nothing yet catches it and turns it into "stage exits 2." T09 implements and tests the shared mechanism (the message-translation itself); the CLI-level half of this scenario — a real stage CLI (starting with `tuner tokenize`) catching `HFAuthError` and exiting 2 — is deferred to **T10**, added as a companion case under this same ID once the Tokenizer CLI exists, per the case-ID convention in [08 README](README.md) ("IDs are stable; never renumber, append instead"). This mirrors `INF-I-005`'s own T04→T06 deferral.

## Slow lane (`@pytest.mark.slow` — nightly + T15, not per-push CI)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| INF-S-020 | Container structure: build `base` and `trainer` images | Both run as uid 1000 non-root (`whoami` ≠ root); `tuner --help` works as the container entrypoint; Python ≥3.11; image has no `.env` or credential files baked in |
| INF-S-021 | Scale smoke: 120 000 synthetic records through ingest → clean | Sharding kicks in at 50 000 (3 Bronze shards); counts conserve exactly; peak RSS of each stage stays bounded (streaming, no full-tier load into memory — assert a generous cap)² |

² **Spec/code gap found running this suite for real (T15, round-1 review on PR #19):** [03-components/cleaner.md](../03-components/cleaner.md) step 1 says the Cleaner should "stream envelopes", but `src/tuner/cleaner/cli.py` actually reads its entire Bronze input into one `bronze_records` list (and the whole mapped output into one `silver_records` list) before writing Silver — peak memory linear in total record count, not bounded. Measured on the T15 dev box at 120,000 records: ingest (which genuinely streams, one 50,000-record shard resident at a time) peaked at ~1.16 GB; clean (which does not stream) peaked at ~1.66 GB. `tests/slow/test_scale.py` asserts a per-stage generous cap above each observed peak rather than proving streaming — a single record-count data point can't distinguish "streaming" from "linear but still under the cap at this scale". T15 chose to record this gap rather than fix it; making the Cleaner actually stream is deferred — no build-plan slice currently owns it ([07-build-plan.md](../07-build-plan.md) §Slices lists Slice 2 as registry/smoke/trainer/source-type work, none of it Cleaner memory), so whichever slice picks this up should also update this footnote and `03-components/cleaner.md` step 1.
