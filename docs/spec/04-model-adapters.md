# Model Adapters — Pluggable Fine-Tune Targets

The SAS does not fix which model gets fine-tuned, and the team's model access may change. Tuner therefore isolates **everything model-specific** behind a `ModelAdapter`: the Tokenizer, Trainer, and Smoke-test are model-agnostic and obtain all model knowledge through the adapter selected by the single config key `model.adapter`. Swapping models is a config change; supporting a new model is a one-file change.

The default adapter is **`gemma-e4b`** (Google Gemma E4B).

---

## 1. The `ModelAdapter` interface (`src/tuner/models/base.py`)

```python
@dataclass(frozen=True)
class TrainingDefaults:
    learning_rate: float
    epochs: int
    per_device_batch_size: int
    gradient_accumulation_steps: int
    warmup_ratio: float
    lora_r: int
    lora_alpha: int
    lora_dropout: float
    lora_target_modules: list[str]
    lora_exclude_modules_regex: str | None = None   # PEFT LoraConfig.exclude_modules
                                                     # regex; None excludes nothing (T15)

class ModelAdapter(ABC):
    name: str                      # registry key, e.g. "gemma-e4b"
    hf_model_id: str               # Hugging Face repo id of the base model
    hf_revision: str               # pinned commit hash or tag — never "main" (reproducibility)
    max_seq_len: int               # default max sequence length
    supports_full_ft: bool         # True only for models where full FT is sanctioned (< 3B, SAS §3.1)
    training_defaults: TrainingDefaults
    quantization: dict             # bitsandbytes config for QLoRA loading (§4)

    @abstractmethod
    def to_chat_messages(self, conversation: list[Turn]) -> list[dict]:
        """Map a Multimodal Contract conversation (02-data-contracts.md §2)
        to the messages list this model's chat template accepts.
        Handles model quirks (e.g. folding a system turn into the first
        user turn for models without a system role)."""

    def load_tokenizer(self):        # default impl: AutoTokenizer.from_pretrained(hf_model_id, revision=hf_revision)
    def load_base_model(self, quantized: bool):  # default impl: AutoModelForCausalLM at hf_revision,
                                                 # with self.quantization when quantized=True
```

Rules:

- Chat templating uses the HF tokenizer's built-in `apply_chat_template`; `to_chat_messages` only reshapes the contract into what that template expects. Adapters must not hand-roll prompt strings unless the model ships no chat template.
- MVP conversations contain only `text` content parts. Adapters concatenate multiple text parts of one turn with `\n\n`. Non-text parts raise `UnsupportedModalityError` until Phase 4.
- Everything a stage might need per-model **must** live on the adapter. If a stage ever branches on `adapter.name`, that's a design bug — add a field/method instead.

## 2. Registry and selection (`src/tuner/models/registry.py`)

```python
ADAPTERS: dict[str, type[ModelAdapter]] = {
    "gemma-e4b": GemmaE4BAdapter,
}
def get_adapter(name: str) -> ModelAdapter: ...   # KeyError -> exit code 2 with the list of known names
```

Selection: `model.adapter` in `configs/pipeline.yaml`. Config `train.hyperparameters` entries override the adapter's `training_defaults` field-by-field ([01-architecture.md §6](01-architecture.md) precedence). If `train.method: full` is requested and `supports_full_ft` is `False`, the Trainer exits 2.

## 3. Default adapter: `gemma-e4b` (`src/tuner/models/gemma_e4b.py`)

| Field | Value |
| :--- | :--- |
| `name` | `gemma-e4b` |
| `hf_model_id` | `google/gemma-4-E4B-it`¹ — this string is the only place the repo id exists |
| `hf_revision` | `ee0ef6023621cff504d758262d4e04895a5af4a2`¹ |
| `max_seq_len` | 4096 |
| `supports_full_ft` | `False` (E4B ≈ 4B effective params; QLoRA only) |
| `lora_target_modules` | `["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]` |
| `lora_exclude_modules_regex` | `r".*\.(vision_tower\|audio_tower)\..*"`² |
| `training_defaults` | lr `2e-4`, epochs `3`, batch `4`, grad-accum `4`, warmup `0.03`, r `16`, alpha `32`, dropout `0.05` |
| `quantization` | 4-bit NF4, double quant, compute dtype bfloat16 |
| `to_chat_messages` | Gemma 4's chat template accepts a `system` role natively (a dedicated `<\|turn>system` segment) → plain passthrough, no folding |

