"""Integration tests for tuner.trainer (TRN suite, docs/spec/08-test-specs/trainer.md).

Needs compose MinIO up. Runs with the tiny-test adapter, `method: full`, 1 epoch --
CPU-capable per the suite's own file header (device selection is left to
torch/accelerate; nothing here forces CPU or requires a GPU). The QLoRA/bitsandbytes
load path is `@pytest.mark.gpu` and covered manually in T15 (`TRN-G-020`); here it's
only exercised up to `LoraConfig` construction (`TRN-I-002`), never a real model load.

`tokenized_run_id` is a session-scoped fixture: it runs the real Tokenizer once
(real HF tokenizer, real MinIO) and every test in this file trains against that same
tokenized output, per the suite's own "shared session-scoped fixture to keep runtime
down" note. Each test's own `train()` call writes to `{run_id}/model/` and its own
registry manifest -- never to `{run_id}/tokens/`, which the shared fixture owns.
"""

from __future__ import annotations

import dataclasses
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import mlflow
import pytest
import torch
import yaml
from peft import PeftModel

from tuner.core.config import merge_hyperparameters
from tuner.core.ids import canonical_hash, new_record_id, new_run_id
from tuner.core.schemas import RegistryManifest
from tuner.core.storage import StorageClient
from tuner.models.gemma_e4b import GemmaE4BAdapter
from tuner.models.tiny_test import TinyTestAdapter
from tuner.tokenizer.cli import tokenize
from tuner.trainer.cli import build_lora_config, train

GOLD_BUCKET = "tuner-gold"
ARTIFACTS_BUCKET = "tuner-artifacts"
REGISTRY_BUCKET = "tuner-registry"
ADAPTER_NAME = "tiny-test"


@pytest.fixture(scope="session", autouse=True)
def _mlflow_env(tmp_path_factory):
    """A single file-backed MLflow store for the whole session -- every test's train()
    call logs into it, queryable independently per test via tags (docs/spec/08 Setup note)."""
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
    adapter: str = ADAPTER_NAME,
    method: str = "full",
    hyperparameters: dict[str, Any] | None = None,
) -> Path:
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model": {"adapter": adapter},
                "ingest": {"sources": []},
                "train": {
                    "method": method,
                    "hyperparameters": hyperparameters or {},
                    "mlflow_experiment": "tuner-trainer-test",
                },
            }
        )
    )
    return path


@pytest.fixture(scope="session")
def tokenized_run_id() -> str:
    """Seeds 15 short Gold records and runs the real Tokenizer once -- expensive (real
    HF tokenizer, real MinIO), so shared across every test in this file rather than
    repeated per test.

    Record IDs are fixed (13 confirmed to hash to train, 2 to eval, at the default
    eval_fraction 0.1), not random `new_record_id()` calls -- with random IDs, whether
    the resulting eval split is empty or not (and therefore whether Trainer's
    has-eval-data branch gets exercised at all) would vary from run to run."""
    storage = StorageClient()
    run_id = new_run_id()
    record_ids = [
        # 13 confirmed train
        "e40f0a98-a5f5-4c35-adc1-42f4bddac842",
        "3dad74b9-a061-4715-9357-4f84e0d4e866",
        "d1fa4519-ce1f-4d4c-94ab-086d32efadef",
        "50c3606e-9b86-4960-8631-1f33566e8880",
        "24a61c90-1ed9-4e28-8331-8b93235d0a9d",
        "a6f9afdb-9948-42d3-90c8-2d6ccda52bc0",
        "7f797681-964b-439a-aab4-be6bc111a372",
        "15c54d6d-8ef0-4d0c-83e5-6dba1f22afe4",
        "479127fa-de45-42e9-8d05-a5f07aa6bbc1",
        "5e5e1389-809c-4ef5-a9d6-44d4228d6036",
        "b73d74e3-c031-4109-9f47-74670bbc022b",
        "f9dab42f-e7be-4172-bee6-3ac8ca9994b2",
        "a992e89c-538f-4410-92a9-7347e76c9c2f",
        # 2 confirmed eval
        "5755cb9b-b0e5-4846-8a23-d040fa2b4417",
        "8dfffa1d-51a0-4902-979a-d92106fd382a",
    ]
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", record_id=record_id)
        for i, record_id in enumerate(record_ids)
    ]
    _seed_gold(storage, run_id, records)

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "pipeline.yaml"
        config_path.write_text(
            yaml.safe_dump({"model": {"adapter": ADAPTER_NAME}, "ingest": {"sources": []}})
        )
        exit_code = tokenize(run_id, str(config_path), storage=storage)
        assert exit_code == 0, "tokenized_run_id fixture: real tokenize() run failed"

    yield run_id

    storage.delete_prefix(GOLD_BUCKET, f"{run_id}/")
    storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/")


