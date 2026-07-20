"""`tuner` entrypoint — subcommand skeleton (01-architecture.md §4.4).

Stage logic lands in later build-plan tasks; every stage here is a stub that
exits 1 until its task implements it.
"""

from __future__ import annotations

import sys

import click

from tuner.core.config import DEFAULT_CONFIG_PATH

STAGES = ("ingest", "clean", "judge", "tokenize", "train", "smoke")


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
