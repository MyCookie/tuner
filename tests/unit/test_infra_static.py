"""Static config checks (INF suite, docs/08-test-specs/infra.md) — no services needed."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from scripts.bootstrap_minio import IAM_MATRIX, _env_prefix

REPO_ROOT = Path(__file__).parents[2]

_TOKEN_SHAPED = re.compile(r"^[A-Za-z0-9+/]{20,}={0,2}$|^[0-9a-f]{20,}$")

_STAGE_KEYS = [
    f"{_env_prefix(p)}_S3_{suffix}" for p in IAM_MATRIX for suffix in ("ACCESS_KEY", "SECRET_KEY")
]
_ALL_ENV_KEYS = [
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "TUNER_S3_ENDPOINT",
    "TUNER_S3_ACCESS_KEY",
    "TUNER_S3_SECRET_KEY",
    "TUNER_S3_REGION",
    "MLFLOW_TRACKING_URI",
    "TUNER_JUDGE_BASE_URL",
    "TUNER_JUDGE_API_KEY",
    "HF_TOKEN",
    *_STAGE_KEYS,
]


def test_env_example_completeness():
    """INF-U-006: .env.example has every 01 §4.3 var, placeholder values only."""
    text = (REPO_ROOT / ".env.example").read_text()
    values = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", text, re.MULTILINE))

    required = (
        "TUNER_S3_ENDPOINT",
        "TUNER_S3_ACCESS_KEY",
        "TUNER_S3_SECRET_KEY",
        "TUNER_S3_REGION",
        "MLFLOW_TRACKING_URI",
        "TUNER_JUDGE_BASE_URL",
        "TUNER_JUDGE_API_KEY",
        "HF_TOKEN",
    )
    for var in required:
        assert var in values, f"{var} missing from .env.example"

    for name, value in values.items():
        assert not _TOKEN_SHAPED.match(value), f"{name} looks token-shaped: {value!r}"


def test_compose_config_is_valid():
    """INF-U-007: `docker compose config -q` succeeds; services/ports/profiles per 05 §1."""
    result = subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **dict.fromkeys(_ALL_ENV_KEYS, "probe")},
    )
    assert result.returncode == 0, result.stderr

    compose_text = (REPO_ROOT / "docker-compose.yaml").read_text()
    for service, ports in (("minio", ("9000", "9001")), ("mlflow", ("5000",))):
        assert f"\n  {service}:" in compose_text
        for port in ports:
            assert f'"{port}:{port}"' in compose_text
    for stage in ("ingestor", "cleaner", "judge", "tokenizer", "trainer", "smoke"):
        block = compose_text.split(f"\n  {stage}:")[1].split("\n\n")[0]
        assert 'profiles: ["pipeline"]' in block


_CREDENTIAL_KEY = re.compile(r"(ACCESS_KEY|SECRET_KEY|PASSWORD|TOKEN|API_KEY)\s*:\s*(.+)$")


def test_compose_defines_no_credential_literal():
    """INF-U-007: no service defines a credential literal — every value is `${VAR}`."""
    compose_text = (REPO_ROOT / "docker-compose.yaml").read_text()
    for lineno, line in enumerate(compose_text.splitlines(), start=1):
        match = _CREDENTIAL_KEY.search(line)
        if not match:
            continue
        value = match.group(2).strip()
        assert value.startswith("${") and value.endswith("}"), (
            f"docker-compose.yaml:{lineno} sets a credential-shaped key to a literal: {line!r}"
        )
