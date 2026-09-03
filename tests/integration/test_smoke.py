"""Integration tests for tuner.smoke (SMK suite, docs/spec/08-test-specs/smoke.md).

Needs compose MinIO up. Runs with the tiny-test adapter, `method: full`, CPU-capable
per the suite's own file header (mirrors trainer.md's footnote for the same reason
-- the QLoRA/PEFT-attach path is GPU-only and covered manually in T15). No mocking of
generation: real HF tokenizer/model, real greedy decoding, real MinIO/MLflow.

`smoke_run_id` is a session-scoped fixture: it runs the real Tokenizer and Trainer
once (real HF downloads, real MinIO/MLflow) and every test in this file runs smoke()
against that same trained output, per the pattern `tokenized_run_id` already
established in tests/integration/test_trainer.py. It also plants a decoy MLflow run
tagged `tuner.stage: judge` for the same run ID -- SMK-I-003's own suite row requires
this present "proving the pair-filter discriminates" (01-architecture.md §7: a lookup
on `tuner.run_id` alone would find two runs here and wrongly exit 2 as ambiguous,
where the correct tuner.run_id + tuner.stage pair-filter finds exactly the trainer's
one).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import mlflow
import pytest
import torch
import yaml
from mlflow.tracking import MlflowClient

from tuner.core.ids import canonical_hash, new_record_id, new_run_id
from tuner.core.schemas import validate_gold
from tuner.core.storage import StorageClient
from tuner.models.tiny_test import TinyTestAdapter
from tuner.smoke.cli import smoke
from tuner.tokenizer.cli import tokenize
from tuner.trainer.cli import train

GOLD_BUCKET = "tuner-gold"
ARTIFACTS_BUCKET = "tuner-artifacts"
REGISTRY_BUCKET = "tuner-registry"
ADAPTER_NAME = "tiny-test"

# Confirmed (at implementation time, via tuner.tokenizer.split.assign_split) to hash
# to "eval"/"train" at the default eval_fraction 0.1 -- SMK-I-001/002/003/004/008 need
# a fixed, non-flaky eval-split size (4), not a probable one from random UUIDs.
_HAPPY_PATH_TRAIN_IDS = [
    "14b5318f-21da-4de0-8fe0-effa93c23f10",
    "5323fa3d-04ca-4ccc-af5e-eb8cabe477eb",
    "d0a3e281-0bad-43e5-a75c-01b2409b4ea0",
    "ecafd882-9d00-4eed-a60e-fe98a027299a",
    "010c5256-735e-4117-83ee-b6f8e66b791a",
    "a1d2461e-09aa-4267-8749-ea2e79f29a00",
]
_HAPPY_PATH_EVAL_IDS = [
    "48f2bbb4-269c-4f9b-a8f9-6cc9dceb84c5",
    "d2ce0b40-6a40-440e-9142-b8378179ef57",
    "a1f907d2-3927-40d5-84d1-66a0f8f77f6c",
    "15029884-4e89-44b4-90db-2b97b7968a3c",
]
# A separate, smaller fixture for SMK-I-006: 2 confirmed-train + 3 confirmed-eval, so
# "num_prompts: 50" genuinely exceeds what's available.
_FEW_EVAL_TRAIN_IDS = [
    "6743f0b7-1851-4f4a-8f02-bd25d7fe8cc0",
    "b75d8e5a-3e4a-4690-a90b-ba12a1f7e983",
]
_FEW_EVAL_EVAL_IDS = [
    "dd1c093f-ad3b-4569-93a4-79b93aa30d40",
    "a1b5260d-23a4-44db-b2f5-504df981ed65",
    "10648353-8371-4961-ae1f-799a99dc1730",
]
# 2 confirmed-train + 1 confirmed-eval for SMK-I-005 (missing adapter/model dir).
# Deliberately non-empty eval, not just "enough to make train non-empty": if the
# missing-dir check were missing/broken, execution would otherwise fall through to
# the *next* gate (zero eval-split records -> exit 3) and still never reach
# load_tokenizer -- which would make the monkeypatch below pass for the wrong
# reason, proving nothing about the missing-dir check specifically.
_NO_TRAIN_STAGE_IDS = [
    "893e71af-47e8-4dcc-a776-57359b03a83b",
    "a3196d9e-5d32-4d8e-a4b3-72d8272fd7a5",
    "153b1190-94de-43ef-a3bb-048989881d2d",
]
# The exact TOK-I-027 fixture IDs (tests/integration/test_tokenizer.py) -- both
# confirmed to hash to "train", guaranteeing an empty eval split (SMK-I-007's suite
# row literally names this scenario).
_ZERO_EVAL_RECORD_IDS = [
    "c168059d-1403-4b9e-8c1c-924d64ff6daa",
    "2880e8da-ec9d-49f4-a35b-4979273783da",
]


@pytest.fixture(scope="session", autouse=True)
def _mlflow_env(tmp_path_factory):
    """A single file-backed MLflow store for the whole session, same pattern as
    test_trainer.py's own fixture of this name -- module-local, so it doesn't
    conflict with that one (docs/spec/08 Setup note)."""
    mlruns_dir = tmp_path_factory.mktemp("mlruns")
    os.environ["MLFLOW_TRACKING_URI"] = mlruns_dir.as_uri()
    os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
    yield
    os.environ.pop("MLFLOW_TRACKING_URI", None)
    os.environ.pop("MLFLOW_ALLOW_FILE_STORE", None)


def _gold_record(run_id: str, question: str, answer: str, *, record_id: str | None = None) -> dict:
    return {
        "id": record_id or new_record_id(),
        "run_id": run_id,
        "lineage": {"bronze_content_hash": f"sha256:{'0' * 64}", "cleaner_version": "0.1.0"},
        "conversation": [
            {"role": "user", "content": [{"type": "text", "value": question}]},
            {"role": "assistant", "content": [{"type": "text", "value": answer}]},
        ],
        "evaluation": {
            "score": 0.9,
            "judge_model": "mock-judge",
            "reasoning": "fine",
            "evaluated_at": "2026-07-20T14:31:10Z",
        },
    }


def _seed_gold(storage: StorageClient, run_id: str, records: list[dict]) -> None:
    storage.write_jsonl(GOLD_BUCKET, f"{run_id}/records-00000.jsonl", records)
    storage.write_json(
        GOLD_BUCKET,
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
            "records_hash": canonical_hash(records),
            "counts": {"read": len(records), "written": len(records), "dropped": 0},
            "drops": [],
        },
    )


def _write_config(
    tmp_path: Path,
    *,
    num_prompts: int = 4,
    max_new_tokens: int = 12,
) -> Path:
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model": {"adapter": ADAPTER_NAME},
                "ingest": {"sources": []},
                "train": {"method": "full", "mlflow_experiment": "tuner-smoke-test"},
                "smoke": {"num_prompts": num_prompts, "max_new_tokens": max_new_tokens},
            }
        )
    )
    return path


def _tokenize_and_train(storage: StorageClient, run_id: str, tmp_path: Path) -> str:
    """Runs the real Tokenizer then Trainer once; returns the model_version."""
    config_path = _write_config(tmp_path)
    with tempfile.TemporaryDirectory() as tmp:
        tok_config_path = Path(tmp) / "pipeline.yaml"
        tok_config_path.write_text(
            yaml.safe_dump({"model": {"adapter": ADAPTER_NAME}, "ingest": {"sources": []}})
        )
        assert tokenize(run_id, str(tok_config_path), storage=storage) == 0
    assert train(run_id, str(config_path), storage=storage) == 0
    return f"{ADAPTER_NAME}-{run_id}"


def _cleanup(storage: StorageClient, run_id: str, model_version: str | None = None) -> None:
    storage.delete_prefix(GOLD_BUCKET, f"{run_id}/")
    storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/")
    if model_version:
        storage.delete_prefix(REGISTRY_BUCKET, f"{model_version}/")


@pytest.fixture(scope="session")
def smoke_run_id() -> str:
    """Seeds 6 train + 4 confirmed-eval Gold records, runs the real Tokenizer and
    Trainer once (tiny-test, method: full), and plants a decoy judge-stage MLflow run
    for the same run ID -- see the module docstring for why."""
    storage = StorageClient()
    run_id = new_run_id()
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", record_id=rid)
        for i, rid in enumerate([*_HAPPY_PATH_TRAIN_IDS, *_HAPPY_PATH_EVAL_IDS])
    ]
    _seed_gold(storage, run_id, records)

    with tempfile.TemporaryDirectory() as tmp:
        model_version = _tokenize_and_train(storage, run_id, Path(tmp))

    manifest = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
    trainer_run = mlflow.get_run(manifest["mlflow_run_id"])
    with mlflow.start_run(
        run_name=f"{run_id}-judge-decoy", experiment_id=trainer_run.info.experiment_id
    ):
        # Same tags shape Judge itself would set (01 §7): tuner.run_id + tuner.stage,
        # nothing else -- a decoy that looks exactly like a real foreign-stage run.
        mlflow.set_tags({"tuner.run_id": run_id, "tuner.stage": "judge"})

    yield run_id

    _cleanup(storage, run_id, model_version)


@pytest.mark.integration
def test_full_happy_path(storage, smoke_run_id, tmp_path):
    """SMK-I-001: happy path, num_prompts: 4 -- exit 0; transcript.json at
    tuner-artifacts/{run_id}/smoke/; validates run_id, model_version, generation
    block, exactly 4 samples."""
    config_path = _write_config(tmp_path, num_prompts=4)

    assert smoke(smoke_run_id, str(config_path), storage=storage) == 0

    transcript = storage.read_json(ARTIFACTS_BUCKET, f"{smoke_run_id}/smoke/transcript.json")
    assert transcript is not None
    assert transcript["run_id"] == smoke_run_id
    assert transcript["model_version"] == f"{ADAPTER_NAME}-{smoke_run_id}"
    assert transcript["generation"] == {"max_new_tokens": 12, "strategy": "greedy"}
    assert len(transcript["samples"]) == 4


@pytest.mark.integration
def test_sample_integrity(storage, smoke_run_id, tmp_path):
    """SMK-I-002: every sample's record_id is in the eval split (never train);
    prompt_messages = conversation minus final assistant turn; reference = that
    turn's text; base_output/tuned_output non-empty."""
    config_path = _write_config(tmp_path, num_prompts=4)
    assert smoke(smoke_run_id, str(config_path), storage=storage) == 0

    index_map = storage.read_json(ARTIFACTS_BUCKET, f"{smoke_run_id}/tokens/index_map.json")
    eval_ids = {e["record_id"] for e in index_map["splits"]["eval"]}
    train_ids = {e["record_id"] for e in index_map["splits"]["train"]}
    assert eval_ids == set(_HAPPY_PATH_EVAL_IDS)  # guard the fixture's own premise

    adapter = TinyTestAdapter()
    gold_by_id = {raw["id"]: raw for raw in storage.read_jsonl(GOLD_BUCKET, f"{smoke_run_id}/")}

    transcript = storage.read_json(ARTIFACTS_BUCKET, f"{smoke_run_id}/smoke/transcript.json")
    assert len(transcript["samples"]) == 4
    for sample in transcript["samples"]:
        assert sample["record_id"] in eval_ids
        assert sample["record_id"] not in train_ids

        record = validate_gold(gold_by_id[sample["record_id"]])
        expected_messages = adapter.to_chat_messages(record.conversation)
        assert sample["prompt_messages"] == expected_messages[:-1]
        assert sample["reference"] == expected_messages[-1]["content"]

        assert sample["base_output"].strip()
        assert sample["tuned_output"].strip()


