"""`tuner` entrypoint — subcommand skeleton (01-architecture.md §4.4).

`ingest` (T06), `clean` (T07), `judge` (T08), `tokenize` (T10), `train` (T11), and
`smoke` (T12) are real; `run` is still a T01 stub that exits 1 until its build task
implements it.

`train` and `smoke` are lazily imported -- their modules pull in
torch/transformers/peft/accelerate, the `train` extra (05-infrastructure.md §3), which
CPU-only stages and their `dev`-extra-only environments never install. Importing them
eagerly here would make every `tuner` invocation -- including `tuner ingest` or even
`tuner --help` -- require the `train` extra, breaking the "same commands, same env
vars" host-venv-fallback contract for everyone else.
"""

from __future__ import annotations

import importlib
import sys

import click

from tuner.cleaner.cli import clean_command
from tuner.core.config import DEFAULT_CONFIG_PATH
from tuner.ingestor.cli import ingest_command
from tuner.judge.cli import judge_command
from tuner.tokenizer.cli import tokenize_command

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
