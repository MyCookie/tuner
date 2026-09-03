"""Integration tests for tuner.tokenizer (TOK suite, docs/spec/08-test-specs/tokenizer.md).

Needs compose MinIO up: `docker compose up -d minio minio-init` then
`uv run pytest -m integration tests/integration/test_tokenizer.py`. Uses the real
`tiny-test` adapter (HuggingFaceTB/SmolLM2-135M-Instruct) -- these tests need real
network access (or a warm local HF cache) the first time they run; no mocking, per
docs/spec/06-testing.md §5's point of that adapter existing at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from safetensors.numpy import load as st_load

from tuner.core.ids import canonical_hash, new_record_id
from tuner.core.storage import StorageClient
from tuner.models.tiny_test import TinyTestAdapter
from tuner.tokenizer.cli import _PlainListTokenizer, tokenize
from tuner.tokenizer.masking import build_labels
from tuner.tokenizer.split import assign_split

GOLD_BUCKET = "tuner-gold"
ARTIFACTS_BUCKET = "tuner-artifacts"

# Two real UUIDv4s independently confirmed (at implementation time) to both hash to
# "train" at eval_fraction 0.1 -- TOK-I-027 needs a *guaranteed* empty eval split, not
# a probable one.
_BOTH_HASH_TO_TRAIN = [
    "c168059d-1403-4b9e-8c1c-924d64ff6daa",
    "2880e8da-ec9d-49f4-a35b-4979273783da",
]


def _write_config(
    tmp_path: Path, *, max_seq_len: int | None = None, eval_fraction: float = 0.1
) -> Path:
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model": {"adapter": "tiny-test"},
                "ingest": {"sources": []},
                "tokenize": {"max_seq_len": max_seq_len, "eval_fraction": eval_fraction},
            }
        )
    )
    return path


_DEFAULT_EVALUATION = {
    "score": 0.9,
    "judge_model": "mock-judge",
    "reasoning": "fine",
    "evaluated_at": "2026-07-20T14:31:10Z",
}
_UNSET = object()


def _gold_record(
    run_id: str,
    question: str,
    answer: str,
    *,
    record_id: str | None = None,
    system: str | None = None,
    image: bool = False,
    evaluation: dict | None = _UNSET,  # type: ignore[assignment]
) -> dict:
    user_content: list[dict[str, str]] = [{"type": "text", "value": question}]
    if image:
        user_content.append({"type": "image", "value": "s3://tuner-assets/run-x/img-001.jpg"})

    conversation = []
    if system:
        conversation.append({"role": "system", "content": [{"type": "text", "value": system}]})
    conversation.append({"role": "user", "content": user_content})
    conversation.append({"role": "assistant", "content": [{"type": "text", "value": answer}]})

    if evaluation is _UNSET:
        evaluation = _DEFAULT_EVALUATION

    return {
        "id": record_id or new_record_id(),
        "run_id": run_id,
        "lineage": {"bronze_content_hash": f"sha256:{'0' * 64}", "cleaner_version": "0.1.0"},
        "conversation": conversation,
        "evaluation": evaluation,
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


def _cleanup(storage: StorageClient, run_id: str) -> None:
    storage.delete_prefix(GOLD_BUCKET, f"{run_id}/")
    storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/")


def _index_map(storage: StorageClient, run_id: str) -> dict | None:
    return storage.read_json(ARTIFACTS_BUCKET, f"{run_id}/tokens/index_map.json")


def _tensors(storage: StorageClient, run_id: str, split: str) -> dict[str, Any] | None:
    data = storage.read_bytes(ARTIFACTS_BUCKET, f"{run_id}/tokens/{split}.safetensors")
    return None if data is None else st_load(data)


def _assert_label_invariant(input_ids, labels) -> None:
    assert len(labels) == len(input_ids)
    for label, token in zip(labels, input_ids, strict=True):
        assert label == -100 or label == token
    assert any(label != -100 for label in labels)


@pytest.mark.integration
def test_tokenize_counts_reconcile_and_tensors_well_shaped(storage, run_id, tmp_path):
    """TOK-I-020: seeded 20-record Gold -> n_train + n_eval + len(dropped) = 20;
    SafeTensors load with int64 dtype, shapes [n, seq] consistent across the 3 keys."""
    records = [_gold_record(run_id, f"Q{i}", f"A{i}") for i in range(20)]
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path)

    try:
        exit_code = tokenize(run_id, str(config_path), storage=storage)
        assert exit_code == 0

        index_map = _index_map(storage, run_id)
        n_train = len(index_map["splits"]["train"])
        n_eval = len(index_map["splits"]["eval"])
        assert n_train + n_eval + len(index_map["dropped"]) == 20

        for split, n in (("train", n_train), ("eval", n_eval)):
            tensors = _tensors(storage, run_id, split)
            for key in ("input_ids", "attention_mask", "labels"):
                assert tensors[key].dtype.name == "int64"
            shapes = {tensors[key].shape for key in ("input_ids", "attention_mask", "labels")}
            assert len(shapes) == 1
            assert next(iter(shapes))[0] == n
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_index_map_contents(storage, run_id, tmp_path):
    """TOK-I-021: adapter name, tokenizer_id, effective max_seq_len, Gold manifest URI
    correct; every row-mapped record_id exists in Gold; rows are dense 0..n-1 per split."""
    records = [_gold_record(run_id, f"Q{i}", f"A{i}") for i in range(10)]
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path, max_seq_len=512)

    try:
        assert tokenize(run_id, str(config_path), storage=storage) == 0

        index_map = _index_map(storage, run_id)
        assert index_map["adapter"] == "tiny-test"
        assert index_map["tokenizer_id"] == TinyTestAdapter.hf_model_id
        assert index_map["max_seq_len"] == 512
        assert index_map["gold_manifest_uri"] == f"s3://{GOLD_BUCKET}/{run_id}/manifest.json"

        gold_ids = {r["id"] for r in records}
        for split in ("train", "eval"):
            entries = index_map["splits"][split]
            assert {e["record_id"] for e in entries} <= gold_ids
            assert sorted(e["row"] for e in entries) == list(range(len(entries)))
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_over_max_len_record_dropped_not_in_any_tensor(storage, run_id, tmp_path):
    """TOK-I-022: one seeded record exceeding max_seq_len is dropped `over_max_len`,
    and not in any tensor."""
    long_record_id = new_record_id()
    records = [_gold_record(run_id, f"Q{i}", f"A{i}") for i in range(9)]
    records.append(
        _gold_record(
            run_id, "Q", " ".join(f"word{i}" for i in range(200)), record_id=long_record_id
        )
    )
    _seed_gold(storage, run_id, records)
    # 50, not something tighter: SmolLM2's chat template auto-injects a ~30-token
    # default system message when a conversation has no system turn of its own, so
    # even the short Q/A records here already cost ~36 tokens before any content.
    config_path = _write_config(tmp_path, max_seq_len=50)

    try:
        assert tokenize(run_id, str(config_path), storage=storage) == 0

        index_map = _index_map(storage, run_id)
        drop_reasons = {d["record_id"]: d["reason"] for d in index_map["dropped"]}
        assert drop_reasons.get(long_record_id) == "over_max_len"
        mapped_ids = {
            e["record_id"] for split in ("train", "eval") for e in index_map["splits"][split]
        }
        assert long_record_id not in mapped_ids
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_image_part_dropped_unsupported_modality(storage, run_id, tmp_path):
    """TOK-I-023: a seeded record with an `image` part (text-only adapter) is dropped
    `unsupported_modality`."""
    image_record_id = new_record_id()
    records = [_gold_record(run_id, f"Q{i}", f"A{i}") for i in range(9)]
    records.append(_gold_record(run_id, "Q", "A", record_id=image_record_id, image=True))
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path)

    try:
        assert tokenize(run_id, str(config_path), storage=storage) == 0

        index_map = _index_map(storage, run_id)
        drop_reasons = {d["record_id"]: d["reason"] for d in index_map["dropped"]}
        assert drop_reasons.get(image_record_id) == "unsupported_modality"
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_assistant_first_record_dropped_not_run_aborted(storage, run_id, tmp_path):
    """TOK-I-034: a contract-valid Gold record whose first turn is `assistant`
    (`SilverGoldRecord` only requires >=1 user and >=1 assistant turn, last must be
    assistant -- nothing pins the *first* turn to user/system) is dropped
    `masking_mismatch`, not left to crash the whole run with the real tokenizer's own
    "empty conversation" error (PR #10 review round 1 finding 1 -- reproduced here
    against real MinIO + the real tiny-test adapter, matching how the reviewer found
    it)."""
    assistant_first_id = new_record_id()
    assistant_first_record = {
        "id": assistant_first_id,
        "run_id": run_id,
        "lineage": {"bronze_content_hash": f"sha256:{'0' * 64}", "cleaner_version": "0.1.0"},
        "conversation": [
            {"role": "assistant", "content": [{"type": "text", "value": "Hi there"}]},
            {"role": "user", "content": [{"type": "text", "value": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "value": "ok"}]},
        ],
        "evaluation": _DEFAULT_EVALUATION,
    }
    # 101 clean records, not 9: the masking_mismatch abort threshold (TOK-I-032) is
    # >1% -- 1/10 would trip it, masking the very drop this test wants to observe.
    # 1/102 (~0.98%) stays under it.
    records = [_gold_record(run_id, f"Q{i}", f"A{i}") for i in range(101)] + [
        assistant_first_record
    ]
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path)

    try:
        exit_code = tokenize(run_id, str(config_path), storage=storage)
        assert exit_code == 0

        index_map = _index_map(storage, run_id)
        drop_reasons = {d["record_id"]: d["reason"] for d in index_map["dropped"]}
        assert drop_reasons.get(assistant_first_id) == "masking_mismatch"
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_full_output_label_invariant_sweep(storage, run_id, tmp_path):
    """TOK-I-024: TOK-U-013's invariant (every label is -100 or its input_ids value;
    >=1 non-masked token) holds for every row of both splits, checked against the
    actual written SafeTensors output."""
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", system="Be helpful." if i % 3 == 0 else None)
        for i in range(15)
    ]
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path)

    try:
        assert tokenize(run_id, str(config_path), storage=storage) == 0

        for split in ("train", "eval"):
            tensors = _tensors(storage, run_id, split)
            for row in range(tensors["input_ids"].shape[0]):
                _assert_label_invariant(tensors["input_ids"][row], tensors["labels"][row])
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_null_evaluation_gold_record_exits_2(storage, run_id, tmp_path):
    """TOK-I-025: a Gold record seeded with evaluation: null exits 2 (CORE-U-024 wired
    in -- means a non-Gold tier was pointed at)."""
    records = [_gold_record(run_id, "Q", "A", evaluation=None)]
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path)

    try:
        assert tokenize(run_id, str(config_path), storage=storage) == 2
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_majority_over_max_len_aborts_exit_1(storage, run_id, tmp_path):
    """TOK-I-026: >50% of records over max_seq_len (a tiny max injected) exits 1 with
    the raise-max_seq_len message."""
    records = [_gold_record(run_id, f"Q{i}", f"A{i}") for i in range(10)]
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path, max_seq_len=1)

    try:
        assert tokenize(run_id, str(config_path), storage=storage) == 1
        assert _index_map(storage, run_id) is None  # aborted -- nothing committed
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_train_split_entirely_empty_exits_3(storage, run_id, tmp_path):
    """TOK-I-033: eval_fraction 1.0 sends every record to eval -- the train split ends
    up empty, which the CLI section of tokenizer.md calls out explicitly ("Exit 3 if
    the train split ends up empty")."""
    records = [_gold_record(run_id, f"Q{i}", f"A{i}") for i in range(3)]
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path, eval_fraction=1.0)

    try:
        assert tokenize(run_id, str(config_path), storage=storage) == 3
        assert _index_map(storage, run_id) is None  # aborted -- nothing committed
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_eval_split_empties_proceeds_with_zero_row_eval_tensor(storage, run_id, tmp_path):
    """TOK-I-027: 2 records, both hashing to train at eval_fraction 0.1 -- proceeds;
    eval.safetensors is present with 0 rows (SMK-I-007 covers the downstream exit-3)."""
    records = [
        _gold_record(run_id, f"Q{i}", f"A{i}", record_id=record_id)
        for i, record_id in enumerate(_BOTH_HASH_TO_TRAIN)
    ]
    assert all(assign_split(r["id"], 0.1) == "train" for r in records)  # guard the premise
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path)

    try:
        assert tokenize(run_id, str(config_path), storage=storage) == 0

        eval_tensors = _tensors(storage, run_id, "eval")
        assert eval_tensors is not None
        assert eval_tensors["input_ids"].shape[0] == 0
        index_map = _index_map(storage, run_id)
        assert index_map["splits"]["eval"] == []
        assert len(index_map["splits"]["train"]) == 2
    finally:
        _cleanup(storage, run_id)


