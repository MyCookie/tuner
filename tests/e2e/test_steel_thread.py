"""E2E steel-thread test (E2E suite, docs/spec/08-test-specs/e2e.md).

Needs compose `minio`, `minio-init`, `mlflow`, and `mock-judge` up
(`docker compose --profile e2e up -d minio minio-init mlflow mock-judge`), plus every
per-stage credential in the environment (same convention as
tests/integration/test_infra.py). Runs the full pipeline exactly once via a real
`tuner run` subprocess against `configs/pipeline.e2e.yaml` + the committed fixtures +
the mock-judge sidecar -- "one pipeline execution, then one assertion function per
case" (e2e.md's own file-level note). `tiny-test` + `method: full`, so this runs on
CPU or GPU without QLoRA/bitsandbytes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import boto3
import mlflow
import pytest
import yaml
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError
from scripts.bootstrap_minio import _env_prefix

from tuner.core.ids import RUN_ID_RE
from tuner.core.schemas import RegistryManifest
from tuner.core.storage import StorageClient
from tuner.registry_ops.cli import registry_list
from tuner.tokenizer.cli import tokenize
from tuner.trainer.cli import train

BRONZE_BUCKET = "tuner-bronze"
SILVER_BUCKET = "tuner-silver"
GOLD_BUCKET = "tuner-gold"
ARTIFACTS_BUCKET = "tuner-artifacts"
REGISTRY_BUCKET = "tuner-registry"
ADAPTER_NAME = "tiny-test"
CONFIG_PATH = str(Path(__file__).parents[2] / "configs" / "pipeline.e2e.yaml")

_EXPECTED_COUNTS = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "expected_counts.json").read_text()
)

# The suffixes an actual pickle-format weight/checkpoint file could carry (E2E-E-005),
# matching TRN-I-003/SMK-I-003's exact list -- deliberately spelled out only as bare
# extensions, never concatenated with a filename stem in one literal string (the
# gate's own pickle-ban grep scans test files too).
_PICKLE_SHAPED_SUFFIXES = (".bin", ".pkl", ".pickle", ".pt", ".pth")


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """RegistryManifest URIs are "s3://bucket/key-or-prefix" (02 §5.2) -- same helper
    TRN-I-004 uses, resolving the manifest's *own* field values, not a re-derivation."""
    assert uri.startswith("s3://"), f"not an s3:// URI: {uri!r}"
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


def _all_object_keys(storage, bucket: str, prefix: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        storage.download_dir(bucket, prefix, tmp)
        root = Path(tmp)
        return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]


