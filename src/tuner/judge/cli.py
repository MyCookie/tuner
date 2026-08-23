"""`tuner judge` (docs/03-components/judge.md)."""

from __future__ import annotations

import json
import os
import random
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import httpx
import mlflow
from pydantic import ValidationError

from tuner import __version__ as STAGE_VERSION
from tuner.core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from tuner.core.manifest import UpstreamIncomplete, read_tier, records_hash
from tuner.core.schemas import (
    ManifestCounts,
    ManifestDrop,
    ManifestInputRef,
    ManifestProducer,
    SilverGoldRecord,
    TierManifest,
)
from tuner.core.storage import StorageClient
from tuner.judge.client import build_http_client, normalize_score, score_record
from tuner.judge.prompts import RUBRIC_V1

SILVER_BUCKET = "tuner-silver"
GOLD_BUCKET = "tuner-gold"
STAGE = "judge"
JUDGE_ERROR_ABORT_FRACTION = 0.10


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shard_bytes(records: list[dict[str, Any]]) -> bytes:
    """Mirrors StorageClient.write_jsonl's exact serialization (json.dumps + newline, joined,
    utf-8-encoded) so `records_hash` matches what's actually written without a read-back
    round trip. If that serialization ever changes, this must change with it."""
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    return body.encode("utf-8")


def _score_histogram_text(scores: list[float]) -> str:
    """A plain-text 10-bin histogram of normalized [0.0, 1.0] scores -- MLflow has no
    native histogram artifact type, so any file representing the distribution satisfies
    "score histogram artifact present" (JDG-I-026)."""
    bins = [0] * 10
    for score in scores:
        bins[min(int(score * 10), 9)] += 1
    lines = [
        f"[{i / 10:.1f}, {(i + 1) / 10:.1f}): {'#' * count} ({count})"
        for i, count in enumerate(bins)
    ]
    return "\n".join(lines)


