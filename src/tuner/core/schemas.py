"""Executable data contracts (docs/02-data-contracts.md) — pydantic v2 models.

This document is normative; if code and docs/02-data-contracts.md disagree,
the doc wins and this module is a bug.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.functional_validators import AfterValidator

CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _validate_content_hash(value: str) -> str:
    if not CONTENT_HASH_RE.match(value):
        raise ValueError("must be 'sha256:' followed by 64 lowercase hex characters")
    return value


def _validate_uuid4(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("must be a valid UUID") from exc
    if parsed.version != 4:
        raise ValueError("must be a UUIDv4")
    return value


def _validate_utc_timestamp(value: str) -> str:
    if not UTC_TIMESTAMP_RE.match(value):
        raise ValueError("must be ISO-8601 UTC with a 'Z' suffix")
    return value


ContentHash = Annotated[str, AfterValidator(_validate_content_hash)]
RecordId = Annotated[str, AfterValidator(_validate_uuid4)]
UtcTimestamp = Annotated[str, AfterValidator(_validate_utc_timestamp)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- 1. Bronze — raw envelope ------------------------------------------------


class BronzeSource(_Strict):
    uri: str
    type: Literal["csv", "jsonl", "sql", "pdf", "api"]
    locator: str
    ingested_at: UtcTimestamp
    ingestor_version: str


class BronzeRecord(_Strict):
    id: RecordId
    run_id: str
    source: BronzeSource
    content_hash: ContentHash
    raw: dict[str, Any]


# --- 2. Silver & Gold — the Multimodal Contract ------------------------------


class Lineage(_Strict):
    bronze_content_hash: ContentHash
    cleaner_version: str


class ContentPart(_Strict):
    type: Literal["text", "image", "audio"]
    value: str

    @model_validator(mode="after")
    def _check_text_value(self) -> ContentPart:
        if self.type == "text" and not self.value.strip():
            raise ValueError("text content value must be non-empty after trim")
        return self


class Turn(_Strict):
    role: Literal["system", "user", "assistant"]
    content: list[ContentPart] = Field(min_length=1)


class Evaluation(_Strict):
    score: float = Field(ge=0.0, le=1.0)
    judge_model: str
    reasoning: str
    evaluated_at: UtcTimestamp


class SilverGoldRecord(_Strict):
    id: RecordId
    run_id: str
    lineage: Lineage
    conversation: list[Turn] = Field(min_length=2)
    evaluation: Evaluation | None = None

    @model_validator(mode="after")
    def _check_conversation_shape(self) -> SilverGoldRecord:
        roles = [turn.role for turn in self.conversation]
        if roles.count("system") > 1:
            raise ValueError("at most one system turn is allowed")
        if "system" in roles and roles.index("system") != 0:
            raise ValueError("a system turn must be first")
        if "user" not in roles:
            raise ValueError("conversation must contain at least one user turn")
        if "assistant" not in roles:
            raise ValueError("conversation must contain at least one assistant turn")
        if roles[-1] != "assistant":
            raise ValueError("the last turn must be assistant")
        return self


def validate_gold(data: dict[str, Any]) -> SilverGoldRecord:
    """Gold-context validation: `evaluation` must be non-null (02-data-contracts.md §2)."""
    record = SilverGoldRecord.model_validate(data)
    if record.evaluation is None:
        raise ValueError("Gold records require a non-null evaluation")
    return record


# --- 3. Tier manifest ---------------------------------------------------------


class ManifestProducer(_Strict):
    stage: str
    version: str


class ManifestInputRef(_Strict):
    tier: Literal["bronze", "silver", "gold"]
    manifest_uri: str


class ManifestCounts(_Strict):
    read: int = Field(ge=0)
    written: int = Field(ge=0)
    dropped: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_totals(self) -> ManifestCounts:
        if self.read != self.written + self.dropped:
            raise ValueError("counts.read must equal counts.written + counts.dropped")
        return self


class ManifestDrop(_Strict):
    reason: str
    count: int = Field(ge=0)


class TierManifest(_Strict):
    tier: Literal["bronze", "silver", "gold"]
    run_id: str
    created_at: UtcTimestamp
    producer: ManifestProducer
    input: ManifestInputRef | None
    files: list[str]
    records_hash: ContentHash
    counts: ManifestCounts
    drops: list[ManifestDrop] = Field(default_factory=list)


def validate_manifest_drops(manifest: TierManifest, allowed_reasons: set[str]) -> TierManifest:
    """Reject drop reasons the producing stage doesn't recognize (02-data-contracts.md §3)."""
    unknown = {drop.reason for drop in manifest.drops} - allowed_reasons
    if unknown:
        raise ValueError(f"unknown drop reason(s) for this stage: {sorted(unknown)}")
    return manifest


# --- 4. Tokenized artifact — index_map.json -----------------------------------


class IndexMapEntry(_Strict):
    row: int = Field(ge=0)
    record_id: RecordId


class IndexMapDrop(_Strict):
    record_id: RecordId
    reason: Literal["over_max_len", "unsupported_modality", "masking_mismatch"]


class IndexMapSplits(_Strict):
    train: list[IndexMapEntry]
    eval: list[IndexMapEntry]


class IndexMap(_Strict):
    run_id: str
    adapter: str
    tokenizer_id: str
    max_seq_len: int = Field(gt=0)
    gold_manifest_uri: str
    splits: IndexMapSplits
    dropped: list[IndexMapDrop] = Field(default_factory=list)


# --- 5. Training artifacts & registry -----------------------------------------


class RegistryEval(_Strict):
    final_train_loss: float
    final_eval_loss: float


class RegistryManifest(_Strict):
    model_version: str
    run_id: str
    adapter_name: str
    base_model: str
    method: Literal["qlora", "full"]
    created_at: UtcTimestamp
    gold_manifest_uri: str
    index_map_uri: str
    weights_uri: str
    mlflow_run_id: str
    hyperparameters: dict[str, Any]
    eval: RegistryEval
    status: Literal["candidate", "promoted", "retired"]


# --- 5.3 Smoke transcript ------------------------------------------------------


class SmokeGeneration(_Strict):
    max_new_tokens: int = Field(gt=0)
    strategy: Literal["greedy"]


class SmokeSample(_Strict):
    record_id: RecordId
    # `to_chat_messages`'s own output (04-model-adapters.md §1), not a re-derivation
    # from the raw conversation -- this is the exact shape the model was prompted
    # with (02 §5.3).
    prompt_messages: list[dict[str, str]] = Field(min_length=1)
    reference: str
    base_output: str
    tuned_output: str


class SmokeTranscript(_Strict):
    run_id: str
    model_version: str
    generation: SmokeGeneration
    samples: list[SmokeSample]