def _cleanup_model_output(storage: StorageClient, run_id: str, model_version: str) -> None:
    storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/model/")
    storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/adapter/")
    storage.delete_prefix(REGISTRY_BUCKET, f"{model_version}/")


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    # RegistryManifest URIs are "s3://bucket/key-or-prefix" (02-data-contracts.md
    # §5.2). TRN-I-004 resolves the manifest's *own* field values against real
    # storage with this -- rebuilding an equivalent-looking key independently, as the
    # original version did, proves nothing about whether those fields are correct
    # (PR #11 review round 2 finding 1).
    assert uri.startswith("s3://"), f"not an s3:// URI: {uri!r}"
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


def _all_object_keys(storage: StorageClient, bucket: str, prefix: str) -> list[str]:
    # StorageClient has no public "list" method (only read/write/delete verbs) --
    # download_dir gives us the same key set via the local file tree it produces,
    # without needing a new StorageClient method just for one test's assertion.
    # Relative to the download root, not absolute: two separate calls each get their
    # own randomly-named tempdir, so absolute paths would never compare equal across
    # calls (TRN-I-011 needs exactly that comparison).
    with tempfile.TemporaryDirectory() as tmp:
        storage.download_dir(bucket, prefix, tmp)
        root = Path(tmp)
        return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]


@pytest.mark.integration
def test_full_happy_path(storage, tokenized_run_id, tmp_path):
    """TRN-I-001: full happy path (tiny-test, method: full, 1 epoch) -- exit 0;
    tuner-artifacts/{run_id}/model/ (full-FT layout) uploaded; finite losses."""
    model_version = f"{ADAPTER_NAME}-{tokenized_run_id}"
    config_path = _write_config(tmp_path)

    try:
        exit_code = train(tokenized_run_id, str(config_path), storage=storage)
        assert exit_code == 0

        model_keys = _all_object_keys(storage, ARTIFACTS_BUCKET, f"{tokenized_run_id}/model/")
        assert any("safetensors" in key for key in model_keys)

        manifest = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
        assert manifest is not None
        train_loss = manifest["eval"]["final_train_loss"]
        eval_loss = manifest["eval"]["final_eval_loss"]
        # math.isfinite rejects inf too, not just NaN -- `x == x` (the original
        # check) is False only for NaN and would let a runaway/overflowed loss
        # through silently (PR #11 review round 2 finding 4).
        assert math.isfinite(train_loss)
        assert math.isfinite(eval_loss)
    finally:
        _cleanup_model_output(storage, tokenized_run_id, model_version)


@pytest.mark.integration
def test_lora_config_construction_no_model_loaded():
    """TRN-I-002: QLoRA config-construction path (GPU-free part) -- LoraConfig built
    from merged hyperparameters; r/alpha/dropout/target_modules match the merge
    result. The model is never loaded (a pure function call, no storage/HF touched).

    Uses the real merge_hyperparameters, not a hand-written dict standing in for its
    result (round 1 finding 9) -- an override of lora_r specifically, so the "merge"
    part is genuinely exercised, not just build_lora_config's own field mapping.

    `exclude_modules is None`: tiny-test has no vision/audio-tower name collision to
    worry about (see the gemma-e4b sibling case below for the branch where it does)."""
    adapter = TinyTestAdapter()
    merged = merge_hyperparameters(adapter.training_defaults, {"lora_r": 8})

    lora_config = build_lora_config(merged)

    assert lora_config.r == 8  # from the override
    assert lora_config.lora_alpha == adapter.training_defaults.lora_alpha  # from defaults
    assert lora_config.lora_dropout == adapter.training_defaults.lora_dropout
    assert set(lora_config.target_modules) == set(adapter.training_defaults.lora_target_modules)
    assert lora_config.exclude_modules is None


