"""`tuner registry list` (docs/03-components/registry.md) -- the MVP-scope-only slice
of the Registry ops CLI (`show`/`promote`/`rollback` are Phase 2)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import click
from pydantic import ValidationError

from tuner.core.schemas import RegistryManifest
from tuner.core.storage import StorageClient

REGISTRY_BUCKET = "tuner-registry"

_COLUMNS = ("MODEL_VERSION", "ADAPTER", "CREATED_AT", "STATUS", "FINAL_EVAL_LOSS")


def _load_manifests(storage: StorageClient) -> tuple[list[RegistryManifest], list[str]]:
    """Downloads every object under `tuner-registry` (CORE-I-048's empty-prefix fix --
    `{model_version}/manifest.json` keys have no shared parent prefix to enumerate
    under) and returns `(valid manifests, invalid object keys)` -- a manifest that
    fails schema validation is reported by key, not silently dropped (CLI-I-022:
    list is a diagnostic tool, it must not die on one bad object)."""
    valid: list[RegistryManifest] = []
    invalid: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        storage.download_dir(REGISTRY_BUCKET, "", tmp)
        root = Path(tmp)
        for path in sorted(root.rglob("manifest.json")):
            key = str(path.relative_to(root).as_posix())
            try:
                raw = json.loads(path.read_text())
                valid.append(RegistryManifest.model_validate(raw))
            except (json.JSONDecodeError, ValidationError):
                invalid.append(key)
    return valid, invalid


def registry_list(storage: StorageClient | None = None) -> int:
    """List every registered model version; returns the process exit code (always 0
    -- a diagnostic tool, per registry.md's own "must not die on one bad object")."""
    storage = storage or StorageClient()
    manifests, invalid_keys = _load_manifests(storage)

    if not manifests and not invalid_keys:
        click.echo("no models registered")
        return 0

    # Newest-first (registry.md "Operations" list column order); created_at is a
    # zero-padded ISO-8601 UTC string, so lexicographic order is chronological order.
    manifests.sort(key=lambda m: m.created_at, reverse=True)

    click.echo(
        f"{_COLUMNS[0]:<45} {_COLUMNS[1]:<15} {_COLUMNS[2]:<22} {_COLUMNS[3]:<10} {_COLUMNS[4]}"
    )
    for manifest in manifests:
        click.echo(
            f"{manifest.model_version:<45} {manifest.adapter_name:<15} "
            f"{manifest.created_at:<22} {manifest.status:<10} "
            f"{manifest.eval.final_eval_loss}"
        )
    for key in invalid_keys:
        click.echo(f"{key:<45} INVALID")

    return 0


@click.group(name="registry")
def registry_group() -> None:
    """Model registry operations (docs/03-components/registry.md). MVP scope: `list`
    only -- `show`/`promote`/`rollback` are Phase 2."""


@registry_group.command(name="list")
def list_command() -> None:
    """List every registered model version (candidate/promoted/retired)."""
    sys.exit(registry_list())
