"""Integration tests for tuner.judge (JDG suite, docs/08-test-specs/judge.md).

Needs compose MinIO up: `docker compose up -d minio minio-init` then
`uv run pytest -m integration tests/integration/test_judge.py`. The judge endpoint
itself is the mock judge (tests/mock_judge/app.py), mounted in-process via an ASGI
transport -- no real network call, no compose sidecar needed for these cases.
"""

from __future__ import annotations

from pathlib import Path

import mlflow
import pytest
import yaml
from starlette.testclient import TestClient
from tests.mock_judge.app import app as mock_app
from tests.mock_judge.app import reset_state

from tuner.core.ids import canonical_hash, new_record_id
from tuner.core.schemas import SilverGoldRecord
from tuner.core.storage import StorageClient
from tuner.judge.cli import judge
from tuner.judge.prompts import RUBRIC_V1, render_rubric_prompt

SILVER_BUCKET = "tuner-silver"
GOLD_BUCKET = "tuner-gold"
JUDGE_MODEL = "mock-judge-v1"


@pytest.fixture(autouse=True)
def _reset_mock_judge():
    reset_state()
    yield
    reset_state()


@pytest.fixture
def mock_http_client():
    """A real `httpx.Client` (TestClient subclasses it) bridged to the mock judge ASGI
    app in-process -- the same sync-to-async bridging FastAPI's own TestClient uses,
    which a bare `httpx.Client(transport=httpx.ASGITransport(...))` can't do: ASGITransport
    only implements the async transport interface, so it needs `httpx.AsyncClient`."""
    with TestClient(mock_app, base_url="http://mock-judge") as client:
        yield client


@pytest.fixture(autouse=True)
def _judge_env(tmp_path, monkeypatch):
    """Every case except JDG-I-027's own parametrization needs TUNER_JUDGE_BASE_URL set --
    the judge() entrypoint checks it exists before doing anything, even though the
    injected `http_client` fixture never actually dials out to it. MLFLOW_TRACKING_URI is
    needed by every case that reaches a successful promotion (_log_to_mlflow runs
    unconditionally on that path) -- file-backed per-test, matching
    docs/08-test-specs/judge.md's Setup note ("no server needed"), not the ambient
    environment's real compose server, which would make every case here depend on
    docker being up regardless of what it's actually testing."""
    monkeypatch.setenv("TUNER_JUDGE_BASE_URL", "http://mock-judge")
    monkeypatch.setenv("TUNER_JUDGE_API_KEY", "unused-mock-key")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", (tmp_path / "mlruns").as_uri())
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")


def _write_config(
    tmp_path: Path,
    *,
    model: str = JUDGE_MODEL,
    threshold: float = 0.7,
    max_concurrency: int = 4,
    max_retries: int = 3,
) -> Path:
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model": {"adapter": "gemma-e4b"},
                "ingest": {"sources": []},
                "judge": {
                    "model": model,
                    "threshold": threshold,
                    "max_concurrency": max_concurrency,
                    "max_retries": max_retries,
                },
            }
        )
    )
    return path


def _silver_record(run_id: str, question: str, answer: str) -> dict:
    return {
        "id": new_record_id(),
        "run_id": run_id,
        "lineage": {"bronze_content_hash": f"sha256:{'0' * 64}", "cleaner_version": "0.1.0"},
        "conversation": [
            {"role": "user", "content": [{"type": "text", "value": question}]},
            {"role": "assistant", "content": [{"type": "text", "value": answer}]},
        ],
        "evaluation": None,
    }


def _seed_silver(storage: StorageClient, run_id: str, records: list[dict]) -> None:
    storage.write_jsonl(SILVER_BUCKET, f"{run_id}/records-00000.jsonl", records)
    storage.write_json(
        SILVER_BUCKET,
        f"{run_id}/manifest.json",
        {
            "tier": "silver",
            "run_id": run_id,
            "created_at": "2026-07-20T14:22:05Z",
            "producer": {"stage": "cleaner", "version": "0.1.0"},
            "input": {
                "tier": "bronze",
                "manifest_uri": f"s3://tuner-bronze/{run_id}/manifest.json",
            },
            "files": ["records-00000.jsonl"],
            "records_hash": canonical_hash(records),
            "counts": {"read": len(records), "written": len(records), "dropped": 0},
            "drops": [],
        },
    )


def _records(storage: StorageClient, bucket: str, run_id: str) -> list[dict]:
    return list(storage.read_jsonl(bucket, f"{run_id}/"))


