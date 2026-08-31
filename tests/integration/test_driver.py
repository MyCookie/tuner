"""Integration tests for `tuner run` (CLI suite, docs/08-test-specs/cli.md).

Needs compose MinIO up. CLI-I-010..013 monkeypatch stage invocation to fast
recorders (the suite's own "stage entrypoints monkeypatched to recorders for speed"
Setup note) -- no real subprocess/HF work, just the driver's own wiring: run-ID
generation, stage order, exit-code propagation, the completion summary. CLI-I-014 is
the one case that runs for real, through a genuine `tuner run` subprocess, no mocks
at all -- the pre-E2E integration checkpoint.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import mlflow
import pytest
import uvicorn
import yaml
from tests.mock_judge.app import app as mock_judge_app
from tests.mock_judge.app import reset_state

from tuner.cli import STAGE_ORDER, run_pipeline
from tuner.core.ids import RUN_ID_RE
from tuner.core.storage import StorageClient

REGISTRY_BUCKET = "tuner-registry"
ARTIFACTS_BUCKET = "tuner-artifacts"
GOLD_BUCKET = "tuner-gold"


@pytest.fixture(autouse=True)
def _mlflow_env(tmp_path_factory, monkeypatch):
    """File-backed MLflow, isolated per test -- matches every other suite's own
    pattern rather than depending on the ambient compose MLflow server."""
    original_tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
    mlruns_dir = tmp_path_factory.mktemp("mlruns")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", mlruns_dir.as_uri())
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    yield
    # mlflow.set_tracking_uri (called by _seed_registry_manifest and by
    # run_pipeline's own completion step) sets process-global state that outlives
    # this test's monkeypatched env var -- reset it explicitly, or a later test file
    # in the same pytest session (alphabetically, test_infra.py) inherits this
    # test's throwaway file store instead of the ambient compose MLflow server it
    # actually expects.
    if original_tracking_uri is not None:
        mlflow.set_tracking_uri(original_tracking_uri)


def _write_config(tmp_path: Path, **overrides: Any) -> Path:
    config: dict[str, Any] = {"model": {"adapter": "tiny-test"}, "ingest": {"sources": []}}
    config.update(overrides)
    path = tmp_path / "pipeline.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def _seed_registry_manifest(storage: StorageClient, run_id: str, model_version: str) -> str:
    """Plants a minimal valid registry manifest + a matching MLflow trainer-tagged
    run, standing in for what a real train() call would have produced -- CLI-I-010,
    012, 013 monkeypatch stage invocation itself, so nothing here ever really calls
    train(). Returns the manifest's weights_uri.

    Sets the tracking URI explicitly from the current env var rather than relying on
    mlflow's own env-var auto-resolution: once anything in this process calls
    `mlflow.set_tracking_uri` explicitly (run_pipeline's own completion step does,
    every test), that value sticks globally until overridden again, regardless of
    which test's MLFLOW_TRACKING_URI env var is current -- a later test's fresh
    per-test tmp dir would otherwise silently write into an earlier test's store.

    Passes `experiment_id` directly to `start_run` rather than calling the stateful
    `mlflow.set_experiment` -- that also sets *global* fluent-API state (the "active
    experiment"), outliving this test exactly like the tracking URI does, but with no
    per-test value to restore afterward. A later test/file's own `start_run()` with no
    explicit experiment would otherwise resolve against this experiment's id in
    *its* tracking store, which may not exist there (tests/integration/test_infra.py's
    own INF-I-004 hit exactly this)."""
    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    experiment = mlflow.get_experiment_by_name("tuner-driver-test")
    experiment_id = (
        experiment.experiment_id if experiment else mlflow.create_experiment("tuner-driver-test")
    )
    with mlflow.start_run(run_name=run_id, experiment_id=experiment_id) as run:
        mlflow.set_tags({"tuner.run_id": run_id, "tuner.stage": "trainer"})
    weights_uri = f"s3://{ARTIFACTS_BUCKET}/{run_id}/model/"
    manifest = {
        "model_version": model_version,
        "run_id": run_id,
        "adapter_name": "tiny-test",
        "base_model": "HuggingFaceTB/SmolLM2-135M-Instruct",
        "method": "full",
        "created_at": "2026-07-20T16:05:00Z",
        "gold_manifest_uri": f"s3://{GOLD_BUCKET}/{run_id}/manifest.json",
        "index_map_uri": f"s3://{ARTIFACTS_BUCKET}/{run_id}/tokens/index_map.json",
        "weights_uri": weights_uri,
        "mlflow_run_id": run.info.run_id,
        "hyperparameters": {},
        "eval": {"final_train_loss": 1.0, "final_eval_loss": 1.0},
        "status": "candidate",
    }
    storage.write_json(REGISTRY_BUCKET, f"{model_version}/manifest.json", manifest)
    return weights_uri


@pytest.mark.integration
def test_happy_path_invokes_stages_in_order(storage, tmp_path):
    """CLI-I-010: tuner run happy path -- generates one valid run ID; invokes
    ingest->clean->judge->tokenize->train->smoke in exactly that order, each
    receiving the same run ID and config path."""
    calls: list[tuple[str, str, str]] = []

    def fake_invoke(stage: str, run_id: str, config_path: str) -> int:
        calls.append((stage, run_id, config_path))
        if stage == "train":
            _seed_registry_manifest(storage, run_id, f"tiny-test-{run_id}")
        return 0

    config_path = _write_config(tmp_path)
    exit_code = run_pipeline(str(config_path), storage=storage, invoke_stage=fake_invoke)
    run_id = calls[0][1] if calls else None
    try:
        assert exit_code == 0
        assert [c[0] for c in calls] == list(STAGE_ORDER)
        assert RUN_ID_RE.match(run_id)
        assert all(c[1] == run_id for c in calls)
        assert all(c[2] == str(config_path) for c in calls)
    finally:
        if run_id:
            storage.delete_prefix(REGISTRY_BUCKET, f"tiny-test-{run_id}/")


@pytest.mark.integration
def test_stage_failure_stops_driver_and_exits_naming_stage(capsys, tmp_path):
    """CLI-I-011: stage failure (judge recorder exits 1) -- driver stops:
    tokenize/train/smoke never invoked; driver exits 1 naming the failed stage."""
    calls: list[str] = []

    def fake_invoke(stage: str, run_id: str, config_path: str) -> int:
        calls.append(stage)
        return 1 if stage == "judge" else 0

    config_path = _write_config(tmp_path)
    exit_code = run_pipeline(str(config_path), invoke_stage=fake_invoke)

    assert exit_code == 1
    assert calls == ["ingest", "clean", "judge"]
    assert "judge" in capsys.readouterr().err


@pytest.mark.integration
def test_stage_exit_3_aborts_with_distinct_message(capsys, tmp_path):
    """CLI-I-012: stage exit 3 (zero records) -- driver aborts with a distinct
    "pipeline empty at <stage>" message, exit 3."""

    def fake_invoke(stage: str, run_id: str, config_path: str) -> int:
        return 3 if stage == "clean" else 0

    config_path = _write_config(tmp_path)
    exit_code = run_pipeline(str(config_path), invoke_stage=fake_invoke)

    assert exit_code == 3
    assert "pipeline empty at clean" in capsys.readouterr().err


@pytest.mark.integration
def test_completion_output_prints_all_four_locations(storage, tmp_path, capsys):
    """CLI-I-013: completion output -- prints run ID, adapter/model URI, transcript
    URI, MLflow run URL (all four present on stdout)."""
    holder: dict[str, str] = {}

    def fake_invoke(stage: str, run_id: str, config_path: str) -> int:
        holder["run_id"] = run_id
        if stage == "train":
            holder["weights_uri"] = _seed_registry_manifest(storage, run_id, f"tiny-test-{run_id}")
        return 0

    config_path = _write_config(tmp_path)
    try:
        exit_code = run_pipeline(str(config_path), storage=storage, invoke_stage=fake_invoke)
        assert exit_code == 0

        out = capsys.readouterr().out
        run_id = holder["run_id"]
        assert run_id in out
        assert holder["weights_uri"] in out
        assert f"{run_id}/smoke/transcript.json" in out
        assert "/#/experiments/" in out  # the MLflow run URL
    finally:
        storage.delete_prefix(REGISTRY_BUCKET, f"tiny-test-{holder['run_id']}/")


@pytest.fixture
def mock_judge_server():
    """A real, bound HTTP server for the mock judge -- CLI-I-014's judge subprocess is
    a genuinely separate OS process, so the in-process ASGI TestClient bridge every
    other suite uses (judge() called directly, same process) can't reach it."""
    reset_state()
    config = uvicorn.Config(mock_judge_app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    # No /v1 suffix: build_http_client's base_url gets /v1/chat/completions appended
    # by score_record itself (judge/client.py) -- matching test_judge.py's own
    # "http://mock-judge" (bare) convention for TUNER_JUDGE_BASE_URL.
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    reset_state()


@pytest.mark.integration
def test_real_mini_run_through_actual_driver(storage, tmp_path, monkeypatch, mock_judge_server):
    """CLI-I-014: real mini-run (no mocks): fixtures + mock judge + tiny-test through
    the actual driver -- exit 0, the pre-E2E integration checkpoint."""
    monkeypatch.setenv("TUNER_JUDGE_BASE_URL", mock_judge_server)
    monkeypatch.setenv("TUNER_JUDGE_API_KEY", "unused-mock-key")

    config_path = _write_config(
        tmp_path,
        ingest={
            "sources": [
                {
                    "type": "csv",
                    "uri": "fixtures/support_dialogs.csv",
                    "mapping": {
                        "prompt_column": "question",
                        "response_column": "answer",
                        "system_column": "system",
                    },
                }
            ]
        },
        judge={"model": "mock-judge"},
        train={"method": "full", "hyperparameters": {"epochs": 1}},
        smoke={"num_prompts": 2, "max_new_tokens": 8},
    )

    result = subprocess.run(
        ["tuner", "run", "--config", str(config_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    run_id = next(
        line.rsplit(maxsplit=1)[-1]
        for line in result.stdout.splitlines()
        if line.startswith("run: starting pipeline run")
    )
    try:
        model_version = f"tiny-test-{run_id}"
        manifest = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
        assert manifest is not None
        assert manifest["status"] == "candidate"
    finally:
        for bucket in ("tuner-bronze", "tuner-silver", GOLD_BUCKET, ARTIFACTS_BUCKET):
            storage.delete_prefix(bucket, f"{run_id}/")
        storage.delete_prefix(REGISTRY_BUCKET, f"tiny-test-{run_id}/")