@pytest.mark.integration
def test_mlflow_attachment(storage, smoke_run_id, tmp_path):
    """SMK-I-003: transcript attached as artifact smoke/transcript.json on the
    trainer's run (matched via the tuner.run_id + tuner.stage: trainer tag pair --
    with the judge decoy run for the same run ID present, proving the pair-filter
    discriminates), not a new run."""
    config_path = _write_config(tmp_path, num_prompts=4)

    # Total run count store-wide, not just runs tagged with this run_id -- a smoke()
    # bug that created a new run without any tuner.* tags at all would be invisible
    # to a tag-filtered count but not to this one (PR #12 review round 1 finding 7).
    runs_before = mlflow.search_runs(search_all_experiments=True, output_format="list")

    assert smoke(smoke_run_id, str(config_path), storage=storage) == 0

    runs_after = mlflow.search_runs(search_all_experiments=True, output_format="list")
    assert len(runs_after) == len(runs_before)

    runs = mlflow.search_runs(
        search_all_experiments=True,
        filter_string=f"tags.`tuner.run_id` = '{smoke_run_id}'",
        output_format="list",
    )
    # Exactly trainer + the decoy judge run -- smoke() never creates a run of its own.
    stages = {r.data.tags.get("tuner.stage") for r in runs}
    assert stages == {"trainer", "judge"}
    assert len(runs) == 2

    trainer_run = next(r for r in runs if r.data.tags.get("tuner.stage") == "trainer")
    judge_run = next(r for r in runs if r.data.tags.get("tuner.stage") == "judge")

    client = MlflowClient()
    trainer_artifacts = {f.path for f in client.list_artifacts(trainer_run.info.run_id, "smoke")}
    assert "smoke/transcript.json" in trainer_artifacts

    judge_artifacts = client.list_artifacts(judge_run.info.run_id)
    assert not any(f.path.startswith("smoke") for f in judge_artifacts)


