"""Shared fixtures (docs/08-test-specs/README.md). More land with their owning tasks."""

from __future__ import annotations

import importlib.util

import pytest

from tuner.core.ids import new_run_id
from tuner.core.storage import StorageClient

# Test modules that import torch/transformers/peft/accelerate (the `train` extra,
# 05-infrastructure.md §3) at module level -- a bare `pytest` in a `--extra
# dev`-only environment (push.yml's own CI lane, and any CPU-only stage's own dev
# setup) would otherwise fail at *collection* time with a raw ModuleNotFoundError,
# before the default addopts marker exclusion (`-m 'not integration and not e2e
# and not gpu and not slow'`) ever gets a chance to deselect them -- pytest has to
# import a module before it can read its markers. Ignored outright when the extra
# genuinely isn't installed; a `--extra dev --extra train` environment (every
# reviewer's own review-setup.sh, and this repo's own CI PR lane) collects them
# normally, so this is purely an accommodation for the leaner dev-only case, not a
# way to silently skip something that should run.
if importlib.util.find_spec("torch") is None:
    collect_ignore_glob = [
        "integration/test_trainer.py",
        "integration/test_smoke.py",
        "e2e/test_steel_thread.py",
    ]


@pytest.fixture
def storage() -> StorageClient:
    """A StorageClient against compose MinIO, configured entirely from env vars."""
    return StorageClient()


@pytest.fixture
def run_id() -> str:
    """A fresh run ID, generated once per test."""
    return new_run_id()
