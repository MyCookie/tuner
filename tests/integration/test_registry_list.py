"""Integration tests for `tuner registry list` (CLI suite, docs/08-test-specs/cli.md).

Needs compose MinIO up. `tuner-registry` is shared across the whole test session (no
per-test run_id prefix scoping, since manifests key on model_version, not run_id) --
each test seeds its own uniquely-named model versions and cleans them up, never
asserting an exact total row count (other tests' or other sessions' leftover
manifests may coexist), only that its own rows appear correctly.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from tuner.cli import cli
from tuner.core.ids import new_run_id
from tuner.registry_ops.cli import registry_list

REGISTRY_BUCKET = "tuner-registry"


def _manifest(model_version: str, run_id: str, *, created_at: str, final_eval_loss: float) -> dict:
    return {
        "model_version": model_version,
        "run_id": run_id,
        "adapter_name": "tiny-test",
        "base_model": "HuggingFaceTB/SmolLM2-135M-Instruct",
        "method": "full",
        "created_at": created_at,
        "gold_manifest_uri": f"s3://tuner-gold/{run_id}/manifest.json",
        "index_map_uri": f"s3://tuner-artifacts/{run_id}/tokens/index_map.json",
        "weights_uri": f"s3://tuner-artifacts/{run_id}/model/",
        "mlflow_run_id": "0" * 32,
        "hyperparameters": {},
        "eval": {"final_train_loss": final_eval_loss, "final_eval_loss": final_eval_loss},
        "status": "candidate",
    }


@pytest.mark.integration
def test_two_seeded_manifests_shown_newest_first(storage, capsys):
    """CLI-I-020: two seeded registry manifests -- table shows both (model_version,
    adapter, created_at, status, final_eval_loss), sorted newest-first."""
    older_run, newer_run = new_run_id(), new_run_id()
    older_version = f"tiny-test-{older_run}"
    newer_version = f"tiny-test-{newer_run}"
    storage.write_json(
        REGISTRY_BUCKET,
        f"{older_version}/manifest.json",
        _manifest(older_version, older_run, created_at="2026-01-01T00:00:00Z", final_eval_loss=1.5),
    )
    storage.write_json(
        REGISTRY_BUCKET,
        f"{newer_version}/manifest.json",
        _manifest(newer_version, newer_run, created_at="2026-06-01T00:00:00Z", final_eval_loss=0.5),
    )

    try:
        exit_code = registry_list(storage=storage)
        out = capsys.readouterr().out

        assert exit_code == 0
        for column in ("MODEL_VERSION", "ADAPTER", "CREATED_AT", "STATUS", "FINAL_EVAL_LOSS"):
            assert column in out
        assert older_version in out
        assert newer_version in out
        # Newest-first: the newer version's row appears before the older's.
        assert out.index(newer_version) < out.index(older_version)
        assert "tiny-test" in out
        assert "candidate" in out
        assert "0.5" in out
    finally:
        storage.delete_prefix(REGISTRY_BUCKET, f"{older_version}/")
        storage.delete_prefix(REGISTRY_BUCKET, f"{newer_version}/")


@pytest.mark.integration
def test_empty_registry_shows_friendly_message(monkeypatch, storage, capsys):
    """CLI-I-021: empty registry bucket -- friendly "no models registered" message,
    exit 0. Simulated by pointing _load_manifests at an empty sub-scope: monkeypatch
    download_dir to a no-op, since the real bucket may hold other tests' manifests."""
    monkeypatch.setattr(storage, "download_dir", lambda *a, **k: None)

    exit_code = registry_list(storage=storage)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "no models registered" in out


@pytest.mark.integration
def test_schema_invalid_manifest_listed_as_invalid(storage, capsys):
    """CLI-I-022: a seeded manifest that fails schema validation is listed as an
    INVALID row with its key, exit 0 -- list is a diagnostic tool, it must not die
    on one bad object."""
    run_id = new_run_id()
    model_version = f"tiny-test-{run_id}"
    # Missing required fields entirely -- guaranteed to fail RegistryManifest validation.
    storage.write_json(REGISTRY_BUCKET, f"{model_version}/manifest.json", {"not": "a manifest"})

    try:
        exit_code = registry_list(storage=storage)
        out = capsys.readouterr().out

        assert exit_code == 0
        assert f"{model_version}/manifest.json" in out
        assert "INVALID" in out
    finally:
        storage.delete_prefix(REGISTRY_BUCKET, f"{model_version}/")


@pytest.mark.integration
def test_registry_list_reachable_via_cli():
    """Confirms `tuner registry list` is wired up as a real subcommand end to end
    (not just the underlying registry_list() function) -- CLI-U-001's own coverage of
    `registry` being listed doesn't prove `list` itself resolves and runs."""
    result = CliRunner().invoke(cli, ["registry", "list"])
    assert result.exit_code == 0
