"""`tuner` entrypoint — subcommand skeleton (01-architecture.md §4.4).

`ingest` (T06), `clean` (T07), `judge` (T08), `tokenize` (T10), `train` (T11),
`smoke` (T12), `run` and `registry` (T13) are all real -- every command in the
architecture doc's CLI glossary is implemented as of this task.

`train` and `smoke` are lazily imported -- their modules pull in
torch/transformers/peft/accelerate, the `train` extra (05-infrastructure.md §3), which
CPU-only stages and their `dev`-extra-only environments never install. Importing them
eagerly here would make every `tuner` invocation -- including `tuner ingest` or even
`tuner --help` -- require the `train` extra, breaking the "same commands, same env
vars" host-venv-fallback contract for everyone else. `registry` has no such
dependency (just `StorageClient` + pydantic, same as `ingest`/`clean`/`judge`) so it's
registered eagerly, same as those.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from typing import Protocol

import click
import mlflow

from tuner.cleaner.cli import clean_command
from tuner.core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from tuner.core.ids import new_run_id
from tuner.core.storage import StorageClient
from tuner.ingestor.cli import ingest_command
from tuner.judge.cli import judge_command
from tuner.registry_ops.cli import registry_group
from tuner.tokenizer.cli import tokenize_command

ARTIFACTS_BUCKET = "tuner-artifacts"
REGISTRY_BUCKET = "tuner-registry"

# ingest -> clean -> judge -> tokenize -> train -> smoke (01 §1.1's full pipeline
# flow; registry ops is a separate, human-in-the-loop CLI, not part of this order).
STAGE_ORDER = ("ingest", "clean", "judge", "tokenize", "train", "smoke")

# name -> "module.path:attribute", imported only when that subcommand is actually
# invoked (click's own documented "Lazily Loading Subcommands" recipe). Paired with a
# static short-help string (_LAZY_HELP) so the group's own `--help` listing never
# imports a lazy command's module just to read its docstring -- see
# _LazyGroup.format_commands.
_LAZY_COMMANDS = {
    "train": "tuner.trainer.cli:train_command",
    "smoke": "tuner.smoke.cli:smoke_command",
}
_LAZY_HELP = {
    "train": "Fine-tune the selected adapter's base model on tokenized Gold data.",
    "smoke": "Generate before/after transcripts proving the trained model changed behavior.",
}


class _LazyGroup(click.Group):
    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*_LAZY_COMMANDS, *super().list_commands(ctx)})

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        if name in _LAZY_COMMANDS:
            module_name, attr_name = _LAZY_COMMANDS[name].split(":")
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError as exc:
                # A raw ModuleNotFoundError here names some third-party package, not
                # the actual fix -- point at the real one (05 §3's host-venv fallback,
                # PR #11 review round 1 nit).
                raise click.ClickException(
                    f"'{name}' needs the `train` extra (torch/transformers/peft/"
                    f"accelerate) -- run `uv sync --extra train` (05-infrastructure.md "
                    f"§3). Underlying import error: {exc}"
                ) from exc
            return getattr(module, attr_name)
        return super().get_command(ctx, name)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Overridden (not the inherited click.Group.format_commands) so `tuner --help`
        # never imports a lazy command's module -- torch/peft/etc (the `train` extra)
        # must not be required merely to list subcommands (05 §3's "same commands,
        # same env vars" contract; T01's own `tuner --help` Verify line). Real
        # subcommands are still fetched via get_command for their actual help text;
        # only lazy ones use the static _LAZY_HELP string instead.
        names = self.list_commands(ctx)
        limit = formatter.width - 6 - max(len(n) for n in names)
        rows: list[tuple[str, str]] = []
        for name in names:
            if name in _LAZY_COMMANDS:
                rows.append((name, _LAZY_HELP.get(name, "")))
                continue
            cmd = super().get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            rows.append((name, cmd.get_short_help_str(limit)))
        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)


@click.group(cls=_LazyGroup)
def cli() -> None:
    """Tuner — Enterprise Fine-Tuning Pipeline."""


cli.add_command(ingest_command)
cli.add_command(clean_command)
cli.add_command(judge_command)
cli.add_command(tokenize_command)
cli.add_command(registry_group)


class InvokeStage(Protocol):
    def __call__(self, stage: str, run_id: str, config_path: str) -> int: ...


def _invoke_stage(stage: str, run_id: str, config_path: str) -> int:
    """Runs one stage as a subprocess (01-architecture.md §2) -- the same
    process-isolation boundary these stages get as separate KFP components in Phase 3
    (05 §4). stdout/stderr are inherited, not captured, so a human watching `tuner
    run` sees each stage's own output as it happens."""
    result = subprocess.run(
        ["tuner", stage, "--run-id", run_id, "--config", config_path], check=False
    )
    return result.returncode


def run_pipeline(
    config_path: str,
    storage: StorageClient | None = None,
    invoke_stage: InvokeStage = _invoke_stage,
) -> int:
    """Run the full pipeline end to end; returns the process exit code (0/1/2/3).

    The driver is intentionally dumb (01 §2): no retries, no partial resume -- it
    generates the run ID, invokes each stage in order, aborts on the first non-zero
    exit code, and prints the final artifact locations on success."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        click.echo(f"run: {exc}", err=True)
        return 2

    run_id = new_run_id()
    click.echo(f"run: starting pipeline run {run_id}")

    for stage in STAGE_ORDER:
        exit_code = invoke_stage(stage, run_id, config_path)
        if exit_code == 0:
            continue
        if exit_code == 3:
            # Named distinctly from a generic failure (CLI-I-012): zero records is
            # an empty-pipeline condition, not a bug in the stage that hit it.
            click.echo(f"run: pipeline empty at {stage}", err=True)
            return 3
        click.echo(f"run: stage {stage!r} failed (exit {exit_code})", err=True)
        return exit_code

    storage = storage or StorageClient()
    model_version = f"{config.model.adapter}-{run_id}"
    manifest = storage.read_json(REGISTRY_BUCKET, f"{model_version}/manifest.json")
    if manifest is None:
        click.echo(f"run: run_id: {run_id}")
        click.echo(
            f"run: pipeline completed but no registry manifest found for "
            f"{model_version} -- this indicates a bug, not a pipeline failure",
            err=True,
        )
        return 1

    transcript_uri = f"s3://{ARTIFACTS_BUCKET}/{run_id}/smoke/transcript.json"
    tracking_uri = os.environ["MLFLOW_TRACKING_URI"]
    mlflow.set_tracking_uri(tracking_uri)
    mlflow_run = mlflow.get_run(manifest["mlflow_run_id"])
    run_url = (
        f"{tracking_uri.rstrip('/')}/#/experiments/"
        f"{mlflow_run.info.experiment_id}/runs/{mlflow_run.info.run_id}"
    )

    click.echo(f"run: run_id: {run_id}")
    click.echo(f"run: model/adapter: {manifest['weights_uri']}")
    click.echo(f"run: transcript: {transcript_uri}")
    click.echo(f"run: mlflow run: {run_url}")

    return 0


@cli.command()
@click.option(
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG_PATH),
    show_default=True,
    help="Pipeline config path.",
)
def run(config_path: str) -> None:
    """Run the full pipeline: ingest -> clean -> judge -> tokenize -> train -> smoke."""
    sys.exit(run_pipeline(config_path))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
