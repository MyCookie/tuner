"""Unit tests for the `tuner` CLI shell (CLI suite, docs/08-test-specs/cli.md)."""

from __future__ import annotations

from click.testing import CliRunner

from tuner.cli import cli


def test_help_lists_exactly_the_documented_commands():
    """CLI-U-001: `tuner --help` lists exactly: ingest, clean, judge, tokenize,
    train, smoke, run, registry (01-architecture.md §4.4)."""
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    listed = set(cli.list_commands(None))
    assert listed == {
        "ingest",
        "clean",
        "judge",
        "tokenize",
        "train",
        "smoke",
        "run",
        "registry",
    }
    for name in listed:
        assert name in result.output


def test_stage_subcommand_without_run_id_exits_2():
    """CLI-U-002: a stage subcommand invoked without --run-id is a click usage
    error, exit 2 -- every subcommand shares this via the same required option."""
    result = CliRunner().invoke(cli, ["ingest", "--config", "configs/pipeline.yaml"])

    assert result.exit_code == 2
    assert "Missing option" in result.output
    assert "--run-id" in result.output


def test_run_id_bad_format_exits_2_naming_the_regex():
    """CLI-U-003: --run-id not-a-run-id (bad format) -- exit 2, with the canonical
    format regex itself in the message, not just a generic "invalid" complaint."""
    result = CliRunner().invoke(
        cli, ["ingest", "--run-id", "not-a-run-id", "--config", "configs/pipeline.yaml"]
    )

    assert result.exit_code == 2
    assert r"run-\d{8}-\d{6}-[0-9a-f]{6}" in result.output


def test_default_config_resolves_to_pipeline_yaml():
    """CLI-U-004: the default --config resolves to configs/pipeline.yaml, shown on
    every stage subcommand's own --help (01 §4.4's common-options contract)."""
    result = CliRunner().invoke(cli, ["ingest", "--help"])

    assert result.exit_code == 0
    assert "configs/pipeline.yaml" in result.output