These defaults are starting points sized for the 128 GB coherent-memory dev box; tune via `train.hyperparameters`.

¹ **Verification (T09, corrected in round 1 review):** this doc's placeholder, `google/gemma-4-e4b-it`, was assumed non-existent ("no Gemma 4 family") and swapped for the prior Gemma 3n generation without first checking whether the literal placeholder itself resolved. It does: `google/gemma-4-E4B-it` is real, current, and — unlike the 3n-generation repo substituted in its place during the initial pass — **ungated**. Confirmed against the live, public repo via the HF API (`GET /api/models/google/gemma-4-E4B-it`): `sha` = the pinned revision above, `gated: false`, SafeTensors only (no `.bin`, consistent with CLAUDE.md's pickle ban). Its `chat_template.jinja` was also checked directly, which is what corrected the `to_chat_messages` row above — the fold-into-first-user-turn behavior an earlier Gemma generation needed doesn't apply here. The repo is `Gemma4ForConditionalGeneration` (architecturally multimodal — text/vision/audio configs) but is registered in `transformers`' causal-LM auto-mapping alongside its image-text-to-text one, so `ModelAdapter`'s inherited `load_base_model` (`AutoModelForCausalLM.from_pretrained`) resolves it correctly for this pipeline's text-only use without an adapter-level override. Being ungated removes the earlier "HF_TOKEN license grant" concern for this specific repo; `HF_TOKEN` is presumably still required for the download itself per [01 §4.3](01-architecture.md)'s env var list. **LoRA target-module names (T09):** not independently confirmed at the time — `lora_target_modules` above keeps the naming every prior Gemma generation has used, with a stated risk that this generation might have renamed them. Resolved for real in T15 (see ² below): it didn't rename them, but a different, unanticipated problem surfaced.

² **Real GPU run, T15 (`TRN-G-020`):** `lora_target_modules`' names matched the text backbone exactly — but this checkpoint's `vision_tower`/`audio_tower` encoders reuse those *same* leaf names for their own projections, and PEFT's `target_modules` matches by bare leaf name (no path qualification), so `get_peft_model` also tried to LoRA-inject the towers. Their projections are wrapped in a custom `Gemma4ClippableLinear` (confirmed against the real, loaded, 4-bit-quantized model's `named_modules()`) that isn't one of PEFT's supported base classes, so injection failed outright: `ValueError: Target module Gemma4ClippableLinear(...) is not supported`. The language backbone's own projections (`model.language_model.layers.N.*_proj`) are plain `Linear4bit` and inject cleanly. `lora_exclude_modules_regex` (a full-string regex, passed to PEFT's own `LoraConfig.exclude_modules`) excludes both towers — correct, not just a workaround, since this pipeline is text-only regardless (§5 below).

## 4. Adding a model — checklist

1. Create `src/tuner/models/<name>.py` defining the adapter class with all fields from §1.
2. Register it in `ADAPTERS`.
3. Add a unit test in `tests/unit/test_adapters.py`: `to_chat_messages` output for a fixture conversation (including the system-turn case), and `training_defaults` completeness.
4. Set `model.adapter: <name>` in config. No other file changes; if any other change is needed, fix the abstraction first.

## 5. Phase 4 — multimodal extensions (specified, not built)

Additive fields for multimodal models (e.g. LLaVA-class, SAS Phase 4): `processor_id` (AutoProcessor repo), `supported_modalities: set[str]`, and `to_chat_messages` accepting `image`/`audio` parts by downloading the `tuner-assets` URI via `StorageClient` and passing the media object per the processor's convention. Text-only adapters keep raising `UnsupportedModalityError`, which the Tokenizer reports as drop reason `unsupported_modality`.

**MVP scope:** §1–§4 with the single `gemma-e4b` adapter; `supports_full_ft` exists but no shipped adapter enables it; §5 deferred.