@pytest.mark.integration
def test_lora_config_excludes_vision_and_audio_towers():
    """TRN-I-002 (gemma-e4b sibling case, T15/TRN-G-020): build_lora_config's
    `exclude_modules` carries `gemma-e4b`'s `lora_exclude_modules_regex` straight
    through -- the other branch `test_lora_config_construction_no_model_loaded`
    doesn't exercise. A pure function call, no model touched."""
    adapter = GemmaE4BAdapter()
    merged = merge_hyperparameters(adapter.training_defaults, {})

    lora_config = build_lora_config(merged)

    assert lora_config.exclude_modules == adapter.training_defaults.lora_exclude_modules_regex
    assert re.fullmatch(
        lora_config.exclude_modules, "model.vision_tower.encoder.layers.0.self_attn.q_proj"
    )
    assert re.fullmatch(lora_config.exclude_modules, "model.audio_tower.layers.0.self_attn.q_proj")
    assert not re.fullmatch(
        lora_config.exclude_modules, "model.language_model.layers.0.self_attn.q_proj"
    )


# The suffixes an actual pickle-format weight/checkpoint file could carry --
# deliberately spelled out only as a tuple of bare extensions, never concatenated with
# a filename stem in one literal string (the gate's own pickle-ban grep scans test
# files too, and a `name.<ext>`-shaped literal would trip it).
_PICKLE_SHAPED_SUFFIXES = (".bin", ".pkl", ".pickle", ".pt", ".pth")


@pytest.mark.integration
def test_artifact_hygiene_no_bin_or_pickle(storage, tokenized_run_id, tmp_path):
    """TRN-I-003: artifact hygiene sweep -- no pickle-format files anywhere under the
    *whole* run prefix (tokens/ and model/, not just this stage's own output), walking
    every object key against every pickle-shaped suffix, not just one (round 1 finding
    4: the original check only walked model/ and only checked `.bin`)."""
    model_version = f"{ADAPTER_NAME}-{tokenized_run_id}"
    config_path = _write_config(tmp_path)

    try:
        assert train(tokenized_run_id, str(config_path), storage=storage) == 0

        keys = _all_object_keys(storage, ARTIFACTS_BUCKET, f"{tokenized_run_id}/")
        assert keys
        for key in keys:
            assert not key.endswith(_PICKLE_SHAPED_SUFFIXES), key
    finally:
        _cleanup_model_output(storage, tokenized_run_id, model_version)


@pytest.mark.integration
def test_registry_manifest_contents(storage, tokenized_run_id, tmp_path):
    """TRN-I-004: registry manifest validates vs. 02 §5.2; status candidate;
    model_version = tiny-test-{run_id}; URIs resolve to existing objects; mlflow_run_id
    resolves to a run."""
    model_version = f"{ADAPTER_NAME}-{tokenized_run_id}"
    config_path = _write_config(tmp_path)

    try:
        assert train(tokenized_run_id, str(config_path), storage=storage) == 0

        manifest_raw = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
        manifest = RegistryManifest.model_validate(manifest_raw)

        assert manifest.status == "candidate"
        assert manifest.model_version == f"{ADAPTER_NAME}-{tokenized_run_id}"
        assert manifest.adapter_name == ADAPTER_NAME
        assert manifest.base_model == TinyTestAdapter.hf_model_id
        assert manifest.method == "full"

        # The manifest's *own* URI fields, not independently-rebuilt keys that happen
        # to match by construction (PR #11 review round 2 finding 1: the original
        # version asserted nothing about weights_uri/gold_manifest_uri/index_map_uri
        # specifically -- a wrong URI in any of them would have gone undetected).
        gold_bucket, gold_key = _parse_s3_uri(manifest.gold_manifest_uri)
        assert storage.read_json(gold_bucket, gold_key) is not None

        index_bucket, index_key = _parse_s3_uri(manifest.index_map_uri)
        assert storage.read_json(index_bucket, index_key) is not None

        weights_bucket, weights_prefix = _parse_s3_uri(manifest.weights_uri)
        assert _all_object_keys(storage, weights_bucket, weights_prefix)

        found_run = mlflow.get_run(manifest.mlflow_run_id)
        assert found_run is not None
    finally:
        _cleanup_model_output(storage, tokenized_run_id, model_version)


