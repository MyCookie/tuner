# Tuner Infrastructure & Security

Environment story from local dev (MVP) to cloud production (Phase 3). The invariant across both: stages are stateless containers configured entirely by the env vars in [01-architecture.md §4.3](01-architecture.md), talking to an S3-compatible store through `StorageClient` — so promotion to the cloud changes deployment artifacts, never pipeline code (SAS §4.1).

---

## 1. Local topology (MVP) — Docker Compose

`docker-compose.yaml` services:

| Service | Image | Ports | Notes |
| :--- | :--- | :--- | :--- |
| `minio` | `minio/minio` | 9000 (S3), 9001 (console) | volume-backed; root creds only in `.env` |
| `minio-init` | tuner base image | — | runs `scripts/bootstrap_minio.py` once: creates the six buckets and per-stage credentials (§5), then exits |
| `mlflow` | `ghcr.io/mlflow/mlflow` | 5000 | backend store: sqlite on a volume; artifact store: `s3://tuner-mlflow` on MinIO, **proxied** (`--serve-artifacts`) so stage clients need no artifact-bucket credentials |
| `mock-judge` | own image (`docker/mock-judge.Dockerfile`) | 8088 | `profiles: [e2e]` (T14) — test infrastructure only ([06-testing.md §4](06-testing.md)), never a real deployment's judge endpoint; used by the E2E steel thread and CI |
| `ingestor`, `cleaner`, `judge`, `tokenizer` | per-stage images (§2) | — | `profiles: [pipeline]` — run on demand via `tuner run` or `docker compose run`, not resident |
| `trainer`, `smoke` | trainer image | — | `deploy.resources.reservations.devices` for GPU (nvidia runtime) |

`.env` (git-ignored; `.env.example` committed) holds: MinIO root creds, per-stage keypairs, `TUNER_JUDGE_BASE_URL`, `TUNER_JUDGE_API_KEY`, `HF_TOKEN`. **No credential ever appears in compose files, configs, or code** (SAS §4.2).

A real judge's LLM endpoint is not part of the compose topology: point `TUNER_JUDGE_BASE_URL` at whatever OpenAI-compatible server the team has (host-side Ollama at `http://host.docker.internal:11434`, a lab vLLM box, or a cloud compat endpoint). The `mock-judge` service (above) is the one exception, and only for tests/E2E/CI — never a real environment.

**Bare origin, no `/v1` suffix (T15 finding):** `tuner.judge.client.score_record` always posts to the fixed relative path `/v1/chat/completions`, so `TUNER_JUDGE_BASE_URL` must be the server's bare origin — every test and CI config (`tests/integration/test_judge.py`, `tests/e2e/test_steel_thread.py`, `scripts/write_ci_env.sh`) already sets it that way. A base URL that itself ends in `/v1` (Ollama's own documented convention, and this doc's own earlier example) makes every judge call 404, since `httpx`'s base-URL join treats a leading-`/` request path as absolute and discards the base's own path component entirely. Confirmed empirically against the real `mock-judge` compose service during T15's GPU pipeline run.

## 2. Container images