def _manifest(storage: StorageClient, bucket: str, run_id: str) -> dict | None:
    return storage.read_json(bucket, f"{run_id}/manifest.json")


def _cleanup(storage: StorageClient, run_id: str) -> None:
    storage.delete_prefix(SILVER_BUCKET, f"{run_id}/")
    storage.delete_prefix(GOLD_BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_promotion_matches_threshold_exactly(storage, run_id, tmp_path, mock_http_client):
    """JDG-I-020: Silver seeded with markers [[score=5..9]], threshold 0.7 -> exactly the
    >=7 records in Gold; others dropped below_threshold; counts reconcile."""
    records = {
        score: _silver_record(run_id, f"Question {score}", f"Answer {score} [[score={score}]]")
        for score in range(5, 10)
    }
    _seed_silver(storage, run_id, list(records.values()))
    config_path = _write_config(tmp_path, threshold=0.7)

    try:
        assert judge(run_id, str(config_path), storage=storage, http_client=mock_http_client) == 0

        gold_ids = {r["id"] for r in _records(storage, GOLD_BUCKET, run_id)}
        assert gold_ids == {records[s]["id"] for s in (7, 8, 9)}

        manifest = _manifest(storage, GOLD_BUCKET, run_id)
        assert manifest["counts"] == {"read": 5, "written": 3, "dropped": 2}
        assert {d["reason"]: d["count"] for d in manifest["drops"]} == {"below_threshold": 2}
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_promoted_records_evaluation_fully_populated(storage, run_id, tmp_path, mock_http_client):
    """JDG-I-021: promoted records have evaluation fully populated -- normalized score,
    judge_model = config value, reasoning, evaluated_at; ids/lineage unchanged from Silver."""
    silver = _silver_record(run_id, "How do I reset my password?", "Go to Settings. [[score=9]]")
    _seed_silver(storage, run_id, [silver])
    config_path = _write_config(tmp_path, model=JUDGE_MODEL)

    try:
        assert judge(run_id, str(config_path), storage=storage, http_client=mock_http_client) == 0

        [gold] = _records(storage, GOLD_BUCKET, run_id)
        SilverGoldRecord.model_validate(gold)
        assert gold["id"] == silver["id"]
        assert gold["lineage"] == silver["lineage"]
        assert gold["evaluation"]["score"] == 0.9
        assert gold["evaluation"]["judge_model"] == JUDGE_MODEL
        assert gold["evaluation"]["reasoning"]
        assert gold["evaluation"]["evaluated_at"]
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_garbage_record_retried_then_dropped_judge_error(
    storage, run_id, tmp_path, mock_http_client
):
    """JDG-I-022: one [[garbage]] record among clean ones, max_retries=2 -> that record is
    retried exactly 2 extra times (mock counts calls), then dropped judge_error; rest
    promoted. 9 good records, not 3: 1 error in 10 total is exactly the 10% abort
    threshold (not over it), while 1 in 4 would trip JDG-I-023's own abort path instead."""
    good_records = [_silver_record(run_id, f"Q{i}", f"A{i}") for i in range(9)]
    garbage_record = _silver_record(run_id, "Garbage question", "Garbage answer [[garbage]]")
    _seed_silver(storage, run_id, [*good_records, garbage_record])
    config_path = _write_config(tmp_path, max_retries=2)

    try:
        exit_code = judge(
            run_id,
            str(config_path),
            storage=storage,
            http_client=mock_http_client,
            sleep=lambda s: None,
        )
        assert exit_code == 0

        garbage_key = render_rubric_prompt(garbage_record["conversation"])
        assert mock_app.state.call_counts[garbage_key] == 3  # 1 initial + 2 retries

        gold_ids = {r["id"] for r in _records(storage, GOLD_BUCKET, run_id)}
        assert gold_ids == {r["id"] for r in good_records}
        manifest = _manifest(storage, GOLD_BUCKET, run_id)
        assert manifest["drops"] == [{"reason": "judge_error", "count": 1}]
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_high_judge_error_rate_aborts(storage, run_id, tmp_path, mock_http_client):
    """JDG-I-023: 6 of 10 records [[fail=500]] (>10% judge_error) -> exit 1, no Gold
    manifest written."""
    failing = [_silver_record(run_id, f"Q{i}", f"A{i} [[fail=500]]") for i in range(6)]
    good = [_silver_record(run_id, f"Q{i}", f"A{i}") for i in range(6, 10)]
    _seed_silver(storage, run_id, [*failing, *good])
    # max_retries=0: [[fail=500]] never recovers regardless of retries, so 0 keeps this fast.
    config_path = _write_config(tmp_path, max_retries=0)

    try:
        exit_code = judge(run_id, str(config_path), storage=storage, http_client=mock_http_client)
        assert exit_code == 1
        assert _manifest(storage, GOLD_BUCKET, run_id) is None
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_transient_failure_recovers_on_retry(storage, run_id, tmp_path, mock_http_client):
    """JDG-I-024: one [[fail_once=429]] record that succeeds on the 2nd attempt (mock:
    fails once then scores) -- promoted; the retry path is proven end to end."""
    record = _silver_record(run_id, "Q", "A [[fail_once=429]]")
    _seed_silver(storage, run_id, [record])
    config_path = _write_config(tmp_path, max_retries=1)

    try:
        exit_code = judge(
            run_id,
            str(config_path),
            storage=storage,
            http_client=mock_http_client,
            sleep=lambda s: None,
        )
        assert exit_code == 0

        gold_ids = {r["id"] for r in _records(storage, GOLD_BUCKET, run_id)}
        assert gold_ids == {record["id"]}
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_max_concurrency_respected(storage, run_id, tmp_path, mock_http_client):
    """JDG-I-025: max_concurrency=3 with a mock that records concurrent in-flight count ->
    peak in-flight <= 3. Also asserts >= 2 (not just the upper bound): 10 records, an
    artificial per-call delay, and 3 workers should make concurrency observable, not
    just theoretically bounded -- a fully-serialized implementation would still pass a
    bare "<= 3" and this is meant to catch that."""
    records = [_silver_record(run_id, f"Q{i}", f"A{i}") for i in range(10)]
    _seed_silver(storage, run_id, records)
    config_path = _write_config(tmp_path, max_concurrency=3)

    try:
        exit_code = judge(
            run_id,
            str(config_path),
            storage=storage,
            http_client=mock_http_client,
            sleep=lambda s: None,
        )
        assert exit_code == 0
        assert 2 <= mock_app.state.peak_in_flight <= 3
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_mlflow_run_logged(storage, run_id, tmp_path, mock_http_client):
    """JDG-I-026: MLflow run exists tagged tuner.run_id and tuner.stage: judge; params
    judge model/threshold/rubric version; metrics mean/median/promotion_rate/
    judge_error_rate; a score histogram artifact is present. MLFLOW_TRACKING_URI (a
    file-backed temp dir, per docs/08-test-specs/judge.md's Setup note) is already set
    by the autouse _judge_env fixture, same as every other case in this file."""
    mlflow_dir = tmp_path / "mlruns"
    records = [
        _silver_record(run_id, f"Q{i}", f"A{i} [[score={score}]]")
        for i, score in enumerate([5, 7, 8, 9])
    ]
    _seed_silver(storage, run_id, records)
    config_path = _write_config(tmp_path, threshold=0.7)

    try:
        assert judge(run_id, str(config_path), storage=storage, http_client=mock_http_client) == 0

        mlflow.set_tracking_uri(mlflow_dir.as_uri())
        experiment = mlflow.get_experiment_by_name("tuner")
        assert experiment is not None
        runs = mlflow.search_runs(
            experiment_ids=[experiment.experiment_id],
            filter_string=f"tags.tuner.run_id = '{run_id}' and tags.tuner.stage = 'judge'",
        )
        assert len(runs) == 1
        mlflow_run_id = runs.iloc[0]["run_id"]
        run_data = mlflow.get_run(mlflow_run_id).data

        assert run_data.params["judge_model"] == JUDGE_MODEL
        assert run_data.params["threshold"] == "0.7"
        assert run_data.params["rubric_version"] == RUBRIC_V1
        for metric in ("mean_score", "median_score", "promotion_rate", "judge_error_rate"):
            assert metric in run_data.metrics

        artifacts = mlflow.artifacts.list_artifacts(run_id=mlflow_run_id)
        assert any(a.path == "score_histogram.txt" for a in artifacts)
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "unset_base_url, empty_model",
    [(True, False), (False, True)],
    ids=["unset-base-url", "empty-model"],
)
def test_missing_model_or_base_url_exits_2_before_any_call(
    storage, run_id, tmp_path, mock_http_client, monkeypatch, unset_base_url, empty_model
):
    """JDG-I-027: empty judge.model / unset TUNER_JUDGE_BASE_URL both exit 2 before any
    Silver read (mock records zero calls)."""
    if unset_base_url:
        monkeypatch.delenv("TUNER_JUDGE_BASE_URL", raising=False)
    config_path = _write_config(tmp_path, model="" if empty_model else JUDGE_MODEL)

    exit_code = judge(run_id, str(config_path), storage=storage, http_client=mock_http_client)

    assert exit_code == 2
    assert sum(mock_app.state.call_counts.values()) == 0


