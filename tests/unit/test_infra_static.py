"""Static config checks (INF suite, docs/spec/08-test-specs/infra.md) — no services needed."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml
from huggingface_hub.utils import GatedRepoError, LocalTokenNotFoundError
from scripts.bootstrap_minio import IAM_MATRIX, _env_prefix

from tuner.models.base import HFAuthError, ModelAdapter
from tuner.models.gemma_e4b import GemmaE4BAdapter
from tuner.models.registry import ADAPTERS
from tuner.tokenizer.cli import tokenize

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


def _published_ports(compose_text: str) -> set[tuple[str, str | None, str]]:
    """(service, host_ip, published port) for every `ports:` entry, short or long syntax."""
    published = set()
    for service, spec in yaml.safe_load(compose_text)["services"].items():
        for entry in spec.get("ports", []):
            if isinstance(entry, dict):
                host_ip, port = entry.get("host_ip"), str(entry.get("published", ""))
            else:
                parts = str(entry).rsplit(":", 2)
                host_ip = parts[0] if len(parts) == 3 else None
                port = parts[-2] if len(parts) >= 2 else ""
            published.add((service, host_ip, port))
    return published


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
    assert _published_ports(compose_text) == {
        ("minio", "127.0.0.1", "9000"),
        ("minio", "127.0.0.1", "9001"),
        ("mlflow", "127.0.0.1", "5000"),
        ("mock-judge", "127.0.0.1", "8088"),
    }
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


_MINIO_IMAGE = re.compile(r"^ghcr\.io/mycookie/minio:(RELEASE\.[^@\s]+)@sha256:[0-9a-f]{64}$")


def test_minio_image_pin_is_consistent():
    """INF-U-008: compose minio image is GHCR tag+digest; tag matches the workflow's
    env.IMAGE; the full string is verbatim in the 05 §1 table and the operations guide."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yaml").read_text())
    image = compose["services"]["minio"]["image"]
    match = _MINIO_IMAGE.match(image)
    assert match, (
        f"minio image is not ghcr.io/mycookie/minio:RELEASE.<tag>@sha256:<digest>: {image}"
    )

    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/minio-image.yml").read_text())
    assert workflow["env"]["IMAGE"] == f"ghcr.io/mycookie/minio:{match.group(1)}"

    for doc in ("docs/spec/05-infrastructure.md", "docs/04-operations.md"):
        assert image in (REPO_ROOT / doc).read_text(), f"{doc} lacks the compose minio image"


# --- Hugging Face interaction (built at T09, once tuner.models exists) -------------


def _gated_repo_error() -> GatedRepoError:
    # HfHubHTTPError (GatedRepoError's base) requires a real httpx.Response -- this is
    # the minimal stub that satisfies its constructor without any network access.
    response = httpx.Response(401, request=httpx.Request("GET", "https://huggingface.co/x"))
    return GatedRepoError("access to this repo is gated", response=response)


@pytest.mark.parametrize(
    "auth_error",
    [LocalTokenNotFoundError("no token found"), _gated_repo_error()],
    ids=["missing-token", "gated-no-access"],
)
def test_load_tokenizer_hf_auth_error_is_actionable(monkeypatch, auth_error):
    """INF-U-010: a gated-model / missing-or-invalid HF_TOKEN error, mocked at the
    huggingface_hub client boundary, becomes an actionable HFAuthError naming HF_TOKEN
    and the model id -- not a raw traceback."""

    def _raise(*args, **kwargs):
        raise auth_error

    monkeypatch.setattr("tuner.models.base.AutoTokenizer.from_pretrained", _raise)
    adapter = GemmaE4BAdapter()

    with pytest.raises(HFAuthError) as exc_info:
        adapter.load_tokenizer()

    message = str(exc_info.value)
    assert "HF_TOKEN" in message
    assert adapter.hf_model_id in message


def test_load_base_model_hf_auth_error_is_actionable(monkeypatch):
    """INF-U-010 (load_base_model path): the same translation applies to the
    load_base_model default impl, not just load_tokenizer."""

    class _RaisingAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            raise _gated_repo_error()

    monkeypatch.setattr("tuner.models.base.AutoModelForCausalLM", _RaisingAutoModel)
    adapter = GemmaE4BAdapter()

    with pytest.raises(HFAuthError) as exc_info:
        adapter.load_base_model(quantized=False)

    message = str(exc_info.value)
    assert "HF_TOKEN" in message
    assert adapter.hf_model_id in message


def test_tokenize_cli_exits_2_on_hf_auth_error(monkeypatch, tmp_path):
    """INF-U-010, CLI-level companion (deferred from T09's own version of this case,
    since no stage CLI existed yet): `tuner tokenize` catches `HFAuthError` from
    `adapter.load_tokenizer()` and exits 2 -- not a raw traceback. Needs no real
    storage: the failure happens before Gold is ever read, mirroring `INF-I-005`'s own
    T04->T06 deferral pattern for a CLI-level companion case.

    Uses its own minimal config naming `gemma-e4b` explicitly, not the shipped
    `configs/pipeline.yaml` -- coupling to that file's current default would let this
    test silently stop testing what it claims (monkeypatching a class the CLI no
    longer even loads) if that default ever changed, instead of failing loudly
    (PR #10 review round 1 nit)."""
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(
        yaml.safe_dump({"model": {"adapter": "gemma-e4b"}, "ingest": {"sources": []}})
    )

    def _raise(self) -> None:
        raise HFAuthError(self.hf_model_id, LocalTokenNotFoundError("no token"))

    monkeypatch.setattr(GemmaE4BAdapter, "load_tokenizer", _raise)

    exit_code = tokenize("run-doesnt-matter", str(config_path))

    assert exit_code == 2


@pytest.mark.parametrize("adapter", [cls() for cls in ADAPTERS.values()], ids=list(ADAPTERS))
def test_revision_pinning_never_main_or_none(adapter: ModelAdapter):
    """INF-U-011: every adapter in ADAPTERS has hf_revision set to a commit hash or
    tag -- never "main"/None; reproducibility depends on it."""
    assert adapter.hf_revision is not None
    assert adapter.hf_revision != "main"
    assert isinstance(adapter.hf_revision, str) and adapter.hf_revision.strip()
