"""Source implementations for the Ingestor (docs/03-components/ingestor.md `Source` interface).

Registered by `type` string: `csv` -> CsvSource, `jsonl` -> JsonlSource (MVP);
`sql`/`pdf`/`api` are reserved for later phases and currently just produce a
clear "unknown type" error, per the component spec's own framing. Local file
paths only for now — `s3://` source URIs are spec'd (03-components/ingestor.md
Input) but no case in this task's suite exercises them, and reading an
arbitrary object by key has no `StorageClient` primitive yet; see the
footnote on this scope in docs/08-test-specs/ingestor.md.
"""

from __future__ import annotations

import csv
import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tuner.core.config import IngestSourceConfig


class SourceConfigError(Exception):
    """A source is misconfigured or its URI is unreadable — CLI exit 2."""


class MalformedLine(Exception):
    """A JSONL line failed to parse; carries its 1-based line number."""

    def __init__(self, line_number: int, cause: Exception) -> None:
        super().__init__(f"line {line_number}: {cause}")
        self.line_number = line_number


class Source(ABC):
    """docs/03-components/ingestor.md `Source` interface."""

    def __init__(self, cfg: IngestSourceConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    def records(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield `(locator, raw_record)` pairs."""


class CsvSource(Source):
    """Reads with `csv.DictReader`; locator `row:{n}` (1-based, excluding header).

    The `mapping` config is validated against the header at construction —
    before any output is written — but *applied* by the Cleaner; Bronze keeps
    whole rows untouched (no trimming, byte-faithful per ING-U-006).
    """

    def __init__(self, cfg: IngestSourceConfig) -> None:
        super().__init__(cfg)
        try:
            with Path(cfg.uri).open(newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), [])
        except OSError as exc:
            raise SourceConfigError(f"cannot read source {cfg.uri!r}: {exc}") from exc

        if cfg.mapping is not None:
            for column in (
                cfg.mapping.prompt_column,
                cfg.mapping.response_column,
                cfg.mapping.system_column,
            ):
                if column is not None and column not in header:
                    raise SourceConfigError(
                        f"mapping column {column!r} not found in {cfg.uri!r} header {header}"
                    )

    def records(self) -> Iterator[tuple[str, dict[str, Any]]]:
        with Path(self.cfg.uri).open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for line_number, row in enumerate(reader, start=1):
                yield f"row:{line_number}", dict(row)


class JsonlSource(Source):
    """Yields each parsed line; locator `line:{n}`. No interpretation of shape at this stage."""

    def __init__(self, cfg: IngestSourceConfig) -> None:
        super().__init__(cfg)
        try:
            Path(cfg.uri).open(encoding="utf-8").close()
        except OSError as exc:
            raise SourceConfigError(f"cannot read source {cfg.uri!r}: {exc}") from exc

    def records(self) -> Iterator[tuple[str, dict[str, Any]]]:
        with Path(self.cfg.uri).open(encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise MalformedLine(line_number, exc) from exc
                yield f"line:{line_number}", obj


_REGISTRY: dict[str, type[Source]] = {"csv": CsvSource, "jsonl": JsonlSource}


def build_source(cfg: IngestSourceConfig) -> Source:
    """Instantiate the `Source` registered for `cfg.type`; unknown type -> `SourceConfigError`."""
    try:
        source_cls = _REGISTRY[cfg.type]
    except KeyError as exc:
        raise SourceConfigError(
            f"unknown source type {cfg.type!r}; known types: {sorted(_REGISTRY)}"
        ) from exc
    return source_cls(cfg)
