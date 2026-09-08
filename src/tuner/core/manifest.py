"""Tier-manifest read/write helpers (docs/spec/02-data-contracts.md §3).

`read_tier` is written against a minimal, duck-typed storage interface
(`StorageLike`) rather than importing `tuner.core.storage`, which doesn't
exist until T03 — the real `StorageClient` satisfies this interface without
any adapter.

`write_tier` was removed (#30): it was a dead abstraction (no production
caller) and was incomplete (no delete_prefix). Per-stage idempotency
(delete → write records → write manifest) is each stage CLI's responsibility.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any, Protocol

from tuner.core.schemas import TierManifest


class UpstreamIncomplete(Exception):
    """Upstream tier's manifest is missing — the commit-marker rule (02-data-contracts.md §3)."""


class StorageLike(Protocol):
    def read_json(self, bucket: str, key: str) -> dict[str, Any] | None: ...
    def write_json(self, bucket: str, key: str, obj: dict[str, Any]) -> None: ...
    def write_jsonl(self, bucket: str, key: str, records: Iterable[dict[str, Any]]) -> None: ...


def records_hash(shard_bytes: Iterable[bytes]) -> str:
    """sha256 over the concatenated bytes of files in listed order (02-data-contracts.md §3)."""
    digest = hashlib.sha256()
    for chunk in shard_bytes:
        digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def shard_bytes(records: list[dict[str, Any]]) -> bytes:
    """Mirrors StorageClient.write_jsonl's exact serialization (json.dumps + newline,
    joined, utf-8-encoded) so `records_hash` matches what's actually written without a
    read-back round trip. If that serialization ever changes, this must change with it."""
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    return body.encode("utf-8")


def read_tier(storage: StorageLike, bucket: str, run_id: str) -> TierManifest:
    """Read a tier's manifest; the manifest's absence means the upstream tier is incomplete."""
    raw = storage.read_json(bucket, f"{run_id}/manifest.json")
    if raw is None:
        raise UpstreamIncomplete(f"missing manifest: s3://{bucket}/{run_id}/manifest.json")
    return TierManifest.model_validate(raw)
