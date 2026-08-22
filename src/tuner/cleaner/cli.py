"""`tuner clean` (docs/03-components/cleaner.md)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

import click
from pydantic import ValidationError

from tuner import __version__ as STAGE_VERSION
from tuner.cleaner.rules import Deduplicator, clean_record
from tuner.core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from tuner.core.manifest import UpstreamIncomplete, read_tier, records_hash
from tuner.core.schemas import (
    BronzeRecord,
    ManifestCounts,
    ManifestDrop,
    ManifestInputRef,
    ManifestProducer,
    TierManifest,
)
from tuner.core.storage import StorageClient

BRONZE_BUCKET = "tuner-bronze"
SILVER_BUCKET = "tuner-silver"
STAGE = "cleaner"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shard_bytes(records: list[dict[str, Any]]) -> bytes:
    """Mirrors StorageClient.write_jsonl's exact serialization (json.dumps + newline, joined,
    utf-8-encoded) so `records_hash` matches what's actually written without a read-back
    round trip. If that serialization ever changes, this must change with it."""
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    return body.encode("utf-8")


def clean(run_id: str, config_path: str, storage: StorageClient | None = None) -> int:
    """Run the Cleaner end to end; returns the process exit code (0/1/2/3)."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        click.echo(f"clean: {exc}", err=True)
        return 2

    storage = storage or StorageClient()

    try:
        read_tier(storage, BRONZE_BUCKET, run_id)
    except (UpstreamIncomplete, ValidationError) as exc:
        # UpstreamIncomplete: the manifest is missing entirely (upstream incomplete).
        # ValidationError: it's present but doesn't parse as a TierManifest -- also a
        # schema-validation failure, not something to let escape uncaught.
        click.echo(f"clean: {exc}", err=True)
        return 2

    bronze_records: list[dict[str, Any]] = []
    try:
        for raw in storage.read_jsonl(BRONZE_BUCKET, f"{run_id}/"):
            try:
                BronzeRecord.model_validate(raw)
            except ValidationError as exc:
                record_id = raw.get("id", "<unknown>")
                click.echo(f"clean: invalid Bronze record {record_id}: {exc}", err=True)
                return 2
            bronze_records.append(raw)
    except Exception as exc:  # unexpected mid-run failure (I/O, storage, ...) -> exit 1
        click.echo(f"clean: {exc}", err=True)
        return 1

    mapping_by_uri = {source.uri: source.mapping for source in config.ingest.sources}

    try:
        storage.delete_prefix(SILVER_BUCKET, f"{run_id}/")

        drops: dict[str, int] = {}
        silver_records: list[dict[str, Any]] = []
        dedup = Deduplicator()

        for bronze in bronze_records:
            turns, reason = clean_record(
                bronze["source"]["type"],
                bronze["raw"],
                mapping_by_uri.get(bronze["source"]["uri"]),
                pii_scrubbers=config.clean.pii,
                min_chars=config.clean.min_chars,
                max_chars=config.clean.max_chars,
            )
            if reason is None and dedup.is_duplicate(turns):
                reason = "duplicate"
            if reason is not None:
                drops[reason] = drops.get(reason, 0) + 1
                continue
            silver_records.append(
                {
                    "id": bronze["id"],
                    "run_id": bronze["run_id"],
                    "lineage": {
                        "bronze_content_hash": bronze["content_hash"],
                        "cleaner_version": STAGE_VERSION,
                    },
                    "conversation": turns,
                    "evaluation": None,
                }
            )

        total_read = len(bronze_records)
        total_written = len(silver_records)

        if total_written == 0:
            click.echo("clean: zero records survived cleaning", err=True)
            return 3

        storage.write_jsonl(SILVER_BUCKET, f"{run_id}/records-00000.jsonl", silver_records)
    except Exception as exc:  # unexpected mid-run failure (I/O, storage, ...) -> exit 1
        click.echo(f"clean: {exc}", err=True)
        return 1

    manifest = TierManifest(
        tier="silver",
        run_id=run_id,
        created_at=_utc_now(),
        producer=ManifestProducer(stage=STAGE, version=STAGE_VERSION),
        input=ManifestInputRef(
            tier="bronze", manifest_uri=f"s3://{BRONZE_BUCKET}/{run_id}/manifest.json"
        ),
        files=["records-00000.jsonl"],
        records_hash=records_hash([_shard_bytes(silver_records)]),
        counts=ManifestCounts(read=total_read, written=total_written, dropped=sum(drops.values())),
        drops=[ManifestDrop(reason=reason, count=count) for reason, count in sorted(drops.items())],
    )
    storage.write_json(SILVER_BUCKET, f"{run_id}/manifest.json", manifest.model_dump(mode="json"))
    return 0


@click.command(name="clean")
@click.option("--run-id", required=True, help="Run ID shared across the pipeline.")
@click.option(
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG_PATH),
    show_default=True,
    help="Pipeline config path.",
)
def clean_command(run_id: str, config_path: str) -> None:
    """Convert Bronze envelopes into scrubbed, filtered, deduplicated Silver records."""
    sys.exit(clean(run_id, config_path))