@pytest.mark.integration
def test_mlflow_run_contents(storage, tokenized_run_id, tmp_path):
    """TRN-I-005: every merged hyperparameter logged as param; >=1 loss metric
    series; tags tuner.run_id/tuner.adapter/tuner.model_version/tuner.stage: trainer;
    dataset-version params (gold_manifest_uri, index_map_uri)."""
    model_version = f"{ADAPTER_NAME}-{tokenized_run_id}"
    config_path = _write_config(tmp_path)

    try:
        assert train(tokenized_run_id, str(config_path), storage=storage) == 0

        manifest = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
        run = mlflow.get_run(manifest["mlflow_run_id"])

        adapter = TinyTestAdapter()
        # Every field of TrainingDefaults, not a hand-picked subset (round 1 finding 8:
        # the original check only covered 5 of the 9 merged hyperparameters).
        for field in dataclasses.fields(adapter.training_defaults):
            expected = getattr(adapter.training_defaults, field.name)
            assert run.data.params[field.name] == str(expected)

        assert "train_loss" in run.data.metrics or "loss" in run.data.metrics

        assert run.data.tags["tuner.run_id"] == tokenized_run_id
        assert run.data.tags["tuner.adapter"] == ADAPTER_NAME
        assert run.data.tags["tuner.model_version"] == model_version
        assert run.data.tags["tuner.stage"] == "trainer"

        assert (
            run.data.params["gold_manifest_uri"]
            == f"s3://{GOLD_BUCKET}/{tokenized_run_id}/manifest.json"
        )
        assert run.data.params["index_map_uri"] == (
            f"s3://{ARTIFACTS_BUCKET}/{tokenized_run_id}/tokens/index_map.json"
        )
    finally:
        _cleanup_model_output(storage, tokenized_run_id, model_version)


@pytest.mark.integration
def test_full_method_unsupported_adapter_exits_2_before_any_load(
    storage, run_id, tmp_path, monkeypatch, capsys
):
    """TRN-I-006: method: full with an adapter where supports_full_ft is False
    (gemma-e4b) exits 2 before any load -- no model download happens.

    "No load happens" and the sanctioned-models message are both asserted directly
    (not just the exit code, which an unrelated exit-2 path -- e.g. run_id's missing
    tokens/index_map.json -- could equally produce and make this test pass without
    actually exercising the supports_full_ft gate at all, PR #11 review round 1
    finding 3)."""

    def _fail_if_called(self):
        raise AssertionError("load_tokenizer must not be called for this scenario")

    monkeypatch.setattr(GemmaE4BAdapter, "load_tokenizer", _fail_if_called)
    config_path = _write_config(tmp_path, adapter="gemma-e4b", method="full")

    exit_code = train(run_id, str(config_path), storage=storage)

    assert exit_code == 2
    assert "does not support full fine-tuning" in capsys.readouterr().err


@pytest.mark.integration
def test_index_map_adapter_mismatch_exits_2(storage, run_id, tmp_path):
    """TRN-I-007: index_map.adapter != model.adapter exits 2 (tensors built for a
    different model). A hand-seeded index_map.json (not a real tokenize() run) is
    enough -- the check fires before any tensor download -- and deliberately uses
    method: full with tiny-test (which supports it and needs no CUDA), so this test
    exercises only the adapter-mismatch gate, not the supports_full_ft or CUDA gates
    that would otherwise fire first for other adapter/method combinations."""
    storage.write_json(
        ARTIFACTS_BUCKET,
        f"{run_id}/tokens/index_map.json",
        {
            "run_id": run_id,
            "adapter": "gemma-e4b",
            "tokenizer_id": "google/gemma-4-E4B-it",
            "max_seq_len": 4096,
            "gold_manifest_uri": f"s3://{GOLD_BUCKET}/{run_id}/manifest.json",
            "splits": {"train": [], "eval": []},
            "dropped": [],
        },
    )
    config_path = _write_config(tmp_path, adapter="tiny-test", method="full")

    try:
        exit_code = train(run_id, str(config_path), storage=storage)
        assert exit_code == 2
    finally:
        storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_missing_index_map_exits_2(storage, run_id, tmp_path):
    """TRN-I-008: missing tokens/index_map.json exits 2."""
    config_path = _write_config(tmp_path)

    exit_code = train(run_id, str(config_path), storage=storage)

    assert exit_code == 2


@pytest.mark.integration
def test_missing_mlflow_tracking_uri_exits_2(storage, run_id, tmp_path, monkeypatch, capsys):
    """TRN-I-017: unset MLFLOW_TRACKING_URI exits 2 before any load -- checked early
    (round 1 finding 5), not left to the later os.environ[...] lookup that previously
    raised an uncaught KeyError only after the base model had already loaded."""
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    config_path = _write_config(tmp_path)

    exit_code = train(run_id, str(config_path), storage=storage)

    assert exit_code == 2
    assert "MLFLOW_TRACKING_URI" in capsys.readouterr().err


