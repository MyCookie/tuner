"""Unit tests for tuner.models.* (ADP suite, docs/08-test-specs/adapters.md). HF
download calls are stubbed throughout -- nothing here touches the network. The
Hugging Face interaction cases assigned to this task (INF-U-010..011) live in
tests/unit/test_infra_static.py instead, per docs/08-test-specs/infra.md's own file
ownership for the INF suite.
"""

from __future__ import annotations

import dataclasses

import pytest

from tuner.core.config import merge_hyperparameters
from tuner.core.schemas import ContentPart, Turn
from tuner.models.base import (
    ModelAdapter,
    TrainingDefaults,
    UnsupportedModalityError,
)
from tuner.models.gemma_e4b import GemmaE4BAdapter
from tuner.models.registry import ADAPTERS, get_adapter
from tuner.models.tiny_test import TinyTestAdapter

_TRAINING_DEFAULTS_FIELDS = {f.name for f in dataclasses.fields(TrainingDefaults)}


def _turn(role: str, *texts: str) -> Turn:
    return Turn(role=role, content=[ContentPart(type="text", value=t) for t in texts])


def _assert_stub_chat_template_contract(messages: list[dict]) -> None:
    """The minimal shape `apply_chat_template` actually needs: a list of dicts, each
    with a string `role` and a string `content` and nothing else (ADP-U-002)."""
    assert isinstance(messages, list) and messages
    for message in messages:
        assert set(message) == {"role", "content"}
        assert isinstance(message["role"], str)
        assert isinstance(message["content"], str)


# --- Interface compliance (parametrized over every registered adapter) -------------

_ADAPTER_INSTANCES = [cls() for cls in ADAPTERS.values()]
_ADAPTER_IDS = list(ADAPTERS)


@pytest.mark.parametrize("adapter", _ADAPTER_INSTANCES, ids=_ADAPTER_IDS)
def test_field_completeness(adapter: ModelAdapter):
    """ADP-U-001: name matches its registry key; hf_model_id non-empty; hf_revision
    pinned; max_seq_len > 0; training_defaults has every TrainingDefaults field;
    quantization non-empty when supports_full_ft is False."""
    assert ADAPTERS[adapter.name] is type(adapter)
    assert adapter.hf_model_id
    assert adapter.hf_revision and adapter.hf_revision != "main"
    assert adapter.max_seq_len > 0
    assert {f.name for f in dataclasses.fields(adapter.training_defaults)} == (
        _TRAINING_DEFAULTS_FIELDS
    )
    if not adapter.supports_full_ft:
        assert adapter.quantization


@pytest.mark.parametrize("adapter", _ADAPTER_INSTANCES, ids=_ADAPTER_IDS)
def test_to_chat_messages_two_turn_text_conversation(adapter: ModelAdapter):
    """ADP-U-002: to_chat_messages on a 2-turn text conversation returns a list of
    role/content dicts accepted by apply_chat_template (a stub tokenizer contract)."""
    conversation = [_turn("user", "Hello there."), _turn("assistant", "Hi, how can I help?")]

    messages = adapter.to_chat_messages(conversation)

    _assert_stub_chat_template_contract(messages)
    assert len(messages) == 2


@pytest.mark.parametrize("adapter", _ADAPTER_INSTANCES, ids=_ADAPTER_IDS)
def test_to_chat_messages_image_part_raises(adapter: ModelAdapter):
    """ADP-U-003: an image content part raises UnsupportedModalityError (until the
    adapter declares the modality, 04 §5)."""
    conversation = [
        Turn(role="user", content=[ContentPart(type="image", value="s3://tuner-assets/x.png")]),
        _turn("assistant", "I see an image."),
    ]

    with pytest.raises(UnsupportedModalityError):
        adapter.to_chat_messages(conversation)


@pytest.mark.parametrize("adapter", _ADAPTER_INSTANCES, ids=_ADAPTER_IDS)
def test_to_chat_messages_multiple_text_parts_joined(adapter: ModelAdapter):
    """ADP-U-004: multiple text parts in one turn are joined with \\n\\n."""
    conversation = [_turn("user", "First part.", "Second part."), _turn("assistant", "Ok.")]

    messages = adapter.to_chat_messages(conversation)

    assert messages[0]["content"] == "First part.\n\nSecond part."


# --- load_tokenizer / load_base_model default impls (HF calls stubbed) -------------
# No case IDs in adapters.md's fixed suite cover these directly, but 08's own coverage
# target is "100% of tuner/models/*" -- assigned ADP-U-005/006, in the numbering gap
# the doc's Interface-compliance block (001..004, then 010) already leaves open.


def test_load_tokenizer_calls_auto_tokenizer_with_pinned_revision(monkeypatch):
    """ADP-U-005: load_tokenizer's default impl calls
    AutoTokenizer.from_pretrained(hf_model_id, revision=hf_revision)."""
    calls = []
    monkeypatch.setattr(
        "tuner.models.base.AutoTokenizer.from_pretrained",
        lambda model_id, revision: calls.append((model_id, revision)) or "tokenizer",
    )
    adapter = TinyTestAdapter()

    result = adapter.load_tokenizer()

    assert result == "tokenizer"
    assert calls == [(adapter.hf_model_id, adapter.hf_revision)]