@pytest.mark.integration
def test_determinism(storage, smoke_run_id, tmp_path):
    """SMK-I-004: two runs over the same artifacts produce identical transcripts
    (greedy decoding)."""
    config_path = _write_config(tmp_path, num_prompts=4)

    assert smoke(smoke_run_id, str(config_path), storage=storage) == 0
    first = storage.read_json(ARTIFACTS_BUCKET, f"{smoke_run_id}/smoke/transcript.json")

    assert smoke(smoke_run_id, str(config_path), storage=storage) == 0
    second = storage.read_json(ARTIFACTS_BUCKET, f"{smoke_run_id}/smoke/transcript.json")

    assert first == second


@pytest.mark.integration
def test_missing_adapter_dir_exits_2(storage, run_id, tmp_path, monkeypatch, capsys):
    """SMK-I-005: missing adapter/model dir (trainer never ran for this run ID) --
    exit 2 with a "trainer has not completed" message; no model download happens."""
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", record_id=rid)
        for i, rid in enumerate(_NO_TRAIN_STAGE_IDS)
    ]
    _seed_gold(storage, run_id, records)
    with tempfile.TemporaryDirectory() as tmp:
        tok_config_path = Path(tmp) / "pipeline.yaml"
        tok_config_path.write_text(
            yaml.safe_dump({"model": {"adapter": ADAPTER_NAME}, "ingest": {"sources": []}})
        )
        assert tokenize(run_id, str(tok_config_path), storage=storage) == 0

    def _fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("load_tokenizer must not be called for this scenario")

    monkeypatch.setattr(TinyTestAdapter, "load_tokenizer", _fail_if_called)

    config_path = _write_config(tmp_path)
    try:
        exit_code = smoke(run_id, str(config_path), storage=storage)
        assert exit_code == 2
        assert "trainer has not completed" in capsys.readouterr().err
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_few_eval_records_uses_all_and_warns(storage, run_id, tmp_path, capsys):
    """SMK-I-006: num_prompts: 50 with only 3 eval records -- uses all 3, logs a
    warning, exit 0."""
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", record_id=rid)
        for i, rid in enumerate([*_FEW_EVAL_TRAIN_IDS, *_FEW_EVAL_EVAL_IDS])
    ]
    _seed_gold(storage, run_id, records)
    with tempfile.TemporaryDirectory() as tmp:
        model_version = _tokenize_and_train(storage, run_id, Path(tmp))

    config_path = _write_config(tmp_path, num_prompts=50)
    try:
        exit_code = smoke(run_id, str(config_path), storage=storage)
        assert exit_code == 0
        stderr = capsys.readouterr().err
        assert "only 3 eval record(s) available" in stderr

        transcript = storage.read_json(ARTIFACTS_BUCKET, f"{run_id}/smoke/transcript.json")
        assert len(transcript["samples"]) == 3
    finally:
        _cleanup(storage, run_id, model_version)


