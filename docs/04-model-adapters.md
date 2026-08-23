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
| `hf_model_id` | `google/gemma-3n-E4B-it`¹ — this string is the only place the repo id exists |
| `hf_revision` | `c1221e9c62e34a43ab7ffacd1be0ea71f126ef10`¹ |
| `max_seq_len` | 4096 |
| `supports_full_ft` | `False` (E4B ≈ 4B effective params; QLoRA only) |
| `lora_target_modules` | `["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]` |
| `training_defaults` | lr `2e-4`, epochs `3`, batch `4`, grad-accum `4`, warmup `0.03`, r `16`, alpha `32`, dropout `0.05` |
| `quantization` | 4-bit NF4, double quant, compute dtype bfloat16 |
| `to_chat_messages` | Gemma templates reject a standalone `system` role → prepend the system text to the first user turn as `"{system}\n\n{user}"` |

These defaults are starting points sized for the 128 GB coherent-memory dev box; tune via `train.hyperparameters`.

¹ **Verification (T09):** this doc's placeholder, `google/gemma-4-e4b-it`, doesn't exist — there is no "Gemma 4" family. The real model is Google's Gemma 3n family, whose MatFormer architecture lets an 8B-parameter checkpoint run with the memory footprint of a ~4B model ("E4B" is Google's own name for that effective size). Confirmed against the live, public repo via the HF API (`GET /api/models/google/gemma-3n-E4B-it`, 2026-08-23): `sha` = the pinned revision above; the repo ships SafeTensors only (no `.bin`, consistent with CLAUDE.md's pickle ban). **Not verified, and not verifiable from a repo-id lookup:** whether this team's `HF_TOKEN` has been granted access — the repo is gated behind Google's license, a per-account grant on huggingface.co that only shows up as a 401 at actual download time (`load_tokenizer`/`load_base_model`, first exercised for real in T10/T11). If that grant hasn't happened yet, it needs to before either of those tasks can run against the real adapter (the `tiny-test` adapter, ungated, is what those tasks' own test suites use instead).

## 4. Adding a model — checklist

1. Create `src/tuner/models/<name>.py` defining the adapter class with all fields from §1.
2. Register it in `ADAPTERS`.
3. Add a unit test in `tests/unit/test_adapters.py`: `to_chat_messages` output for a fixture conversation (including the system-turn case), and `training_defaults` completeness.
4. Set `model.adapter: <name>` in config. No other file changes; if any other change is needed, fix the abstraction first.

## 5. Phase 4 — multimodal extensions (specified, not built)

Additive fields for multimodal models (e.g. LLaVA-class, SAS Phase 4): `processor_id` (AutoProcessor repo), `supported_modalities: set[str]`, and `to_chat_messages` accepting `image`/`audio` parts by downloading the `tuner-assets` URI via `StorageClient` and passing the media object per the processor's convention. Text-only adapters keep raising `UnsupportedModalityError`, which the Tokenizer reports as drop reason `unsupported_modality`.

**MVP scope:** §1–§4 with the single `gemma-e4b` adapter; `supports_full_ft` exists but no shipped adapter enables it; §5 deferred.
