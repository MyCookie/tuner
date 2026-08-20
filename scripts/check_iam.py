"""Print the full IAM matrix sweep result (05-infrastructure.md §5).

For every principal x bucket cell, probes actual MinIO behavior with that
principal's credentials and compares it against the expected grant. Exits 0
if every cell matches, 1 otherwise — a fast manual/CI-adjacent check
alongside the exhaustive pytest version (INF-I-003, tests/integration/test_infra.py).
"""

from __future__ import annotations

import os
import sys

import boto3
from bootstrap_minio import BUCKETS, IAM_MATRIX, _env_prefix
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

_PROBE_KEY = "__check_iam_probe__.txt"


def _client(access_key: str, secret_key: str):
    return boto3.client(
        "s3",
        endpoint_url=os.environ["TUNER_S3_ENDPOINT"],
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.environ.get("TUNER_S3_REGION", "us-east-1"),
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _denied(fn) -> bool:
    try:
        fn()
    except ClientError as exc:
        return exc.response.get("Error", {}).get("Code") == "AccessDenied"
    return False


def _probe_cell(client, bucket: str, level: str | None) -> list[str]:
    """Return a list of mismatch descriptions for this principal x bucket cell."""
    mismatches = []
    read_ok = not _denied(lambda: client.list_objects_v2(Bucket=bucket, MaxKeys=1))
    expect_read = level in ("R", "W")
    if read_ok != expect_read:
        mismatches.append(f"read: expected {'allow' if expect_read else 'deny'}, got other")

    write_ok = not _denied(lambda: client.put_object(Bucket=bucket, Key=_PROBE_KEY, Body=b"x"))
    expect_write = level == "W"
    if write_ok != expect_write:
        mismatches.append(f"write: expected {'allow' if expect_write else 'deny'}, got other")
    if write_ok:
        client.delete_object(Bucket=bucket, Key=_PROBE_KEY)

    return mismatches


def main() -> int:
    ok = True
    for principal, grants in IAM_MATRIX.items():
        prefix = _env_prefix(principal)
        client = _client(
            os.environ[f"{prefix}_S3_ACCESS_KEY"],
            os.environ[f"{prefix}_S3_SECRET_KEY"],
        )
        for bucket in BUCKETS:
            level = grants.get(bucket)
            mismatches = _probe_cell(client, bucket, level)
            status = "OK" if not mismatches else "FAIL"
            if mismatches:
                ok = False
            print(f"{status:4} {principal:14} {bucket:16} expected={level or '-':2} {mismatches}")

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
