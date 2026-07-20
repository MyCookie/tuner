# Test Suite: Infrastructure & Tooling (`INF`)

Tests for the tooling *around* the pipeline: MinIO bootstrap + IAM, the MLflow server, Docker images, compose config, Hugging Face interaction, and failure behavior when infrastructure is absent. Specs under test: [05-infrastructure.md](../05-infrastructure.md), [04-model-adapters.md](../04-model-adapters.md). Files: `tests/integration/test_infra.py`, `tests/unit/test_infra_static.py`, `tests/slow/test_containers.py`, `tests/slow/test_scale.py`. Built in T04 (001–007), T09 (010–011), T14 (012), T15 exercises the slow lane.

## Object store & IAM (integration)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| INF-I-001 | `bootstrap_minio.py` against fresh MinIO | All 7 buckets exist (`eftp-bronze/silver/gold/artifacts/registry/assets/mlflow`); every per-stage user from the [05 §5](../05-infrastructure.md) matrix exists with its policy attached |
| INF-I-002 | Bootstrap re-run on an initialized store | Exit 0, idempotent: no duplicate policies, existing bucket contents untouched |
| INF-I-003 | **Full IAM matrix sweep** — parametrized over every principal×bucket cell of 05 §5, both directions | Every granted op (R: list+get, W: put+delete under own run prefix) succeeds; every ungranted op raises AccessDenied. The test's expectation table is a literal transcription of the doc table, so doc↔policy drift fails here |
| INF-I-004 | MLflow server round-trip (real compose `mlflow`, not file-backed) | Param/metric/artifact logged via `MLFLOW_TRACKING_URI` are readable back; artifact bytes appear under `eftp-mlflow` (proxied artifacts working); a stage credential attempting direct `eftp-mlflow` access is denied |
| INF-I-005 | Object store unreachable (endpoint pointed at a closed port) | Any stage CLI fails fast with exit 1 and a connection-error message — no hang (bounded retries/timeout), no partial manifest |

## Static config checks (unit)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| INF-U-006 | `.env.example` completeness | Contains every env var named in [01 §4.3](../01-architecture.md), placeholder values only (a regex asserts nothing token-shaped) |
| INF-U-007 | Compose validity | `docker compose config -q` succeeds; services/ports/profiles match the [05 §1](../05-infrastructure.md) table; no service defines a credential literal |

## Hugging Face interaction

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| INF-U-010 | Gated model, missing/invalid `HF_TOKEN` (HF auth error mocked at the hub-client boundary) | Stage exits 2 with an actionable message naming `HF_TOKEN` and the model id — not a raw traceback |
| INF-U-011 | Revision pinning | Every adapter in `ADAPTERS` has `hf_revision` set to a commit hash or tag — never `"main"`/`None` ([04 §1](../04-model-adapters.md)); reproducibility depends on it |
| INF-I-012 | Offline CI mode | With `HF_HUB_OFFLINE=1` and the pre-seeded tiny-test cache, the TOK integration suite passes with zero network calls. Implemented as a property of the CI job (cache-seed step + offline env), spec'd here so it can't be silently dropped |

## Slow lane (`@pytest.mark.slow` — nightly + T15, not per-push CI)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| INF-S-020 | Container structure: build `base` and `trainer` images | Both run as uid 1000 non-root (`whoami` ≠ root); `eftp --help` works as the container entrypoint; Python ≥3.11; image has no `.env` or credential files baked in |
| INF-S-021 | Scale smoke: 120 000 synthetic records through ingest → clean | Sharding kicks in at 50 000 (3 Bronze shards); counts conserve exactly; peak RSS of each stage stays bounded (streaming, no full-tier load into memory — assert a generous cap) |
