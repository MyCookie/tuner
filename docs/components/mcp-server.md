# MCP server

`tuner mcp` exposes the model registry to any standard
[Model Context Protocol](https://modelcontextprotocol.io) client — Claude Code
included — as two **read-only** tools over stdio. "Registry" means exactly what
[registry.md](registry.md) means: the manifests in `tuner-registry`. It holds no
state and has no database; the normative spec is
[spec/03-components/mcp-server.md](../spec/03-components/mcp-server.md).

## What it exposes

| Tool | Arguments | Returns |
| :--- | :--- | :--- |
| `list_models` | `status?` (`candidate`/`promoted`/`retired`), `adapter_name?` (both given: AND) | `models`: rows of `model_version`, `adapter_name`, `created_at`, `status`, `final_eval_loss`, newest first; `invalid_keys`: manifests that fail validation |
| `get_model` | `model_version` | the full registry manifest |

Unknown versions, bad filter values, invalid manifests and storage failures come
back as tool errors (`isError`); the server keeps running. `get_model` does not
check that the weights exist (`tuner registry show` would need read access to
`tuner-artifacts`, which the MCP principal doesn't have).

## Install and credentials

```bash
uv sync --extra dev --extra mcp     # not bare `--extra mcp`: that uninstalls dev and train
```

The server runs as the least-privilege `tuner-mcp` principal: read on
`tuner-registry` only. Its keypair is `MCP_S3_ACCESS_KEY` / `MCP_S3_SECRET_KEY` in
your `.env`; the client config maps it onto the `TUNER_S3_*` variables the server
reads. **Don't** use `uv run --env-file .env`: `.env` holds the root credentials
under `TUNER_S3_*`, which would defeat the point.

## Connect Claude Code

Make sure the stack is up (`docker compose up -d minio minio-init mlflow`), then:

```bash
claude mcp add --scope local --transport stdio tuner-registry \
  -e TUNER_S3_ENDPOINT=http://localhost:9000 \
  -e TUNER_S3_ACCESS_KEY=<value of MCP_S3_ACCESS_KEY> \
  -e TUNER_S3_SECRET_KEY=<value of MCP_S3_SECRET_KEY> \
  -- uv run --extra mcp --directory /path/to/tuner tuner mcp
```

Local scope keeps the config, secrets included, in your uncommitted
`~/.claude.json`. Then ask Claude to list the models in the registry.

Alternatively, a `.mcp.json` that keeps the keys in your shell environment
(Claude Code expands `${VAR}` inside `env`; export `MCP_S3_ACCESS_KEY` and
`MCP_S3_SECRET_KEY` before launching `claude`):

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

No project `.mcp.json` is committed to this repo, on purpose.

## Inspect it (optional)

The MCP Inspector can browse the tools interactively. It needs Node, and is not
installed or exercised by this project's gate. It launches any stdio server, so
give it the same command and the same `TUNER_S3_*` environment as above:

```bash
TUNER_S3_ENDPOINT=http://localhost:9000 \
TUNER_S3_ACCESS_KEY="$MCP_S3_ACCESS_KEY" TUNER_S3_SECRET_KEY="$MCP_S3_SECRET_KEY" \
  npx @modelcontextprotocol/inspector uv run --extra mcp --directory /path/to/tuner tuner mcp
```

(`uv run mcp dev <file>` from the SDK's `mcp[cli]` extra does the same for a server
module, but this package has no standalone module to point it at.)

## stdout is the wire

The server writes nothing to stdout except protocol messages; logs go to stderr.
If you wrap or extend it, never `print()`.
