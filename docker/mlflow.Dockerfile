# MLflow tracking server with S3/MinIO artifact support (#46).
# ghcr.io/mlflow/mlflow images do not bundle boto3; without it the server raises
# ModuleNotFoundError on first artifact upload (s3_artifact_repo._cached_get_s3_client).
# We layer boto3 on top of a pinned, immutable upstream release so the nightly
# cannot drift again. Bump the base tag intentionally and re-run e2e before merging.
FROM ghcr.io/mlflow/mlflow:v3.16.1
RUN pip install --no-cache-dir boto3
