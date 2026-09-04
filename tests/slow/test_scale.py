"""Slow-lane scale smoke (INF-S-021, docs/spec/08-test-specs/infra.md).

Needs compose MinIO up (`docker compose up -d minio minio-init`) and the usual
`TUNER_S3_*` env vars exported -- same convention as every other integration-style
suite (tests/conftest.py's `storage`/`run_id` fixtures). Not marked `integration`
(it would then also need `-m integration` to run) -- `pytest -m slow` alone is the
nightly-lane / T15 invocation named in docs/spec/07-build-plan.md and 06-testing.md
§6.

Runs `tuner ingest` and `tuner clean` as real subprocesses (not in-process CliRunner
calls, unlike every other stage's own exit-code assertions per 08 README's own
convention) specifically so each stage's peak RSS can be measured in isolation via
`/proc/<pid>/status`'s kernel-tracked `VmHWM` -- the in-process convention shares one
pytest process's heap across every stage under test, which would make "ingest's peak"
and "clean's peak" impossible to tell apart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

BRONZE_BUCKET = "tuner-bronze"
SILVER_BUCKET = "tuner-silver"

TOTAL_RECORDS = 120_000
SHARD_SIZE = 50_000  # tuner.ingestor.cli.SHARD_SIZE -- not imported, so a drift
# between the two would fail this test's own shard-count assertion rather than
# silently tracking whatever the constant happens to be.

# "A generous cap" (08 infra.md's own wording, deliberately not a tight bound): this
# suite's job is catching a full-tier-in-memory regression (e.g. an accidental
# blowup to the multi-GB range), not chasing a specific byte count. Ingestor caps
# its own working set at one shard at a time (writes and drops each 50,000-record
# shard as it fills, 03-components/ingestor.md step 4); Cleaner reads its whole
# Bronze input into one `bronze_records` list before writing Silver
# (`src/tuner/cleaner/cli.py`) -- a real, known, already-accepted design point (the
# component spec never promised streaming for Cleaner), not a regression this test
# is meant to force a rewrite over.
#
# Calibrated against real measurements on the T15 dev box for this exact
# 120,000-record fixture, not guessed: ingest peaked at ~1.16 GB (one shard's worth
# of Bronze envelopes plus the interpreter/import baseline -- pydantic, boto3,
# click), clean peaked at ~1.66 GB (all 120,000 Bronze envelopes resident as Python
# dicts at once, as expected from the code above). A first attempt at this cap
# guessed both figures without actually measuring either (~90 MB / ~140 MB) and
# failed the very first real run against this box on the clean side alone -- real
# ingest overhead turned out to be an order of magnitude past that guess too. Set
# to ~2.4x the observed clean peak (the larger of the two): enough headroom for
# normal run-to-run variance, still low enough to fail hard if either stage's
# memory profile changes by an order of magnitude.
PEAK_RSS_CAP_KB = 4_000_000  # 4 GB


def _generate_jsonl(path: Path, count: int) -> None:
    """`count` unique, well-formed Silver-mappable conversations -- unique per-record
    text so none collide under the Cleaner's exact-hash dedup (every record must
    survive cleaning unchanged for this test's "counts conserve exactly" check)."""
    with path.open("w", encoding="utf-8") as f:
        for i in range(count):
            record = {
                "conversation": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "value": f"Question {i}: how do I configure widget {i}?",
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "value": f"Answer {i}: open settings and adjust option {i}.",
                            }
                        ],
                    },
                ]
            }
            f.write(json.dumps(record) + "\n")


def _write_config(tmp_path: Path, source_uri: str) -> Path:
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model": {"adapter": "gemma-e4b"},
                "ingest": {"sources": [{"type": "jsonl", "uri": source_uri}]},
                "clean": {"min_chars": 20, "max_chars": 32000, "pii": ["email", "phone"]},
            }
        )
    )
    return path