class _StubAutoModel:
    """A stand-in for transformers.AutoModelForCausalLM: the real class is a
    lazy-loading placeholder whose attributes (even for patching purposes) require
    PyTorch to be importable, which isn't installed outside the `train` extra. Patching
    the whole name -- not just `.from_pretrained` on the real class -- avoids ever
    touching that lazy loader."""

    calls: list[tuple[str, dict]] = []

    @classmethod
    def from_pretrained(cls, model_id, **kwargs):
        cls.calls.append((model_id, kwargs))
        return "model"


def test_load_base_model_quantization_wired_only_when_requested(monkeypatch):
    """ADP-U-006: load_base_model(quantized=True) passes self.quantization through as a
    BitsAndBytesConfig `quantization_config`; quantized=False omits it entirely."""
    _StubAutoModel.calls = []
    monkeypatch.setattr("tuner.models.base.AutoModelForCausalLM", _StubAutoModel)
    monkeypatch.setattr(
        "tuner.models.base.BitsAndBytesConfig", lambda **kwargs: ("bnb-config", kwargs)
    )
    adapter = GemmaE4BAdapter()
    calls = _StubAutoModel.calls

    assert adapter.load_base_model(quantized=True) == "model"
    model_id, kwargs = calls[-1]
    assert model_id == adapter.hf_model_id
    assert kwargs["revision"] == adapter.hf_revision
    assert kwargs["quantization_config"] == ("bnb-config", adapter.quantization)

    assert adapter.load_base_model(quantized=False) == "model"
    _, kwargs = calls[-1]
    assert "quantization_config" not in kwargs


# --- Registry -----------------------------------------------------------------------


def test_get_adapter_known_names_return_correct_instances():
    """ADP-U-010: get_adapter("gemma-e4b") / get_adapter("tiny-test") return correct
    class instances."""
    assert isinstance(get_adapter("gemma-e4b"), GemmaE4BAdapter)
    assert isinstance(get_adapter("tiny-test"), TinyTestAdapter)


def test_get_adapter_unknown_name_lists_known_names():
    """ADP-U-011: get_adapter("nope") raises an error listing all known names (a stage
    CLI turns this into exit code 2)."""
    with pytest.raises(KeyError) as exc_info:
        get_adapter("nope")

    message = str(exc_info.value)
    assert "nope" in message
    for known_name in ADAPTERS:
        assert known_name in message


# --- Gemma E4B specifics --------------------------------------------------------------


def test_gemma_system_turn_folded_into_first_user_turn():
    """ADP-U-020: [system, user, assistant] -> 2 messages; first user content is
    "{system}\\n\\n{user}" (golden output pinned)."""
    conversation = [
        _turn("system", "You are a helpful assistant."),
        _turn("user", "How do I reset my password?"),
        _turn("assistant", "Go to Settings > Security."),
    ]

    messages = GemmaE4BAdapter().to_chat_messages(conversation)

    assert messages == [
        {
            "role": "user",
            "content": "You are a helpful assistant.\n\nHow do I reset my password?",
        },
        {"role": "assistant", "content": "Go to Settings > Security."},
    ]


def test_gemma_no_system_turn_is_passthrough():
    """ADP-U-021: no system turn -> passthrough mapping, golden output pinned."""
    conversation = [_turn("user", "What's the weather?"), _turn("assistant", "Sunny today.")]

    messages = GemmaE4BAdapter().to_chat_messages(conversation)

    assert messages == [
        {"role": "user", "content": "What's the weather?"},
        {"role": "assistant", "content": "Sunny today."},
    ]


def test_gemma_declared_values_match_spec():
    """ADP-U-022: supports_full_ft False; lora_target_modules equals the 04 §3 list;
    defaults match the §3 table."""
    adapter = GemmaE4BAdapter()

    assert adapter.supports_full_ft is False
    assert adapter.training_defaults.lora_target_modules == [
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
    ]
    assert adapter.training_defaults.learning_rate == 2e-4
    assert adapter.training_defaults.epochs == 3
    assert adapter.training_defaults.per_device_batch_size == 4
    assert adapter.training_defaults.gradient_accumulation_steps == 4
    assert adapter.training_defaults.warmup_ratio == 0.03
    assert adapter.training_defaults.lora_r == 16
    assert adapter.training_defaults.lora_alpha == 32
    assert adapter.training_defaults.lora_dropout == 0.05


# --- Hyperparameter merge (complements CORE-U-004 from the adapter side) -----------


def test_merge_overrides_single_field_keeps_the_rest():
    """ADP-U-030: merge with an override of learning_rate only -- lr from config, every
    other field from the real adapter's training_defaults."""
    adapter = GemmaE4BAdapter()

    merged = merge_hyperparameters(adapter.training_defaults, {"learning_rate": 5e-5})
    effective = TrainingDefaults(**merged)

    assert effective.learning_rate == 5e-5
    assert effective.epochs == adapter.training_defaults.epochs
    assert effective.lora_r == adapter.training_defaults.lora_r
    assert effective.lora_target_modules == adapter.training_defaults.lora_target_modules


def test_merge_unknown_hyperparameter_key_rejected():
    """ADP-U-031: an unknown hyperparameter key is rejected once the merged dict is
    used to reconstruct the real TrainingDefaults -- the dataclass itself is the config
    model that catches it."""
    adapter = TinyTestAdapter()

    merged = merge_hyperparameters(adapter.training_defaults, {"not_a_real_field": 1})

    with pytest.raises(TypeError):
        TrainingDefaults(**merged)
