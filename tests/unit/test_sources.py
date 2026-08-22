"""Unit tests for tuner.ingestor.sources (ING suite, docs/08-test-specs/ingestor.md).

No services needed — ING-U-002/005 invoke the CLI via CliRunner, but a bad
source config short-circuits before StorageClient is ever constructed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from tuner.cli import cli
from tuner.core.config import IngestSourceConfig, IngestSourceMapping
from tuner.ingestor.sources import CsvSource, JsonlSource, MalformedLine, SourceConfigError

REPO_ROOT = Path(__file__).parents[2]


def _csv_config(uri: str, **mapping_kwargs) -> IngestSourceConfig:
    mapping = IngestSourceMapping(**mapping_kwargs) if mapping_kwargs else None
    return IngestSourceConfig(type="csv", uri=uri, mapping=mapping)


def _jsonl_config(uri: str) -> IngestSourceConfig:
    return IngestSourceConfig(type="jsonl", uri=uri, mapping=None)


def _write_minimal_config(tmp_path: Path, source: dict) -> Path:
    data = {
        "model": {"adapter": "gemma-e4b"},
        "ingest": {"sources": [source]},
    }
    path = tmp_path / "pipeline.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_csv_source_yields_rows_with_locators(tmp_path):
    """ING-U-001: CsvSource over a 3-row temp CSV yields 3 (locator, raw); locators row:1..3;
    raw keys = header names, values = cell strings."""
    csv_path = tmp_path / "dialogs.csv"
    csv_path.write_text("question,answer\nq1,a1\nq2,a2\nq3,a3\n")

    source = CsvSource(_csv_config(str(csv_path)))
    result = list(source.records())

    assert [locator for locator, _ in result] == ["row:1", "row:2", "row:3"]
    assert [raw for _, raw in result] == [
        {"question": "q1", "answer": "a1"},
        {"question": "q2", "answer": "a2"},
        {"question": "q3", "answer": "a3"},
    ]


def test_csv_mapping_column_absent_from_header_exits_2(tmp_path):
    """ING-U-002: CSV mapping referencing a column absent from the header -> config
    validation error -> exit 2, no records yielded."""
    csv_path = tmp_path / "dialogs.csv"
    csv_path.write_text("question,answer\nq1,a1\n")

    with pytest.raises(SourceConfigError, match="not_a_column"):
        CsvSource(
            _csv_config(str(csv_path), prompt_column="not_a_column", response_column="answer")
        )

    config_path = _write_minimal_config(
        tmp_path,
        {
            "type": "csv",
            "uri": str(csv_path),
            "mapping": {"prompt_column": "not_a_column", "response_column": "answer"},
        },
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["ingest", "--run-id", "run-test", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "not_a_column" in result.output


def test_jsonl_source_yields_parsed_lines_with_locators(tmp_path):
    """ING-U-003: JsonlSource over a 3-line temp file: locators line:1..3; raw = parsed objects."""
    jsonl_path = tmp_path / "extra.jsonl"
    jsonl_path.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n')

    source = JsonlSource(_jsonl_config(str(jsonl_path)))
    result = list(source.records())

    assert [locator for locator, _ in result] == ["line:1", "line:2", "line:3"]
    assert [raw for _, raw in result] == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_jsonl_source_malformed_line_raises_with_line_number():
    """ING-U-004: JsonlSource hitting a malformed line (fixtures/bad_lines.jsonl) raises a
    parse error carrying the line number. The CLI maps this to exit 2 — see ING-I-019 in
    tests/integration/test_ingestor.py, since exercising that path needs a real store."""
    source = JsonlSource(_jsonl_config(str(REPO_ROOT / "fixtures" / "bad_lines.jsonl")))

    with pytest.raises(MalformedLine) as exc_info:
        list(source.records())

    assert exc_info.value.line_number == 1


def test_unknown_source_type_exits_2_naming_known_types(tmp_path):
    """ING-U-005: unknown `type: parquet` in source config -> exit 2 naming known types."""
    config_path = _write_minimal_config(tmp_path, {"type": "parquet", "uri": "whatever.parquet"})
    runner = CliRunner()

    result = runner.invoke(cli, ["ingest", "--run-id", "run-test", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "parquet" in result.output
    assert "csv" in result.output
    assert "jsonl" in result.output


def test_csv_header_decode_failure_exits_2_at_construction(tmp_path):
    """ING-U-007: a CSV whose header line isn't valid UTF-8 raises SourceConfigError at
    construction — the same bucket as an unreadable source URI (exit 2, before any output
    is written) — instead of an uncaught UnicodeDecodeError escaping the stage entirely."""
    csv_path = tmp_path / "dialogs.csv"
    csv_path.write_bytes(b"ques\xfftion,answer\nq1,a1\n")

    with pytest.raises(SourceConfigError, match="cannot read source"):
        CsvSource(_csv_config(str(csv_path)))


def test_csv_data_row_decode_failure_raises_malformed_line(tmp_path):
    """ING-U-008: a non-UTF-8 byte deep in a CSV file (the header decodes fine, so
    construction succeeds) raises MalformedLine from records() -- not an uncaught
    UnicodeDecodeError. The row number isn't asserted here: it's best-effort for CSV (see
    the comment on CsvSource.records()), unlike JsonlSource's, which is exact. Padded well
    past the text-decoder's internal buffer so the bad byte isn't swept up by the
    construction-time header check, which would raise SourceConfigError instead."""
    csv_path = tmp_path / "dialogs.csv"
    padding = "".join(f"q{i},a{i}\n" for i in range(2000))
    csv_path.write_bytes(f"question,answer\n{padding}".encode() + b"qbad,\xffbad\n")

    with pytest.raises(MalformedLine):
        list(CsvSource(_csv_config(str(csv_path))).records())


def test_jsonl_data_row_decode_failure_raises_with_exact_line_number(tmp_path):
    """ING-U-009: a non-UTF-8 byte in a JSONL data line (the first line decodes fine, so
    construction succeeds) raises MalformedLine carrying the exact line number -- JsonlSource
    decodes one physical line at a time, unlike CsvSource, so this is precise, not best-effort."""
    jsonl_path = tmp_path / "extra.jsonl"
    jsonl_path.write_bytes(b'{"a": 1}\n{"a": \xffbad}\n')

    with pytest.raises(MalformedLine) as exc_info:
        list(JsonlSource(_jsonl_config(str(jsonl_path))).records())

    assert exc_info.value.line_number == 2


def test_csv_row_more_fields_than_header_raises_malformed_line(tmp_path):
    """ING-U-010: a CSV row with more fields than the header (DictReader's `None` restkey)
    raises MalformedLine naming the row, instead of crashing downstream when canonical_hash
    tries to sort a dict with a `None` key alongside `str` keys."""
    csv_path = tmp_path / "dialogs.csv"
    csv_path.write_text("question,answer\nq1,a1,extra,fields\n")

    with pytest.raises(MalformedLine) as exc_info:
        list(CsvSource(_csv_config(str(csv_path))).records())

    assert exc_info.value.line_number == 1


def test_csv_byte_fidelity_no_trimming(tmp_path):
    """ING-U-006: CSV cell with leading/trailing spaces, embedded quotes, unicode -> raw values
    byte-identical to source (no trimming at Bronze)."""
    csv_path = tmp_path / "dialogs.csv"
    csv_path.write_text(
        "question,answer\n"
        '"  spaced question  ","he said ""hello"" back"\n'
        "café?,résumé attached — 你好\n",
        encoding="utf-8",
    )

    source = CsvSource(_csv_config(str(csv_path)))
    result = list(source.records())

    assert result[0][1] == {
        "question": "  spaced question  ",
        "answer": 'he said "hello" back',
    }
    assert result[1][1] == {
        "question": "café?",
        "answer": "résumé attached — 你好",
    }