- `docker/base.Dockerfile`: `python:3.11-slim`, non-root user `tuner` (uid 1000), uv-installed dependencies, `src/tuner` installed. All stage images derive from it (SAS §4.2 hardening).
- CPU stages (`ingestor`, `cleaner`, `judge`, `tokenizer`) share one image, entrypoint `tuner`.
- `docker/trainer.Dockerfile`: CUDA runtime base (`nvidia/cuda:*-runtime`), same non-root pattern, adds torch/transformers/peft/bitsandbytes. Used by `trainer` and `smoke`. **T15 finding (`INF-S-020`):** the `ubuntu24.04`-tagged CUDA runtime base ships its own default non-root user already sitting on uid/gid 1000 (Canonical's own Ubuntu 24.04 base-image policy), so a plain `useradd --uid 1000 tuner` fails outright (`UID 1000 is not unique`) against this specific base — this build had never actually been run for real before T15. Fixed by removing that user first (`userdel -r ubuntu`) so `tuner` can claim uid/gid 1000 in its place; still exactly one non-root uid-1000 user in the final image.
- `docker/mock-judge.Dockerfile` (T14): same non-root pattern, `dev` extra (fastapi/uvicorn) instead of `train`, copies `tests/mock_judge/` instead of `src/tuner` alone. Test infrastructure only — no product code.
- Images never bake data or credentials; everything arrives via env + object store.

## 3. GPU access & host-venv fallback

Trainer/smoke need the NVIDIA container toolkit. If Docker GPU passthrough is a blocker on the dev box, the sanctioned fallback is running **only those two stages** from a host uv venv (`uv sync --extra train`, then `tuner train ...` with the same env vars pointed at compose's MinIO/MLflow). The CLI contract makes this a zero-code-change substitution. Document actual choice in the run log; CI never uses GPUs ([06-testing.md §6](06-testing.md)).

## 4. Cloud topology (Phase 3) — K8s + Kubeflow

- **Storage:** MinIO → cloud object store (S3 / Azure Blob via S3-compat gateway / GCS via interop). Only `TUNER_S3_ENDPOINT` and credentials change.
- **Secrets:** `.env` → **K8s Secrets** mounted as env vars, one Secret per stage matching the §5 scopes (SAS §4.2). Sealed-secrets/external-secrets operator choice is a platform decision at migration time.
- **Orchestration:** each stage CLI is wrapped as a KFP v2 container component (thin `@container_component` wrappers in a new `kfp/` dir passing run-id/config args). The DAG replicates the `tuner run` order; the run ID becomes a pipeline parameter. Retries/caching per-step become KFP-native. The local driver remains for dev.
- **MLflow:** moves to a shared tracked deployment (postgres backend, cloud artifact store); `MLFLOW_TRACKING_URI` is the only client-side change.
- **Serving:** Inference Engine deployment per [03-components/inference.md](03-components/inference.md).

## 5. IAM matrix (SAS §4.2)

Enforced locally with per-stage MinIO users + policies (created by `bootstrap_minio.py`), and in cloud with per-stage IAM roles carrying the same statements.

Legend — `R` = `s3:ListBucket` + `s3:GetObject`; `W` = `R` **plus** `s3:PutObject` + `s3:DeleteObject` (a producer must list, read back, and delete its own tier for the idempotent rewrite contract, so W always implies R on the same bucket). Enforcement is **bucket-level** (per the SAS); confinement to the *own run prefix* is the code-level idempotency contract ([01 §5.3](01-architecture.md)), not policy — run IDs are dynamic and can't be enumerated in static policy.

| Principal | bronze | silver | gold | artifacts | registry | assets⁴ | mlflow bkt |
| :--- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| ingestor | W | — | — | — | — | W | — |
| cleaner | R | W | — | — | — | R | — |
| judge | — | R | W | — | — | R | — |
| tokenizer | — | — | R | W | — | R | — |
| trainer | — | — | —¹ | RW | W | — | — |
| smoke | — | — | R² | RW | — | — | — |
| registry-ops | — | — | — | R | RW | — | — |
| inference (P3) | — | — | — | R | R | — | — |
| mlflow server | — | — | — | — | — | — | RW |

¹ trainer needs no Gold access: tensors suffice, and the Gold-manifest URI it logs is copied from `index_map.json` — the grant stays omitted until a real need appears. ² smoke reads Gold to fetch prompt text. ⁴ Phase 4 bucket.

**Mechanics:** `bootstrap_minio.py` uses the `minio` Python package — `Minio` client for bucket creation, `MinioAdmin` for users, canned policies, and policy attachment (one user + one policy per principal, named `tuner-<stage>`). Each policy is AWS-syntax JSON generated from a single table literal in the script that mirrors the matrix above. Shape, using cleaner (R bronze, W silver) as the template:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetObject"],
     "Resource": ["arn:aws:s3:::tuner-bronze", "arn:aws:s3:::tuner-bronze/*"]},
    {"Effect": "Allow",
     "Action": ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
     "Resource": ["arn:aws:s3:::tuner-silver", "arn:aws:s3:::tuner-silver/*"]}
  ]
}
```

(Bucket-level actions like `s3:ListBucket` attach to the bucket ARN; object-level actions to `bucket/*` — both lines are needed.) The deny-by-default shape is the point — e.g. **Ingestor cannot write Gold; Trainer cannot read Bronze** (SAS §4.2 verbatim); INF-I-003 sweeps every cell in both directions.

## 6. Security posture summary

- **Secrets:** env-only, per-stage scoped, never logged; `.env` git-ignored; K8s Secrets in cloud.
- **Containers:** non-root, slim/CUDA-runtime bases, no package managers invoked at runtime.
- **Weights & tensors:** SafeTensors end-to-end; `torch.load`/pickle is banned repo-wide (enforced by a ruff custom rule / grep check in CI).
- **Network:** MVP has no ingress at all; only MinIO/MLflow consoles bound to localhost.

**MVP scope:** §1–§3, §5 (MinIO policies), §6. §4 is Phase 3.
