"""The `ModelAdapter` interface (docs/spec/04-model-adapters.md §1).

Everything model-specific lives behind this interface: the Tokenizer, Trainer, and
Smoke-test are model-agnostic and obtain all model knowledge through the adapter
selected by `model.adapter`. A stage branching on `adapter.name` is a design bug --
add a field or method here instead (04 §1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from huggingface_hub.utils import HfHubHTTPError, LocalTokenNotFoundError
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from tuner.core.schemas import Turn

# Both are raised at the huggingface_hub client boundary transformers' from_pretrained
# calls through: LocalTokenNotFoundError when HF_TOKEN isn't set at all, HfHubHTTPError
# (its GatedRepoError subclass, specifically) when it's set but lacks access to a gated
# repo. Caught together so both surface as one actionable message (INF-U-010) instead of
# a raw traceback naming neither HF_TOKEN nor the model id.
_HF_AUTH_ERRORS = (HfHubHTTPError, LocalTokenNotFoundError)


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


class UnsupportedModalityError(Exception):
    """A conversation turn has a non-text content part this adapter can't handle yet
    (Phase 4, docs/spec/04-model-adapters.md §5). The Tokenizer reports this as drop reason
    `unsupported_modality`."""


class HFAuthError(Exception):
    """A Hugging Face Hub call failed because of a missing or insufficiently-scoped
    `HF_TOKEN` -- raised instead of letting the underlying huggingface_hub exception
    (which never mentions `HF_TOKEN` by name) surface as a raw traceback. A stage CLI
    catches this and exits 2 (INF-U-010)."""

    def __init__(self, hf_model_id: str, cause: Exception) -> None:
        super().__init__(
            f"cannot access {hf_model_id!r} on Hugging Face -- set HF_TOKEN to a token "
            f"with access to this model (it may be gated and require accepting a "
            f"license): {cause}"
        )


class ModelAdapter(ABC):
    """Registry key + everything a stage needs to know about one base model.

    Concrete adapters set every field below as a class attribute; nothing here has a
    default because there is no meaningful one -- `ADP-U-001` enforces that every
    registered adapter actually sets all of them.
    """

    name: str
    hf_model_id: str
    hf_revision: str
    max_seq_len: int
    supports_full_ft: bool
    training_defaults: TrainingDefaults
    quantization: dict[str, Any]

    @abstractmethod
    def to_chat_messages(self, conversation: list[Turn]) -> list[dict[str, str]]:
        """Map a Multimodal Contract conversation (02-data-contracts.md §2) to the
        messages list this model's chat template accepts via `apply_chat_template`.
        Handles model quirks (e.g. folding a system turn into the first user turn for
        models without a system role)."""

    @staticmethod
    def _turn_text(turn: Turn) -> str:
        """Every adapter's `to_chat_messages` goes through this: multiple text parts in
        one turn are joined with `\\n\\n` (ADP-U-004), and a non-text part raises
        `UnsupportedModalityError` (ADP-U-003) -- both rules apply identically to every
        adapter (04 §1), so they live here once rather than being reimplemented per
        model."""
        parts = []
        for part in turn.content:
            if part.type != "text":
                raise UnsupportedModalityError(
                    f"{part.type!r} content is not supported until Phase 4 (04 §5)"
                )
            parts.append(part.value)
        return "\n\n".join(parts)

    def load_tokenizer(self) -> Any:
        """Default impl: `AutoTokenizer.from_pretrained` at the pinned revision. An
        adapter only needs to override this if its tokenizer needs special handling."""
        try:
            return AutoTokenizer.from_pretrained(self.hf_model_id, revision=self.hf_revision)
        except _HF_AUTH_ERRORS as exc:
            raise HFAuthError(self.hf_model_id, exc) from exc

    def load_base_model(self, quantized: bool) -> Any:
        """Default impl: `AutoModelForCausalLM` at the pinned revision, with
        `self.quantization` applied as a `BitsAndBytesConfig` when `quantized=True`
        (04 §1)."""
        kwargs: dict[str, Any] = {"revision": self.hf_revision}
        if quantized:
            kwargs["quantization_config"] = BitsAndBytesConfig(**self.quantization)
        try:
            return AutoModelForCausalLM.from_pretrained(self.hf_model_id, **kwargs)
        except _HF_AUTH_ERRORS as exc:
            raise HFAuthError(self.hf_model_id, exc) from exc
