"""`tuner tokenize` (docs/03-components/tokenizer.md)."""

from __future__ import annotations

import sys
from typing import Any

import click
from pydantic import ValidationError
from safetensors.numpy import save as st_save

from tuner.core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from tuner.core.ids import validate_run_id_option
from tuner.core.manifest import UpstreamIncomplete, read_tier
from tuner.core.schemas import IndexMap, IndexMapDrop, IndexMapEntry, IndexMapSplits, validate_gold
from tuner.core.storage import StorageClient
from tuner.models.base import HFAuthError, ModelAdapter, UnsupportedModalityError
from tuner.models.registry import get_adapter
from tuner.tokenizer.masking import (
    ChatTemplateTokenizer,
    MaskingMismatch,
    build_labels,
    pad_and_stack,
)
from tuner.tokenizer.split import assign_split

GOLD_BUCKET = "tuner-gold"
ARTIFACTS_BUCKET = "tuner-artifacts"
STAGE = "tokenizer"
OVER_MAX_LEN_ABORT_FRACTION = 0.5
MASKING_MISMATCH_ABORT_FRACTION = 0.01

# One row while it's being assembled: the record it came from, its full token sequence,
# and that sequence's labels. Kept as a plain tuple (not a dataclass) since it never
# leaves this module.
_Row = tuple[str, list[int], list[int]]


class _PlainListTokenizer(ChatTemplateTokenizer):
    """Wraps a real HF tokenizer so `apply_chat_template` always returns a plain list
    of token ids -- current `transformers` defaults `tokenize=True` to
    `return_dict=True` (a dict-like `BatchEncoding`, not the plain list
    `build_labels`'s incremental-prefix slicing/equality checks are written against,
    and which `masking.py`'s own doc-quoted signature promises)."""

    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer

    def apply_chat_template(
        self, messages: list[dict[str, Any]], *, tokenize: bool, add_generation_prompt: bool
    ) -> list[int]:
        return self._tokenizer.apply_chat_template(
            messages,
            tokenize=tokenize,
            add_generation_prompt=add_generation_prompt,
            return_dict=False,
        )


def _process_records(
    gold_records: list[Any],
    adapter: ModelAdapter,
    tokenizer: ChatTemplateTokenizer,
    eval_fraction: float,
    max_seq_len: int,
) -> tuple[list[_Row], list[_Row], list[IndexMapDrop], int, int]:
    """One pass over validated Gold records: builds the train/eval row lists (in Gold
    file order -- no shuffling anywhere, core logic 4) and the dropped list. Returns
    `(train_rows, eval_rows, dropped, over_max_len_count, masking_mismatch_count)`;
    the two counts are returned separately from `dropped` so the caller can apply each
    abort threshold without re-scanning the list by reason."""
    train_rows: list[_Row] = []
    eval_rows: list[_Row] = []
    dropped: list[IndexMapDrop] = []
    over_max_len_count = 0
    masking_mismatch_count = 0

    for record in gold_records:
        record_id = record.id

        try:
            messages = adapter.to_chat_messages(record.conversation)
        except UnsupportedModalityError:
            dropped.append(IndexMapDrop(record_id=record_id, reason="unsupported_modality"))
            continue

        input_ids = tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=False
        )
        if len(input_ids) > max_seq_len:
            # No truncation in MVP -- truncation silently destroys assistant answers
            # (tokenizer.md step 7) -- so an over-length record is dropped whole,
            # before spending the extra incremental-prefix encode calls build_labels
            # needs. This ordering is a documented deviation from tokenizer.md's own
            # step numbering (which lists over_max_len after masking) -- see the
            # round-1-review note in docs/08-test-specs/tokenizer.md for what actually
            # changes for a record that would fail both checks.
            dropped.append(IndexMapDrop(record_id=record_id, reason="over_max_len"))
            over_max_len_count += 1
            continue

        try:
            labels = build_labels(messages, tokenizer)
        except MaskingMismatch:
            dropped.append(IndexMapDrop(record_id=record_id, reason="masking_mismatch"))
            masking_mismatch_count += 1
            continue

        row: _Row = (record_id, input_ids, labels)
        if assign_split(record_id, eval_fraction) == "eval":
            eval_rows.append(row)
        else:
            train_rows.append(row)

    return train_rows, eval_rows, dropped, over_max_len_count, masking_mismatch_count


