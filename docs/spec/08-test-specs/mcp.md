# Test Suite: MCP Server (`MCP`)

Spec under test: [mcp-server.md](../03-components/mcp-server.md), with contracts in [02 §5.2](../02-data-contracts.md) and the principal in [05 §5](../05-infrastructure.md). Files: `tests/unit/test_mcp_server.py`, `tests/integration/test_mcp_server.py`. Built in the implementation task for #63 ([07](../07-build-plan.md)).

## Setup

- **Unit:** drive the server through the SDK's in-memory client, `mcp.Client(server)`, with `@pytest.mark.anyio` (the anyio pytest plugin ships with anyio; no pytest-asyncio), against a stub or seeded `StorageClient`.
- **Integration:** compose MinIO, connecting **with the `tuner-mcp` credentials** (`MCP_S3_ACCESS_KEY`/`MCP_S3_SECRET_KEY`); manifests are seeded with a separate, privileged fixture client.
- `INF-I-001`/`INF-I-003` already cover the `mcp` principal via the matrix transcription; there are no INF cases here.

## Unit (in-memory client)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| MCP-U-001 | `tools/list` | Exactly `list_models` and `get_model`; each has an input schema, an output schema, and `readOnlyHint: true` |
| MCP-U-010 | `list_models`, empty registry | Success; empty rows; empty `invalid_keys` |
| MCP-U-011 | `list_models`, several manifests with differing `created_at` | Rows newest first; each row carries model_version, adapter_name, created_at, status, final_eval_loss ([mcp-server.md](../03-components/mcp-server.md)) |
| MCP-U-012 | `list_models` with `status`, with `adapter_name`, and with both | Only matching rows; filters combine as AND |
| MCP-U-013 | `list_models` with an invalid `status` value | `isError: true` |
| MCP-U-014 | A manifest that fails schema validation among valid ones | Valid rows returned; its key listed in `invalid_keys`; not an error (mirrors `CLI-I-022`) |
| MCP-U-020 | `get_model` for an existing version | `structuredContent` validates as `RegistryManifest` ([02 §5.2](../02-data-contracts.md)) and equals the seeded manifest |
| MCP-U-021 | `get_model` for an unknown version | `isError: true`, message names the version |
| MCP-U-022 | `get_model` for a version whose manifest is invalid | `isError: true` |
| MCP-U-023 | `get_model` whose manifest's weights objects do not exist | Success (no weight-existence check; deliberate difference from `registry show`) |
| MCP-U-030 | Storage raises on a call | `isError: true`; the same session then serves a following call successfully |

## Integration (compose MinIO, `tuner-mcp` credentials)

| ID | Scenario | Expected |
| :--- | :--- | :--- |
| MCP-I-030 | Seeded manifests round-trip through both tools against real MinIO | `list_models` lists them newest first; `get_model` returns each as a valid `RegistryManifest` |
| MCP-I-031 | The `tuner-mcp` credentials attempting a write to `tuner-registry` through the same `StorageClient` | Denied (the server's principal is read-only; complements `INF-I-003`) |
| MCP-I-032 | **Stdio subprocess:** launch `tuner mcp` via `mcp.Client(StdioServerParameters(...))` with `TUNER_S3_*` set to the `tuner-mcp` pair; call `list_models` | The call succeeds, and nothing but JSON-RPC messages reached the server's stdout¹ |

¹ **Explicit exception** to the README's "never subprocess" rule for exit-code assertions ([README Conventions](README.md)): the protocol wire is exactly what is under test. Coverage comes from the unit cases, not this one.