@pytest.mark.integration
def test_unknown_adapter_exits_2(storage, run_id, tmp_path, capsys):
    """TRN-I-013: unknown model.adapter in config exits 2, mirroring every sibling
    stage's own version of this case (TOK-I-029 via ADP-U-011, JDG's own get_adapter
    use, etc.) -- trainer.md's own suite table omitted it even though the
    implementation resolves the adapter the same way every other stage does. The
    message is asserted, not just the exit code (round 1 finding 3's pattern)."""
    config_path = _write_config(tmp_path, adapter="nope")

    exit_code = train(run_id, str(config_path), storage=storage)

    assert exit_code == 2
    assert "nope" in capsys.readouterr().err


@pytest.mark.integration
def test_unknown_hyperparameter_key_exits_2(storage, run_id, tmp_path, capsys):
    """TRN-I-014: an unknown key in train.hyperparameters exits 2 -- the same
    ConfigError merge_hyperparameters raises for ADP-U-031/CORE-U-007, now exercised
    at the Trainer CLI level (the only stage that actually calls it with a real
    adapter's training_defaults). The message is asserted, not just the exit code
    (round 1 finding 3's pattern)."""
    config_path = _write_config(tmp_path, hyperparameters={"not_a_real_field": 1})

    exit_code = train(run_id, str(config_path), storage=storage)

    assert exit_code == 2
    assert "not_a_real_field" in capsys.readouterr().err


@pytest.mark.integration
def test_invalid_index_map_exits_2(storage, run_id, tmp_path):
    """TRN-I-015: a present but schema-invalid tokens/index_map.json exits 2 --
    TRN-I-008's sibling case for "invalid", not just "missing" (the same
    invalid-input-is-an-abort pattern every other stage's own upstream-validation
    cases follow, e.g. CLN-I-034, JDG-I-031, TOK-I-025)."""
    storage.write_json(
        ARTIFACTS_BUCKET,
        f"{run_id}/tokens/index_map.json",
        {"run_id": run_id, "adapter": ADAPTER_NAME},  # missing required fields
    )
    config_path = _write_config(tmp_path)

    try:
        exit_code = train(run_id, str(config_path), storage=storage)
        assert exit_code == 2
    finally:
        storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/")


@pytest.mark.integration
def test_no_eval_split_final_eval_loss_mirrors_train_loss(storage, tmp_path):
    """TRN-I-016: when every record hashes to train (no eval split at all, mirroring
    TOK-I-027's own "eval empties" scenario one stage downstream), training still
    proceeds -- Trainer skips evaluation and RegistryEval's required final_eval_loss
    field mirrors final_train_loss rather than a value implying eval actually ran."""
    run_id = new_run_id()
    # Two record IDs independently confirmed (T10) to both hash to "train" at the
    # default eval_fraction 0.1.
    both_hash_to_train = [
        "c168059d-1403-4b9e-8c1c-924d64ff6daa",
        "2880e8da-ec9d-49f4-a35b-4979273783da",
    ]
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", record_id=record_id)
        for i, record_id in enumerate(both_hash_to_train)
    ]
    _seed_gold(storage, run_id, records)
    model_version = f"{ADAPTER_NAME}-{run_id}"

    try:
        tok_config_path = tmp_path / "tokenize.yaml"
        tok_config_path.write_text(
            yaml.safe_dump({"model": {"adapter": ADAPTER_NAME}, "ingest": {"sources": []}})
        )
        assert tokenize(run_id, str(tok_config_path), storage=storage) == 0
        index_map = storage.read_json(ARTIFACTS_BUCKET, f"{run_id}/tokens/index_map.json")
        assert index_map["splits"]["eval"] == []  # guard the premise

        train_config_path = _write_config(tmp_path)
        assert train(run_id, str(train_config_path), storage=storage) == 0

        manifest = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
        assert manifest["eval"]["final_eval_loss"] == manifest["eval"]["final_train_loss"]
    finally:
        storage.delete_prefix(GOLD_BUCKET, f"{run_id}/")
        storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/")
        storage.delete_prefix(REGISTRY_BUCKET, f"{model_version}/")


