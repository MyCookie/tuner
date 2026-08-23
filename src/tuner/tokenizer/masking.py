"""Label masking via incremental-prefix tokenization (docs/03-components/tokenizer.md
core logic 6).

Generic over any tokenizer exposing `apply_chat_template(messages, tokenize=True,
add_generation_prompt=bool) -> list[int]` -- a real HF tokenizer's chat template, or a
stub built for the unit suite (TOK-U-010..014).
"""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np


class MaskingMismatch(Exception):
    """A prefix-property assertion failed while building labels for message index
    `turn_index`: re-tokenizing a longer prefix of the conversation did not extend the
    shorter prefix's own tokens, so the assistant span's boundaries can't be trusted.
    This is the adapter's chat template merging tokens across a turn boundary in a way
    the incremental-prefix method can't safely locate -- a data problem only in the
    sense that padding/formatting quirks in the record's text can trigger it, but the
    template interaction itself is what's broken (docs/03-components/tokenizer.md core
    logic 6). The caller drops the record with reason `masking_mismatch`."""

    def __init__(self, turn_index: int) -> None:
        self.turn_index = turn_index
        super().__init__(f"prefix property violated at message index {turn_index}")


class ChatTemplateTokenizer(Protocol):
    def apply_chat_template(
        self, messages: list[dict[str, Any]], *, tokenize: bool, add_generation_prompt: bool
    ) -> list[int]: ...


def build_labels(messages: list[dict[str, Any]], tokenizer: ChatTemplateTokenizer) -> list[int]:
    """`labels` copies `input_ids` (the full conversation's tokens) with every token
    outside an assistant turn's own span set to `-100` (loss-masked).

    For each assistant message at index `k`, the span is located by comparing three
    tokenizations: the full conversation, the prefix before `k` with the template's own
    generation-prompt appended (`ids_before` -- e.g. `<start_of_turn>model\\n`, which
    stays masked: the model must generate this, not be trained to reproduce a token it
    never has to predict as a *choice*), and the prefix through `k` rendered as a
    finished turn (`ids_with`). The assistant's content tokens *and* its end-of-turn
    token(s) -- the slice between those two prefixes' lengths -- are unmasked, so the
    model learns to stop.

    Two prefix-property assertions guard against tokenizer merge effects at turn
    boundaries changing token IDs near a boundary in a way that would silently shift
    the span: `ids_with` must literally start with all of `ids_before`, and the full
    sequence must literally start with all of `ids_with`. Either failing means the
    boundary can't be trusted, so this raises `MaskingMismatch(k)` (JDG has no
    equivalent -- this is genuinely new territory, since the Judge never re-tokenizes
    incrementally) rather than silently masking the wrong span (TOK-U-014)."""

    def tokenize_prefix(msgs: list[dict[str, Any]], generation_prompt: bool) -> list[int]:
        return tokenizer.apply_chat_template(
            msgs, tokenize=True, add_generation_prompt=generation_prompt
        )

    ids_full = tokenize_prefix(messages, False)
    labels = [-100] * len(ids_full)

    for k, message in enumerate(messages):
        if message["role"] != "assistant":
            continue

        ids_before = tokenize_prefix(messages[:k], True)
        ids_with = tokenize_prefix(messages[: k + 1], False)

        if ids_with[: len(ids_before)] != ids_before or ids_full[: len(ids_with)] != ids_with:
            raise MaskingMismatch(k)

        labels[len(ids_before) : len(ids_with)] = ids_full[len(ids_before) : len(ids_with)]

    return labels


def pad_and_stack(
    sequences: list[tuple[list[int], list[int]]], pad_token_id: int
) -> dict[str, np.ndarray]:
    """Pad every `(input_ids, labels)` pair to the longest `input_ids` in `sequences`
    with `pad_token_id` (`attention_mask` 0, `labels` `-100` for the padded tail);
    stack to int64 arrays (docs/03-components/tokenizer.md step 8, TOK-U-012). An empty
    `sequences` still returns well-shaped `(0, 0)` arrays -- a split having zero rows
    is a valid, tested outcome (TOK-I-027), not an error."""
    if not sequences:
        empty = np.zeros((0, 0), dtype=np.int64)
        return {"input_ids": empty, "attention_mask": empty.copy(), "labels": empty.copy()}

    max_len = max(len(ids) for ids, _ in sequences)
    n = len(sequences)
    input_ids = np.full((n, max_len), pad_token_id, dtype=np.int64)
    attention_mask = np.zeros((n, max_len), dtype=np.int64)
    labels = np.full((n, max_len), -100, dtype=np.int64)
    for i, (ids, lbls) in enumerate(sequences):
        length = len(ids)
        input_ids[i, :length] = ids
        attention_mask[i, :length] = 1
        labels[i, :length] = lbls
    return {"input_ids": input_ids, "attention_mask": attention_mask, "labels": labels}
