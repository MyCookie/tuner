#!/usr/bin/env python3
"""Pre-seed the tiny-test model's HF cache (docs/spec/06-testing.md §6's CI "cache-seed
step"; docs/spec/08-test-specs/infra.md INF-I-012). Needs real network access -- run this
*before* setting HF_HUB_OFFLINE=1 for the actual (offline) test/CI run.

Usage: `seed_hf_cache.py [CACHE_DIR]`. With no argument, downloads into whatever
`HF_HOME` (or its own default) already resolves to -- what a CI job wants, since the
later offline test run reads from that same ambient location. `tests/integration/
test_infra.py`'s own INF-I-012 case passes an isolated tmp dir explicitly instead,
so the test proves "pre-seed then run offline" from a genuine cold start.
"""

from __future__ import annotations

import sys

from huggingface_hub import snapshot_download

from tuner.models.tiny_test import TinyTestAdapter

# Excludes the pickle-format training-args file and ONNX exports this repo also
# ships (tiny_test.py's own comment) -- neither is needed to load the
# tokenizer/model, and the pickle file must never land in any cache this pipeline
# reads from (CLAUDE.md hard rule 2).
ALLOW_PATTERNS = ["*.safetensors", "*.json", "tokenizer.model", "*.txt"]


def seed(cache_dir: str | None = None) -> str:
    return snapshot_download(
        TinyTestAdapter.hf_model_id,
        revision=TinyTestAdapter.hf_revision,
        cache_dir=f"{cache_dir}/hub" if cache_dir else None,
        allow_patterns=ALLOW_PATTERNS,
    )


def main(argv: list[str]) -> int:
    cache_dir = argv[0] if argv else None
    path = seed(cache_dir)
    print(f"seed_hf_cache: {TinyTestAdapter.hf_model_id}@{TinyTestAdapter.hf_revision} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