@pytest.mark.integration
def test_induced_upload_failure_no_registry_manifest_mlflow_failed(
    storage, tokenized_run_id, tmp_path, monkeypatch
):
    """TRN-I-009: induced failure during upload (monkeypatch upload_dir to raise) --
    no registry manifest (commit-marker rule); MLflow run status FAILED with a
    traceback artifact."""
    model_version = f"{ADAPTER_NAME}-{tokenized_run_id}"
    config_path = _write_config(tmp_path)

    def _raise(*args, **kwargs):
        raise RuntimeError("induced upload failure")

    monkeypatch.setattr(storage, "upload_dir", _raise)

    try:
        exit_code = train(tokenized_run_id, str(config_path), storage=storage)
        assert exit_code == 1

        assert storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json") is None

        runs = mlflow.search_runs(
            experiment_names=["tuner-trainer-test"],
            filter_string=f"tags.tuner.run_id = '{tokenized_run_id}'",
            output_format="list",
        )
        assert runs, "expected the failed run to still be recorded in MLflow"
        failed_run = runs[0]
        assert failed_run.info.status == "FAILED"
        artifact_uris = [
            a.path for a in mlflow.artifacts.list_artifacts(run_id=failed_run.info.run_id)
        ]
        assert "traceback.txt" in artifact_uris
    finally:
        _cleanup_model_output(storage, tokenized_run_id, model_version)


@pytest.mark.integration
def test_hyperparameter_override_reflected_in_mlflow(storage, tokenized_run_id, tmp_path):
    """TRN-I-010: hyperparameter override via config (epochs: 2) -- MLflow param shows
    2, not the adapter default."""
    model_version = f"{ADAPTER_NAME}-{tokenized_run_id}"
    config_path = _write_config(tmp_path, hyperparameters={"epochs": 2})

    try:
        assert train(tokenized_run_id, str(config_path), storage=storage) == 0

        manifest = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
        assert manifest["hyperparameters"]["epochs"] == 2

        run = mlflow.get_run(manifest["mlflow_run_id"])
        assert run.data.params["epochs"] == "2"
    finally:
        _cleanup_model_output(storage, tokenized_run_id, model_version)


@pytest.mark.integration
def test_rerun_same_run_id_rebuilds_output_single_manifest(storage, tokenized_run_id, tmp_path):
    """TRN-I-011: re-run of the same run ID rebuilds the model/adapter prefix; exactly
    one registry manifest exists for the version afterward (idempotency)."""
    model_version = f"{ADAPTER_NAME}-{tokenized_run_id}"
    config_path = _write_config(tmp_path)

    try:
        assert train(tokenized_run_id, str(config_path), storage=storage) == 0
        first_keys = set(_all_object_keys(storage, ARTIFACTS_BUCKET, f"{tokenized_run_id}/model/"))

        # Plant a stale object under model/ that only a real delete_prefix removes.
        # Deliberately not a banned-extension-shaped filename (CLAUDE.md's pickle
        # ban) -- that would trip the gate's own grep, which scans test files too.
        storage.write_bytes(ARTIFACTS_BUCKET, f"{tokenized_run_id}/model/stale-marker", b"stale")

        assert train(tokenized_run_id, str(config_path), storage=storage) == 0

        second_keys = set(_all_object_keys(storage, ARTIFACTS_BUCKET, f"{tokenized_run_id}/model/"))
        assert second_keys == first_keys  # stale marker gone; same real files rebuilt

        assert storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json") is not None
        # "Exactly one" counted directly, not just "a manifest exists" (round 1 finding
        # 10) -- the fixed {model_version}/manifest.json key makes a second manifest
        # for the same version structurally impossible, but assert the count anyway
        # rather than relying on that as an unstated assumption.
        registry_keys = _all_object_keys(storage, REGISTRY_BUCKET, f"{model_version}/")
        assert registry_keys == ["manifest.json"]
    finally:
        _cleanup_model_output(storage, tokenized_run_id, model_version)


