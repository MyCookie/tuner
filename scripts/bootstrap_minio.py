"""Create the seven canonical buckets and per-stage MinIO users/policies.

Run once by the `minio-init` compose service (05-infrastructure.md §1, §5).
Idempotent: safe to re-run against an already-bootstrapped store (INF-I-002).

Uses the `minio` package directly (Minio for buckets, MinioAdmin for IAM) —
this is infra tooling, not a pipeline stage, so CLAUDE.md hard rule 1 (object
storage only via StorageClient) doesn't apply here; 05 §5 "Mechanics"
specifies this exact approach.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit

from minio import Minio
from minio.credentials import StaticProvider
from minio.error import MinioAdminException
from minio.minioadmin import MinioAdmin

# Bucket list (01-architecture.md §4.1): the six pipeline tiers + the MLflow
# artifact-store bucket. tuner-assets is reserved for Phase 4 but created now
# per INF-I-001 ("all 7 buckets exist").
BUCKETS = (
    "tuner-bronze",
    "tuner-silver",
    "tuner-gold",
    "tuner-artifacts",
    "tuner-registry",
    "tuner-assets",
    "tuner-mlflow",
)

# The IAM matrix (05-infrastructure.md §5), transcribed once here and used to
# generate policies. R = ListBucket+GetObject; W = R + PutObject+DeleteObject
# (W always implies R on the same bucket per the legend). The doc table spells
# some W cells "RW" for emphasis (e.g. trainer/artifacts) — same grant, W is
# the only other symbol besides R so both collapse to "W" here.
# Keyed by principal name -> {bucket: "R"|"W"}. Inference (Phase 3) is out of
# MVP scope and intentionally has no principal here.
IAM_MATRIX: dict[str, dict[str, str]] = {
    "ingestor": {"tuner-bronze": "W", "tuner-assets": "W"},
    "cleaner": {"tuner-bronze": "R", "tuner-silver": "W", "tuner-assets": "R"},
    "judge": {"tuner-silver": "R", "tuner-gold": "W", "tuner-assets": "R"},
    "tokenizer": {"tuner-gold": "R", "tuner-artifacts": "W", "tuner-assets": "R"},
    "trainer": {"tuner-artifacts": "W", "tuner-registry": "W"},
    "smoke": {"tuner-gold": "R", "tuner-artifacts": "W"},
    "registry-ops": {"tuner-artifacts": "R", "tuner-registry": "W"},
    "mlflow": {"tuner-mlflow": "W"},
}

_READ_ACTIONS = ["s3:ListBucket", "s3:GetObject"]
_WRITE_ACTIONS = [*_READ_ACTIONS, "s3:PutObject", "s3:DeleteObject"]


def _env_prefix(principal: str) -> str:
    """`registry-ops` -> `REGISTRY_OPS`, the .env.example var-name prefix."""
    return principal.upper().replace("-", "_")


def _policy_document(grants: dict[str, str]) -> dict:
    """Build the AWS-syntax policy JSON for one principal's grants (05 §5 shape)."""
    statements = []
    for bucket, level in sorted(grants.items()):
        actions = _WRITE_ACTIONS if level == "W" else _READ_ACTIONS
        statements.append(
            {
                "Effect": "Allow",
                "Action": actions,
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _admin_client() -> MinioAdmin:
    endpoint = os.environ["TUNER_S3_ENDPOINT"]
    parts = urlsplit(endpoint)
    root_user = os.environ["MINIO_ROOT_USER"]
    root_password = os.environ["MINIO_ROOT_PASSWORD"]
    return MinioAdmin(
        endpoint=parts.netloc or parts.path,
        credentials=StaticProvider(root_user, root_password),
        secure=parts.scheme == "https",
        cert_check=False,
    )


def _s3_client() -> Minio:
    endpoint = os.environ["TUNER_S3_ENDPOINT"]
    parts = urlsplit(endpoint)
    root_user = os.environ["MINIO_ROOT_USER"]
    root_password = os.environ["MINIO_ROOT_PASSWORD"]
    return Minio(
        parts.netloc or parts.path,
        access_key=root_user,
        secret_key=root_password,
        secure=parts.scheme == "https",
        cert_check=False,
    )


def _user_exists(admin: MinioAdmin, access_key: str) -> bool:
    try:
        admin.user_info(access_key)
    except MinioAdminException as exc:
        if "does not exist" in str(exc) or "NoSuchUser" in str(exc):
            return False
        raise
    return True


def ensure_buckets(s3: Minio) -> None:
    for bucket in BUCKETS:
        if not s3.bucket_exists(bucket):
            s3.make_bucket(bucket)
            print(f"bucket created: {bucket}")
        else:
            print(f"bucket exists: {bucket}")


def ensure_principal(admin: MinioAdmin, principal: str, grants: dict[str, str]) -> None:
    prefix = _env_prefix(principal)
    access_key = os.environ[f"{prefix}_S3_ACCESS_KEY"]
    secret_key = os.environ[f"{prefix}_S3_SECRET_KEY"]

    if _user_exists(admin, access_key):
        print(f"user exists: {principal} ({access_key})")
    else:
        admin.user_add(access_key, secret_key)
        print(f"user created: {principal} ({access_key})")

    policy_name = f"tuner-{principal}"
    admin.policy_add(policy_name, policy=_policy_document(grants))
    admin.policy_set(policy_name, user=access_key)
    print(f"policy attached: {policy_name} -> {principal}")


def main() -> int:
    s3 = _s3_client()
    admin = _admin_client()

    ensure_buckets(s3)
    for principal, grants in IAM_MATRIX.items():
        ensure_principal(admin, principal, grants)

    print(json.dumps({"buckets": list(BUCKETS), "principals": list(IAM_MATRIX)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