@pytest.mark.integration
def test_zero_eval_records_exits_3(storage, run_id, tmp_path):
    """SMK-I-007: zero eval records (artifacts from the TOK-I-027 scenario) -- exit 3."""
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", record_id=rid)
        for i, rid in enumerate(_ZERO_EVAL_RECORD_IDS)
    ]
    _seed_gold(storage, run_id, records)
    with tempfile.TemporaryDirectory() as tmp:
        model_version = _tokenize_and_train(storage, run_id, Path(tmp))

    config_path = _write_config(tmp_path)
    try:
        assert smoke(run_id, str(config_path), storage=storage) == 3
    finally:
        _cleanup(storage, run_id, model_version)


@pytest.mark.integration
def test_rerun_same_run_id_rebuilds_single_transcript(storage, smoke_run_id, tmp_path):
    """SMK-I-008: re-run same run ID -- smoke/ prefix rebuilt, single transcript
    (idempotency)."""
    config_path = _write_config(tmp_path, num_prompts=4)

    assert smoke(smoke_run_id, str(config_path), storage=storage) == 0

    # Plant a stale object under smoke/ that only a real delete_prefix removes --
    # without this, "single transcript" would hold trivially (the stage only ever
    # writes one object), proving nothing about the prefix actually being rebuilt
    # (PR #12 review round 1 finding 1: not a banned-extension-shaped filename,
    # matching TRN-I-011's own precedent for this exact check).
    storage.write_bytes(ARTIFACTS_BUCKET, f"{smoke_run_id}/smoke/stale-marker", b"stale")

    assert smoke(smoke_run_id, str(config_path), storage=storage) == 0

    with tempfile.TemporaryDirectory() as tmp:
        storage.download_dir(ARTIFACTS_BUCKET, f"{smoke_run_id}/smoke/", tmp)
        files = [p.name for p in Path(tmp).rglob("*") if p.is_file()]
        assert files == ["transcript.json"]  # stale marker gone, real file rebuilt


