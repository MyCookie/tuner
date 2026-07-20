# Test Suite: Tokenizer (`TOK`)

Spec under test: [tokenizer.md](../03-components/tokenizer.md). Files: `tests/unit/test_split.py`, `tests/unit/test_masking.py`, `tests/integration/test_tokenizer.py`. Coverage target: **100 %** of `split.py`/`masking.py`.

## Split (unit)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| TOK-U-001 | Split of 5 fixed record IDs at `eval_fraction 0.1` | Assignments equal constants pinned at implementation time (compute once, hard-code in the test — this is the cross-machine stability guarantee) |
| TOK-U-002 | `eval_fraction 0.0` / `1.0` | All train / all eval |
| TOK-U-003 | 1 000 synthetic UUIDs at 0.1 | Eval share within [0.05, 0.15]; same call twice → identical sets |
| TOK-U-004 | Split is independent of record order | Shuffled input, same assignments |

## Label masking (unit, stub tokenizer with 1-token-per-word behavior)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| TOK-U-010 | Handcrafted `[system, user, assistant]` conversation | Token-by-token: system+user+template positions `-100`; assistant positions equal `input_ids` |
| TOK-U-011 | Multi-turn: `[user, assistant, user, assistant]` | **Both** assistant spans unmasked, both user spans masked (the incremental-prefix method's key case) |
| TOK-U-012 | Padding | Padded tail: `attention_mask` 0, `labels` −100 |
| TOK-U-013 | Every label is either −100 or its `input_ids` value; ≥1 non-masked token per record | Structural invariant, run over all integration outputs too (asserted in TOK-I-024) |
| TOK-U-014 | Stub tokenizer engineered to violate the prefix property (merges a token across the turn boundary) | `MaskingMismatch` raised → record dropped `masking_mismatch`; >1 % such drops ⇒ stage exit 1 ([tokenizer.md step 6](../03-components/tokenizer.md)) |

## Pipeline behavior (integration, `tiny-test` adapter)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| TOK-I-020 | Seeded 20-record Gold → tokenize | `n_train + n_eval + len(index_map.dropped)` = 20; SafeTensors load with int64 dtype, shapes `[n, seq]` consistent across the 3 keys |
| TOK-I-021 | `index_map.json` contents | Adapter name, `tokenizer_id`, effective `max_seq_len`, Gold manifest URI correct; every row-mapped `record_id` exists in Gold; rows are dense 0..n−1 per split |
| TOK-I-022 | One seeded record exceeding `max_seq_len` | In `dropped` with `over_max_len`; not in any tensor |
| TOK-I-023 | Seeded record with an `image` part (text-only adapter) | Dropped `unsupported_modality` |
| TOK-I-024 | Full-output invariant sweep | TOK-U-013 invariant holds for every row of both splits |
| TOK-I-025 | Gold record with `evaluation: null` seeded | Exit 2 (CORE-U-024 wired in — wrong tier pointed at) |
| TOK-I-026 | >50 % of records over `max_seq_len` (tiny max injected) | Exit 1 with the raise-`max_seq_len` message |
| TOK-I-027 | Eval split empties (2 records, fraction 0.1, both hash to train) | Proceeds; `eval.safetensors` present with 0 rows; SMK-I-007 covers the downstream exit-3 |
| TOK-I-028 | Re-run same run ID | `tokens/` prefix rebuilt cleanly; `index_map.json` written last (spy-order assert, mirrors CORE-I-032) |
| TOK-I-029 | Unknown `model.adapter` in config | Exit 2 via ADP-U-011 path |
| TOK-I-030 | **Real-tokenizer masking** (tiny-test's actual HF tokenizer, offline cache): all fixture conversations incl. multi-turn | Prefix properties hold for every record (zero `masking_mismatch`); every assistant span non-empty; decoding the unmasked positions yields the assistant text (+ end-of-turn token); all generation-prompt positions are −100 — the stub-tokenizer suite cannot catch real boundary-merge effects, this case can |
