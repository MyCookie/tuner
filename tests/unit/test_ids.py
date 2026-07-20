"""Unit tests for eftp.core.ids (CORE suite, docs/08-test-specs/core.md)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from eftp.core import ids


def test_new_run_id_format():
    """CORE-U-010: new_run_id() format matches the run-ID regex with an injected UTC clock."""
    frozen = datetime(2026, 7, 20, 14, 22, 1, tzinfo=UTC)

    run_id = ids.new_run_id(now=frozen)

    assert ids.RUN_ID_RE.match(run_id)
    assert run_id.startswith("run-20260720-142201-")


def test_run_id_and_record_id_uniqueness_and_validity():
    """CORE-U-011: 1000 generated run/record IDs are all unique; record IDs are valid UUIDv4."""
    run_ids = [ids.new_run_id() for _ in range(1000)]
    record_ids = [ids.new_record_id() for _ in range(1000)]

    assert len(set(run_ids)) == 1000
    assert len(set(record_ids)) == 1000
    for record_id in record_ids:
        assert uuid.UUID(record_id).version == 4


def test_main_prints_one_valid_run_id(capsys):
    """CORE-U-012: `python -m eftp.core.ids` prints one valid run ID with a trailing newline."""
    ids._main()

    captured = capsys.readouterr()
    assert captured.out.endswith("\n")
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert ids.RUN_ID_RE.match(lines[0])
