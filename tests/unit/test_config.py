"""Unit tests for tuner.core.config (CORE suite, docs/08-test-specs/core.md)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from tuner.core.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    load_config,
    merge_hyperparameters,
)


def _base_config_dict() -> dict:
    return {
        "model": {"adapter": "gemma-e4b"},
        "ingest": {
            "sources": [
                {
                    "type": "csv",
                    "uri": "fixtures/support_dialogs.csv",
                    "mapping": {
                        "prompt_column": "question",
                        "response_column": "answer",
                        "system_column": None,
                    },
                }
            ]
        },
        "clean": {"min_chars": 20, "max_chars": 32000, "pii": ["email", "phone"]},
        "judge": {"model": "", "threshold": 0.7, "max_concurrency": 4, "max_retries": 3},
        "tokenize": {"max_seq_len": None, "eval_fraction": 0.1},
        "train": {"method": "qlora", "hyperparameters": {}, "mlflow_experiment": "tuner"},
        "smoke": {"num_prompts": 8, "max_new_tokens": 256},
    }


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "pipeline.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_shipped_config_round_trips_with_documented_defaults():
    """CORE-U-001: the shipped configs/pipeline.yaml parses and matches 01-architecture.md §6."""
    config = load_config(DEFAULT_CONFIG_PATH)

    assert config.model.adapter == "gemma-e4b"
    assert len(config.ingest.sources) == 1
    source = config.ingest.sources[0]
    assert source.type == "csv"
    assert source.uri == "fixtures/support_dialogs.csv"
    assert source.mapping.prompt_column == "question"
    assert source.mapping.response_column == "answer"
    assert source.mapping.system_column is None
    assert config.clean.min_chars == 20
    assert config.clean.max_chars == 32000
    assert config.clean.pii == ["email", "phone"]
    assert config.judge.model == ""
    assert config.judge.threshold == 0.7
    assert config.judge.max_concurrency == 4
    assert config.judge.max_retries == 3
    assert config.tokenize.max_seq_len is None
    assert config.tokenize.eval_fraction == 0.1
    assert config.train.method == "qlora"
    assert config.train.hyperparameters == {}
    assert config.train.mlflow_experiment == "tuner"
    assert config.smoke.num_prompts == 8
    assert config.smoke.max_new_tokens == 256


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(unknown_top_level_key="oops"),
        lambda d: d["clean"].update(unknown_nested_key="oops"),
    ],
    ids=["top-level", "nested"],
)
def test_unknown_key_rejected(tmp_path, mutate):
    """CORE-U-002: an unknown key (top-level or nested) raises a ConfigError naming it."""
    data = _base_config_dict()
    mutate(data)
    path = _write_config(tmp_path, data)

    with pytest.raises(ConfigError, match="unknown_.*_key"):
        load_config(path)


def test_missing_config_file_path(tmp_path):
    """CORE-U-003: a missing config file path raises ConfigError with the path in the message."""
    missing = tmp_path / "does-not-exist.yaml"

    with pytest.raises(ConfigError, match="does-not-exist.yaml"):
        load_config(missing)


def test_hyperparameter_override_precedence():
    """CORE-U-004: a hyperparameters override wins for its field; others keep adapter defaults."""

    @dataclasses.dataclass(frozen=True)
    class FakeTrainingDefaults:
        learning_rate: float
        epochs: int
        lora_r: int

    defaults = FakeTrainingDefaults(learning_rate=2e-4, epochs=3, lora_r=16)

    merged = merge_hyperparameters(defaults, {"epochs": 5})

    assert merged["epochs"] == 5
    assert merged["learning_rate"] == 2e-4
    assert merged["lora_r"] == 16


def test_judge_model_empty_string_accepted_at_load(tmp_path):
    """CORE-U-005: judge.model empty string accepted at load (the Judge rejects it, JDG-I-027)."""
    data = _base_config_dict()
    data["judge"]["model"] = ""
    path = _write_config(tmp_path, data)

    config = load_config(path)

    assert config.judge.model == ""


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["judge"].update(threshold="high"),
        lambda d: d["judge"].update(max_concurrency=-1),
        lambda d: d["tokenize"].update(eval_fraction=1.5),
        lambda d: d["clean"].update(pii=["ssn"]),
    ],
    ids=[
        "threshold-wrong-type",
        "negative-max-concurrency",
        "eval-fraction-out-of-range",
        "unknown-pii-scrubber",
    ],
)
def test_type_and_range_errors_rejected(tmp_path, mutate):
    """CORE-U-006: type/range errors are each rejected with a field-specific error. An
    unknown clean.pii scrubber name (PR #7 review round 1 finding 3) must fail here, at
    config-load time, rather than reach tuner.cleaner.rules.scrub()'s lookup and raise an
    uncaught KeyError deep in a Cleaner run."""
    data = _base_config_dict()
    mutate(data)
    path = _write_config(tmp_path, data)

    with pytest.raises(ConfigError):
        load_config(path)
