"""Unit tests for tuner.tokenizer.masking (TOK suite, docs/08-test-specs/tokenizer.md).

Uses a stub tokenizer with 1-token-per-word behavior, per the suite's own description --
`apply_chat_template` renders each turn as `<START_role> word word ... <END>`, plus a
trailing `<START_assistant>` when `add_generation_prompt=True`. Word/marker -> id
mappings are assigned on first sight and stay stable within one tokenizer instance, so
re-tokenizing a prefix always reproduces the same ids for that prefix (the "happy path"
naturally satisfies the prefix property `build_labels` depends on).
"""

from __future__ import annotations

import numpy as np
import pytest

from tuner.tokenizer.masking import MaskingMismatch, build_labels, pad_and_stack


class _StubTokenizer:
    def __init__(self) -> None:
        self._vocab: dict[str, int] = {}

    def _id(self, token: str) -> int:
        return self._vocab.setdefault(token, len(self._vocab))

    def apply_chat_template(
        self, messages: list[dict[str, str]], *, tokenize: bool, add_generation_prompt: bool
    ) -> list[int]:
        ids = []
        for message in messages:
            ids.append(self._id(f"<START_{message['role']}>"))
            ids.extend(self._id(word) for word in message["content"].split())
            ids.append(self._id("<END>"))
        if add_generation_prompt:
            ids.append(self._id("<START_assistant>"))
        return ids


class _MergingStubTokenizer(_StubTokenizer):
    """Deliberately violates the prefix property (TOK-U-014): the last token of every
    rendered sequence is replaced with a marker keyed on the sequence's own length, so
    it can never match the token a longer render (which includes this one as a true
    prefix) has at that same position -- simulating a real tokenizer's merge effect at
    a turn boundary."""

    def apply_chat_template(
        self, messages: list[dict[str, str]], *, tokenize: bool, add_generation_prompt: bool
    ) -> list[int]:
        ids = super().apply_chat_template(
            messages, tokenize=tokenize, add_generation_prompt=add_generation_prompt
        )
        if ids:
            ids[-1] = self._id(f"<merged-{len(ids)}>")
        return ids


def _assert_label_invariant(input_ids: list[int], labels: list[int]) -> None:
    """TOK-U-013's structural invariant: every label is either -100 or its input_ids
    value; at least one non-masked token."""
    assert len(labels) == len(input_ids)
    assert all(label in (-100, token) for label, token in zip(labels, input_ids, strict=True))
    assert any(label != -100 for label in labels)


def test_system_user_assistant_masks_everything_but_assistant():
    """TOK-U-010: handcrafted [system, user, assistant] -- system+user+template
    positions are -100; assistant positions equal input_ids, token by token."""
    tokenizer = _StubTokenizer()
    messages = [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "How do I reset my password"},
        {"role": "assistant", "content": "Go to Settings"},
    ]

    input_ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=False)
    labels = build_labels(messages, tokenizer)

    _assert_label_invariant(input_ids, labels)
    # The generation-prompt marker plus the assistant's own content and <END> are the
    # only unmasked positions -- everything before them (system+user turns) is -100.
    assistant_start = input_ids.index(tokenizer._id("<START_assistant>"))
    assert all(label == -100 for label in labels[:assistant_start])
    assert labels[assistant_start] == -100  # the generation-prompt token itself stays masked
    assert labels[assistant_start + 1 :] == input_ids[assistant_start + 1 :]


def test_multi_turn_both_assistant_spans_unmasked():
    """TOK-U-011: [user, assistant, user, assistant] -- both assistant spans unmasked,
    both user spans masked (the incremental-prefix method's key case: it must find the
    *second* assistant span too, not just the first)."""
    tokenizer = _StubTokenizer()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello there"},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "you are welcome"},
    ]

    input_ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=False)
    labels = build_labels(messages, tokenizer)

    _assert_label_invariant(input_ids, labels)
    unmasked_words = {
        input_ids[i]
        for i, label in enumerate(labels)
        if label != -100 and input_ids[i] not in (tokenizer._id("<START_assistant>"),)
    }
    for word in ("hello", "there", "you", "are", "welcome"):
        assert tokenizer._id(word) in unmasked_words
    for word in ("hi", "thanks"):
        word_index = input_ids.index(tokenizer._id(word))
        assert labels[word_index] == -100


def test_padding_tail_masked_and_zero_attention():
    """TOK-U-012: padding -- the padded tail has attention_mask 0 and labels -100."""
    pad_token_id = 999
    short = ([1, 2, 3], [-100, -100, 3])
    long = ([4, 5, 6, 7, 8], [-100, 6, 7, 8, -100])

    tensors = pad_and_stack([short, long], pad_token_id)

    assert tensors["input_ids"].shape == (2, 5)
    # The short sequence's padded tail (positions 3, 4):
    np.testing.assert_array_equal(tensors["input_ids"][0, 3:], [pad_token_id, pad_token_id])
    np.testing.assert_array_equal(tensors["attention_mask"][0, 3:], [0, 0])
    np.testing.assert_array_equal(tensors["labels"][0, 3:], [-100, -100])
    # The long sequence fills the whole row -- no padding, full attention.
    np.testing.assert_array_equal(tensors["attention_mask"][1], [1, 1, 1, 1, 1])
    assert tensors["input_ids"].dtype == np.int64
    assert tensors["attention_mask"].dtype == np.int64
    assert tensors["labels"].dtype == np.int64


def test_pad_and_stack_empty_sequences_returns_well_shaped_empty_arrays():
    """TOK-U-012 (empty-split branch, needed for TOK-I-027's eval.safetensors-with-0-rows
    case): an empty sequence list still returns (0, 0)-shaped arrays, not an error."""
    tensors = pad_and_stack([], pad_token_id=0)

    for key in ("input_ids", "attention_mask", "labels"):
        assert tensors[key].shape == (0, 0)
        assert tensors[key].dtype == np.int64


@pytest.mark.parametrize(
    "messages",
    [
        [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "How do I reset my password"},
            {"role": "assistant", "content": "Go to Settings"},
        ],
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
            {"role": "user", "content": "thanks"},
            {"role": "assistant", "content": "you are welcome"},
        ],
        [{"role": "user", "content": "just one turn"}, {"role": "assistant", "content": "ok"}],
    ],
    ids=["system-user-assistant", "multi-turn", "minimal"],
)
def test_label_invariant_holds_across_conversation_shapes(messages):
    """TOK-U-013: the structural invariant (every label is -100 or its input_ids value;
    >=1 non-masked token) holds across every conversation shape exercised here -- also
    run over every integration-suite output (TOK-I-024)."""
    tokenizer = _StubTokenizer()
    input_ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=False)
    labels = build_labels(messages, tokenizer)

    _assert_label_invariant(input_ids, labels)


def test_prefix_property_violation_raises_masking_mismatch():
    """TOK-U-014: a stub tokenizer engineered to violate the prefix property (merges a
    token across the turn boundary) raises MaskingMismatch naming the assistant
    message's index -- the caller drops the record `masking_mismatch`."""
    tokenizer = _MergingStubTokenizer()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello there"},
    ]

    with pytest.raises(MaskingMismatch) as exc_info:
        build_labels(messages, tokenizer)

    assert exc_info.value.turn_index == 1
