"""`tuner` entrypoint — subcommand skeleton (01-architecture.md §4.4).

`ingest` (T06), `clean` (T07), `judge` (T08), and `tokenize` (T10) are real; the rest of
STAGES plus `run` are still T01 stubs that exit 1 until their build task implements them.
"""

from __future__ import annotations

import sys

import click

from tuner.cleaner.cli import clean_command
from tuner.core.config import DEFAULT_CONFIG_PATH
from tuner.ingestor.cli import ingest_command
from tuner.judge.cli import judge_command
from tuner.tokenizer.cli import tokenize_command

STAGES = ("train", "smoke")


@click.group()
def cli() -> None:
    """Tuner — Enterprise Fine-Tuning Pipeline."""


def _make_stage_stub(stage: str) -> click.Command:
    @click.command(name=stage)
    @click.option("--run-id", required=True, help="Run ID shared across the pipeline.")
    @click.option(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        show_default=True,
        help="Pipeline config path.",
    )
    def _stub(run_id: str, config: str) -> None:
        click.echo(f"{stage}: not implemented", err=True)
        sys.exit(1)

    return _stub


cli.add_command(ingest_command)
cli.add_command(clean_command)
cli.add_command(judge_command)
cli.add_command(tokenize_command)
for _stage in STAGES:
    cli.add_command(_make_stage_stub(_stage))


@cli.command()
@click.option(
    "--config",
    default=str(DEFAULT_CONFIG_PATH),
    show_default=True,
    help="Pipeline config path.",
)
def run(config: str) -> None:
    """Run the full pipeline (driver implemented in build task T13)."""
    click.echo("run: not implemented", err=True)
    sys.exit(1)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
