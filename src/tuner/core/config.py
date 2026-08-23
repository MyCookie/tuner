"""Pipeline configuration loading & validation (01-architecture.md §6)."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_CONFIG_PATH = Path("configs/pipeline.yaml")


class ConfigError(Exception):
    """A missing config file or a schema/validation failure (exit code 2 at the CLI)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(_Strict):
    adapter: str


class IngestSourceMapping(_Strict):
    prompt_column: str | None = None
    response_column: str | None = None
    system_column: str | None = None


class IngestSourceConfig(_Strict):
    # `str`, not `Literal["csv", "jsonl"]`: the registry in tuner.ingestor.sources
    # is the thing that validates known types and rejects unknown ones with a
    # clear message (03-components/ingestor.md: "Registered by type string ...
    # sql, pdf, api reserved (Future). Unknown type => exit 2"). A Literal here
    # would make pydantic double as that gate and would reject a config naming
    # a reserved-but-not-yet-implemented type outright, instead of failing with
    # a message that names what's actually supported today.
    type: str
    uri: str
    mapping: IngestSourceMapping | None = None


class IngestConfig(_Strict):
    sources: list[IngestSourceConfig]


class CleanConfig(_Strict):
    min_chars: int = Field(default=20, ge=0)
    max_chars: int = Field(default=32000, ge=0)
    # Literal, not `list[str]`: an unknown scrubber name (e.g. "ssn", not yet implemented)
    # must fail at config-load time (exit 2 via ConfigError) rather than reach
    # tuner.cleaner.rules.scrub()'s lookup and raise an uncaught KeyError there
    # (PR #7 review round 1 finding 3).
    pii: list[Literal["email", "phone"]] = Field(default_factory=lambda: ["email", "phone"])


class JudgeConfig(_Strict):
    model: str = ""
    threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_concurrency: int = Field(default=4, gt=0)
    max_retries: int = Field(default=3, ge=0)


class TokenizeConfig(_Strict):
    max_seq_len: int | None = None
    eval_fraction: float = Field(default=0.1, ge=0.0, le=1.0)


class TrainConfig(_Strict):
    method: Literal["qlora", "full"] = "qlora"
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    mlflow_experiment: str = "tuner"


class SmokeConfig(_Strict):
    num_prompts: int = Field(default=8, gt=0)
    max_new_tokens: int = Field(default=256, gt=0)


class PipelineConfig(_Strict):
    model: ModelConfig
    ingest: IngestConfig
    clean: CleanConfig = Field(default_factory=CleanConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    tokenize: TokenizeConfig = Field(default_factory=TokenizeConfig)
    train: TrainConfig = Field(default_factory=TrainConfig)
    smoke: SmokeConfig = Field(default_factory=SmokeConfig)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> PipelineConfig:
    """Load and validate a pipeline config file; raises ConfigError on any failure."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text()) or {}
    try:
        return PipelineConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc


def merge_hyperparameters(defaults: Any, overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Merge config overrides onto adapter defaults, field by field (01-architecture.md §6)."""
    base = dataclasses.asdict(defaults) if dataclasses.is_dataclass(defaults) else dict(defaults)
    return {**base, **overrides}
