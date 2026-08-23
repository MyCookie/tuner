"""Adapter registry and selection (docs/04-model-adapters.md §2)."""

from __future__ import annotations

from tuner.models.base import ModelAdapter
from tuner.models.gemma_e4b import GemmaE4BAdapter
from tuner.models.tiny_test import TinyTestAdapter

ADAPTERS: dict[str, type[ModelAdapter]] = {
    "gemma-e4b": GemmaE4BAdapter,
    "tiny-test": TinyTestAdapter,
}


def get_adapter(name: str) -> ModelAdapter:
    """Look up and instantiate an adapter by its `model.adapter` config value. Raises
    `KeyError` naming every known adapter if `name` isn't registered -- the caller (a
    stage CLI) turns that into exit code 2 (04 §2, ADP-U-011)."""
    try:
        adapter_cls = ADAPTERS[name]
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise KeyError(f"unknown model adapter {name!r}; known adapters: {known}") from None
    return adapter_cls()
