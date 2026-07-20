"""Run/record identifiers (01-architecture.md §4.2).

canonical_hash() is deliberately not implemented here yet: its only spec'd test
case (CORE-U-050, docs/08-test-specs/core.md) is a T02 property test living in
test_schemas.py, alongside its first consumers (schemas.py, manifest.py).
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime

RUN_ID_RE = re.compile(r"^run-\d{8}-\d{6}-[0-9a-f]{6}$")


def new_run_id(now: datetime | None = None) -> str:
    """Generate a run ID: run-{YYYYMMDD}-{HHMMSS}-{6 lowercase hex} (UTC)."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"run-{moment:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def new_record_id() -> str:
    """Generate a record ID: UUIDv4, assigned at Bronze and immutable thereafter."""
    return str(uuid.uuid4())


def _main() -> None:
    print(new_run_id())


if __name__ == "__main__":
    _main()