class _SpyStorage:
    """Wraps a real StorageClient, recording call order for write_bytes/write_json --
    everything else passes straight through (TOK-I-028, mirrors CORE-I-032)."""

    def __init__(self, real: StorageClient) -> None:
        self._real = real
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        attr = getattr(self._real, name)
        if name not in ("write_bytes", "write_json"):
            return attr

        def _spied(*args, **kwargs):
            self.calls.append(name)
            return attr(*args, **kwargs)

        return _spied


@pytest.mark.integration
def test_rerun_rebuilds_tokens_prefix_index_map_written_last(storage, run_id, tmp_path):
    """TOK-I-028: re-run of the same run ID rebuilds tokens/ cleanly; index_map.json is
    written after both SafeTensors shards (spy-order assert, mirrors CORE-I-032)."""
    records = [_gold_record(run_id, f"Q{i}", f"A{i}") for i in range(5)]
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path)

    try:
        assert tokenize(run_id, str(config_path), storage=storage) == 0

        # Plant a stale object under tokens/ that only a real delete_prefix removes.
        storage.write_bytes(ARTIFACTS_BUCKET, f"{run_id}/tokens/stale.txt", b"stale")

        spy = _SpyStorage(storage)
        assert tokenize(run_id, str(config_path), storage=spy) == 0

        assert storage.read_bytes(ARTIFACTS_BUCKET, f"{run_id}/tokens/stale.txt") is None
        assert spy.calls == ["write_bytes", "write_bytes", "write_json"]
        assert _index_map(storage, run_id) is not None
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_unknown_adapter_exits_2(storage, run_id, tmp_path):
    """TOK-I-029: unknown model.adapter in config exits 2 via the ADP-U-011 path."""
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(
        yaml.safe_dump({"model": {"adapter": "nope"}, "ingest": {"sources": []}})
    )

    assert tokenize(run_id, str(config_path), storage=storage) == 2


