"""Integration tests for scripts/bootstrap_minio.py, compose MinIO/MLflow, and
StorageClient failure behavior (INF suite, docs/08-test-specs/infra.md).
Static checks (INF-U-006/007) live in tests/unit/test_infra_static.py instead.
Needs compose `minio`, `minio-init`, `mlflow` up; env vars for MinIO root
admin creds and every per-stage keypair (see .env.example), exported into the
shell before running `pytest -m integration` — same convention as
tests/integration/test_storage.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
import mlflow
import pytest
import yaml
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError
from scripts.bootstrap_minio import BUCKETS, IAM_MATRIX, _admin_client, _env_prefix
from scripts.bootstrap_minio import main as bootstrap_main

from tuner.core.ids import canonical_hash, new_record_id
from tuner.core.storage import StorageClient

REPO_ROOT = Path(__file__).parents[2]


def _root_s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["TUNER_S3_ENDPOINT"],
        aws_access_key_id=os.environ["MINIO_ROOT_USER"],
        aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"],
        region_name=os.environ.get("TUNER_S3_REGION", "us-east-1"),
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _principal_creds(principal: str) -> tuple[str, str]:
    prefix = _env_prefix(principal)
    return os.environ[f"{prefix}_S3_ACCESS_KEY"], os.environ[f"{prefix}_S3_SECRET_KEY"]


def _principal_s3(principal: str):
    access_key, secret_key = _principal_creds(principal)
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


@pytest.mark.integration
def test_bootstrap_creates_buckets_and_principals():
    """INF-I-001: bootstrap_minio.py — all 7 buckets exist; every principal has its policy."""
    assert bootstrap_main() == 0

    s3_root = _root_s3()
    for bucket in BUCKETS:
        assert s3_root.head_bucket(Bucket=bucket)

    admin = _admin_client()
    users = json.loads(admin.user_list())
    for principal in IAM_MATRIX:
        access_key, _ = _principal_creds(principal)
        assert users[access_key]["policyName"] == f"tuner-{principal}"


@pytest.mark.integration
def test_bootstrap_rerun_is_idempotent():
    """INF-I-002: bootstrap re-run on an initialized store — exit 0, idempotent, contents kept."""
    assert bootstrap_main() == 0
    admin = _admin_client()
    tuner_policies_before = {
        name for name in json.loads(admin.policy_list()) if name.startswith("tuner-")
    }

    s3_root = _root_s3()
    s3_root.put_object(Bucket="tuner-bronze", Key="__inf_i_002_probe__", Body=b"untouched")
    try:
        assert bootstrap_main() == 0

        tuner_policies_after = {
            name for name in json.loads(admin.policy_list()) if name.startswith("tuner-")
        }
        assert tuner_policies_after == tuner_policies_before == {f"tuner-{p}" for p in IAM_MATRIX}
        body = s3_root.get_object(Bucket="tuner-bronze", Key="__inf_i_002_probe__")["Body"].read()
        assert body == b"untouched"
    finally:
        s3_root.delete_object(Bucket="tuner-bronze", Key="__inf_i_002_probe__")


# Independent transcription of 05-infrastructure.md §5's IAM matrix (deliberately
# NOT imported from scripts.bootstrap_minio.IAM_MATRIX) — a drift between the doc
# table and the generated policies must fail *this* table, per 08 infra.md's note
# on INF-I-003. "RW" cells collapse to "W" per the doc's own legend (W implies R).
EXPECTED_MATRIX: dict[str, dict[str, str]] = {
    "ingestor": {"tuner-bronze": "W", "tuner-assets": "W"},
    "cleaner": {"tuner-bronze": "R", "tuner-silver": "W", "tuner-assets": "R"},
    "judge": {"tuner-silver": "R", "tuner-gold": "W", "tuner-assets": "R"},
    "tokenizer": {"tuner-gold": "R", "tuner-artifacts": "W", "tuner-assets": "R"},
    "trainer": {"tuner-artifacts": "W", "tuner-registry": "W"},
    "smoke": {"tuner-gold": "R", "tuner-artifacts": "W"},
    "registry-ops": {"tuner-artifacts": "R", "tuner-registry": "W"},
    "mlflow": {"tuner-mlflow": "W"},
}

_MATRIX_CASES = [(principal, bucket) for principal in EXPECTED_MATRIX for bucket in BUCKETS]


@pytest.mark.integration
@pytest.mark.parametrize(
    "principal, bucket", _MATRIX_CASES, ids=[f"{p}-{b}" for p, b in _MATRIX_CASES]
)
def test_iam_matrix_sweep(principal, bucket):
    """INF-I-003: full IAM matrix sweep — every granted op allowed, every ungranted op denied."""
    level = EXPECTED_MATRIX[principal].get(bucket)
    client = _principal_s3(principal)
    probe_key = "__inf_i_003_probe__"

    read_allowed = not _denied(lambda: client.list_objects_v2(Bucket=bucket, MaxKeys=1))
    assert read_allowed == (level in ("R", "W")), f"{principal} read on {bucket}"

    write_allowed = not _denied(lambda: client.put_object(Bucket=bucket, Key=probe_key, Body=b"x"))
    assert write_allowed == (level == "W"), f"{principal} write on {bucket}"
    if write_allowed:
        client.delete_object(Bucket=bucket, Key=probe_key)


@pytest.mark.integration
def test_mlflow_round_trip_and_artifact_bucket_isolated():
    """INF-I-004: MLflow round-trip (real compose server); tuner-mlflow denied to a stage cred."""
    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    with mlflow.start_run(run_name="inf-i-004-probe") as run:
        mlflow.log_param("p", "1")
        mlflow.log_metric("m", 0.5)
        probe = REPO_ROOT / "tests" / "integration" / "__inf_i_004_probe__.txt"
        probe.write_text("hello")
        try:
            mlflow.log_artifact(str(probe))
        finally:
            probe.unlink()

    fetched = mlflow.get_run(run.info.run_id)
    assert fetched.data.params == {"p": "1"}
    assert fetched.data.metrics == {"m": 0.5}

    s3_root = _root_s3()
    key = f"0/{run.info.run_id}/artifacts/__inf_i_004_probe__.txt"
    body = s3_root.get_object(Bucket="tuner-mlflow", Key=key)["Body"].read()
    assert body == b"hello"

    # A stage credential (cleaner has no mlflow-bucket grant) must be denied direct access.
    cleaner = _principal_s3("cleaner")
    assert _denied(lambda: cleaner.list_objects_v2(Bucket="tuner-mlflow"))


@pytest.mark.integration
def test_storage_client_unreachable_endpoint_fails_fast(monkeypatch):
    """INF-I-005: StorageClient against a closed port fails fast, bounded, no hang.

    Scoped to StorageClient at T04 (see the footnote on this case in
    08 infra.md) — no stage CLI does real storage I/O until T06's Ingestor;
    the CLI-level companion case for INF-I-005 lands there.
    """
    monkeypatch.setenv("TUNER_S3_ENDPOINT", "http://localhost:1")
    monkeypatch.setenv("TUNER_S3_ACCESS_KEY", "unreachable")
    monkeypatch.setenv("TUNER_S3_SECRET_KEY", "unreachable")
    client = StorageClient()

    start = time.monotonic()
    with pytest.raises(Exception, match=r"(?i)connect"):
        list(client.read_jsonl("tuner-bronze", "whatever/"))
    elapsed = time.monotonic() - start

    assert elapsed < 30, f"took {elapsed:.1f}s — retries/timeout not bounded"


@pytest.mark.integration
def test_ingest_cli_against_unreachable_store_fails_fast(monkeypatch, run_id, tmp_path, capsys):
    """INF-I-005 (CLI companion, deferred from T04 — see the footnote on this case in
    08 infra.md): a real `tuner ingest` against an unreachable object store exits 1 with a
    connection-error message and no partial manifest (nothing ever reaches the real store —
    every I/O in this run targets the unreachable endpoint, so there is nothing to write)."""
    csv_path = tmp_path / "dialogs.csv"
    csv_path.write_text("question,answer\nq1,a1\n")
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(
        json.dumps(
            {
                "model": {"adapter": "gemma-e4b"},
                "ingest": {"sources": [{"type": "csv", "uri": str(csv_path)}]},
            }
        )
    )
    monkeypatch.setenv("TUNER_S3_ENDPOINT", "http://localhost:1")
    monkeypatch.setenv("TUNER_S3_ACCESS_KEY", "unreachable")
    monkeypatch.setenv("TUNER_S3_SECRET_KEY", "unreachable")

    from tuner.ingestor.cli import ingest

    start = time.monotonic()
    exit_code = ingest(run_id, str(config_path))
    elapsed = time.monotonic() - start

    assert exit_code == 1
    assert elapsed < 30, f"took {elapsed:.1f}s — retries/timeout not bounded"
    assert "connect" in capsys.readouterr().err.lower()


@pytest.mark.integration
def test_offline_hf_mode_with_preseeded_cache(storage, run_id, tmp_path):
    """INF-I-012: with HF_HUB_OFFLINE=1 and the pre-seeded tiny-test cache, a real
    tokenize() run (representative of the TOK integration suite -- the property this
    case specs is CI's own cache-seed step + offline env, not one specific stage)
    passes with zero network calls.

    Runs `tuner tokenize` as a subprocess with the env set from process start, not
    `monkeypatch.setenv` mid-test: huggingface_hub/transformers resolve their cache
    location from `HF_HOME` when the module is first imported, so changing the env
    var after either is already imported in this pytest process has no effect --
    confirmed empirically (an in-process negative control loaded from an "empty"
    cache anyway; the same negative control run as a subprocess correctly raised
    `OSError` naming the offline mode)."""
    from scripts.seed_hf_cache import seed

    hf_home = tmp_path / "hf-home"
    # Same seeding logic CI's own cache-seed step uses (scripts/seed_hf_cache.py) --
    # an isolated cache_dir here, so this proves "pre-seed then run offline" from a
    # genuine cold start rather than relying on the runner's ambient cache.
    seed(str(hf_home))

    record = {
        "id": new_record_id(),
        "run_id": run_id,
        "lineage": {"bronze_content_hash": f"sha256:{'0' * 64}", "cleaner_version": "0.1.0"},
        "conversation": [
            {"role": "user", "content": [{"type": "text", "value": "Q"}]},
            {"role": "assistant", "content": [{"type": "text", "value": "A"}]},
        ],
        "evaluation": {
            "score": 0.9,
            "judge_model": "mock-judge",
            "reasoning": "fine",
            "evaluated_at": "2026-07-20T14:31:10Z",
        },
    }
    storage.write_jsonl("tuner-gold", f"{run_id}/records-00000.jsonl", [record])
    storage.write_json(
        "tuner-gold",
        f"{run_id}/manifest.json",
        {
            "tier": "gold",
            "run_id": run_id,
            "created_at": "2026-07-20T14:31:10Z",
            "producer": {"stage": "judge", "version": "0.1.0"},
            "input": {
                "tier": "silver",
                "manifest_uri": f"s3://tuner-silver/{run_id}/manifest.json",
            },
            "files": ["records-00000.jsonl"],
            "records_hash": canonical_hash([record]),
            "counts": {"read": 1, "written": 1, "dropped": 0},
            "drops": [],
        },
    )
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(
        yaml.safe_dump({"model": {"adapter": "tiny-test"}, "ingest": {"sources": []}})
    )

    env = {**os.environ, "HF_HOME": str(hf_home), "HF_HUB_OFFLINE": "1"}
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tuner",
                "tokenize",
                "--run-id",
                run_id,
                "--config",
                str(config_path),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert storage.read_json("tuner-artifacts", f"{run_id}/tokens/index_map.json") is not None
    finally:
        storage.delete_prefix("tuner-gold", f"{run_id}/")
        storage.delete_prefix("tuner-artifacts", f"{run_id}/")
