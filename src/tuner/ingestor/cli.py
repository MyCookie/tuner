"""`tuner ingest` (docs/spec/03-components/ingestor.md)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

import click
from pydantic import ValidationError

from tuner import __version__ as STAGE_VERSION
from tuner.core.config import DEFAULT_CONFIG_PATH, ConfigError, IngestSourceConfig, load_config
from tuner.core.ids import canonical_hash, new_record_id, validate_run_id_option
from tuner.core.manifest import records_hash
from tuner.core.schemas import BronzeRecord, ManifestCounts, ManifestProducer, TierManifest
from tuner.core.storage import StorageClient
from tuner.ingestor.sources import MalformedLine, SourceConfigError, build_source

BUCKET = "tuner-bronze"
STAGE = "ingestor"
SHARD_SIZE = 50_000


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_envelope(
    run_id: str, source_cfg: IngestSourceConfig, locator: str, raw: dict[str, Any]
) -> dict[str, Any]:
    envelope = BronzeRecord(
        id=new_record_id(),
        run_id=run_id,
        source={
            "uri": source_cfg.uri,
            "type": source_cfg.type,
            "locator": locator,
            "ingested_at": _utc_now(),
            "ingestor_version": STAGE_VERSION,
        },
        content_hash=canonical_hash(raw),
        raw=raw,
    )
    return envelope.model_dump(mode="json")


def _shard_bytes(records: list[dict[str, Any]]) -> bytes:
    """Mirrors StorageClient.write_jsonl's exact serialization (json.dumps + newline, joined,
    utf-8-encoded) so `records_hash` matches what's actually written without a read-back
    round trip. If that serialization ever changes, this must change with it."""
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    return body.encode("utf-8")


def _write_shard(
    storage: StorageClient, run_id: str, shard_index: int, records: list[dict[str, Any]]
) -> tuple[str, bytes]:
    """Write one shard; returns (filename, its exact written bytes for `records_hash`)."""
    key_name = f"records-{shard_index:05d}.jsonl"
    storage.write_jsonl(BUCKET, f"{run_id}/{key_name}", records)
    return key_name, _shard_bytes(records)


def ingest(
    run_id: str,
    config_path: str,
    storage: StorageClient | None = None,
    shard_size: int = SHARD_SIZE,
) -> int:
    """Run the Ingestor end to end; returns the process exit code (0/1/2/3)."""
    try:
        config = load_config(config_path)
        sources = [build_source(source_cfg) for source_cfg in config.ingest.sources]
        storage = storage or StorageClient()
    except (ConfigError, SourceConfigError) as exc:
        click.echo(f"ingest: {exc}", err=True)
        return 2

    shard: list[dict[str, Any]] = []
    shard_index = 0
    written_files: list[str] = []
    shard_byte_chunks: list[bytes] = []
    total = 0

    try:
        storage.delete_prefix(BUCKET, f"{run_id}/")

        for source_cfg, source in zip(config.ingest.sources, sources, strict=True):
            for locator, raw in source.records():
                shard.append(_build_envelope(run_id, source_cfg, locator, raw))
                total += 1
                if len(shard) >= shard_size:
                    key_name, chunk = _write_shard(storage, run_id, shard_index, shard)
                    written_files.append(key_name)
                    shard_byte_chunks.append(chunk)
                    shard_index += 1
                    shard = []

        if total == 0:
            click.echo("ingest: zero records ingested across all sources", err=True)
            return 3

        if shard:
            key_name, chunk = _write_shard(storage, run_id, shard_index, shard)
            written_files.append(key_name)
            shard_byte_chunks.append(chunk)
    except (MalformedLine, ValidationError) as exc:
        # MalformedLine: bad JSON syntax. ValidationError: syntactically valid JSON that
        # isn't shaped like a Bronze envelope -- e.g. a JSONL line whose parsed value isn't
        # an object, so `raw` fails BronzeRecord's `dict[str, Any]`. Both are input-schema
        # validation failures (01-architecture.md §4.4 exit 2), not unexpected errors.
        click.echo(f"ingest: {exc}", err=True)
        return 2
    except Exception as exc:  # unexpected mid-run failure (I/O, storage, ...) -> exit 1
        click.echo(f"ingest: {exc}", err=True)
        return 1

    manifest = TierManifest(
        tier="bronze",
        run_id=run_id,
        created_at=_utc_now(),
        producer=ManifestProducer(stage=STAGE, version=STAGE_VERSION),
        input=None,
        files=written_files,
        records_hash=records_hash(shard_byte_chunks),
        counts=ManifestCounts(read=total, written=total, dropped=0),
        drops=[],
    )
    storage.write_json(BUCKET, f"{run_id}/manifest.json", manifest.model_dump(mode="json"))
    return 0


@click.command(name="ingest")
@click.option(
    "--run-id",
    required=True,
    callback=validate_run_id_option,
    help="Run ID shared across the pipeline.",
)
@click.option(
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG_PATH),
    show_default=True,
    help="Pipeline config path.",
)
def ingest_command(run_id: str, config_path: str) -> None:
    """Convert configured sources into Bronze envelopes."""
    sys.exit(ingest(run_id, config_path))