def judge(
    run_id: str,
    config_path: str,
    storage: StorageClient | None = None,
    http_client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the Judge end to end; returns the process exit code (0/1/2/3). `sleep` is
    injectable so tests can skip real backoff delays without changing retry counts."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        click.echo(f"judge: {exc}", err=True)
        return 2

    judge_base_url = os.environ.get("TUNER_JUDGE_BASE_URL")
    if not config.judge.model or not judge_base_url:
        click.echo(
            "judge: judge.model must be non-empty and TUNER_JUDGE_BASE_URL must be set",
            err=True,
        )
        return 2

    # Checked here, not just left to _log_to_mlflow's os.environ[...] lookup: without
    # this, a missing MLFLOW_TRACKING_URI raised a KeyError only after the Gold shard and
    # manifest were already committed -- exit 1 reporting failure on a run that had
    # actually succeeded (PR #8 review round 1 finding 7).
    if not os.environ.get("MLFLOW_TRACKING_URI"):
        click.echo("judge: MLFLOW_TRACKING_URI must be set", err=True)
        return 2

    storage = storage or StorageClient()

    try:
        read_tier(storage, SILVER_BUCKET, run_id)
    except (UpstreamIncomplete, ValidationError) as exc:
        click.echo(f"judge: {exc}", err=True)
        return 2

    silver_records: list[dict[str, Any]] = []
    try:
        for raw in storage.read_jsonl(SILVER_BUCKET, f"{run_id}/"):
            try:
                SilverGoldRecord.model_validate(raw)
            except ValidationError as exc:
                record_id = raw.get("id", "<unknown>")
                click.echo(f"judge: invalid Silver record {record_id}: {exc}", err=True)
                return 2
            silver_records.append(raw)
    except Exception as exc:  # unexpected mid-run failure (I/O, storage, ...) -> exit 1
        click.echo(f"judge: {exc}", err=True)
        return 1

    owns_http_client = http_client is None
    http_client = http_client or build_http_client(
        judge_base_url, os.environ.get("TUNER_JUDGE_API_KEY", "")
    )

    try:
        # Delete Gold's own prefix before scoring even starts (core logic 2), not after
        # the error-rate check: an aborted run should leave Gold definitively absent, not
        # a stale copy from a previous run that this run's failure gives no reason to trust.
        storage.delete_prefix(GOLD_BUCKET, f"{run_id}/")

        results: dict[str, tuple[int, str] | None] = {}
        with ThreadPoolExecutor(max_workers=config.judge.max_concurrency) as pool:
            futures = {
                pool.submit(
                    score_record,
                    http_client,
                    config.judge.model,
                    record["conversation"],
                    config.judge.max_retries,
                    random.Random(),  # a fresh instance per task -- random.Random isn't
                    # safe to share across the worker pool's threads
                    sleep,
                ): record["id"]
                for record in silver_records
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()

        total_read = len(silver_records)
        judge_error_ids = [rid for rid, outcome in results.items() if outcome is None]
        if total_read > 0 and len(judge_error_ids) / total_read > JUDGE_ERROR_ABORT_FRACTION:
            click.echo(
                f"judge: judge_error rate {len(judge_error_ids)}/{total_read} exceeds "
                f"{JUDGE_ERROR_ABORT_FRACTION:.0%} -- endpoint looks unhealthy, aborting",
                err=True,
            )
            return 1

        gold_records: list[dict[str, Any]] = []
        drops: dict[str, int] = {}
        all_scores: list[float] = []
        for record in silver_records:
            outcome = results[record["id"]]
            if outcome is None:
                drops["judge_error"] = drops.get("judge_error", 0) + 1
                continue
            raw_score, reasoning = outcome
            normalized = normalize_score(raw_score)
            all_scores.append(normalized)
            if normalized >= config.judge.threshold:
                gold_records.append(
                    {
                        **record,
                        "evaluation": {
                            "score": normalized,
                            "judge_model": config.judge.model,
                            "reasoning": reasoning,
                            "evaluated_at": _utc_now(),
                        },
                    }
                )
            else:
                drops["below_threshold"] = drops.get("below_threshold", 0) + 1

        if not gold_records:
            click.echo("judge: zero records promoted to Gold", err=True)
            return 3

        storage.write_jsonl(GOLD_BUCKET, f"{run_id}/records-00000.jsonl", gold_records)

        manifest = TierManifest(
            tier="gold",
            run_id=run_id,
            created_at=_utc_now(),
            producer=ManifestProducer(stage=STAGE, version=STAGE_VERSION),
            input=ManifestInputRef(
                tier="silver", manifest_uri=f"s3://{SILVER_BUCKET}/{run_id}/manifest.json"
            ),
            files=["records-00000.jsonl"],
            records_hash=records_hash([_shard_bytes(gold_records)]),
            counts=ManifestCounts(
                read=total_read, written=len(gold_records), dropped=sum(drops.values())
            ),
            drops=[
                ManifestDrop(reason=reason, count=count) for reason, count in sorted(drops.items())
            ],
        )
        storage.write_json(GOLD_BUCKET, f"{run_id}/manifest.json", manifest.model_dump(mode="json"))

        _log_to_mlflow(
            config, run_id, all_scores, len(gold_records), len(judge_error_ids), total_read
        )
    except Exception as exc:  # unexpected mid-run failure (I/O, storage, MLflow, ...) -> exit 1
        click.echo(f"judge: {exc}", err=True)
        return 1
    finally:
        if owns_http_client:
            http_client.close()

    return 0


def _log_to_mlflow(
    config: Any, run_id: str, scores: list[float], promoted: int, judge_errors: int, total_read: int
) -> None:
    """Judge's own MLflow run (01-architecture.md §7, JDG-I-026): tags tuner.run_id +
    tuner.stage: judge; params judge model/threshold/rubric version; metrics
    mean/median/promotion_rate/judge_error_rate; a score histogram artifact."""
    mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
    mlflow.set_experiment(config.train.mlflow_experiment)
    with mlflow.start_run(run_name=f"judge-{run_id}"):
        mlflow.set_tags({"tuner.run_id": run_id, "tuner.stage": STAGE})
        mlflow.log_params(
            {
                "judge_model": config.judge.model,
                "threshold": config.judge.threshold,
                "rubric_version": RUBRIC_V1,
            }
        )
        mlflow.log_metrics(
            {
                "mean_score": statistics.mean(scores) if scores else 0.0,
                "median_score": statistics.median(scores) if scores else 0.0,
                "promotion_rate": promoted / total_read if total_read else 0.0,
                "judge_error_rate": judge_errors / total_read if total_read else 0.0,
            }
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            histogram_path = Path(tmp_dir) / "score_histogram.txt"
            histogram_path.write_text(_score_histogram_text(scores))
            mlflow.log_artifact(str(histogram_path))


@click.command(name="judge")
@click.option("--run-id", required=True, help="Run ID shared across the pipeline.")
@click.option(
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG_PATH),
    show_default=True,
    help="Pipeline config path.",
)
def judge_command(run_id: str, config_path: str) -> None:
    """Score Silver records with an LLM and promote passing ones to Gold."""
    sys.exit(judge(run_id, config_path))