@pytest.mark.integration
def test_missing_gold_manifest_exits_2(storage, run_id, tmp_path):
    """TOK-I-031: a missing Gold manifest exits 2 (upstream incomplete) -- mirrors
    ING-I-015/CLN-I-035/JDG-I-030, the same "no manifest, no commit" check every
    stage's own suite already has, added here as the gap was noticed against the
    sibling stages rather than left as an untested code path with no case to justify
    it (docs/spec/08-test-specs/tokenizer.md didn't have this case; the others do)."""
    config_path = _write_config(tmp_path)

    assert tokenize(run_id, str(config_path), storage=storage) == 2


class _AlwaysMismatchTokenizer:
    """Deterministically violates the masking prefix property for every assistant
    turn (TOK-I-032) -- the same technique as tests/unit/test_masking.py's
    `_MergingStubTokenizer`, adapted to accept the `return_dict` kwarg
    `_PlainListTokenizer` always passes through."""

    pad_token_id = 0
    eos_token_id = 0

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        return_dict: bool = False,
    ) -> list[int]:
        ids: list[int] = []
        for message in messages:
            ids.append(1)
            ids.extend(range(2, 2 + len(message["content"].split())))
            ids.append(1)
        if add_generation_prompt:
            ids.append(1)
        if ids:
            ids[-1] = 1000 + len(ids)  # corrupt the last token, keyed on length
        return ids


