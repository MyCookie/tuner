"""Unit tests for tuner.core.manifest (CORE suite, docs/08-test-specs/core.md).

read_tier/write_tier are tested here against an in-memory fake satisfying
manifest.StorageLike, per core.md's "spy client" wording for CORE-I-031/032 —
the real StorageClient (T03) isn't needed since these functions only depend
on the minimal duck-typed interface.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tuner.core import manifest
from tuner.core.schemas import TierManifest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures_schemas"


class FakeStorage:
    """An in-memory spy satisfying manifest.StorageLike, recording call order."""

    def __init__(self) -> None:
        self._json: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str, str]] = []

    def read_json(self, bucket: str, key: str) -> dict | None:
        self.calls.append(("read_json", bucket, key))
        return self._json.get((bucket, key))

    def write_json(self, bucket: str, key: str, obj: dict) -> None:
        self.calls.append(("write_json", bucket, key))
        self._json[(bucket, key)] = obj

    def write_jsonl(self, bucket: str, key: str, records) -> None:
        self.calls.append(("write_jsonl", bucket, key))


def _load_manifest() -> TierManifest:
    raw = json.loads((FIXTURES_DIR / "tier_manifest.json").read_text())
    return TierManifest.model_validate(raw)


def test_records_hash_over_known_shards():
    """CORE-U-030: records_hash equals sha256 of the concatenated shard bytes in files order."""
    shard_a = b'{"id": "1"}\n'
    shard_b = b'{"id": "2"}\n'
    expected = f"sha256:{hashlib.sha256(shard_a + shard_b).hexdigest()}"

    assert manifest.records_hash([shard_a, shard_b]) == expected


def test_read_tier_raises_when_manifest_absent_but_shards_present():
    """CORE-I-031: read_tier raises UpstreamIncomplete when the manifest is absent."""
    storage = FakeStorage()
    # Record shards exist under the run prefix, but manifest.json was never written.
    storage.write_jsonl("tuner-silver", "run-20260720-142201-a3f9c2/records-00000.jsonl", [])

    with pytest.raises(manifest.UpstreamIncomplete):
        manifest.read_tier(storage, "tuner-silver", "run-20260720-142201-a3f9c2")


def test_read_tier_returns_validated_manifest():
    """CORE-I-031 (happy path): read_tier returns the validated manifest when present."""
    storage = FakeStorage()
    run_id = "run-20260720-142201-a3f9c2"
    manifest_dict = json.loads((FIXTURES_DIR / "tier_manifest.json").read_text())
    storage.write_json("tuner-silver", f"{run_id}/manifest.json", manifest_dict)

    result = manifest.read_tier(storage, "tuner-silver", run_id)

    assert result.model_dump(mode="json") == manifest_dict


def test_write_tier_writes_manifest_after_shards():
    """CORE-I-032: write_tier writes record shards, then the manifest, in that order."""
    storage = FakeStorage()
    run_id = "run-20260720-142201-a3f9c2"

    manifest.write_tier(storage, "tuner-silver", run_id, [{"id": "1"}], _load_manifest())

    call_kinds = [call[0] for call in storage.calls]
    assert call_kinds == ["write_jsonl", "write_json"]