@pytest.mark.integration
def test_missing_mlflow_tracking_uri_exits_2_before_any_call(
    storage, run_id, tmp_path, mock_http_client, monkeypatch
):
    """JDG-I-032: unset MLFLOW_TRACKING_URI exits 2 before any Silver read, same as a
    missing judge.model/TUNER_JUDGE_BASE_URL -- checked early so a missing value can't
    surface as an exit-1 failure after Gold was already committed (PR #8 review round 1
    finding 7; regression-tested per round 2 minor finding)."""
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    config_path = _write_config(tmp_path)

    exit_code = judge(run_id, str(config_path), storage=storage, http_client=mock_http_client)

    assert exit_code == 2
    assert sum(mock_app.state.call_counts.values()) == 0


@pytest.mark.integration
def test_all_below_threshold_exits_3(storage, run_id, tmp_path, mock_http_client):
    """JDG-I-028: all records score below threshold -> exit 3."""
    records = [
        _silver_record(run_id, f"Q{i}", f"A{i} [[score={score}]]")
        for i, score in enumerate([1, 2, 3])
    ]
    _seed_silver(storage, run_id, records)
    config_path = _write_config(tmp_path, threshold=0.7)

    try:
        exit_code = judge(run_id, str(config_path), storage=storage, http_client=mock_http_client)
        assert exit_code == 3
        assert _manifest(storage, GOLD_BUCKET, run_id) is None
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_rerun_same_run_id_rebuilds_gold(storage, run_id, tmp_path, mock_http_client):
    """JDG-I-029: re-run same run ID -> Gold prefix rebuilt; single manifest
    (idempotency). Plants a stale shard between runs that only a real delete-prefix
    would remove."""
    records = [_silver_record(run_id, f"Q{i}", f"A{i} [[score=9]]") for i in range(3)]
    _seed_silver(storage, run_id, records)
    config_path = _write_config(tmp_path, threshold=0.7)

    try:
        assert judge(run_id, str(config_path), storage=storage, http_client=mock_http_client) == 0
        first_count = len(_records(storage, GOLD_BUCKET, run_id))

        storage.write_jsonl(GOLD_BUCKET, f"{run_id}/records-00099.jsonl", [{"stale": True}])

        assert judge(run_id, str(config_path), storage=storage, http_client=mock_http_client) == 0
        second_records = _records(storage, GOLD_BUCKET, run_id)
        second_manifest = _manifest(storage, GOLD_BUCKET, run_id)

        assert first_count == len(second_records) == 3
        assert second_manifest["files"] == ["records-00000.jsonl"]
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_missing_silver_manifest_exits_2(storage, run_id, tmp_path, mock_http_client):
    """JDG-I-030: missing Silver manifest -> exit 2 (upstream incomplete)."""
    config_path = _write_config(tmp_path)

    assert judge(run_id, str(config_path), storage=storage, http_client=mock_http_client) == 2


@pytest.mark.integration
def test_invalid_silver_record_exits_2(storage, run_id, tmp_path, mock_http_client, capsys):
    """JDG-I-031: a seeded invalid Silver record (schema-breaking) exits 2 naming the
    record id -- invalid input is an abort, not a drop."""
    bad_record = {
        "id": new_record_id(),
        "run_id": run_id,
        "lineage": {"bronze_content_hash": "not-a-valid-hash", "cleaner_version": "0.1.0"},
        "conversation": [
            {"role": "user", "content": [{"type": "text", "value": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "value": "hello"}]},
        ],
        "evaluation": None,
    }
    _seed_silver(storage, run_id, [bad_record])
    config_path = _write_config(tmp_path)

    try:
        exit_code = judge(run_id, str(config_path), storage=storage, http_client=mock_http_client)

        assert exit_code == 2
        assert bad_record["id"] in capsys.readouterr().err
        assert _manifest(storage, GOLD_BUCKET, run_id) is None
    finally:
        _cleanup(storage, run_id)
