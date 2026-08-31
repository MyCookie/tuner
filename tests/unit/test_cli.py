"""Unit tests for the `tuner` CLI shell (CLI suite, docs/08-test-specs/cli.md)."""

from __future__ import annotations

import subprocess
import sys

from click.testing import CliRunner

import tuner.cli as cli_module
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


def test_invoke_stage_builds_correct_subprocess_argv(monkeypatch):
    """CLI-U-005: _invoke_stage (the real subprocess wrapper) with subprocess.run
    monkeypatched -- builds [sys.executable, "-m", "tuner", stage, "--run-id", ...,
    "--config", ...]; returns the subprocess's exact returncode. CLI-I-014 proves the
    real subprocess path end to end but isn't coverage-measurable for it (docs/08
    README's own "never subprocess, keeps coverage measurable" convention) -- this is
    that convention's in-process equivalent for this one boundary."""
    captured = {}

    def fake_run(argv, check):
        captured["argv"] = argv
        captured["check"] = check
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    exit_code = cli_module._invoke_stage("judge", "run-20260101-000000-abcdef", "/tmp/p.yaml")

    assert exit_code == 7
    assert captured["check"] is False
    assert captured["argv"] == [
        sys.executable,
        "-m",
        "tuner",
        "judge",
        "--run-id",
        "run-20260101-000000-abcdef",
        "--config",
        "/tmp/p.yaml",
    ]


def test_run_command_exits_with_run_pipelines_code(monkeypatch, tmp_path):
    """CLI-U-006: tuner run (the click command) with run_pipeline monkeypatched --
    exits with exactly run_pipeline's return value. Same coverage-gap reasoning as
    CLI-U-005: CLI-I-010..013 call run_pipeline() directly, never through the `run`
    command wrapper itself."""
    monkeypatch.setattr(cli_module, "run_pipeline", lambda config_path, **kwargs: 3)
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text("model:\n  adapter: tiny-test\ningest:\n  sources: []\n")

    result = CliRunner().invoke(cli, ["run", "--config", str(config_path)])

    assert result.exit_code == 3
