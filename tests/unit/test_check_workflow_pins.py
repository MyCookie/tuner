"""CI workflow pin checker (INF suite, docs/spec/08-test-specs/infra.md) -- runs
scripts/check_workflow_pins.py's `main` in-process against sample workflows in
tmp_path (passed as WORKFLOW_DIR), never the real .github/workflows/."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml
from scripts.check_workflow_pins import main

SHA = "a" * 40
CHECKOUT = f"actions/checkout@{SHA}"


class Result(NamedTuple):
    returncode: int
    stderr: str


@pytest.fixture
def run_check(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def run(workflow_dir: Path) -> Result:
        monkeypatch.setattr(sys, "argv", ["check_workflow_pins.py", str(workflow_dir)])
        code = main()
        return Result(code, capsys.readouterr().err)

    return run


def _workflow(tmp_path: Path, body: str, name: str = "wf.yml") -> Path:
    (tmp_path / name).write_text(textwrap.dedent(body))
    return tmp_path


def _single_job(step: dict[str, object]) -> str:
    wf = {
        "on": "push",
        "permissions": {"contents": "read"},
        "jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [step]}},
    }
    return yaml.safe_dump(wf)


@pytest.mark.parametrize(
    "uses",
    [
        "astral-sh/setup-uv@v7",
        "astral-sh/setup-uv@main",
        "astral-sh/setup-uv@" + SHA[:7],
        "astral-sh/setup-uv@" + SHA[:39],
        "astral-sh/setup-uv@" + SHA + "0",
        "astral-sh/setup-uv@" + SHA.upper(),
        "astral-sh/setup-uv",
        "docker://alpine:3.20",
        "docker://alpine@sha256:" + "b" * 64,
    ],
    ids=[
        "tag",
        "branch",
        "short-sha",
        "39-hex",
        "41-hex",
        "uppercase-sha",
        "no-ref",
        "docker-tag",
        "docker-digest",
    ],
)
def test_unpinned_step_action_is_flagged(tmp_path: Path, run_check, uses: str) -> None:
    """INF-U-013: an action not pinned to a full lowercase 40-hex SHA fails the check."""
    result = run_check(_workflow(tmp_path, _single_job({"uses": uses})))
    assert result.returncode == 1
    assert f"`{uses}` is not pinned to a full SHA" in result.stderr


def test_unpinned_reusable_workflow_job_is_flagged(tmp_path: Path, run_check) -> None:
    """INF-U-013: `uses` on the job itself (reusable-workflow call) is checked too."""
    wf = """\
        on: push
        permissions: {contents: read}
        jobs:
          call:
            uses: org/repo/.github/workflows/x.yml@v1
        """
    result = run_check(_workflow(tmp_path, wf))
    assert result.returncode == 1
    assert "is not pinned to a full SHA" in result.stderr


@pytest.mark.parametrize(
    "step",
    [
        {"uses": CHECKOUT},
        {"uses": CHECKOUT, "with": {"fetch-depth": 0}},
        {"uses": CHECKOUT, "with": {"persist-credentials": True}},
        {"uses": CHECKOUT, "with": {"persist-credentials": "false"}},
    ],
    ids=["no-with", "with-other-key", "persist-true", "persist-string-false"],
)
def test_checkout_without_persist_credentials_false_is_flagged(
    tmp_path: Path, run_check, step: dict[str, object]
) -> None:
    """INF-U-014: an `actions/checkout` without a boolean `persist-credentials: false` fails."""
    result = run_check(_workflow(tmp_path, _single_job(step)))
    assert result.returncode == 1
    assert "lacks `persist-credentials: false`" in result.stderr
    assert "not pinned" not in result.stderr


def test_checkout_with_persist_credentials_false_passes(tmp_path: Path, run_check) -> None:
    """INF-U-014: a SHA-pinned checkout with `persist-credentials: false` is accepted."""
    step = {"uses": CHECKOUT, "with": {"persist-credentials": False}}
    result = run_check(_workflow(tmp_path, _single_job(step)))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            """\
            on: push
            jobs:
              a: {runs-on: x, steps: [{run: echo}]}
            """,
            "job(s) ['a']",
            id="none-at-either-level",
        ),
        pytest.param(
            """\
            on: push
            jobs:
              a: {runs-on: x, permissions: {contents: read}, steps: [{run: echo}]}
              b: {runs-on: x, steps: [{run: echo}]}
            """,
            "job(s) ['b']",
            id="some-jobs-only",
        ),
        pytest.param("on: push\n", "job(s) []", id="no-jobs"),
    ],
)
def test_missing_permissions_is_flagged(
    tmp_path: Path, run_check, body: str, expected: str
) -> None:
    """INF-U-015: no `permissions` at workflow level or on every job fails the check."""
    result = run_check(_workflow(tmp_path, body))
    assert result.returncode == 1
    assert "no `permissions:` at workflow level or on job(s)" in result.stderr
    assert expected in result.stderr


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            f"""\
            on: push
            permissions: {{contents: read}}
            jobs:
              a:
                runs-on: x
                steps:
                  - uses: astral-sh/setup-uv@{SHA} # v7.6.0
            """,
            id="sha-with-version-comment",
        ),
        pytest.param(_single_job({"uses": "./.github/actions/local"}), id="local-action"),
        pytest.param(_single_job({"run": "echo hi"}), id="run-step-only"),
        pytest.param(
            """\
            on: push
            permissions: {contents: read}
            jobs:
              a: {runs-on: x, steps: [{run: echo}]}
              b: {runs-on: x, steps: [{run: echo}]}
            """,
            id="workflow-level-permissions-jobs-without",
        ),
        pytest.param(
            """\
            on: push
            jobs:
              a: {runs-on: x, permissions: {contents: read}, steps: [{run: echo}]}
              b: {runs-on: x, permissions: {}, steps: [{run: echo}]}
            """,
            id="permissions-on-every-job",
        ),
        pytest.param(
            f"""\
            on: push
            permissions: {{contents: read}}
            jobs:
              call:
                uses: org/repo/.github/workflows/x.yml@{SHA}
            """,
            id="pinned-reusable-workflow",
        ),
    ],
)
def test_legitimate_workflow_passes(tmp_path: Path, run_check, body: str) -> None:
    """INF-U-016: workflows that superficially resemble violations are accepted."""
    result = run_check(_workflow(tmp_path, body))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_clean_tree_with_yml_and_yaml_files_exits_zero(tmp_path: Path, run_check) -> None:
    """INF-U-017: a tree of clean `.yml` and `.yaml` workflows exits 0."""
    _workflow(tmp_path, _single_job({"run": "echo"}), "a.yml")
    _workflow(tmp_path, _single_job({"run": "echo"}), "b.yaml")
    assert run_check(tmp_path).returncode == 0


def test_violation_in_one_file_fails_and_names_it(tmp_path: Path, run_check) -> None:
    """INF-U-017: one bad file among clean ones (either extension) fails the whole run."""
    _workflow(tmp_path, _single_job({"run": "echo"}), "good.yml")
    _workflow(tmp_path, _single_job({"uses": "astral-sh/setup-uv@v7"}), "bad.yaml")
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert "bad.yaml" in result.stderr
    assert "good.yml" not in result.stderr


def test_empty_workflow_directory_exits_one(tmp_path: Path, run_check) -> None:
    """INF-U-017: a directory with no workflow file fails rather than passing empty."""
    (tmp_path / "notes.txt").write_text("not a workflow")
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert "no workflow files under" in result.stderr


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param("on: [push\njobs: {", "invalid YAML", id="unparseable-yaml"),
        pytest.param("- a\n- b\n", "top level is not a mapping", id="top-level-list"),
        pytest.param("on: push\njobs: [a, b]\n", "`jobs` is not a mapping", id="jobs-list"),
        pytest.param("on: push\njobs: build\n", "`jobs` is not a mapping", id="jobs-scalar"),
        pytest.param(
            "on: push\npermissions: {}\njobs:\n  a: [x]\n",
            "job a: body is not a mapping",
            id="job-body-list",
        ),
        pytest.param(
            "on: push\npermissions: {}\njobs:\n  a: build\n",
            "job a: body is not a mapping",
            id="job-body-scalar",
        ),
        pytest.param(
            "on: push\npermissions: {}\njobs:\n  a: {steps: build}\n",
            "job a: `steps` is not a list of mappings",
            id="steps-scalar",
        ),
        pytest.param(
            "on: push\npermissions: {}\njobs:\n  a: {steps: [build]}\n",
            "job a: `steps` is not a list of mappings",
            id="step-scalar",
        ),
    ],
)
def test_malformed_workflow_reports_path_and_reason(
    tmp_path: Path, run_check, body: str, reason: str
) -> None:
    """INF-U-018: malformed YAML or structure exits 1 with a `path: reason` line, no traceback."""
    _workflow(tmp_path, body, "bad.yml")
    _workflow(tmp_path, _single_job({"run": "echo"}), "good.yml")
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert f"bad.yml: {reason}" in result.stderr
    assert "Traceback" not in result.stderr
    assert "good.yml" not in result.stderr
