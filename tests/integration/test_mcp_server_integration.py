"""Integration tests for the MCP server (MCP suite, docs/spec/08-test-specs/mcp.md).

Needs compose MinIO up and the `tuner-mcp` pair (MCP_S3_ACCESS_KEY/SECRET_KEY) exported,
like every other integration suite. The server only ever sees the `tuner-mcp` credentials,
mapped onto TUNER_S3_*; the root TUNER_S3_* in the test environment is never passed on.
Manifests are seeded with the privileged `storage` fixture.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from mcp import Client, StdioServerParameters

from tuner.core.ids import new_run_id
from tuner.core.schemas import RegistryManifest
from tuner.core.storage import StorageClient
from tuner.mcp_server.server import build_server

REGISTRY_BUCKET = "tuner-registry"
REPO_ROOT = Path(__file__).parents[2]


def _manifest(version: str, run_id: str, created_at: str) -> dict:
    return {
        "model_version": version,
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
        "eval": {"final_train_loss": 1.0, "final_eval_loss": 0.5},
        "status": "candidate",
    }


@pytest.fixture
def mcp_env() -> dict[str, str]:
    """Exactly what the server process gets: the endpoint/region and the `mcp` pair."""
    env = {
        "TUNER_S3_ENDPOINT": os.environ["TUNER_S3_ENDPOINT"],
        "TUNER_S3_ACCESS_KEY": os.environ["MCP_S3_ACCESS_KEY"],
        "TUNER_S3_SECRET_KEY": os.environ["MCP_S3_SECRET_KEY"],
    }
    if "TUNER_S3_REGION" in os.environ:
        env["TUNER_S3_REGION"] = os.environ["TUNER_S3_REGION"]
    return env


@pytest.fixture
def mcp_storage(storage, monkeypatch, mcp_env) -> StorageClient:
    # `storage` (the privileged client) is built first, while the env still holds the root
    # credentials; only then is the env switched to the `mcp` pair.
    for name, value in mcp_env.items():
        monkeypatch.setenv(name, value)
    return StorageClient()


@pytest.fixture
def seeded(storage):
    """Two manifests (older, newer) written with the privileged client, then removed."""
    older_run, newer_run = new_run_id(), new_run_id()
    older, newer = f"tiny-test-{older_run}", f"tiny-test-{newer_run}"
    manifests = {
        older: _manifest(older, older_run, "2026-01-01T00:00:00Z"),
        newer: _manifest(newer, newer_run, "2026-06-01T00:00:00Z"),
    }
    for version, body in manifests.items():
        storage.write_json(REGISTRY_BUCKET, f"{version}/manifest.json", body)
    try:
        yield older, newer, manifests
    finally:
        for version in manifests:
            storage.delete_prefix(REGISTRY_BUCKET, f"{version}/")


@pytest.mark.integration
@pytest.mark.anyio
async def test_manifests_round_trip_through_both_tools(mcp_storage, seeded):
    """MCP-I-030: seeded manifests round-trip through both tools against real MinIO --
    list_models lists them newest first, get_model returns each as a valid manifest."""
    older, newer, manifests = seeded
    async with Client(build_server(mcp_storage)) as client:
        listed = await client.call_tool("list_models", {})
        got = {v: await client.call_tool("get_model", {"model_version": v}) for v in (older, newer)}

    versions = [r["model_version"] for r in listed.structured_content["models"]]
    assert {older, newer} <= set(versions)
    assert versions.index(newer) < versions.index(older)
    for version, result in got.items():
        assert not result.is_error
        assert (
            RegistryManifest.model_validate(result.structured_content).model_dump()
            == (manifests[version])
        )


@pytest.mark.integration
def test_mcp_principal_cannot_write_registry(mcp_storage, storage):
    """MCP-I-031: the `tuner-mcp` credentials, through the same StorageClient, are denied a
    write to tuner-registry (complements INF-I-003)."""
    key = f"mcp-write-probe-{new_run_id()}/manifest.json"
    try:
        with pytest.raises(ClientError) as excinfo:
            mcp_storage.write_json(REGISTRY_BUCKET, key, {"probe": True})
        assert excinfo.value.response["Error"]["Code"] == "AccessDenied"
    finally:
        # Only reached with a leaked object if the write was (wrongly) allowed.
        storage.delete_prefix(REGISTRY_BUCKET, key.split("/")[0] + "/")


# Runs `tuner mcp` as a child and copies its stdout to ours line by line, logging each raw
# line to a file the test reads back. The SDK's stdio client swallows non-JSON-RPC lines
# (and message_handler never sees them), so the raw wire has to be observed outside it.
_TEE = """
import subprocess, sys
log = open(sys.argv[1], "w")
child = subprocess.Popen([sys.executable, "-m", "tuner", "mcp"], stdout=subprocess.PIPE, text=True)
for line in child.stdout:
    log.write(line); log.flush()
    sys.stdout.write(line); sys.stdout.flush()
sys.exit(child.wait())
"""


@pytest.mark.integration
@pytest.mark.anyio
async def test_stdio_subprocess_stdout_is_only_json_rpc(mcp_env, seeded, tmp_path):
    """MCP-I-032: `tuner mcp` as a real stdio subprocess, driven by the SDK's own client
    with only the `tuner-mcp` pair. The call succeeds, and EVERY line the server wrote to
    stdout (read raw, through a tee) is a JSON-RPC message."""
    older, newer, _ = seeded
    wire = tmp_path / "stdout.log"
    params = StdioServerParameters(
        command=sys.executable, args=["-c", _TEE, str(wire)], env=mcp_env, cwd=REPO_ROOT
    )
    async with Client(params) as client:
        result = await client.call_tool("list_models", {})

    assert not result.is_error
    assert {older, newer} <= {r["model_version"] for r in result.structured_content["models"]}
    lines = wire.read_text().splitlines()
    assert lines, "the tee saw no stdout at all"
    for line in lines:
        assert json.loads(line)["jsonrpc"] == "2.0", line
