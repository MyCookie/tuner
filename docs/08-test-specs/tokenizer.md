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
| TOK-U-015 | Assistant-first conversation (`[assistant, user, assistant]`) against a tokenizer that raises on an empty message list (as a real chat template can refuse `add_generation_prompt` on zero turns) | `MaskingMismatch` raised, not the tokenizer's own exception left to escape |

TOK-U-015 was added in review round 1 on PR #10: the schema permits a conversation whose first turn is `assistant` (only ≥1 user, ≥1 assistant, and a final `assistant` turn are required), and for such a conversation the incremental-prefix method's first assistant-turn iteration renders an *empty* prefix (`messages[:0]`) with `add_generation_prompt=True` — which the real tiny-test tokenizer refuses outright (`ValueError: Cannot apply chat template to an empty conversation`) rather than returning a normal generation-prompt-only sequence. `build_labels` previously let this propagate uncaught, escaping the CLI's per-record drop handling entirely and aborting the whole run over one record. Now caught and converted to `MaskingMismatch` (`TOK-I-034` is the same regression against the real tokenizer end to end).

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
| TOK-I-031 | Missing Gold manifest | Exit 2 (upstream incomplete) |
| TOK-I-032 | masking_mismatch rate > 1 % (a mock tokenizer engineered to violate the prefix property for every record, injected via `tokenize()`'s `hf_tokenizer` parameter) | Exit 1 with the masking_mismatch-rate message — the CLI-level companion to TOK-U-014's `build_labels`-level test |
| TOK-I-033 | `eval_fraction: 1.0` — every record goes to eval, train ends up empty | Exit 3 (the CLI section of [tokenizer.md](../03-components/tokenizer.md) calls this out explicitly) |
| TOK-I-034 | A contract-valid Gold record whose first turn is `assistant`, against the real tiny-test tokenizer | Dropped `masking_mismatch`; run proceeds (exit 0), not aborted (round 1 review finding 1's regression, reproduced end to end) |

TOK-I-031..034 were added while implementing T10, as gaps noticed against sibling suites, the component spec's own CLI section, and (034) a round-1 review finding, rather than left as untested code paths with no case to justify them: TOK-I-031 mirrors ING-I-015/CLN-I-035/JDG-I-030 (every other stage's own "no manifest, no commit" case) — the original table above omitted the Tokenizer's equivalent even though the implementation validates the same way. TOK-I-032 is the CLI-level threshold-abort companion to TOK-U-014, needed for parity with how JDG-I-023 tests Judge's own >10% abort threshold at the CLI level, not just at the underlying function. TOK-I-033 exercises the "Exit 3 if the train split ends up empty" sentence in tokenizer.md's CLI section directly, which nothing else in the original table did (TOK-I-027 only empties *eval*). TOK-I-034 is TOK-U-015's end-to-end regression against the real tokenizer.

## Notes

- **`tuner.tokenizer.cli.tokenize()` takes an injectable `hf_tokenizer` parameter**, mirroring the existing injectable `storage` parameter (and Judge's own injectable `http_client`) — needed for TOK-I-032, which has no reliable way to make a real tokenizer violate the masking prefix property on demand.
- **`build_labels`'s doc-quoted `T = lambda msgs, gen: tokenizer.apply_chat_template(...)` returns a plain list of token ids in the pseudocode, but current `transformers` defaults `tokenize=True` to `return_dict=True`** (a dict-like `BatchEncoding`, not a list) — confirmed against the real tiny-test tokenizer at implementation time. `masking.py` itself stays exactly as pseudocode-shaped (any tokenizer conforming to the documented signature works, including the stub suite's own tokenizers, which never return a `BatchEncoding`). The real HF tokenizer is wrapped by `tuner.tokenizer.cli._PlainListTokenizer`, which always passes `return_dict=False` through to the underlying call — isolating this library-API detail at the one place a real tokenizer actually enters the pipeline, rather than baking a transformers-version-specific kwarg into the otherwise-generic `masking.py`.
- **SmolLM2's chat template auto-injects a ~30-token default system message when a conversation has no system turn of its own** ("You are a helpful AI assistant named SmolLM, trained by Hugging Face") — confirmed against the real tokenizer. This matters for picking `max_seq_len` values in tests (TOK-I-022 uses 50, not something tighter, precisely because even minimal-content records already cost ~36 tokens before any real content).
- **`StorageClient` gained `write_bytes`/`read_bytes`** (`CORE-I-047`, [08 core.md](core.md)) — SafeTensors shards are raw binary, and the class previously only had jsonl/JSON paths.