def tokenize(
    run_id: str,
    config_path: str,
    storage: StorageClient | None = None,
    hf_tokenizer: Any = None,
) -> int:
    """Run the Tokenizer end to end; returns the process exit code (0/1/2/3).
    `hf_tokenizer` is injectable so tests can exercise failure paths (e.g. a mock
    tokenizer engineered to violate the masking prefix property, TOK-I-032) without a
    real HF download or a real broken chat template."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        click.echo(f"tokenize: {exc}", err=True)
        return 2

    try:
        adapter = get_adapter(config.model.adapter)
    except KeyError as exc:
        click.echo(f"tokenize: {exc}", err=True)
        return 2

    if hf_tokenizer is None:
        try:
            hf_tokenizer = adapter.load_tokenizer()
        except HFAuthError as exc:
            click.echo(f"tokenize: {exc}", err=True)
            return 2

    storage = storage or StorageClient()

    try:
        read_tier(storage, GOLD_BUCKET, run_id)
    except (UpstreamIncomplete, ValidationError) as exc:
        click.echo(f"tokenize: {exc}", err=True)
        return 2

    gold_records = []
    try:
        for raw in storage.read_jsonl(GOLD_BUCKET, f"{run_id}/"):
            try:
                gold_records.append(validate_gold(raw))
            except (ValidationError, ValueError) as exc:
                # ValueError, not just ValidationError: validate_gold raises a plain
                # ValueError for its own business-rule check (a non-null evaluation),
                # distinct from pydantic's schema-level ValidationError -- catching
                # only the latter let a null-evaluation record (the wrong tier
                # pointed at, TOK-I-025) fall through to the generic exit-1 handler
                # below instead of this exit-2 one.
                record_id = raw.get("id", "<unknown>")
                click.echo(f"tokenize: invalid Gold record {record_id}: {exc}", err=True)
                return 2
    except Exception as exc:  # unexpected mid-run failure (I/O, storage, ...) -> exit 1
        click.echo(f"tokenize: {exc}", err=True)
        return 1

    max_seq_len = (
        config.tokenize.max_seq_len
        if config.tokenize.max_seq_len is not None
        else adapter.max_seq_len
    )
    total_read = len(gold_records)

    try:
        # Delete the tokens/ prefix before writing anything new (idempotency, step 3) --
        # an aborted run should leave it definitively absent, not a stale copy.
        storage.delete_prefix(ARTIFACTS_BUCKET, f"{run_id}/tokens/")

        tokenizer = _PlainListTokenizer(hf_tokenizer)
        train_rows, eval_rows, dropped, over_max_len_count, masking_mismatch_count = (
            _process_records(
                gold_records, adapter, tokenizer, config.tokenize.eval_fraction, max_seq_len
            )
        )

        if total_read > 0 and over_max_len_count / total_read > OVER_MAX_LEN_ABORT_FRACTION:
            click.echo(
                f"tokenize: over_max_len rate {over_max_len_count}/{total_read} exceeds "
                f"{OVER_MAX_LEN_ABORT_FRACTION:.0%} -- consider raising tokenize.max_seq_len",
                err=True,
            )
            return 1
        if total_read > 0 and masking_mismatch_count / total_read > MASKING_MISMATCH_ABORT_FRACTION:
            click.echo(
                f"tokenize: masking_mismatch rate {masking_mismatch_count}/{total_read} "
                f"exceeds {MASKING_MISMATCH_ABORT_FRACTION:.0%} -- the adapter's template "
                "interaction is broken, not the data",
                err=True,
            )
            return 1
        if not train_rows:
            click.echo("tokenize: train split is empty", err=True)
            return 3

        pad_token_id = hf_tokenizer.pad_token_id
        if pad_token_id is None:
            pad_token_id = hf_tokenizer.eos_token_id
        train_tensors = pad_and_stack([(ids, lbls) for _, ids, lbls in train_rows], pad_token_id)
        eval_tensors = pad_and_stack([(ids, lbls) for _, ids, lbls in eval_rows], pad_token_id)

        storage.write_bytes(
            ARTIFACTS_BUCKET, f"{run_id}/tokens/train.safetensors", st_save(train_tensors)
        )
        storage.write_bytes(
            ARTIFACTS_BUCKET, f"{run_id}/tokens/eval.safetensors", st_save(eval_tensors)
        )

        index_map = IndexMap(
            run_id=run_id,
            adapter=adapter.name,
            tokenizer_id=adapter.hf_model_id,
            max_seq_len=max_seq_len,
            gold_manifest_uri=f"s3://{GOLD_BUCKET}/{run_id}/manifest.json",
            splits=IndexMapSplits(
                train=[
                    IndexMapEntry(row=i, record_id=record_id)
                    for i, (record_id, _, _) in enumerate(train_rows)
                ],
                eval=[
                    IndexMapEntry(row=i, record_id=record_id)
                    for i, (record_id, _, _) in enumerate(eval_rows)
                ],
            ),
            dropped=dropped,
        )
        storage.write_json(
            ARTIFACTS_BUCKET, f"{run_id}/tokens/index_map.json", index_map.model_dump(mode="json")
        )
    except Exception as exc:  # unexpected mid-run failure (I/O, storage, ...) -> exit 1
        click.echo(f"tokenize: {exc}", err=True)
        return 1

    return 0


@click.command(name="tokenize")
@click.option(
    "--run-id",
    required=True,
    callback=validate_run_id_option,
    help="Run ID shared across the pipeline.",
)
@click.option(
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG_PATH),
    show_default=True,
    help="Pipeline config path.",
)
def tokenize_command(run_id: str, config_path: str) -> None:
    """Map Gold records to the target model's vocabulary; write SafeTensors + index_map."""
    sys.exit(tokenize(run_id, config_path))