@pytest.mark.integration
def test_majority_masking_mismatch_aborts_exit_1(storage, run_id, tmp_path):
    """TOK-I-032: masking_mismatch rate > 1% (a mock tokenizer engineered to violate
    the prefix property for every record) exits 1 with the corresponding message --
    the CLI-level companion to TOK-U-014's build_labels-level test."""
    records = [_gold_record(run_id, f"Q{i}", f"A{i}") for i in range(2)]
    _seed_gold(storage, run_id, records)
    config_path = _write_config(tmp_path)

    try:
        exit_code = tokenize(
            run_id, str(config_path), storage=storage, hf_tokenizer=_AlwaysMismatchTokenizer()
        )
        assert exit_code == 1
        assert _index_map(storage, run_id) is None  # aborted -- nothing committed
    finally:
        _cleanup(storage, run_id)


@pytest.mark.integration
def test_real_tokenizer_masking_prefix_properties_hold():
    """TOK-I-030: the real tiny-test HF tokenizer, exercised directly (not the stub
    suite): prefix properties hold for every fixture conversation (zero
    MaskingMismatch), every assistant span is non-empty, decoding the unmasked
    positions yields the assistant text (+ end-of-turn token), and every
    generation-prompt position is -100 -- the stub-tokenizer suite can't catch real
    boundary-merge effects; this can."""
    adapter = TinyTestAdapter()
    hf_tokenizer = adapter.load_tokenizer()
    tokenizer = _PlainListTokenizer(hf_tokenizer)

    conversations = [
        [
            {"role": "user", "content": "How do I reset my password?"},
            {"role": "assistant", "content": "Go to Settings and click Security."},
        ],
        [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "What's 2+2?"},
            {"role": "assistant", "content": "4."},
        ],
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there!"},
            {"role": "user", "content": "thanks, bye"},
            {"role": "assistant", "content": "you're welcome, goodbye!"},
        ],
    ]

    for messages in conversations:
        input_ids = tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=False
        )
        labels = build_labels(messages, tokenizer)  # raises MaskingMismatch on failure

        _assert_label_invariant(input_ids, labels)

        for k, message in enumerate(messages):
            if message["role"] != "assistant":
                continue
            # Four tokenizations pin down *this* turn's own boundaries exactly, even
            # when it isn't the conversation's only (or last) assistant turn:
            # ids_before_plain -> everything strictly before this turn; ids_before_gen
            # adds the template's own generation-prompt tokens for it; ids_with is the
            # prefix through this turn, finished. The generation-prompt delta and the
            # turn's own content+end-of-turn delta are two disjoint, adjacent spans.
            ids_before_plain = tokenizer.apply_chat_template(
                messages[:k], tokenize=True, add_generation_prompt=False
            )
            ids_before_gen = tokenizer.apply_chat_template(
                messages[:k], tokenize=True, add_generation_prompt=True
            )
            ids_with = tokenizer.apply_chat_template(
                messages[: k + 1], tokenize=True, add_generation_prompt=False
            )

            gen_prompt_span = range(len(ids_before_plain), len(ids_before_gen))
            assert all(labels[i] == -100 for i in gen_prompt_span)

            assistant_span = range(len(ids_before_gen), len(ids_with))
            assert len(assistant_span) > 0
            assert all(labels[i] == input_ids[i] for i in assistant_span)
            decoded = hf_tokenizer.decode(
                [input_ids[i] for i in assistant_span], skip_special_tokens=False
            )
            assert message["content"] in decoded
