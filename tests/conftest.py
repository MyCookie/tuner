"""Shared fixtures (docs/08-test-specs/README.md). More land with their owning tasks."""

from __future__ import annotations

import pytest

from tuner.core.ids import new_run_id
from tuner.core.storage import StorageClient


@pytest.fixture
def storage() -> StorageClient:
    """A StorageClient against compose MinIO, configured entirely from env vars."""
    return StorageClient()


@pytest.fixture
def run_id() -> str:
    """A fresh run ID, generated once per test."""
    return new_run_id()
