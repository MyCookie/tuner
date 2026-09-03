"""Integration tests for tuner.cleaner (CLN suite, docs/spec/08-test-specs/cleaner.md).

Needs compose MinIO up: `docker compose up -d minio minio-init` then
`uv run pytest -m integration tests/integration/test_cleaner.py`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
import pytest
import yaml
from botocore.client import Config as BotoConfig

from tuner.cleaner.cli import clean
from tuner.cleaner.patterns import EMAIL_RE, PHONE_RE
from tuner.core.ids import canonical_hash, new_record_id
from tuner.core.schemas import SilverGoldRecord
from tuner.core.storage import StorageClient
from tuner.ingestor.cli import ingest

BRONZE_BUCKET = "tuner-bronze"
SILVER_BUCKET = "tuner-silver"
REPO_ROOT = Path(__file__).parents[2]
EXPECTED_COUNTS = json.loads((REPO_ROOT / "fixtures" / "expected_counts.json").read_text())


def _write_config(tmp_path: Path, sources: list[dict]) -> Path:
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model": {"adapter": "gemma-e4b"},
                "ingest": {"sources": sources},
                "clean": {"min_chars": 20, "max_chars": 32000, "pii": ["email", "phone"]},
            }
        )
    )
    return path


def _two_source_config(tmp_path: Path) -> Path:
    return _write_config(
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


def _records(storage: StorageClient, bucket: str, run_id: str) -> list[dict]:
    return list(storage.read_jsonl(bucket, f"{run_id}/"))


def _manifest(storage: StorageClient, bucket: str, run_id: str) -> dict | None:
    return storage.read_json(bucket, f"{run_id}/manifest.json")


def _seed_bronze(storage: StorageClient, run_id: str, config_path: Path) -> None:
    assert ingest(run_id, str(config_path), storage=storage) == 0


def _cleaner_s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["TUNER_S3_ENDPOINT"],
        aws_access_key_id=os.environ["CLEANER_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["CLEANER_S3_SECRET_KEY"],
        region_name=os.environ.get("TUNER_S3_REGION", "us-east-1"),
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@pytest.mark.integration
def test_full_fixture_bronze_to_silver_drop_counts(storage, run_id, tmp_path):
    """CLN-I-030: full fixture Bronze -> Silver; per-reason drop counts equal
    expected_counts.json; read = written + dropped."""
    config_path = _two_source_config(tmp_path)
    try:
        _seed_bronze(storage, run_id, config_path)
        assert clean(run_id, str(config_path), storage=storage) == 0

        manifest = _manifest(storage, SILVER_BUCKET, run_id)
        expected = EXPECTED_COUNTS["clean"]["combined"]
        assert manifest["counts"] == {
            "read": expected["read"],
            "written": expected["written"],
            "dropped": expected["dropped"],
        }
        assert manifest["counts"]["read"] == (
            manifest["counts"]["written"] + manifest["counts"]["dropped"]
        )
        assert {d["reason"]: d["count"] for d in manifest["drops"]} == expected["drops"]
    finally:
        storage.delete_prefix(BRONZE_BUCKET, f"{run_id}/")
        storage.delete_prefix(SILVER_BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_output_records_valid_silver(storage, run_id, tmp_path):
    """CLN-I-031: output records are all valid Silver -- evaluation null,
    lineage.bronze_content_hash matches the source envelope, ids subset of Bronze ids."""
    config_path = _two_source_config(tmp_path)
    try:
        _seed_bronze(storage, run_id, config_path)
        assert clean(run_id, str(config_path), storage=storage) == 0

        bronze_by_id = {r["id"]: r for r in _records(storage, BRONZE_BUCKET, run_id)}
        silver_records = _records(storage, SILVER_BUCKET, run_id)
        assert silver_records

        for record in silver_records:
            SilverGoldRecord.model_validate(record)
            assert record["evaluation"] is None
            assert record["id"] in bronze_by_id
            bronze_hash = bronze_by_id[record["id"]]["content_hash"]
            assert record["lineage"]["bronze_content_hash"] == bronze_hash
    finally:
        storage.delete_prefix(BRONZE_BUCKET, f"{run_id}/")
        storage.delete_prefix(SILVER_BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_rerun_byte_identical_records(storage, run_id, tmp_path):
    """CLN-I-032: running the Cleaner twice on the same Bronze yields byte-identical
    records-*.jsonl (determinism)."""
    config_path = _two_source_config(tmp_path)
    client = _cleaner_s3()

    def shard_bytes(manifest: dict) -> dict[str, bytes]:
        return {
            filename: client.get_object(Bucket=SILVER_BUCKET, Key=f"{run_id}/{filename}")[
                "Body"
            ].read()
            for filename in manifest["files"]
        }

    try:
        _seed_bronze(storage, run_id, config_path)
        assert clean(run_id, str(config_path), storage=storage) == 0
        first_bytes = shard_bytes(_manifest(storage, SILVER_BUCKET, run_id))

        assert clean(run_id, str(config_path), storage=storage) == 0
        second_bytes = shard_bytes(_manifest(storage, SILVER_BUCKET, run_id))

        assert first_bytes == second_bytes
    finally:
        storage.delete_prefix(BRONZE_BUCKET, f"{run_id}/")
        storage.delete_prefix(SILVER_BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_pii_sweep_zero_matches(storage, run_id, tmp_path):
    """CLN-I-033: PII sweep -- regex scan of all output text finds zero email/phone matches."""
    config_path = _two_source_config(tmp_path)
    try:
        _seed_bronze(storage, run_id, config_path)
        assert clean(run_id, str(config_path), storage=storage) == 0

        for record in _records(storage, SILVER_BUCKET, run_id):
            for turn in record["conversation"]:
                for part in turn["content"]:
                    if part["type"] == "text":
                        assert EMAIL_RE.search(part["value"]) is None
                        assert PHONE_RE.search(part["value"]) is None
    finally:
        storage.delete_prefix(BRONZE_BUCKET, f"{run_id}/")
        storage.delete_prefix(SILVER_BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_invalid_bronze_record_exits_2(storage, run_id, tmp_path, capsys):
    """CLN-I-034: a seeded invalid Bronze record (schema-breaking) exits 2 naming the
    record id -- invalid input is an abort, not a drop."""
    bad_id = new_record_id()
    bad_record = {
        "id": bad_id,
        "run_id": run_id,
        "source": {
            "uri": "fixtures/support_dialogs.csv",
            "type": "csv",
            "locator": "row:1",
            "ingested_at": "2026-07-20T14:22:05Z",
            "ingestor_version": "0.1.0",
        },
        "content_hash": "not-a-valid-hash",  # schema-breaking: must be sha256:<64 hex>
        "raw": {"question": "hi", "answer": "fine"},
    }
    config_path = _two_source_config(tmp_path)
    try:
        storage.write_jsonl(BRONZE_BUCKET, f"{run_id}/records-00000.jsonl", [bad_record])
        storage.write_json(
            BRONZE_BUCKET,
            f"{run_id}/manifest.json",
            {
                "tier": "bronze",
                "run_id": run_id,
                "created_at": "2026-07-20T14:22:05Z",
                "producer": {"stage": "ingestor", "version": "0.1.0"},
                "input": None,
                "files": ["records-00000.jsonl"],
                "records_hash": canonical_hash([bad_record]),
                "counts": {"read": 1, "written": 1, "dropped": 0},
                "drops": [],
            },
        )

        exit_code = clean(run_id, str(config_path), storage=storage)

        assert exit_code == 2
        assert bad_id in capsys.readouterr().err
        assert _manifest(storage, SILVER_BUCKET, run_id) is None
    finally:
        storage.delete_prefix(BRONZE_BUCKET, f"{run_id}/")
        storage.delete_prefix(SILVER_BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_missing_bronze_manifest_exits_2(storage, run_id, tmp_path):
    """CLN-I-035: missing Bronze manifest -> exit 2 (upstream incomplete)."""
    config_path = _two_source_config(tmp_path)
    assert clean(run_id, str(config_path), storage=storage) == 2


@pytest.mark.integration
def test_all_records_dropped_exits_3(storage, run_id, tmp_path):
    """CLN-I-036: all records dropped (seed 3 too-short records only) -> exit 3, no manifest."""
    csv_path = tmp_path / "short.csv"
    csv_path.write_text("question,answer\nHi,Ok\nYo,No\nEh,Um\n")
    config_path = _write_config(
        tmp_path,
        [
            {
                "type": "csv",
                "uri": str(csv_path),
                "mapping": {"prompt_column": "question", "response_column": "answer"},
            }
        ],
    )
    try:
        _seed_bronze(storage, run_id, config_path)
        exit_code = clean(run_id, str(config_path), storage=storage)
        assert exit_code == 3
        assert _manifest(storage, SILVER_BUCKET, run_id) is None
    finally:
        storage.delete_prefix(BRONZE_BUCKET, f"{run_id}/")
        storage.delete_prefix(SILVER_BUCKET, f"{run_id}/")
