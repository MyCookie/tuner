"""Integration tests for tuner.ingestor (ING suite, docs/08-test-specs/ingestor.md).

Needs compose MinIO up: `docker compose up -d minio minio-init` then
`uv run pytest -m integration tests/integration/test_ingestor.py`.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import boto3
import pytest
import yaml
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

import tuner.ingestor.cli as ingestor_cli
from tuner.core.config import DEFAULT_CONFIG_PATH
from tuner.core.ids import canonical_hash
from tuner.core.schemas import BronzeRecord
from tuner.core.storage import StorageClient
from tuner.ingestor.cli import ingest

BUCKET = "tuner-bronze"


def _records(storage: StorageClient, run_id: str) -> list[dict]:
    return list(storage.read_jsonl(BUCKET, f"{run_id}/"))


def _manifest(storage: StorageClient, run_id: str) -> dict | None:
    return storage.read_json(BUCKET, f"{run_id}/manifest.json")


def _write_config(tmp_path: Path, sources: list[dict]) -> Path:
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump({"model": {"adapter": "gemma-e4b"}, "ingest": {"sources": sources}})
    )
    return path


@pytest.mark.integration
def test_ingest_support_dialogs_csv(storage, run_id):
    """ING-I-010: ingest fixtures/support_dialogs.csv -> 100 Bronze envelopes, all valid vs
    schema; manifest {read:100, written:100, dropped:0}, input: null."""
    try:
        assert ingest(run_id, str(DEFAULT_CONFIG_PATH), storage=storage) == 0

        records = _records(storage, run_id)
        assert len(records) == 100
        for record in records:
            BronzeRecord.model_validate(record)

        manifest = _manifest(storage, run_id)
        assert manifest["counts"] == {"read": 100, "written": 100, "dropped": 0}
        assert manifest["input"] is None

        # records_hash must match the sha256 of the bytes actually stored, not merely
        # of however _shard_bytes() reconstructed them to compute it in the first place.
        client = _ingestor_s3()
        digest = hashlib.sha256()
        for filename in manifest["files"]:
            body = client.get_object(Bucket=BUCKET, Key=f"{run_id}/{filename}")["Body"].read()
            digest.update(body)
        assert manifest["records_hash"] == f"sha256:{digest.hexdigest()}"
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_content_hash_spot_check(storage, run_id):
    """ING-I-011: content_hash spot-check -- recompute canonical-JSON sha256 for 5 records,
    matches stored hash."""
    try:
        assert ingest(run_id, str(DEFAULT_CONFIG_PATH), storage=storage) == 0

        for record in _records(storage, run_id)[:5]:
            assert canonical_hash(record["raw"]) == record["content_hash"]
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_rerun_same_run_id_is_idempotent(storage, run_id):
    """ING-I-012: re-run same run ID -> same count, no duplicate objects, fresh manifest
    (idempotency). Plants a stale shard between runs that only a real delete-prefix would
    remove, so a no-op'd delete-prefix would be caught here rather than passing silently."""
    try:
        assert ingest(run_id, str(DEFAULT_CONFIG_PATH), storage=storage) == 0
        first_count = len(_records(storage, run_id))

        storage.write_jsonl(BUCKET, f"{run_id}/records-00099.jsonl", [{"stale": True}])

        assert ingest(run_id, str(DEFAULT_CONFIG_PATH), storage=storage) == 0
        second_records = _records(storage, run_id)
        second_manifest = _manifest(storage, run_id)

        assert first_count == len(second_records) == 100
        assert second_manifest["counts"] == {"read": 100, "written": 100, "dropped": 0}
        assert second_manifest["files"] == ["records-00000.jsonl"]
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_two_sources_merge_into_one_bronze_tier(storage, run_id, tmp_path):
    """ING-I-013: two sources (csv + extra_dialogs.jsonl) in one config -> one merged Bronze
    tier; per-record source.uri distinguishes; counts sum."""
    config_path = _write_config(
        tmp_path,
        [
            {
                "type": "csv",
                "uri": "fixtures/support_dialogs.csv",
                "mapping": {"prompt_column": "question", "response_column": "answer"},
            },
            {"type": "jsonl", "uri": "fixtures/extra_dialogs.jsonl"},
        ],
    )
    try:
        assert ingest(run_id, str(config_path), storage=storage) == 0

        records = _records(storage, run_id)
        assert len(records) == 120
        uris = {r["source"]["uri"] for r in records}
        assert uris == {"fixtures/support_dialogs.csv", "fixtures/extra_dialogs.jsonl"}

        manifest = _manifest(storage, run_id)
        assert manifest["counts"] == {"read": 120, "written": 120, "dropped": 0}
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_empty_source_exits_3_no_manifest(storage, run_id, tmp_path):
    """ING-I-014: source yielding zero records (empty CSV with header) -> exit 3, no manifest."""
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("question,answer\n")
    config_path = _write_config(tmp_path, [{"type": "csv", "uri": str(csv_path)}])

    try:
        assert ingest(run_id, str(config_path), storage=storage) == 3
        assert _manifest(storage, run_id) is None
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_unreadable_source_uri_exits_2_before_any_write(storage, run_id, tmp_path):
    """ING-I-015: unreadable source URI -> exit 2 before any object is written (prefix empty)."""
    config_path = _write_config(tmp_path, [{"type": "csv", "uri": str(tmp_path / "missing.csv")}])

    try:
        assert ingest(run_id, str(config_path), storage=storage) == 2
        assert _records(storage, run_id) == []
        assert _manifest(storage, run_id) is None
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_mid_write_failure_leaves_no_manifest(storage, run_id, tmp_path, monkeypatch):
    """ING-I-016: induced failure mid-write (monkeypatch shard write to raise on shard 2,
    shard size 10) -> no manifest.json in the prefix."""
    csv_path = tmp_path / "many.csv"
    csv_path.write_text("question,answer\n" + "\n".join(f"q{i},a{i}" for i in range(25)) + "\n")
    config_path = _write_config(tmp_path, [{"type": "csv", "uri": str(csv_path)}])

    real_write_shard = ingestor_cli._write_shard
    calls = {"n": 0}

    def _flaky_write_shard(storage_arg, run_id_arg, shard_index, records):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("induced failure on shard 2")
        return real_write_shard(storage_arg, run_id_arg, shard_index, records)

    monkeypatch.setattr(ingestor_cli, "_write_shard", _flaky_write_shard)

    try:
        assert ingest(run_id, str(config_path), storage=storage, shard_size=10) == 1
        assert _manifest(storage, run_id) is None
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_shard_boundary_25_records_shard_size_10(storage, run_id, tmp_path):
    """ING-I-017: shard boundary -- 25 records, shard size injected to 10 -> shards
    00000..00002 with 10/10/5; manifest files lists all three in order."""
    csv_path = tmp_path / "many.csv"
    csv_path.write_text("question,answer\n" + "\n".join(f"q{i},a{i}" for i in range(25)) + "\n")
    config_path = _write_config(tmp_path, [{"type": "csv", "uri": str(csv_path)}])

    try:
        assert ingest(run_id, str(config_path), storage=storage, shard_size=10) == 0

        manifest = _manifest(storage, run_id)
        assert manifest["files"] == [
            "records-00000.jsonl",
            "records-00001.jsonl",
            "records-00002.jsonl",
        ]
        for shard_name, expected_len in zip(manifest["files"], (10, 10, 5), strict=True):
            shard_records = list(storage.read_jsonl(BUCKET, f"{run_id}/{shard_name}"))
            assert len(shard_records) == expected_len
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


