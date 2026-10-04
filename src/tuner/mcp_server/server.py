"""The `MCPServer` and its two read-only tools (docs/spec/03-components/mcp-server.md).

stdout is the protocol wire: nothing here prints, and the SDK logs to stderr.
"""

from __future__ import annotations

from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError

from tuner.core.buckets import REGISTRY
from tuner.core.schemas import RegistryManifest
from tuner.core.storage import StorageClient
from tuner.registry_ops.cli import load_manifests

Status = Literal["candidate", "promoted", "retired"]
_READ_ONLY = ToolAnnotations(readOnlyHint=True)


class ModelSummary(BaseModel):
    model_version: str
    adapter_name: str
    created_at: str
    status: Status
    final_eval_loss: float


class ModelList(BaseModel):
    models: list[ModelSummary]
    invalid_keys: list[str]


def build_server(storage: StorageClient) -> MCPServer:
    server = MCPServer("tuner-registry")

    @server.tool(annotations=_READ_ONLY)
    def list_models(status: Status | None = None, adapter_name: str | None = None) -> ModelList:
        """List registered model versions, newest first. Filters combine as AND.
        `invalid_keys` names manifests that fail schema validation."""
        try:
            manifests, invalid_keys = load_manifests(storage)
        except Exception as exc:
            raise ToolError(f"registry read failed: {exc}") from exc
        manifests.sort(key=lambda m: m.created_at, reverse=True)
        return ModelList(
            models=[
                ModelSummary(
                    model_version=m.model_version,
                    adapter_name=m.adapter_name,
                    created_at=m.created_at,
                    status=m.status,
                    final_eval_loss=m.eval.final_eval_loss,
                )
                for m in manifests
                if status in (None, m.status) and adapter_name in (None, m.adapter_name)
            ],
            invalid_keys=invalid_keys,
        )

    @server.tool(annotations=_READ_ONLY)
    def get_model(model_version: str) -> RegistryManifest:
        """Return the full registry manifest for one model version. Does not check that the
        weights objects exist."""
        try:
            raw = storage.read_json(REGISTRY, f"{model_version}/manifest.json")
        except Exception as exc:
            raise ToolError(f"registry read failed: {exc}") from exc
        if raw is None:
            raise ToolError(f"unknown model_version: {model_version}")
        try:
            return RegistryManifest.model_validate(raw)
        except ValidationError as exc:
            raise ToolError(f"invalid manifest for {model_version}: {exc}") from exc

    return server
