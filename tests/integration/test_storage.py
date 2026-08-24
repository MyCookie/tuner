"""Tests for tuner.core.storage (CORE suite, docs/08-test-specs/core.md).

CORE-I-* need compose MinIO: `docker compose up -d minio minio-init` then
`uv run pytest -m integration tests/integration/test_storage.py`. CORE-U-046
is static (no services) and lives here since it's part of the same suite doc.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from tuner.core.config import ConfigError
from tuner.core.ids import new_run_id
from tuner.core.storage import StorageClient

BUCKET = "tuner-bronze"
REPO_ROOT = Path(__file__).parents[2]


def test_only_storage_imports_boto3():
    """CORE-U-046: only tuner/core/storage.py imports boto3 (CLAUDE.md hard rule 1)."""
    result = subprocess.run(
        ["grep", "-rl", "--include=*.py", "import boto3", "src/tuner"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    matches = [line for line in result.stdout.splitlines() if line]
    assert matches == ["src/tuner/core/storage.py"]


@pytest.mark.integration
def test_construct_requires_env_vars(monkeypatch):
    """CORE-I-045: constructing with env unset raises ConfigError; no default-chain fallback."""
    for var in ("TUNER_S3_ENDPOINT", "TUNER_S3_ACCESS_KEY", "TUNER_S3_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ConfigError):
        StorageClient()


@pytest.mark.integration
def test_jsonl_round_trip_single_shard(storage, run_id):
    """CORE-I-040: jsonl round-trip, single shard — records identical, order preserved."""
    records = [{"id": str(i), "text": f"row {i}"} for i in range(5)]
    try:
        storage.write_jsonl(BUCKET, f"{run_id}/records-00000.jsonl", records)
        # A sibling manifest.json in the same prefix must not be picked up as a shard.
        storage.write_json(BUCKET, f"{run_id}/manifest.json", {"run_id": run_id})

        result = list(storage.read_jsonl(BUCKET, f"{run_id}/"))

        assert result == records
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_jsonl_round_trip_across_shards(storage, run_id):
    """CORE-I-041: shard size 10 — records-00000/00001/... naming; iterator yields shard order."""
    records = [{"id": str(i)} for i in range(25)]
    shard_size = 10
    try:
        for shard_idx, start in enumerate(range(0, len(records), shard_size)):
            shard = records[start : start + shard_size]
            storage.write_jsonl(BUCKET, f"{run_id}/records-{shard_idx:05d}.jsonl", shard)

        result = list(storage.read_jsonl(BUCKET, f"{run_id}/"))

        assert result == records
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_json_round_trip(storage, run_id):
    """CORE-I-042: read_json/write_json round-trip — identical dict."""
    obj = {"tier": "bronze", "run_id": run_id, "counts": {"read": 3, "written": 3, "dropped": 0}}
    try:
        storage.write_json(BUCKET, f"{run_id}/manifest.json", obj)

        assert storage.read_json(BUCKET, f"{run_id}/manifest.json") == obj
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_json_read_missing_key_returns_none(storage, run_id):
    """CORE-I-042: read_json on an absent key returns None (the manifest commit-marker contract)."""
    assert storage.read_json(BUCKET, f"{run_id}/manifest.json") is None


@pytest.mark.integration
def test_json_read_other_client_error_propagates(storage):
    """CORE-I-042: read_json re-raises non-NoSuchKey errors (e.g. missing bucket) unchanged."""
    with pytest.raises(ClientError):
        storage.read_json("tuner-bucket-does-not-exist", "whatever.json")


@pytest.mark.integration
def test_bytes_round_trip(storage, run_id):
    """CORE-I-047: write_bytes/read_bytes round-trip — identical bytes, including a
    payload that isn't valid UTF-8 (SafeTensors shards are raw binary, T10)."""
    payload = b"\x00\x01\xffnot valid utf-8 \xfe\xfd"
    try:
        storage.write_bytes(BUCKET, f"{run_id}/tokens/train.safetensors", payload)

        assert storage.read_bytes(BUCKET, f"{run_id}/tokens/train.safetensors") == payload
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_bytes_read_missing_key_returns_none(storage, run_id):
    """CORE-I-047: read_bytes on an absent key returns None, matching read_json's contract."""
    assert storage.read_bytes(BUCKET, f"{run_id}/tokens/train.safetensors") is None


@pytest.mark.integration
def test_bytes_read_other_client_error_propagates(storage):
    """CORE-I-047: read_bytes re-raises non-NoSuchKey errors (e.g. missing bucket)
    unchanged, matching read_json's contract."""
    with pytest.raises(ClientError):
        storage.read_bytes("tuner-bucket-does-not-exist", "whatever.safetensors")


@pytest.mark.integration
def test_upload_download_dir_byte_identical(storage, run_id, tmp_path):
    """CORE-I-043: upload_dir/download_dir of a nested dir — byte-identical tree."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "adapter_model.safetensors").write_bytes(b"\x00\x01\x02alpha-weights")
    (src / "sub" / "config.json").write_text('{"k": "v"}')
    prefix = f"{run_id}/adapter"

    try:
        storage.upload_dir(BUCKET, prefix, src)

        dest = tmp_path / "dest"
        storage.download_dir(BUCKET, prefix, dest)

        downloaded = sorted(p.relative_to(dest) for p in dest.rglob("*") if p.is_file())
        expected = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file())
        assert downloaded == expected
        for rel in expected:
            assert (dest / rel).read_bytes() == (src / rel).read_bytes()
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_delete_prefix_leaves_sibling_run_untouched(storage, run_id):
    """CORE-I-044: delete_prefix removes only its own prefix; a sibling run's objects survive."""
    sibling_id = new_run_id()
    storage.write_json(BUCKET, f"{run_id}/manifest.json", {"run_id": run_id})
    storage.write_json(BUCKET, f"{sibling_id}/manifest.json", {"run_id": sibling_id})

    try:
        storage.delete_prefix(BUCKET, f"{run_id}/")

        assert storage.read_json(BUCKET, f"{run_id}/manifest.json") is None
        assert storage.read_json(BUCKET, f"{sibling_id}/manifest.json") == {"run_id": sibling_id}
    finally:
        storage.delete_prefix(BUCKET, f"{sibling_id}/")


@pytest.mark.integration
def test_delete_prefix_on_empty_prefix_is_a_noop(storage, run_id):
    """CORE-I-044: delete_prefix on an empty prefix does not raise (idempotency, 01 §5.3)."""
    storage.delete_prefix(BUCKET, f"{run_id}/")
