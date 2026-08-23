"""Default adapter: Google Gemma E4B (docs/04-model-adapters.md §3).

`hf_model_id`/`hf_revision` verified against the real, public Hugging Face repo at
implementation time (2026-08-23): `google/gemma-3n-E4B-it` -- the "Gemma 3n" family's
effective-4B-parameter instruction-tuned variant (Gemma 3n's MatFormer architecture lets
an 8B-parameter checkpoint run with a ~4B memory footprint by offloading low-utilization
matrices; "E4B" is Google's own name for that effective size). `docs/04 §3`'s
`google/gemma-4-e4b-it` was a placeholder pending this verification -- there is no
"Gemma 4" family. `hf_revision` is pinned to that repo's `main`-branch commit SHA as of
the same date (fetched via the HF API, not the "main" ref itself -- `INF-U-011`).

**Not verified, and not verifiable from here:** whether this team's `HF_TOKEN` has
accepted Google's Gemma license for this specific repo -- it is gated, and access is a
per-account grant on huggingface.co, not something an adapter definition can confirm.
`load_tokenizer`/`load_base_model` (this file doesn't call either; the Tokenizer and
Trainer do, in T10/T11) will fail with a 401 at that point if it hasn't been granted.
"""

from __future__ import annotations

from tuner.core.schemas import Turn
from tuner.models.base import ModelAdapter, TrainingDefaults


class GemmaE4BAdapter(ModelAdapter):
    name = "gemma-e4b"
    hf_model_id = "google/gemma-3n-E4B-it"
    hf_revision = "c1221e9c62e34a43ab7ffacd1be0ea71f126ef10"
    max_seq_len = 4096
    supports_full_ft = False  # E4B ~4B effective params; QLoRA only (04 §3)
    training_defaults = TrainingDefaults(
        learning_rate=2e-4,
        epochs=3,
        per_device_batch_size=4,
        gradient_accumulation_steps=4,
        warmup_ratio=0.03,
        lora_r=16,
        lora_alpha=32,
        lora_dropout=0.05,
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
    quantization = {
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_use_double_quant": True,
        "bnb_4bit_compute_dtype": "bfloat16",
    }

    def to_chat_messages(self, conversation: list[Turn]) -> list[dict[str, str]]:
        """Gemma's chat template rejects a standalone `system` role -- fold its text
        into the first `user` turn as `"{system}\\n\\n{user}"` (04 §3, ADP-U-020).
        No system turn at all is a plain passthrough mapping (ADP-U-021)."""
        system_text: str | None = None
        folded = False
        messages = []
        for turn in conversation:
            text = self._turn_text(turn)
            if turn.role == "system":
                system_text = text
                continue
            if turn.role == "user" and not folded and system_text is not None:
                text = f"{system_text}\n\n{text}"
                folded = True
            messages.append({"role": turn.role, "content": text})
        return messages
