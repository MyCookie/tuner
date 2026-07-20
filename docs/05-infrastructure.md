# EFTP Infrastructure & Security

Environment story from local dev (MVP) to cloud production (Phase 3). The invariant across both: stages are stateless containers configured entirely by the env vars in [01-architecture.md §4.3](01-architecture.md), talking to an S3-compatible store through `StorageClient` — so promotion to the cloud changes deployment artifacts, never pipeline code (SAS §4.1).

---

## 1. Local topology (MVP) — Docker Compose

`docker-compose.yaml` services:

| Service | Image | Ports | Notes |
| :--- | :--- | :--- | :--- |
| `minio` | `minio/minio` | 9000 (S3), 9001 (console) | volume-backed; root creds only in `.env` |
| `minio-init` | eftp base image | — | runs `scripts/bootstrap_minio.py` once: creates the six buckets and per-stage credentials (§5), then exits |
| `mlflow` | `ghcr.io/mlflow/mlflow` | 5000 | backend store: sqlite on a volume; artifact store: `s3://eftp-mlflow` on MinIO, **proxied** (`--serve-artifacts`) so stage clients need no artifact-bucket credentials |
| `ingestor`, `cleaner`, `judge`, `tokenizer` | per-stage images (§2) | — | `profiles: [pipeline]` — run on demand via `eftp run` or `docker compose run`, not resident |
| `trainer`, `smoke` | trainer image | — | `deploy.resources.reservations.devices` for GPU (nvidia runtime) |

`.env` (git-ignored; `.env.example` committed) holds: MinIO root creds, per-stage keypairs, `EFTP_JUDGE_BASE_URL`, `EFTP_JUDGE_API_KEY`, `HF_TOKEN`. **No credential ever appears in compose files, configs, or code** (SAS §4.2).

The judge's LLM endpoint is not part of the compose topology: point `EFTP_JUDGE_BASE_URL` at whatever OpenAI-compatible server the team has (host-side Ollama at `http://host.docker.internal:11434/v1`, a lab vLLM box, or a cloud compat endpoint).

## 2. Container images

- `docker/base.Dockerfile`: `python:3.11-slim`, non-root user `eftp` (uid 1000), uv-installed dependencies, `src/eftp` installed. All stage images derive from it (SAS §4.2 hardening).
- CPU stages (`ingestor`, `cleaner`, `judge`, `tokenizer`) share one image, entrypoint `eftp`.
- `docker/trainer.Dockerfile`: CUDA runtime base (`nvidia/cuda:*-runtime`), same non-root pattern, adds torch/transformers/peft/bitsandbytes. Used by `trainer` and `smoke`.
- Images never bake data or credentials; everything arrives via env + object store.

## 3. GPU access & host-venv fallback

Trainer/smoke need the NVIDIA container toolkit. If Docker GPU passthrough is a blocker on the dev box, the sanctioned fallback is running **only those two stages** from a host uv venv (`uv sync --extra train`, then `eftp train ...` with the same env vars pointed at compose's MinIO/MLflow). The CLI contract makes this a zero-code-change substitution. Document actual choice in the run log; CI never uses GPUs ([06-testing.md §6](06-testing.md)).

## 4. Cloud topology (Phase 3) — K8s + Kubeflow

- **Storage:** MinIO → cloud object store (S3 / Azure Blob via S3-compat gateway / GCS via interop). Only `EFTP_S3_ENDPOINT` and credentials change.
- **Secrets:** `.env` → **K8s Secrets** mounted as env vars, one Secret per stage matching the §5 scopes (SAS §4.2). Sealed-secrets/external-secrets operator choice is a platform decision at migration time.
- **Orchestration:** each stage CLI is wrapped as a KFP v2 container component (thin `@container_component` wrappers in a new `kfp/` dir passing run-id/config args). The DAG replicates the `eftp run` order; the run ID becomes a pipeline parameter. Retries/caching per-step become KFP-native. The local driver remains for dev.
- **MLflow:** moves to a shared tracked deployment (postgres backend, cloud artifact store); `MLFLOW_TRACKING_URI` is the only client-side change.
- **Serving:** Inference Engine deployment per [03-components/inference.md](03-components/inference.md).

## 5. IAM matrix (SAS §4.2)

Enforced locally with per-stage MinIO users + bucket policies (created by `bootstrap_minio.py`), and in cloud with per-stage IAM roles carrying the same statements. `R` = read/list, `W` = write/delete under own run prefix.

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

Written as actual policy JSON in `scripts/bootstrap_minio.py`; the deny-by-default shape is the point — e.g. **Ingestor cannot write Gold; Trainer cannot read Bronze** (SAS §4.2 verbatim).

## 6. Security posture summary

- **Secrets:** env-only, per-stage scoped, never logged; `.env` git-ignored; K8s Secrets in cloud.
- **Containers:** non-root, slim/CUDA-runtime bases, no package managers invoked at runtime.
- **Weights & tensors:** SafeTensors end-to-end; `torch.load`/pickle is banned repo-wide (enforced by a ruff custom rule / grep check in CI).
- **Network:** MVP has no ingress at all; only MinIO/MLflow consoles bound to localhost.

**MVP scope:** §1–§3, §5 (MinIO policies), §6. §4 is Phase 3.
