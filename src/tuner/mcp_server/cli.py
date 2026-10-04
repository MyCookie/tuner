"""`tuner mcp` -- serve the registry over MCP stdio until the client disconnects."""

from __future__ import annotations

import sys

import click

from tuner.core.config import ConfigError
from tuner.core.storage import StorageClient
from tuner.mcp_server.server import build_server


@click.command(name="mcp")
def mcp_command() -> None:
    """Serve the model registry to MCP clients over stdio (read-only)."""
    try:
        storage = StorageClient()
    except ConfigError as exc:
        click.echo(f"mcp: {exc}", err=True)
        sys.exit(2)
    build_server(storage).run("stdio")