@pytest.mark.integration
def test_qlora_without_cuda_exits_2(storage, run_id, tmp_path, monkeypatch, capsys):
    """TRN-I-012: method: qlora with no CUDA device available exits 2 with a clear
    message naming the host-venv fallback doc -- the CLI section's own "requires CUDA"
    sentence, otherwise untested by any other case in this suite.

    The message is asserted, not just the exit code: run_id is unseeded, so if the
    CUDA gate didn't actually fire (e.g. this mock silently failed to take effect),
    the later missing-tokens/index_map.json check would independently produce the
    same exit 2 and this test would pass without having proven anything about the
    CUDA gate at all (PR #11 review round 1 finding 2 -- reproduced: with CUDA
    genuinely available and no mock, this exact config still exits 2, from that
    later check)."""
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    config_path = _write_config(tmp_path, adapter="gemma-e4b", method="qlora")

    exit_code = train(run_id, str(config_path), storage=storage)

    assert exit_code == 2
    assert "CUDA device" in capsys.readouterr().err


@pytest.mark.gpu
def test_real_gemma_e4b_qlora_smoke(storage):
    """TRN-G-020: real `gemma-e4b` QLoRA smoke -- 10 fixture records, 1 epoch, on a
    genuine CUDA device. Completes; the written adapter directory independently
    reloads via `PeftModel.from_pretrained`; peak CUDA memory stays within the dev
    box's 128 GB budget. Manual/GPU-only (`pytest -m gpu`, run in T15) -- excluded
    from every other lane by the default addopts marker exclusion.

    Real HF downloads (revision-pinned `google/gemma-4-E4B-it`, ~16 GB safetensors,
    cached under `~/.cache/huggingface` after the first run) and real bitsandbytes
    4-bit quantization -- there is no mock anywhere in this test.
    """
    assert torch.cuda.is_available(), "TRN-G-020 requires a real CUDA device"
    torch.cuda.reset_peak_memory_stats()

    run_id = new_run_id()
    adapter_name = "gemma-e4b"
    model_version = f"{adapter_name}-{run_id}"
    record_ids = [new_record_id() for _ in range(10)]
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", record_id=record_id)
        for i, record_id in enumerate(record_ids)
    ]
    _seed_gold(storage, run_id, records)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "pipeline.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "model": {"adapter": adapter_name},
                        "ingest": {"sources": []},
                        # eval_fraction: 0.0 -- 10 records is too few to guarantee a
                        # non-empty eval split by hash (same reasoning as INF-I-012's
                        # own eval_fraction: 0.0 note); this case is about proving the
                        # real QLoRA path completes, not exercising the eval branch.
                        "tokenize": {"eval_fraction": 0.0},
                        "train": {
                            "method": "qlora",
                            "hyperparameters": {"epochs": 1},
                            "mlflow_experiment": "tuner-trainer-gpu-test",
                        },
                    }
                )
            )
            assert tokenize(run_id, str(config_path), storage=storage) == 0
            exit_code = train(run_id, str(config_path), storage=storage)

        assert exit_code == 0, "TRN-G-020: real QLoRA train() did not complete"

        adapter_keys = _all_object_keys(storage, ARTIFACTS_BUCKET, f"{run_id}/adapter/")
        assert any(key.endswith("adapter_config.json") for key in adapter_keys)
        assert any("safetensors" in key for key in adapter_keys)

        manifest = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
        assert manifest is not None
        assert math.isfinite(manifest["eval"]["final_train_loss"])

        # Independent reload proof, not just "train() exited 0" -- the same
        # PeftModel.from_pretrained call the Smoke-test stage makes against a real
        # qlora adapter dir (src/tuner/smoke/cli.py's own GPU-only branch).
        with tempfile.TemporaryDirectory() as tmp:
            local_adapter_dir = Path(tmp) / "adapter"
            storage.download_dir(ARTIFACTS_BUCKET, f"{run_id}/adapter/", local_adapter_dir)
            adapter = GemmaE4BAdapter()
            base_model = adapter.load_base_model(quantized=True)
            peft_model = PeftModel.from_pretrained(base_model, str(local_adapter_dir))
            assert peft_model is not None

        peak_bytes = torch.cuda.max_memory_allocated()
        peak_gib = peak_bytes / (1024**3)
        print(f"TRN-G-020: peak CUDA memory allocated: {peak_gib:.2f} GiB")
        # "VRAM within the 128 GB box" (08 trainer.md) -- a loose upper bound, not a
        # tight one: this case is about catching a pathological blow-up, and a real
        # OOM would already have raised before this line ran.
        assert peak_gib < 100, f"peak CUDA memory {peak_gib:.2f} GiB looks unbounded"
    finally:
        storage.delete_prefix(GOLD_BUCKET, f"{run_id}/")
        _cleanup_model_output(storage, run_id, model_version)
