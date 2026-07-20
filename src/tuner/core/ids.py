"""Run/record identifiers and canonical hashing (01-architecture.md §4.2, 02-data-contracts.md)."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

RUN_ID_RE = re.compile(r"^run-\d{8}-\d{6}-[0-9a-f]{6}$")


def new_run_id(now: datetime | None = None) -> str:
    """Generate a run ID: run-{YYYYMMDD}-{HHMMSS}-{6 lowercase hex} (UTC)."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"run-{moment:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def new_record_id() -> str:
    """Generate a record ID: UUIDv4, assigned at Bronze and immutable thereafter."""
    return str(uuid.uuid4())


def canonical_hash(obj: Any) -> str:
    """sha256 over canonical JSON: sorted keys, compact separators, UTF-8 (02-data-contracts.md)."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _main() -> None:
    print(new_run_id())


if __name__ == "__main__":
    _main()
