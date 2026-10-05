# Component Spec: MCP Server (model registry, read-only)

**Purpose:** Expose the model registry to any standard [Model Context Protocol](https://modelcontextprotocol.io) client (Claude Code included) as two read-only tools. "Registry" means exactly what [registry.md](registry.md) means: the registry manifests in `tuner-registry` ([02-data-contracts.md §5.2](../02-data-contracts.md)) — the same objects `tuner registry list` reads. No tier or stage manifests, no MLflow. There is no database and the server holds no state.

## CLI

```
tuner mcp
```

Serves MCP over **stdio** until the client closes the stream. It takes neither `--run-id` nor `--config` (like `registry`, it spans every run; [01 §4.4](../01-architecture.md)). It is registered lazily in `tuner/cli.py` (`_LAZY_COMMANDS`/`_LAZY_HELP`), so `tuner --help` lists `mcp` and the base install works without the SDK. When the `mcp` extra is missing, `tuner mcp` exits non-zero with a message naming the `mcp` extra, noting that `dev` includes it, and prescribing the project-standard `uv sync --extra dev --extra train` (any `uv sync` naming `--extra mcp` instead would uninstall `train`). `_LazyGroup`'s missing-extra message is currently hard-coded to the `train` extra; it becomes **per-command** (each lazy command names its own extra).

Package: `src/tuner/mcp_server/` (not `tuner/mcp/`, which could be confused with the SDK's top-level `mcp` package). Extra: `mcp = ["mcp>=2.3,<3"]`; the `dev` extra includes the same requirement. The SDK requires `pydantic>=2.12`, so the base `pydantic` floor is raised from `>=2.6`. SDK 2.x renamed `FastMCP` to `mcp.server.MCPServer` and the in-memory test helper to `mcp.Client`; v1 tutorials do not apply.

## Env and credentials

The server reads only the canonical `TUNER_S3_ENDPOINT`, `TUNER_S3_ACCESS_KEY`, `TUNER_S3_SECRET_KEY`, `TUNER_S3_REGION` ([01 §4.3](../01-architecture.md)), through `StorageClient`. It must run as the read-only principal **`mcp`** (the policy is `tuner-mcp`; the MinIO user is keyed by the `MCP_S3_ACCESS_KEY` value): **R on `tuner-registry` only** ([05 §5](../05-infrastructure.md)). The registry-ops credentials (RW registry, R artifacts) are not used.

The provisioning pair follows the existing `<PRINCIPAL>_S3_*` pattern (`_env_prefix` in `scripts/bootstrap_minio.py`): **`MCP_S3_ACCESS_KEY` / `MCP_S3_SECRET_KEY`**, consumed by `minio-init` and by the client config below. The client config maps that pair onto `TUNER_S3_*` for the server process.

`uv run --env-file .env` is **not acceptable** for this server: `.env` sets `TUNER_S3_*` to the **root** credentials ([08 infra.md](../08-test-specs/infra.md) Setup), which would defeat the least-privilege principal.

`INF-I-001` and `INF-I-003` ([08 infra.md](../08-test-specs/infra.md)) already cover the new principal through the literal matrix transcription; no new `INF` IDs are added for it.

### Client configuration

Documented form: `claude mcp add` at **local** scope. It stores the config, secrets included, in the user's uncommitted `~/.claude.json`, so nothing secret reaches the repo:

```bash
claude mcp add --scope local --transport stdio tuner-registry \
  -e TUNER_S3_ENDPOINT=http://localhost:9000 \
  -e TUNER_S3_ACCESS_KEY=<value of MCP_S3_ACCESS_KEY> \
  -e TUNER_S3_SECRET_KEY=<value of MCP_S3_SECRET_KEY> \
  -- uv run --extra mcp --directory /path/to/tuner tuner mcp
```

`--extra mcp` makes `uv run` install the SDK if absent without removing other installed extras (`uv run` syncs inexactly; checked 2026-10-04); the `--directory` flag selects the project; no `--env-file` is used. (Server name `tuner-registry` is a client-side label.)

Alternative, `.mcp.json` form: Claude Code expands `${VAR}` from the launching shell's environment into a server's `env` block (**VERIFIED 2026-10-04**, Claude Code 2.1.285, human-run probe: [#61 comment](https://github.com/MyCookie/tuner/issues/61#issuecomment-5984964626)). The pair can therefore stay in the user's shell environment and out of the file:

```json
{"mcpServers": {"tuner-registry": {
  "command": "uv", "args": ["run", "--extra", "mcp", "--directory", "/path/to/tuner", "tuner", "mcp"],
  "env": {
    "TUNER_S3_ENDPOINT": "http://localhost:9000",
    "TUNER_S3_ACCESS_KEY": "${MCP_S3_ACCESS_KEY}",
    "TUNER_S3_SECRET_KEY": "${MCP_S3_SECRET_KEY}"
  }
}}}
```

Only `${VAR}` inside `env` is verified; `${VAR:-default}` and expansion in `command`/`args`/`url`/`headers` are not.

Committing a project-scope `.mcp.json` is **out of scope**: it would prompt every agent session in this repo.

## Operations

Primitives: **tools only, no resources** (every standard client supports tools, and Claude Code calls them itself). Both tools declare the read-only tool annotation (`readOnlyHint: true`). Each returns a typed pydantic model, so the SDK publishes an `outputSchema` and returns `structuredContent` alongside the text content. Manifest loading reuses the `registry_ops` loader (`_load_manifests` becomes a public function, e.g. `load_manifests`); it is not re-implemented, and storage is reached only through `StorageClient`.

- **`list_models(status?, adapter_name?)`** — optional filters (`status` ∈ the `RegistryManifest` status values, [02 §5.2](../02-data-contracts.md)); when both are given they combine as AND. Returns summary rows — `model_version`, `adapter_name`, `created_at`, `status`, `final_eval_loss` — newest first (the same order as `tuner registry list`), plus `invalid_keys`: the keys of manifests that fail validation. This mirrors `CLI-I-022`: a diagnostic tool must not fail because of one bad object. An empty registry returns empty rows, not an error.
- **`get_model(model_version)`** — returns the full `RegistryManifest` ([02 §5.2](../02-data-contracts.md); not restated here).

**Errors** are raised as the SDK's `ToolError`, which returns `isError: true`: an unknown `model_version`; an invalid filter value; an invalid manifest under the requested key; a storage failure. A storage failure never crashes the server. An error string is never returned as a success.

**Deliberate difference from `registry show`:** `get_model` does **not** verify that the `weights_uri` objects exist. `show` does, but that needs R on `tuner-artifacts`, which the `mcp` principal does not hold.

**stdout is the protocol wire.** Nothing but the SDK may write to stdout: no `print()`, and all logging goes to stderr. A test proves it (`MCP-I-032`).

## Acceptance criteria

- `tools/list` returns exactly `list_models` and `get_model`, with input schemas, output schemas and the read-only annotation.
- With a trained candidate in the registry, a client calling `list_models` sees it; `get_model` returns its full manifest.
- The server runs with the `tuner-mcp` credentials alone; it cannot write to or read any bucket but `tuner-registry`.
- `uv sync` without extras then `tuner --help` lists `mcp` without importing the SDK; `tuner mcp` without the extra prints the "needs the `mcp` extra" message.
- Test suite: [08 mcp.md](../08-test-specs/mcp.md).

## Future phases

Resources (e.g. one resource per manifest); streamable HTTP transport (needs auth, which stdio does not); `show`-style weight-existence checks (needs an artifacts-read grant, a separate principal decision); promote/rollback tools (Phase 2′, after [registry.md](registry.md) Phase 2).
