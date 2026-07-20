# Test Suite: Model Adapters (`ADP`)

Spec under test: [04-model-adapters.md](../04-model-adapters.md). File: `tests/unit/test_adapters.py`. Coverage target: **100 %** of `tuner/models/*` (HF download calls stubbed).

## Interface compliance (parametrized over every entry in `ADAPTERS` — new adapters get these for free)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| ADP-U-001 | Field completeness | `name` matches its registry key; `hf_model_id` non-empty; `hf_revision` pinned (INF-U-011 enforces the never-`main` rule); `max_seq_len` > 0; `training_defaults` has every `TrainingDefaults` field; `quantization` non-empty when `supports_full_ft` is False |
| ADP-U-002 | `to_chat_messages` on a 2-turn text conversation | Returns a list of role/content dicts accepted by `apply_chat_template` (validated against a stub tokenizer contract) |
| ADP-U-003 | `to_chat_messages` with an `image` content part | Raises `UnsupportedModalityError` (until the adapter declares the modality, [04 §5](../04-model-adapters.md)) |
| ADP-U-004 | Multiple text parts in one turn | Joined with `\n\n` |

## Registry

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| ADP-U-010 | `get_adapter("gemma-e4b")` / `get_adapter("tiny-test")` | Correct class instances |
| ADP-U-011 | `get_adapter("nope")` | Error listing all known names → CLI exit 2 |

## Gemma E4B specifics

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| ADP-U-020 | System-turn folding | `[system, user, assistant]` → 2 messages; first user content is `"{system}\n\n{user}"` (golden output pinned) |
| ADP-U-021 | No system turn | Passthrough mapping, golden output pinned |
| ADP-U-022 | Declared values | `supports_full_ft` False; `lora_target_modules` equals the [04 §3](../04-model-adapters.md) list; defaults match the §3 table |

## Hyperparameter merge

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| ADP-U-030 | Merge with override of `learning_rate` only | lr from config; all other fields from adapter defaults (complements CORE-U-004 from the adapter side) |
| ADP-U-031 | Merge with unknown hyperparameter key | Rejected (config model catches it) |
