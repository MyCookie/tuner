"""`tiny-test`: a permanently registered adapter over a small, ungated model, so
CPU-runnable tests exercise real tokenizer/trainer code paths instead of mocking them
away (docs/06-testing.md §5). Test infrastructure, not a product model -- never the
default, only selected explicitly via `model.adapter: tiny-test`.

`HuggingFaceTB/SmolLM2-135M-Instruct` (135M params, chat-templated, ungated,
Llama-architecture). `hf_revision` verified against the real, public repo at
implementation time (2026-08-23) via the HF API, pinned to that commit SHA -- never
`"main"` (`INF-U-011`). This is also the model whose cache CI pre-seeds for offline
mode (`INF-I-012`).
"""

from __future__ import annotations

from tuner.core.schemas import Turn
from tuner.models.base import ModelAdapter, TrainingDefaults


class TinyTestAdapter(ModelAdapter):
    name = "tiny-test"
    hf_model_id = "HuggingFaceTB/SmolLM2-135M-Instruct"
    hf_revision = "12fd25f77366fa6b3b4b768ec3050bf629380bac"
    max_seq_len = 2048
    supports_full_ft = True  # 135M params -- small enough that full FT is fine for tests
    training_defaults = TrainingDefaults(
        learning_rate=2e-4,
        epochs=1,
        per_device_batch_size=2,
        gradient_accumulation_steps=1,
        warmup_ratio=0.0,
        lora_r=8,
        lora_alpha=16,
        lora_dropout=0.0,
        # Same Llama-family attention/MLP projection names gemma-e4b uses -- SmolLM2 is
        # LlamaForCausalLM, so the standard target-module set applies unchanged.
        lora_target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
    )
    # No QLoRA config: supports_full_ft is True and this adapter is never used for a
    # real quantized load (ADP-U-001 only requires quantization non-empty when
    # supports_full_ft is False).
    quantization: dict = {}

    def to_chat_messages(self, conversation: list[Turn]) -> list[dict[str, str]]:
        """SmolLM2's chat template accepts a `system` role natively -- unlike Gemma,
        no folding is needed; each turn maps straight across."""
        return [{"role": turn.role, "content": self._turn_text(turn)} for turn in conversation]