def _run_stage_peak_rss_kb(cmd: list[str], env: dict[str, str]) -> tuple[int, int]:
    """Run `cmd` to completion; return `(returncode, peak_rss_kb)`.

    Polls `/proc/<pid>/status`'s `VmHWM` ("peak resident set size") while the child is
    alive -- Linux-only, matching this project's sole target platform (no macOS/Windows
    lane anywhere in docs/spec). Best-effort: a process that lives for less than one
    poll interval could under-report, but every stage here runs for seconds, not
    milliseconds, against 120,000 records.
    """
    proc = subprocess.Popen(cmd, env=env)
    peak_kb = 0
    status_path = f"/proc/{proc.pid}/status"
    while proc.poll() is None:
        try:
            with open(status_path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmHWM:"):
                        peak_kb = max(peak_kb, int(line.split()[1]))
                        break
        except (FileNotFoundError, ProcessLookupError):
            pass  # process exited between poll() and the /proc read
        time.sleep(0.05)
    proc.wait()
    return proc.returncode, peak_kb


@pytest.mark.slow
def test_scale_120k_ingest_clean(storage, run_id, tmp_path):
    """INF-S-021: 120,000 synthetic records through ingest -> clean. Sharding kicks in
    at 50,000 (3 Bronze shards, 50k/50k/20k); counts conserve exactly end to end;
    peak RSS of each stage stays under a generous cap (streaming, no full-tier load)."""
    source_path = tmp_path / "scale_fixture.jsonl"
    _generate_jsonl(source_path, TOTAL_RECORDS)
    config_path = _write_config(tmp_path, str(source_path))

    env = {**os.environ}

    try:
        # -- ingest --------------------------------------------------------------
        returncode, ingest_peak_kb = _run_stage_peak_rss_kb(
            [
                sys.executable,
                "-m",
                "tuner",
                "ingest",
                "--run-id",
                run_id,
                "--config",
                str(config_path),
            ],
            env,
        )
        assert returncode == 0, f"ingest exited {returncode}"
        print(f"INF-S-021: ingest peak RSS: {ingest_peak_kb} KiB")

        bronze_manifest = storage.read_json(BRONZE_BUCKET, f"{run_id}/manifest.json")
        assert bronze_manifest is not None
        assert bronze_manifest["counts"] == {
            "read": TOTAL_RECORDS,
            "written": TOTAL_RECORDS,
            "dropped": 0,
        }
        assert bronze_manifest["files"] == [
            "records-00000.jsonl",
            "records-00001.jsonl",
            "records-00002.jsonl",
        ]

        expected_shard_sizes = [SHARD_SIZE, SHARD_SIZE, TOTAL_RECORDS - 2 * SHARD_SIZE]
        for shard_name, expected_len in zip(
            bronze_manifest["files"], expected_shard_sizes, strict=True
        ):
            actual_len = sum(1 for _ in storage.read_jsonl(BRONZE_BUCKET, f"{run_id}/{shard_name}"))
            assert actual_len == expected_len, f"{shard_name}: expected {expected_len}"

        assert ingest_peak_kb <= PEAK_RSS_CAP_KB, (
            f"ingest peak RSS {ingest_peak_kb} KiB exceeds the {PEAK_RSS_CAP_KB} KiB cap "
            "-- looks like a full-tier in-memory load, not streaming"
        )

        # -- clean -----------------------------------------------------------------
        returncode, clean_peak_kb = _run_stage_peak_rss_kb(
            [
                sys.executable,
                "-m",
                "tuner",
                "clean",
                "--run-id",
                run_id,
                "--config",
                str(config_path),
            ],
            env,
        )
        assert returncode == 0, f"clean exited {returncode}"
        print(f"INF-S-021: clean peak RSS: {clean_peak_kb} KiB")

        silver_manifest = storage.read_json(SILVER_BUCKET, f"{run_id}/manifest.json")
        assert silver_manifest is not None
        assert silver_manifest["counts"] == {
            "read": TOTAL_RECORDS,
            "written": TOTAL_RECORDS,
            "dropped": 0,
        }
        silver_count = sum(1 for _ in storage.read_jsonl(SILVER_BUCKET, f"{run_id}/"))
        assert silver_count == TOTAL_RECORDS

        assert clean_peak_kb <= PEAK_RSS_CAP_KB, (
            f"clean peak RSS {clean_peak_kb} KiB exceeds the {PEAK_RSS_CAP_KB} KiB cap "
            "-- looks like a full-tier in-memory load, not streaming"
        )
    finally:
        storage.delete_prefix(BRONZE_BUCKET, f"{run_id}/")
        storage.delete_prefix(SILVER_BUCKET, f"{run_id}/")
