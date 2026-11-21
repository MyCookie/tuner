# MLflow tracking server with S3/MinIO artifact support (#46).
# ghcr.io/mlflow/mlflow images do not bundle boto3; without it the server raises
# ModuleNotFoundError on first artifact upload (s3_artifact_repo._cached_get_s3_client).
# We layer boto3 on top of a pinned, immutable upstream release so the nightly
# cannot drift again. Bump the base tag intentionally and re-run e2e before merging.
# The server tag must be no older than the mlflow client locked in uv.lock
# (spec 05 section 1, "MLflow client and server versions").
# boto3 is pinned exactly and bumped by hand (spec 09 section 8).
FROM ghcr.io/mlflow/mlflow:v3.18.0@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13
RUN pip install --no-cache-dir boto3==1.44.4
