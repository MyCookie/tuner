# MLflow tracking server with S3/MinIO artifact support (#46).
# ghcr.io/mlflow/mlflow images do not bundle boto3; without it the server raises
# ModuleNotFoundError on first artifact upload (s3_artifact_repo._cached_get_s3_client).
# We layer boto3 on top of a pinned, immutable upstream release so the nightly
# cannot drift again. Bump the base tag intentionally and re-run e2e before merging.
# boto3 is pinned exactly and bumped by hand (spec 09 section 8).
FROM ghcr.io/mlflow/mlflow:v3.16.1@sha256:06058cc872276873e9759c635e773342aca4731e240f6d20d88ea43774083984
RUN pip install --no-cache-dir boto3==1.44.4