def _principal_s3(principal: str):
    """A raw boto3 client under one stage's own restricted credentials -- the same
    pattern test_infra.py's own INF-I-003 sweep uses, duplicated here rather than
    imported (each suite file is self-contained, per this codebase's convention)."""
    prefix = _env_prefix(principal)
    return boto3.client(
        "s3",
        endpoint_url=os.environ["TUNER_S3_ENDPOINT"],
        aws_access_key_id=os.environ[f"{prefix}_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ[f"{prefix}_S3_SECRET_KEY"],
        region_name=os.environ.get("TUNER_S3_REGION", "us-east-1"),
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _denied(fn) -> bool:
    try:
        fn()
    except ClientError as exc:
        return exc.response.get("Error", {}).get("Code") == "AccessDenied"
    return False


def _cleanup(storage, run_id: str, model_version: str) -> None:
    for bucket in (BRONZE_BUCKET, SILVER_BUCKET, GOLD_BUCKET, ARTIFACTS_BUCKET):
        storage.delete_prefix(bucket, f"{run_id}/")
    storage.delete_prefix(REGISTRY_BUCKET, f"{model_version}/")


@pytest.fixture(scope="module")
def storage() -> StorageClient:
    """Module-scoped override of the conftest `storage` fixture (function-scoped
    there) -- `e2e_run` below is itself module-scoped (one pipeline execution shared
    across every E2E-E-* case), and pytest disallows a narrower-scoped fixture
    depending on a function-scoped one."""
    return StorageClient()


@pytest.fixture(scope="module")
def e2e_run(storage):
    """Runs `tuner run --config configs/pipeline.e2e.yaml` once, for real, as a
    subprocess (mirrors CLI-I-014's own technique) against the mock-judge sidecar.
    Every E2E-E-* test asserts something about this one completed run."""
    env = {
        **os.environ,
        "TUNER_JUDGE_BASE_URL": "http://localhost:8088",
        "TUNER_JUDGE_API_KEY": "unused-mock-key",
    }
    result = subprocess.run(
        [sys.executable, "-m", "tuner", "run", "--config", CONFIG_PATH],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    run_id = next(
        line.rsplit(maxsplit=1)[-1]
        for line in result.stdout.splitlines()
        if line.startswith("run: starting pipeline run")
    )
    assert RUN_ID_RE.match(run_id)
    model_version = f"{ADAPTER_NAME}-{run_id}"

    yield {
        "run_id": run_id,
        "model_version": model_version,
        "stdout": result.stdout,
        "returncode": result.returncode,
    }

    _cleanup(storage, run_id, model_version)


@pytest.mark.e2e
def test_e2e_e_001_exit_status(e2e_run):
    """E2E-E-001: tuner run returned 0 -- proven by the fixture's own assert, which
    would have failed the whole module already; this restates it as its own case."""
    assert e2e_run["returncode"] == 0


@pytest.mark.e2e
def test_e2e_e_002_lineage_chain(storage, e2e_run):
    """E2E-E-002: Gold manifest input.manifest_uri -> Silver -> Bronze all resolve;
    every tier's counts.read equals the upstream tier's counts.written."""
    run_id = e2e_run["run_id"]

    gold_manifest = storage.read_json(GOLD_BUCKET, f"{run_id}/manifest.json")
    assert gold_manifest is not None
    silver_bucket, silver_key = _parse_s3_uri(gold_manifest["input"]["manifest_uri"])
    silver_manifest = storage.read_json(silver_bucket, silver_key)
    assert silver_manifest is not None
    bronze_bucket, bronze_key = _parse_s3_uri(silver_manifest["input"]["manifest_uri"])
    bronze_manifest = storage.read_json(bronze_bucket, bronze_key)
    assert bronze_manifest is not None
    assert bronze_manifest["input"] is None  # Bronze's input is external (02 §3)

    assert silver_manifest["counts"]["read"] == bronze_manifest["counts"]["written"]
    assert gold_manifest["counts"]["read"] == silver_manifest["counts"]["written"]


@pytest.mark.e2e
def test_e2e_e_003_count_conservation(storage, e2e_run):
    """E2E-E-003: Bronze written = fixture ingestable count; each tier read = written +
    dropped; drop reasons match expected_counts.json + marker-derived judge
    expectations."""
    run_id = e2e_run["run_id"]

    bronze_manifest = storage.read_json(BRONZE_BUCKET, f"{run_id}/manifest.json")
    assert bronze_manifest["counts"]["written"] == _EXPECTED_COUNTS["ingest"]["combined"]["read"]
    assert bronze_manifest["counts"]["dropped"] == 0

    silver_manifest = storage.read_json(SILVER_BUCKET, f"{run_id}/manifest.json")
    expected_clean = _EXPECTED_COUNTS["clean"]["combined"]
    assert silver_manifest["counts"]["read"] == expected_clean["read"]
    assert silver_manifest["counts"]["written"] == expected_clean["written"]
    assert silver_manifest["counts"]["dropped"] == expected_clean["dropped"]
    silver_drops = {d["reason"]: d["count"] for d in silver_manifest["drops"]}
    assert silver_drops == expected_clean["drops"]

    gold_manifest = storage.read_json(GOLD_BUCKET, f"{run_id}/manifest.json")
    expected_judge = _EXPECTED_COUNTS["judge"]["combined"]
    assert gold_manifest["counts"]["read"] == expected_judge["read"]
    assert gold_manifest["counts"]["written"] == expected_judge["written"]
    assert gold_manifest["counts"]["dropped"] == expected_judge["dropped"]
    gold_drops = {d["reason"]: d["count"] for d in gold_manifest["drops"]}
    assert gold_drops == expected_judge["drops"]

    for manifest in (bronze_manifest, silver_manifest, gold_manifest):
        assert manifest["counts"]["read"] == (
            manifest["counts"]["written"] + manifest["counts"]["dropped"]
        )


@pytest.mark.e2e
def test_e2e_e_004_record_conservation(storage, e2e_run):
    """E2E-E-004: every Gold id appears in index_map splits or index_map.dropped --
    nothing silently lost between Gold and tensors."""
    run_id = e2e_run["run_id"]

    gold_ids = {r["id"] for r in storage.read_jsonl(GOLD_BUCKET, f"{run_id}/")}
    index_map = storage.read_json(ARTIFACTS_BUCKET, f"{run_id}/tokens/index_map.json")

    accounted_ids = {
        e["record_id"] for e in [*index_map["splits"]["train"], *index_map["splits"]["eval"]]
    }
    accounted_ids |= {d["record_id"] for d in index_map["dropped"]}

    assert gold_ids == accounted_ids


@pytest.mark.e2e
def test_e2e_e_005_artifacts(storage, e2e_run):
    """E2E-E-005: adapter/model dir present and HF-loadable; zero .bin/pickle objects
    under the run's artifacts prefix."""
    run_id = e2e_run["run_id"]

    keys = _all_object_keys(storage, ARTIFACTS_BUCKET, f"{run_id}/")
    assert keys
    for key in keys:
        assert not key.endswith(_PICKLE_SHAPED_SUFFIXES), key

    model_keys = [k for k in keys if k.startswith("model/")]
    assert any("safetensors" in k for k in model_keys)

    with tempfile.TemporaryDirectory() as tmp:
        storage.download_dir(ARTIFACTS_BUCKET, f"{run_id}/model/", tmp)
        from transformers import AutoModelForCausalLM

        model = AutoModelForCausalLM.from_pretrained(tmp)
        assert model is not None


@pytest.mark.e2e
def test_e2e_e_006_registry(storage, e2e_run, capsys):
    """E2E-E-006: exactly one manifest, status candidate, all URIs resolve, visible in
    `tuner registry list`."""
    run_id, model_version = e2e_run["run_id"], e2e_run["model_version"]

    registry_keys = _all_object_keys(storage, REGISTRY_BUCKET, f"{model_version}/")
    assert registry_keys == ["manifest.json"]

    manifest_raw = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
    manifest = RegistryManifest.model_validate(manifest_raw)
    assert manifest.status == "candidate"
    assert manifest.model_version == model_version
    assert manifest.run_id == run_id

    gold_bucket, gold_key = _parse_s3_uri(manifest.gold_manifest_uri)
    assert storage.read_json(gold_bucket, gold_key) is not None
    index_bucket, index_key = _parse_s3_uri(manifest.index_map_uri)
    assert storage.read_json(index_bucket, index_key) is not None
    weights_bucket, weights_prefix = _parse_s3_uri(manifest.weights_uri)
    assert _all_object_keys(storage, weights_bucket, weights_prefix)

    # registry_list's own exit code is always 0 by design (registry.md "must not die
    # on one bad object") -- it proves nothing here. The actual "visible in `tuner
    # registry list`" claim is the model_version appearing in its printed output
    # (PR #14 round 1 review finding 3).
    exit_code = registry_list(storage=storage)
    assert exit_code == 0
    assert model_version in capsys.readouterr().out


@pytest.mark.e2e
def test_e2e_e_007_mlflow(storage, e2e_run):
    """E2E-E-007: trainer run with params/metrics/tags per TRN-I-005; judge run per
    JDG-I-026; smoke transcript artifact attached to the trainer run."""
    run_id = e2e_run["run_id"]
    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])

    trainer_runs = mlflow.search_runs(
        search_all_experiments=True,
        filter_string=f"tags.`tuner.run_id` = '{run_id}' and tags.`tuner.stage` = 'trainer'",
        output_format="list",
    )
    assert len(trainer_runs) == 1
    trainer_run = trainer_runs[0]
    assert trainer_run.data.tags["tuner.adapter"] == ADAPTER_NAME
    assert "train_loss" in trainer_run.data.metrics or "loss" in trainer_run.data.metrics
    assert "gold_manifest_uri" in trainer_run.data.params

    from mlflow.tracking import MlflowClient

    trainer_artifacts = {
        f.path for f in MlflowClient().list_artifacts(trainer_run.info.run_id, "smoke")
    }
    assert "smoke/transcript.json" in trainer_artifacts

    judge_runs = mlflow.search_runs(
        search_all_experiments=True,
        filter_string=f"tags.`tuner.run_id` = '{run_id}' and tags.`tuner.stage` = 'judge'",
        output_format="list",
    )
    assert len(judge_runs) == 1
    judge_run = judge_runs[0]
    for metric in ("mean_score", "median_score", "promotion_rate", "judge_error_rate"):
        assert metric in judge_run.data.metrics
    assert judge_run.data.params["judge_model"] == "mock-judge"


@pytest.mark.e2e
def test_e2e_e_008_transcript(storage, e2e_run):
    """E2E-E-008: sample count = config smoke.num_prompts (or all eval records if
    fewer); all sampled ids from eval split."""
    run_id = e2e_run["run_id"]

    index_map = storage.read_json(ARTIFACTS_BUCKET, f"{run_id}/tokens/index_map.json")
    eval_ids = {e["record_id"] for e in index_map["splits"]["eval"]}

    config = yaml.safe_load(Path(CONFIG_PATH).read_text())
    num_prompts = config["smoke"]["num_prompts"]

    transcript = storage.read_json(ARTIFACTS_BUCKET, f"{run_id}/smoke/transcript.json")
    assert len(transcript["samples"]) == min(num_prompts, len(eval_ids))
    for sample in transcript["samples"]:
        assert sample["record_id"] in eval_ids


@pytest.mark.e2e
def test_e2e_e_009_idempotent_stage_rerun(storage, e2e_run):
    """E2E-E-009: re-running tuner tokenize + tuner train for the same run ID leaves
    one coherent set of artifacts and an unchanged-or-replaced single registry
    manifest. Runs the stages in-process (not a subprocess) for speed, matching every
    other suite's own idempotency checks (TRN-I-011/SMK-I-008).

    TRN-I-011's own idempotency contract is scoped to the model/adapter storage
    prefix and the registry manifest, not MLflow run count -- re-running train()
    always opens a fresh MLflow run (docs/spec/08-test-specs/trainer.md), so this second
    call leaves a second `tuner.stage: trainer` run for this run_id behind. Left
    alone, that's cross-test pollution: E2E-E-007's own "exactly one trainer run"
    assertion would only still hold by accident of this file's function-definition
    order. Clean it up here instead of relying on that order."""
    run_id, model_version = e2e_run["run_id"], e2e_run["model_version"]

    from mlflow.tracking import MlflowClient

    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])

    def _trainer_run_ids() -> set[str]:
        runs = mlflow.search_runs(
            search_all_experiments=True,
            filter_string=f"tags.`tuner.run_id` = '{run_id}' and tags.`tuner.stage` = 'trainer'",
            output_format="list",
        )
        return {r.info.run_id for r in runs}

    trainer_runs_before = _trainer_run_ids()
    tokens_before = set(_all_object_keys(storage, ARTIFACTS_BUCKET, f"{run_id}/tokens/"))
    model_before = set(_all_object_keys(storage, ARTIFACTS_BUCKET, f"{run_id}/model/"))

    assert tokenize(run_id, CONFIG_PATH, storage=storage) == 0
    assert train(run_id, CONFIG_PATH, storage=storage) == 0

    tokens_after = set(_all_object_keys(storage, ARTIFACTS_BUCKET, f"{run_id}/tokens/"))
    model_after = set(_all_object_keys(storage, ARTIFACTS_BUCKET, f"{run_id}/model/"))
    assert tokens_after == tokens_before
    assert model_after == model_before

    registry_keys = _all_object_keys(storage, REGISTRY_BUCKET, f"{model_version}/")
    assert registry_keys == ["manifest.json"]
    manifest = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
    assert manifest["status"] == "candidate"

    # Delete exactly the run(s) this re-run just created -- not "keep newest" or
    # "keep oldest": the *original* trainer run (from the module's one real `tuner
    # run` pipeline execution) has E2E-E-007's own smoke-transcript artifact attached
    # (smoke() logs it there), which this in-process re-run never produces, so it
    # must survive regardless of which of E2E-E-007/E2E-E-009 runs first.
    client = MlflowClient()
    for stale_run_id in _trainer_run_ids() - trainer_runs_before:
        client.delete_run(stale_run_id)


@pytest.mark.e2e
def test_e2e_e_010_iam_spot_check():
    """E2E-E-010: cleaner creds denied writing tuner-gold; trainer creds denied
    reading tuner-bronze (05-infrastructure.md §5's IAM matrix -- neither principal
    has any grant on that bucket at all)."""
    cleaner_s3 = _principal_s3("cleaner")
    assert _denied(
        lambda: cleaner_s3.put_object(Bucket=GOLD_BUCKET, Key="__e2e_probe__", Body=b"x")
    )

    trainer_s3 = _principal_s3("trainer")
    assert _denied(lambda: trainer_s3.list_objects_v2(Bucket=BRONZE_BUCKET))
