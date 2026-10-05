"""Unit tests for the MCP server (MCP suite, docs/spec/08-test-specs/mcp.md), driven
through the SDK's in-memory client against an in-memory stand-in for StorageClient."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from tuner.core.schemas import RegistryManifest
from tuner.mcp_server.server import build_server


class FakeStorage:
    """The two StorageClient reads the server uses, over a dict of key -> bytes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False

    def put(self, manifest: dict[str, Any]) -> None:
        self.objects[f"{manifest['model_version']}/manifest.json"] = json.dumps(manifest).encode()

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("storage down")

    def download_dir(self, bucket: str, prefix: str, local_dir: str) -> None:
        self._check()
        for key, body in self.objects.items():
            path = Path(local_dir) / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

    def read_json(self, bucket: str, key: str) -> dict[str, Any] | None:
        self._check()
        body = self.objects.get(key)
        return None if body is None else json.loads(body)


def manifest(version: str, created_at: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "model_version": version,
        "run_id": "run-20260101-000000-abc123",
        "adapter_name": "tiny-test",
        "base_model": "org/base",
        "method": "qlora",
        "created_at": created_at,
        "gold_manifest_uri": "s3://tuner-gold/run/manifest.json",
        "index_map_uri": "s3://tuner-gold/run/index_map.json",
        "weights_uri": "s3://tuner-artifacts/run/adapter",
        "mlflow_run_id": "abc",
        "hyperparameters": {"lr": 0.1},
        "eval": {"final_train_loss": 1.0, "final_eval_loss": 0.5},
        "status": "candidate",
    }
    out = {**base, **overrides}
    RegistryManifest.model_validate(out)  # the fixture itself must be valid
    return out


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def server(storage):
    return build_server(storage)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_tools_list(server):
    """MCP-U-001: tools/list is exactly list_models and get_model, each with input and
    output schemas and readOnlyHint."""
    async with Client(server) as client:
        tools = (await client.list_tools()).tools

    assert {t.name for t in tools} == {"list_models", "get_model"}
    for tool in tools:
        assert tool.input_schema
        assert tool.output_schema
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


@pytest.mark.anyio
async def test_list_empty_registry(server):
    """MCP-U-010: an empty registry returns empty rows and empty invalid_keys."""
    async with Client(server) as client:
        result = await client.call_tool("list_models", {})

    assert not result.is_error
    assert result.structured_content == {"models": [], "invalid_keys": []}


@pytest.mark.anyio
async def test_list_newest_first_with_summary_fields(server, storage):
    """MCP-U-011: rows are newest first and carry the five summary fields."""
    storage.put(manifest("old", "2026-01-01T00:00:00Z"))
    storage.put(
        manifest(
            "new", "2026-03-01T00:00:00Z", eval={"final_train_loss": 2, "final_eval_loss": 0.25}
        )
    )
    storage.put(manifest("mid", "2026-02-01T00:00:00Z"))
    async with Client(server) as client:
        result = await client.call_tool("list_models", {})

    rows = result.structured_content["models"]
    assert [r["model_version"] for r in rows] == ["new", "mid", "old"]
    assert set(rows[0]) == {
        "model_version",
        "adapter_name",
        "created_at",
        "status",
        "final_eval_loss",
    }
    assert rows[0]["final_eval_loss"] == 0.25
    assert rows[0]["created_at"] == "2026-03-01T00:00:00Z"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"status": "promoted"}, {"a"}),
        ({"adapter_name": "other"}, {"b"}),
        ({"status": "promoted", "adapter_name": "other"}, set()),
        ({"status": "candidate", "adapter_name": "other"}, {"b"}),
    ],
)
async def test_list_filters_combine_as_and(server, storage, args, expected):
    """MCP-U-012: status, adapter_name, and both together (AND)."""
    storage.put(manifest("a", "2026-01-01T00:00:00Z", status="promoted"))
    storage.put(manifest("b", "2026-01-02T00:00:00Z", adapter_name="other"))
    async with Client(server) as client:
        result = await client.call_tool("list_models", args)

    assert {r["model_version"] for r in result.structured_content["models"]} == expected


@pytest.mark.anyio
async def test_list_invalid_status_is_error(server):
    """MCP-U-013: an invalid status filter value is isError."""
    async with Client(server) as client:
        result = await client.call_tool("list_models", {"status": "bogus"})

    assert result.is_error


@pytest.mark.anyio
async def test_list_reports_invalid_manifest_keys(server, storage):
    """MCP-U-014: a schema-invalid manifest is listed in invalid_keys, not an error."""
    storage.put(manifest("good", "2026-01-01T00:00:00Z"))
    storage.objects["bad/manifest.json"] = b'{"not": "a manifest"}'
    async with Client(server) as client:
        result = await client.call_tool("list_models", {})

    assert not result.is_error
    assert [r["model_version"] for r in result.structured_content["models"]] == ["good"]
    assert result.structured_content["invalid_keys"] == ["bad/manifest.json"]


@pytest.mark.anyio
async def test_get_model_returns_full_manifest(server, storage):
    """MCP-U-020: get_model's structuredContent validates as RegistryManifest and equals
    the seeded manifest."""
    seeded = manifest("v1", "2026-01-01T00:00:00Z")
    storage.put(seeded)
    async with Client(server) as client:
        result = await client.call_tool("get_model", {"model_version": "v1"})

    assert not result.is_error
    assert RegistryManifest.model_validate(result.structured_content).model_dump() == seeded


@pytest.mark.anyio
async def test_get_model_unknown_version(server):
    """MCP-U-021: unknown version is isError and the message names the version."""
    async with Client(server) as client:
        result = await client.call_tool("get_model", {"model_version": "nope-1"})

    assert result.is_error
    assert "nope-1" in result.content[0].text


@pytest.mark.anyio
async def test_get_model_invalid_manifest(server, storage):
    """MCP-U-022: a manifest that fails validation is isError."""
    storage.objects["v1/manifest.json"] = b'{"not": "a manifest"}'
    async with Client(server) as client:
        result = await client.call_tool("get_model", {"model_version": "v1"})

    assert result.is_error


@pytest.mark.anyio
async def test_get_model_does_not_check_weights(server, storage):
    """MCP-U-023: succeeds although the weights objects do not exist (the storage fake
    holds only the manifest), unlike `registry show`."""
    storage.put(manifest("v1", "2026-01-01T00:00:00Z"))
    async with Client(server) as client:
        result = await client.call_tool("get_model", {"model_version": "v1"})

    assert not result.is_error


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "args"),
    [("list_models", {}), ("get_model", {"model_version": "v1"})],
)
async def test_storage_failure_is_error_and_session_survives(server, storage, tool, args):
    """MCP-U-030: a storage failure is isError, and the same session then serves a
    following call successfully."""
    storage.put(manifest("v1", "2026-01-01T00:00:00Z"))
    async with Client(server) as client:
        storage.fail = True
        failed = await client.call_tool(tool, args)
        storage.fail = False
        ok = await client.call_tool(tool, args)

    assert failed.is_error
    assert "storage down" in failed.content[0].text
    assert not ok.is_error
