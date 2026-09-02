#!/usr/bin/env bash
# Writes a working .env for CI's own disposable, single-run MinIO/MLflow/mock-judge
# stack (pr.yml and nightly.yml both call this, so the two workflows can't drift on
# what a working CI .env actually needs -- T14's own "one script" philosophy,
# extended to CI setup after the first version of this hardcoded the same throwaway
# values twice and only one copy got fixed).
#
# None of this is a real secret (CLAUDE.md hard rule 3 is about production
# credentials; nothing here outlives one ephemeral CI job) -- but MinIO itself
# still enforces real constraints on these values: every per-stage IAM user's
# access key must be *distinct* from MINIO_ROOT_USER (add-user 403s otherwise,
# "Credential is not allowed to be same as admin access key" -- caught by actually
# running this against real MinIO, not just reading the docs), and the root
# password needs its own minimum length. TUNER_S3_ACCESS_KEY/SECRET_KEY are the
# root pair, not a per-stage one -- the cross-tier `storage`/`run_id` test fixtures
# need broad access no single scoped principal has (.env.example's own comment).
set -euo pipefail

cd "$(dirname "$0")/.."

cat > .env <<'EOF'
MINIO_ROOT_USER=ci-minio-root
MINIO_ROOT_PASSWORD=ci-minio-root-password
INGESTOR_S3_ACCESS_KEY=ci-ingestor-key
INGESTOR_S3_SECRET_KEY=ci-ingestor-secret
CLEANER_S3_ACCESS_KEY=ci-cleaner-key
CLEANER_S3_SECRET_KEY=ci-cleaner-secret
JUDGE_S3_ACCESS_KEY=ci-judge-key
JUDGE_S3_SECRET_KEY=ci-judge-secret
TOKENIZER_S3_ACCESS_KEY=ci-tokenizer-key
TOKENIZER_S3_SECRET_KEY=ci-tokenizer-secret
TRAINER_S3_ACCESS_KEY=ci-trainer-key
TRAINER_S3_SECRET_KEY=ci-trainer-secret
SMOKE_S3_ACCESS_KEY=ci-smoke-key
SMOKE_S3_SECRET_KEY=ci-smoke-secret
REGISTRY_OPS_S3_ACCESS_KEY=ci-registry-ops-key
REGISTRY_OPS_S3_SECRET_KEY=ci-registry-ops-secret
MLFLOW_S3_ACCESS_KEY=ci-mlflow-key
MLFLOW_S3_SECRET_KEY=ci-mlflow-secret
TUNER_S3_ENDPOINT=http://localhost:9000
TUNER_S3_ACCESS_KEY=ci-minio-root
TUNER_S3_SECRET_KEY=ci-minio-root-password
TUNER_S3_REGION=us-east-1
MLFLOW_TRACKING_URI=http://localhost:5000
TUNER_JUDGE_BASE_URL=http://localhost:8088
TUNER_JUDGE_API_KEY=ci-unused-mock-key
HF_TOKEN=ci-unused-tiny-test-is-ungated
EOF

echo "write_ci_env: wrote .env for the disposable CI stack"