@pytest.mark.integration
def test_qlora_without_cuda_exits_2(storage, run_id, tmp_path, monkeypatch, capsys):
    """SMK-I-009: method: qlora with no CUDA device available exits 2 with a clear
    message naming the host-venv fallback doc, mirroring TRN-I-012 -- added in review
    round 1 alongside the fix it regression-tests (PR #12 review round 1 finding 4:
    smoke had no equivalent to the Trainer's own CUDA gate at all)."""
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    def _fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("load_tokenizer must not be called for this scenario")

    monkeypatch.setattr(TinyTestAdapter, "load_tokenizer", _fail_if_called)

    path = tmp_path / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model": {"adapter": ADAPTER_NAME},
                "ingest": {"sources": []},
                "train": {"method": "qlora"},
            }
        )
    )
    exit_code = smoke(run_id, str(path), storage=storage)
    assert exit_code == 2
    assert "requires a CUDA device" in capsys.readouterr().err


@pytest.mark.integration
def test_mid_run_failure_exits_1(storage, run_id, tmp_path, monkeypatch, capsys):
    """SMK-I-010: an unexpected mid-run failure (here, a broken MLflow attachment)
    surfaces as a "smoke: ..." message and exit 1, not a raw traceback -- mirrors
    TRN-I-009's own generic-exit-1 path (PR #12 review round 1 finding 4)."""
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", record_id=rid)
        for i, rid in enumerate([*_FEW_EVAL_TRAIN_IDS, *_FEW_EVAL_EVAL_IDS])
    ]
    _seed_gold(storage, run_id, records)
    with tempfile.TemporaryDirectory() as tmp:
        model_version = _tokenize_and_train(storage, run_id, Path(tmp))

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("induced mlflow failure")

    monkeypatch.setattr(MlflowClient, "log_artifact", _raise)

    config_path = _write_config(tmp_path)
    try:
        exit_code = smoke(run_id, str(config_path), storage=storage)
        assert exit_code == 1
        assert "induced mlflow failure" in capsys.readouterr().err
    finally:
        _cleanup(storage, run_id, model_version)
