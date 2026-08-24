"""Unit tests for tuner.tokenizer.split (TOK suite, docs/08-test-specs/tokenizer.md)."""

from __future__ import annotations

import uuid

from tuner.tokenizer.split import assign_split

# Deterministic (not random) record IDs -- UUIDv5 over a fixed namespace+name, so
# anyone re-deriving them gets the same values `uuid.uuid5(uuid.NAMESPACE_URL,
# f"tuner-test-record-{i}")` for i in (0, 1, 2, 8, 18).
_FIXED_RECORD_IDS = [
    "86e7b8c7-a612-5e5e-9d72-e83d7551043c",
    "0bbc4916-a5ee-545e-aa90-7b6cb96bdb8b",
    "d73b2630-957b-5ef7-93ba-8ffa0f4f1cbf",
    "bac7f6df-792c-54bc-baa2-322c1762ef66",
    "88924b37-19f5-5dc6-90ee-1782368a1d16",
]


def test_split_of_five_fixed_ids_pinned():
    """TOK-U-001: split of 5 fixed record IDs at eval_fraction 0.1 -- assignments equal
    constants computed once at implementation time and hard-coded here (the
    cross-machine stability guarantee: this test fails if the split algorithm or its
    hash function ever changes)."""
    assignments = [assign_split(record_id, 0.1) for record_id in _FIXED_RECORD_IDS]

    assert assignments == ["train", "train", "train", "eval", "eval"]


def test_eval_fraction_zero_and_one_are_all_train_all_eval():
    """TOK-U-002: eval_fraction 0.0 -> all train; eval_fraction 1.0 -> all eval."""
    record_ids = _FIXED_RECORD_IDS + [str(uuid.uuid4()) for _ in range(20)]

    assert all(assign_split(record_id, 0.0) == "train" for record_id in record_ids)
    assert all(assign_split(record_id, 1.0) == "eval" for record_id in record_ids)


def test_thousand_synthetic_ids_eval_share_within_bounds_and_reproducible():
    """TOK-U-003: 1,000 synthetic UUIDs at eval_fraction 0.1 -- eval share within
    [0.05, 0.15]; the same call twice produces identical sets (no hidden randomness)."""
    record_ids = [str(uuid.uuid4()) for _ in range(1000)]

    eval_set_a = {rid for rid in record_ids if assign_split(rid, 0.1) == "eval"}
    eval_set_b = {rid for rid in record_ids if assign_split(rid, 0.1) == "eval"}

    assert 50 <= len(eval_set_a) <= 150
    assert eval_set_a == eval_set_b


def test_split_independent_of_record_order():
    """TOK-U-004: split is independent of record order -- shuffling the input produces
    the same per-record assignments."""
    record_ids = [str(uuid.uuid4()) for _ in range(50)]
    original = {rid: assign_split(rid, 0.1) for rid in record_ids}

    shuffled = list(reversed(record_ids))
    reshuffled = {rid: assign_split(rid, 0.1) for rid in shuffled}

    assert original == reshuffled
