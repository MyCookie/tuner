"""Default adapter: Google Gemma E4B (docs/spec/04-model-adapters.md §3).

`hf_model_id`/`hf_revision` verified against the real, public Hugging Face repo at
implementation time (2026-08-23): `google/gemma-4-E4B-it` -- the current "Gemma 4"
family's effective-4B-parameter instruction-tuned variant (the "E" naming and the
MatFormer-style effective-parameter architecture carry over from the earlier Gemma 3n
generation; "E4B" is Google's own name for that effective size). `hf_revision` is
pinned to that repo's commit SHA as of the same date, fetched via the HF API rather
than the `main` ref itself (`INF-U-011`).

**Correction, round 1 review:** this file originally pointed at `google/gemma-3n-E4B-it`
with a footnote claiming `docs/spec/04 §3`'s literal placeholder, `google/gemma-4-e4b-it`,
"doesn't exist." That was wrong on both counts: the placeholder string itself resolves
(case-insensitively, via a redirect) to a real, current, **ungated** repo -- the review
caught this by checking the placeholder directly rather than assuming a newer Gemma
generation couldn't exist yet. `docs/spec/02-data-contracts.md`'s examples are corrected to
match this repo's real casing.

This repo is `Gemma4ForConditionalGeneration` -- architecturally multimodal (text/vision/
audio configs), but registered in `transformers`' `MODEL_FOR_CAUSAL_LM_MAPPING_NAMES`
alongside the image-text-to-text mapping, so the inherited `load_base_model`'s
`AutoModelForCausalLM.from_pretrained` resolves it correctly for text-only use -- no
adapter-level override needed. Being ungated also means the "team's `HF_TOKEN` needs a
license grant" caveat this file previously carried doesn't apply to this repo (though
`HF_TOKEN` is presumably still needed for the download itself, per 01 §4.3's env list).

**Resolved in T15 (`TRN-G-020`), a real GPU run against the actual repo:** the
projection names weren't renamed -- `lora_target_modules` below (the standard
Llama/Gemma-family attention+MLP names) match the text backbone's real module names
exactly. What T09 didn't anticipate is that this checkpoint's vision/audio towers
reuse those *same* leaf names for their own projections, and PEFT's `target_modules`
matches by bare leaf name -- see `lora_exclude_modules_regex` below for what that
caused and how it's excluded.
"""

from __future__ import annotations

from tuner.core.schemas import Turn
from tuner.models.base import ModelAdapter, TrainingDefaults


class GemmaE4BAdapter(ModelAdapter):
    name = "gemma-e4b"
    hf_model_id = "google/gemma-4-E4B-it"
    hf_revision = "ee0ef6023621cff504d758262d4e04895a5af4a2"
    max_seq_len = 4096  # a training-time choice sized for the dev box, not the model's
    # own much larger max context -- see 04 §3's "starting points" note.
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
        # T15 (TRN-G-020) real-GPU finding: this repo's `Gemma4ForConditionalGeneration`
        # is genuinely multimodal, and its vision_tower/audio_tower encoders reuse these
        # exact same leaf names (q_proj, k_proj, ..., gate_proj, up_proj, down_proj) for
        # their own projections -- `lora_target_modules` above matches by bare leaf name
        # (04 §3), so without this exclusion PEFT also tries to LoRA-inject the towers.
        # Their projections are wrapped in a custom `Gemma4ClippableLinear` (confirmed by
        # inspecting the real, loaded, 4-bit-quantized model's `named_modules()`), which
        # isn't one of PEFT's supported base classes (plain `torch.nn.Linear`/`Linear4bit`,
        # `Conv1d/2d/3d`, `transformers.pytorch_utils.Conv1D`, `MultiheadAttention`) --
        # `get_peft_model` raised `ValueError: Target module Gemma4ClippableLinear(...) is
        # not supported` the first time this ran for real. The language backbone's own
        # projections (`model.language_model.layers.N.*_proj`) are plain `Linear4bit` and
        # inject cleanly; this pipeline is text-only regardless (04 §5 -- multimodal
        # ingestion/training is Phase 4, not built), so excluding both towers is correct,
        # not just a workaround.
        lora_exclude_modules_regex=r".*\.(vision_tower|audio_tower)\..*",
    )
    quantization = {
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_use_double_quant": True,
        "bnb_4bit_compute_dtype": "bfloat16",
    }

    def to_chat_messages(self, conversation: list[Turn]) -> list[dict[str, str]]:
        """Gemma 4's chat template accepts a `system` role natively (a dedicated
        `<|turn>system` segment) -- unlike the folding older Gemma generations needed,
        this is a plain passthrough mapping, system turn included (ADP-U-020,
        ADP-U-021)."""
        return [{"role": turn.role, "content": self._turn_text(turn)} for turn in conversation]
