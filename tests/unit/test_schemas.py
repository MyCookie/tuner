"""Unit tests for tuner.core.schemas (CORE suite, docs/08-test-specs/core.md)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tuner.core import schemas
from tuner.core.ids import canonical_hash

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures_schemas"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _copy(name: str) -> dict:
    return copy.deepcopy(_load(name))


DOC02_CASES = [
    ("bronze.json", schemas.BronzeRecord),
    ("silver.json", schemas.SilverGoldRecord),
    ("gold.json", schemas.SilverGoldRecord),
    ("tier_manifest.json", schemas.TierManifest),
    ("index_map.json", schemas.IndexMap),
    ("registry_manifest.json", schemas.RegistryManifest),
]


@pytest.mark.parametrize("filename,model_cls", DOC02_CASES, ids=[c[0] for c in DOC02_CASES])
def test_doc02_examples_validate_unchanged(filename, model_cls):
    """CORE-U-020: each doc-02 example validates and round-trips unchanged."""
    data = _load(filename)

    record = model_cls.model_validate(data)

    assert record.model_dump(mode="json") == data


def _bronze_missing_id():
    data = _copy("bronze.json")
    del data["id"]
    return data


def _bronze_bad_source_type():
    data = _copy("bronze.json")
    data["source"]["type"] = "xml"
    return data


def _bronze_malformed_content_hash():
    data = _copy("bronze.json")
    data["content_hash"] = "not-a-hash"
    return data


def _bronze_non_object_raw():
    data = _copy("bronze.json")
    data["raw"] = "not-an-object"
    return data


def _bronze_id_not_a_uuid():
    data = _copy("bronze.json")
    data["id"] = "not-a-uuid"
    return data


def _bronze_id_uuid_wrong_version():
    data = _copy("bronze.json")
    data["id"] = "e932515d-1f15-1f90-8def-01cbbc736b02"  # version nibble 1, not 4
    return data


BRONZE_MUTATIONS = [
    _bronze_missing_id,
    _bronze_bad_source_type,
    _bronze_malformed_content_hash,
    _bronze_non_object_raw,
    _bronze_id_not_a_uuid,
    _bronze_id_uuid_wrong_version,
]


@pytest.mark.parametrize("mutate", BRONZE_MUTATIONS, ids=[f.__name__ for f in BRONZE_MUTATIONS])
def test_bronze_mutations_rejected(mutate):
    """CORE-U-021: Bronze mutations (missing id, bad type, bad hash, non-object raw) rejected."""
    with pytest.raises(ValidationError):
        schemas.BronzeRecord.model_validate(mutate())


def _flat_string_content():
    data = _copy("silver.json")
    data["conversation"][1]["content"] = "How do I reset my password?"
    return data


def _empty_content_array():
    data = _copy("silver.json")
    data["conversation"][1]["content"] = []
    return data


def _system_not_first():
    data = _copy("silver.json")
    turns = data["conversation"]
    turns[0], turns[1] = turns[1], turns[0]
    return data


def _two_system_turns():
    data = _copy("silver.json")
    data["conversation"].insert(
        1, {"role": "system", "content": [{"type": "text", "value": "Another system turn."}]}
    )
    return data


def _no_user_turn():
    data = _copy("silver.json")
    data["conversation"] = [data["conversation"][0], data["conversation"][2]]
    return data


def _no_assistant_turn():
    data = _copy("silver.json")
    data["conversation"] = [data["conversation"][1], data["conversation"][1]]
    return data


def _last_turn_not_assistant():
    data = _copy("silver.json")
    system, user, assistant = data["conversation"]
    data["conversation"] = [system, assistant, user]
    return data


def _empty_after_trim_text_value():
    data = _copy("silver.json")
    data["conversation"][1]["content"][0]["value"] = "   "
    return data


def _unknown_content_type():
    data = _copy("silver.json")
    data["conversation"][1]["content"][0]["type"] = "video"
    return data


CONVERSATION_MUTATIONS = [
    _flat_string_content,
    _empty_content_array,
    _system_not_first,
    _two_system_turns,
    _no_user_turn,
    _no_assistant_turn,
    _last_turn_not_assistant,
    _empty_after_trim_text_value,
    _unknown_content_type,
]


@pytest.mark.parametrize(
    "mutate", CONVERSATION_MUTATIONS, ids=[f.__name__ for f in CONVERSATION_MUTATIONS]
)
def test_conversation_rules_rejected(mutate):
    """CORE-U-022: each conversation-shape mutation is rejected."""
    with pytest.raises(ValidationError):
        schemas.SilverGoldRecord.model_validate(mutate())


@pytest.mark.parametrize("score", [-0.1, 1.1], ids=["score-too-low", "score-too-high"])
def test_evaluation_score_out_of_range_rejected(score):
    """CORE-U-023: evaluation.score outside [0.0, 1.0] is rejected."""
    data = _copy("gold.json")
    data["evaluation"]["score"] = score

    with pytest.raises(ValidationError):
        schemas.SilverGoldRecord.model_validate(data)


def test_evaluation_missing_judge_model_rejected():
    """CORE-U-023: a non-null evaluation missing judge_model is rejected."""
    data = _copy("gold.json")
    del data["evaluation"]["judge_model"]

    with pytest.raises(ValidationError):
        schemas.SilverGoldRecord.model_validate(data)


def test_validate_gold_rejects_null_evaluation():
    """CORE-U-024: validate_gold(record) rejects a record with evaluation: null."""
    data = _copy("silver.json")

    with pytest.raises(ValueError, match="non-null evaluation"):
        schemas.validate_gold(data)


def test_validate_gold_accepts_non_null_evaluation():
    """CORE-U-024: validate_gold(record) accepts a record with a populated evaluation."""
    record = schemas.validate_gold(_copy("gold.json"))

    assert record.evaluation is not None


def test_tier_manifest_bad_counts_rejected():
    """CORE-U-025: counts.read != written + dropped is rejected."""
    data = _copy("tier_manifest.json")
    data["counts"]["read"] = 999

    with pytest.raises(ValidationError):
        schemas.TierManifest.model_validate(data)


def test_tier_manifest_unknown_drop_reason_rejected():
    """CORE-U-025: an unknown drop reason for the producing stage is rejected."""
    manifest = schemas.TierManifest.model_validate(_load("tier_manifest.json"))

    with pytest.raises(ValueError, match="unknown drop reason"):
        schemas.validate_manifest_drops(manifest, allowed_reasons={"too_short", "duplicate"})


def test_tier_manifest_known_drop_reasons_accepted():
    """CORE-U-025: drop reasons that are all known to the producing stage are accepted."""
    manifest = schemas.TierManifest.model_validate(_load("tier_manifest.json"))
    allowed = {"too_short", "duplicate", "unmappable"}

    result = schemas.validate_manifest_drops(manifest, allowed_reasons=allowed)

    assert result is manifest


@pytest.mark.parametrize(
    "bad_timestamp",
    ["2026-07-20T14:22:05", "2026-07-20T14:22:05+00:00", "2026-07-20 14:22:05Z"],
    ids=["no-z-suffix", "offset-instead-of-z", "space-separator"],
)
def test_timestamp_requires_utc_z_suffix(bad_timestamp):
    """CORE-U-026: timestamps without a Z suffix or with a non-UTC offset are rejected."""
    data = _copy("bronze.json")
    data["source"]["ingested_at"] = bad_timestamp

    with pytest.raises(ValidationError):
        schemas.BronzeRecord.model_validate(data)


def test_registry_manifest_bad_status_rejected():
    """CORE-U-027: registry manifest status outside candidate|promoted|retired is rejected."""
    data = _copy("registry_manifest.json")
    data["status"] = "archived"

    with pytest.raises(ValidationError):
        schemas.RegistryManifest.model_validate(data)


def _make_turn(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "value": text}]}


_ARBITRARY_TEXT = st.text(min_size=1, max_size=30).map(lambda s: s.strip() or "x")


@st.composite
def _silver_gold_records(draw):
    include_system = draw(st.booleans())
    pairs = draw(st.integers(min_value=1, max_value=4))
    n_texts = pairs * 2 + (1 if include_system else 0)
    texts = draw(st.lists(_ARBITRARY_TEXT, min_size=n_texts, max_size=n_texts))

    turns = []
    idx = 0
    if include_system:
        turns.append(_make_turn("system", texts[idx]))
        idx += 1
    for _ in range(pairs):
        turns.append(_make_turn("user", texts[idx]))
        idx += 1
        turns.append(_make_turn("assistant", texts[idx]))
        idx += 1

    return {
        "id": "e932515d-1f15-4f90-8def-01cbbc736b02",
        "run_id": "run-20260720-142201-a3f9c2",
        "lineage": {"bronze_content_hash": f"sha256:{'a' * 64}", "cleaner_version": "0.1.0"},
        "conversation": turns,
        "evaluation": None,
    }


_DROP_REASONS = st.sampled_from(["too_short", "duplicate", "unmappable"])


@st.composite
def _tier_manifests(draw):
    n_drops = draw(st.integers(min_value=0, max_value=5))
    counts_strategy = st.integers(min_value=0, max_value=10)
    reasons = draw(st.lists(_DROP_REASONS, min_size=n_drops, max_size=n_drops))
    counts = draw(st.lists(counts_strategy, min_size=n_drops, max_size=n_drops))
    drops = [{"reason": r, "count": c} for r, c in zip(reasons, counts, strict=True)]
    written = draw(st.integers(min_value=0, max_value=100))
    dropped = draw(st.integers(min_value=0, max_value=50))
    manifest_uri = "s3://tuner-bronze/run-20260720-142201-a3f9c2/manifest.json"

    return {
        "tier": "silver",
        "run_id": "run-20260720-142201-a3f9c2",
        "created_at": "2026-07-20T14:25:33Z",
        "producer": {"stage": "cleaner", "version": "0.1.0"},
        "input": {"tier": "bronze", "manifest_uri": manifest_uri},
        "files": ["records-00000.jsonl"],
        "records_hash": f"sha256:{'b' * 64}",
        "counts": {"read": written + dropped, "written": written, "dropped": dropped},
        "drops": drops,
    }


@given(st.one_of(_silver_gold_records(), _tier_manifests()))
def test_schema_round_trip_and_canonical_hash_stable(data):
    """CORE-U-050: generated records round-trip model->dict->JSON->model, canonical_hash stable."""
    model_cls = schemas.SilverGoldRecord if "conversation" in data else schemas.TierManifest

    record = model_cls.model_validate(data)
    dumped = record.model_dump(mode="json")
    reloaded = model_cls.model_validate(json.loads(json.dumps(dumped)))

    assert reloaded.model_dump(mode="json") == dumped
    assert canonical_hash(dumped) == canonical_hash(json.loads(json.dumps(dumped)))
