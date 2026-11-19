# MLflow tracking server with S3/MinIO artifact support (#46).
# ghcr.io/mlflow/mlflow images do not bundle boto3; without it the server raises
# ModuleNotFoundError on first artifact upload (s3_artifact_repo._cached_get_s3_client).
# We layer boto3 on top of a pinned, immutable upstream release so the nightly
# cannot drift again. Bump the base tag intentionally and re-run e2e before merging.
# boto3 is pinned exactly and bumped by hand (spec 09 section 8).
FROM ghcr.io/mlflow/mlflow:v3.17.2@sha256:2cc40b3208991940d824d04f339833a0b69507569012aa5ec545690608602fc0
RUN pip install --no-cache-dir boto3==1.44.4
