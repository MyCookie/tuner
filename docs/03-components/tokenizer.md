# Component Spec: Tokenizer

**Purpose:** Map Gold records to the target model's vocabulary, producing SafeTensors tensors and an `index_map` for row→record traceability (SAS §3.1). All model specifics come from the selected [model adapter](../04-model-adapters.md).

## CLI

```
eftp tokenize --run-id <RUN_ID> [--config configs/pipeline.yaml]
```

Env: `EFTP_S3_*`, `HF_TOKEN`. Exit 3 if the train split ends up empty.

## Input / Output

- **Input:** `eftp-gold/{run_id}/`.
- **Output:** `eftp-artifacts/{run_id}/tokens/` — `train.safetensors`, `eval.safetensors`, `index_map.json` per [02-data-contracts.md §4](../02-data-contracts.md).

## Config (`tokenize.*` + `model.adapter`)

`max_seq_len` (null ⇒ adapter default), `eval_fraction` (default 0.1).

## Core logic

1. Resolve adapter via `get_adapter(config.model.adapter)`; load its tokenizer.
2. Read Gold manifest + records; validate schema (every record must have non-null `evaluation` — a null one is exit 2, it means a non-Gold tier was pointed at).
3. Delete `eftp-artifacts/{run_id}/tokens/` (idempotency).
4. **Split assignment** (before any tokenization, deterministic): record goes to eval iff `int(sha256(record_id)[:8], 16) / 0xFFFFFFFF < eval_fraction`, else train. No shuffling anywhere — ordering is Gold file order.
5. Per record: `messages = adapter.to_chat_messages(conversation)`; tokenize with `tokenizer.apply_chat_template(messages, tokenize=True)`.
6. **Label masking:** `labels` copies `input_ids` with every token outside assistant-generated spans set to `-100`. The generic method in `src/eftp/tokenizer/masking.py` locates assistant spans by incremental prefix tokenization:

   ```python
   def build_labels(messages, tokenizer) -> list[int]:
       T = lambda msgs, gen: tokenizer.apply_chat_template(
               msgs, tokenize=True, add_generation_prompt=gen)
       ids_full = T(messages, gen=False)
       labels = [-100] * len(ids_full)
       for k, msg in enumerate(messages):
           if msg["role"] != "assistant":
               continue
           ids_before = T(messages[:k], gen=True)     # prefix + the template's
                                                      # generation-prompt tokens
           ids_with   = T(messages[:k + 1], gen=False)
           # Prefix properties — MUST hold, or the span boundaries are wrong:
           if ids_with[:len(ids_before)] != ids_before \
                   or ids_full[:len(ids_with)] != ids_with:
               raise MaskingMismatch(k)
           labels[len(ids_before):len(ids_with)] = \
               ids_full[len(ids_before):len(ids_with)]
       return labels
   ```

   Semantics this fixes: the template's generation-prompt tokens (e.g. `<start_of_turn>model\n`) stay **masked**; the assistant's content tokens **and** its end-of-turn token(s) are **unmasked** (the model must learn to stop). The two prefix-property assertions are the guard against tokenizer merge effects at turn boundaries — a record raising `MaskingMismatch` is dropped with reason `masking_mismatch`, and if more than 1 % of records drop this way the stage exits 1 (the adapter's template interaction is broken, not the data; the adapter should override the masking method). Adapters whose template natively supports assistant-only masks (`return_assistant_tokens_mask`) may override with that instead.
7. Records longer than `max_seq_len` are dropped (`index_map.dropped`, reason `over_max_len`) — no truncation in MVP, truncation silently destroys assistant answers. Records with non-text parts under a text-only adapter drop as `unsupported_modality`; prefix-property failures drop as `masking_mismatch` (step 6).
8. Pad all sequences to the longest sequence in the split with the tokenizer's pad token (`attention_mask` 0, `labels` -100); stack to int64 tensors; write SafeTensors.
9. Write `index_map.json` last (commit marker for this prefix): adapter name, `tokenizer_id` = adapter `hf_model_id`, effective `max_seq_len`, Gold manifest URI, row→record-ID lists per split, drops.

## Error handling

- Unknown adapter name ⇒ exit 2 (from `get_adapter`).
- `> 50 %` of records dropped `over_max_len` ⇒ exit 1 with a message suggesting raising `max_seq_len` (misconfiguration, not data policy).

## Acceptance criteria

- For the fixture Gold tier: `n_train + n_eval + len(dropped)` = Gold record count; every `index_map` record ID exists in Gold; split is stable across re-runs and across machines.
- `labels` verification on a known fixture: user/system token positions are `-100`, assistant token positions equal `input_ids`.
- Output loads via `safetensors.torch.load_file` with expected shapes/dtypes.
- Changing `model.adapter` to a second registered adapter (test stub) re-tokenizes without any Tokenizer code change.

## MVP scope

All of the above, text-only.

## Future phases

**Phase 4:** multimodal adapters return processor inputs (pixel values etc.); tensor layout gains per-modality keys — specified in the Phase 4 revision of contract §4, not now.