@pytest.mark.integration
@pytest.mark.parametrize(
    "line",
    [
        pytest.param('{"question": "bad, "answer": "oops"}\n', id="malformed-json-syntax"),
        pytest.param("[1, 2, 3]\n", id="valid-json-not-an-object"),
    ],
)
def test_bad_jsonl_line_exits_2_via_full_cli(storage, run_id, tmp_path, line):
    """Regression (PR #6 review round 1, findings 1 & 2): a JSONL line that either fails to
    parse or parses to something that isn't an object both exit 2 through the full `ingest()`
    pipeline (01-architecture.md §4.4 input-schema validation), with no manifest written.
    Closes the coverage gap ING-U-004's docstring incorrectly claimed was covered here, and
    the bug where the latter case fell through to exit 1 as an "unexpected" error instead."""
    jsonl_path = tmp_path / "bad.jsonl"
    jsonl_path.write_text(line)
    config_path = _write_config(tmp_path, [{"type": "jsonl", "uri": str(jsonl_path)}])

    try:
        assert ingest(run_id, str(config_path), storage=storage) == 2
        assert _manifest(storage, run_id) is None
    finally:
        storage.delete_prefix(BUCKET, f"{run_id}/")


def _ingestor_s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["TUNER_S3_ENDPOINT"],
        aws_access_key_id=os.environ["INGESTOR_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["INGESTOR_S3_SECRET_KEY"],
        region_name=os.environ.get("TUNER_S3_REGION", "us-east-1"),
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@pytest.mark.integration
def test_ingestor_credentials_denied_write_to_gold():
    """ING-I-018: IAM -- ingestor credentials attempt a write to tuner-gold -> denied by
    policy (AccessDenied), per 05 §5."""
    client = _ingestor_s3()

    with pytest.raises(ClientError) as exc_info:
        client.put_object(Bucket="tuner-gold", Key="__ing_i_018_probe__", Body=b"x")

    assert exc_info.value.response["Error"]["Code"] == "AccessDenied"
