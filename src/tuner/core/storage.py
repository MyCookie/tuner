"""StorageClient — the sole object-storage access path (01-architecture.md §5.1).

Wraps boto3 with endpoint/credentials read exclusively from the §4.3 env
vars. Credentials are mandatory — no boto3 default credential-chain fallback
to instance-profile/shared-credentials lookup, that's a config error. The
endpoint is optional: unset means "AWS default" endpoint resolution (§4.3),
which is how the same code targets MinIO locally and S3 in the cloud. Every
other module talks to object storage only through this class — CLAUDE.md
hard rule 1; CI greps for stray `import boto3` outside this file (CORE-U-046).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from tuner.core.config import ConfigError

_ENV_ENDPOINT = "TUNER_S3_ENDPOINT"
_ENV_ACCESS_KEY = "TUNER_S3_ACCESS_KEY"
_ENV_SECRET_KEY = "TUNER_S3_SECRET_KEY"
_ENV_REGION = "TUNER_S3_REGION"
_DEFAULT_REGION = "us-east-1"
_DELETE_BATCH_SIZE = 1000


class StorageClient:
    """The only path to object storage (01-architecture.md §5.1). Config is env-only."""

    def __init__(self) -> None:
        # TUNER_S3_ENDPOINT is optional: unset means "AWS default" endpoint
        # resolution (01-architecture.md §4.3). Credentials are never optional —
        # that's the no-default-credential-chain guarantee (CORE-I-045).
        endpoint = os.environ.get(_ENV_ENDPOINT)
        access_key = os.environ.get(_ENV_ACCESS_KEY)
        secret_key = os.environ.get(_ENV_SECRET_KEY)
        missing = [
            name
            for name, value in (
                (_ENV_ACCESS_KEY, access_key),
                (_ENV_SECRET_KEY, secret_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"missing required env var(s): {', '.join(missing)}")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=os.environ.get(_ENV_REGION, _DEFAULT_REGION),
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                # Bounded so an unreachable store fails fast, not hangs
                # (botocore's defaults are 60s/60s + broad retries) — INF-I-005.
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )

    # -- jsonl record shards --------------------------------------------------

    def write_jsonl(self, bucket: str, key: str, records: Iterable[dict[str, Any]]) -> None:
        """Write one shard: records as newline-delimited JSON at the given key."""
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        self._client.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"))

    def read_jsonl(self, bucket: str, prefix: str) -> Iterator[dict[str, Any]]:
        """Iterate every record across all `{prefix}records-*.jsonl` shards, in key order."""
        for key in self._list_keys(bucket, prefix):
            if not key.endswith(".jsonl"):
                continue
            body = self._client.get_object(Bucket=bucket, Key=key)["Body"].read()
            for line in body.decode("utf-8").splitlines():
                yield json.loads(line)

    # -- json objects ----------------------------------------------------------

    def read_json(self, bucket: str, key: str) -> dict[str, Any] | None:
        """Read a JSON object; None if absent (the manifest commit-marker contract)."""
        try:
            body = self._client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return json.loads(body.decode("utf-8"))

    def write_json(self, bucket: str, key: str, obj: dict[str, Any]) -> None:
        self._client.put_object(
            Bucket=bucket, Key=key, Body=json.dumps(obj, ensure_ascii=False).encode("utf-8")
        )

    # -- raw bytes (SafeTensors shards; anything that isn't jsonl/json) ---------

    def read_bytes(self, bucket: str, key: str) -> bytes | None:
        """Read a raw object; None if absent, matching `read_json`'s absent-key contract."""
        try:
            return self._client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise

    def write_bytes(self, bucket: str, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=bucket, Key=key, Body=data)

    # -- directories (adapter weights, transcripts) -----------------------------

    def upload_dir(self, bucket: str, prefix: str, local_dir: str | Path) -> None:
        """Upload every file under `local_dir`, preserving its relative layout under `prefix`."""
        local_dir = Path(local_dir)
        base = prefix.rstrip("/")
        for path in sorted(p for p in local_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(local_dir).as_posix()
            self._client.upload_file(str(path), bucket, f"{base}/{rel}")

    def download_dir(self, bucket: str, prefix: str, local_dir: str | Path) -> None:
        """Download every object under `prefix` into `local_dir`, mirroring its key layout."""
        local_dir = Path(local_dir)
        base = prefix if prefix.endswith("/") else f"{prefix}/"
        for key in self._list_keys(bucket, base):
            dest = local_dir / key[len(base) :]
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._client.download_file(bucket, key, str(dest))

    # -- prefix deletion (idempotent-rewrite contract) ---------------------------

    def delete_prefix(self, bucket: str, prefix: str) -> None:
        """Delete every object under `prefix`; a no-op if nothing matches (01 §5.3)."""
        keys = self._list_keys(bucket, prefix)
        for start in range(0, len(keys), _DELETE_BATCH_SIZE):
            chunk = keys[start : start + _DELETE_BATCH_SIZE]
            self._client.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk]}
            )

    # -- internal ------------------------------------------------------------

    def _list_keys(self, bucket: str, prefix: str) -> list[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
        return sorted(obj["Key"] for page in pages for obj in page.get("Contents", []))
