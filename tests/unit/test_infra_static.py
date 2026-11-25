"""Static config checks (INF suite, docs/spec/08-test-specs/infra.md) — no services needed."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
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
    for var in (*required, *_STAGE_KEYS):
        assert var in values, f"{var} missing from .env.example"

    for name, value in values.items():
        assert not _TOKEN_SHAPED.match(value), f"{name} looks token-shaped: {value!r}"


def test_compose_config_is_valid():
    """INF-U-007: `docker compose config` succeeds; services/ports/profiles per 05 §1."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yaml",
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **dict.fromkeys(_ALL_ENV_KEYS, "probe")},
    )
    assert result.returncode == 0, result.stderr

    services = json.loads(result.stdout)["services"]
    published = {
        (name, port.get("host_ip"), port["published"])
        for name, svc in services.items()
        for port in svc.get("ports", [])
    }
    assert published == {
        ("minio", "127.0.0.1", "9000"),
        ("minio", "127.0.0.1", "9001"),
        ("mlflow", "127.0.0.1", "5000"),
        ("mock-judge", "127.0.0.1", "8088"),
    }
    for name, svc in services.items():
        assert "expose" not in svc, f"{name} declares expose"
        assert svc.get("network_mode") != "host", f"{name} uses host networking"
    # Hard-coded from the 05 §1 table on purpose (#141, #136): a table change updates this too.
    stages = ("ingestor", "cleaner", "judge", "tokenizer", "trainer", "smoke")
    always_on = ("minio", "minio-data-perms", "minio-init", "mlflow")
    assert set(services) == {*always_on, "mock-judge", *stages}
    for stage in stages:
        assert services[stage]["profiles"] == ["pipeline"]
    assert services["mock-judge"]["profiles"] == ["e2e"]
    for name in always_on:
        assert "profiles" not in services[name], f"{name} must not be profile-gated"


_CREDENTIAL_LITERAL = re.compile(
    r"[A-Za-z0-9_]*(?:ACCESS_KEY|SECRET_KEY|PASSWORD|TOKEN|API_KEY)[A-Za-z0-9_]*[\"']?\s*[:=]\s*(.*)$"
)
_VAR_REF = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _credential_literals(compose_text: str) -> list[str]:
    """Lines setting a credential-named key (`KEY: v` or `- KEY=v`, suffixed or quoted keys
    included) to anything but exactly `${NAME}`. Raw text: `compose config` interpolates."""
    bad = []
    for lineno, line in enumerate(compose_text.splitlines(), start=1):
        match = _CREDENTIAL_LITERAL.search(line)
        if match and not _VAR_REF.fullmatch(match.group(1).strip().strip("\"'")):
            bad.append(f"docker-compose.yaml:{lineno}: {line!r}")
    return bad


def test_compose_defines_no_credential_literal():
    """INF-U-007: no service defines a credential literal — every value is `${VAR}`."""
    assert _credential_literals((REPO_ROOT / "docker-compose.yaml").read_text()) == []
    # The check itself must see both environment forms.
    assert _credential_literals("      MINIO_ROOT_PASSWORD: hunter2")
    assert _credential_literals("      - MINIO_ROOT_PASSWORD=hunter2")
    assert _credential_literals("      AWS_ACCESS_KEY_ID: AKIAEXAMPLE")  # suffixed key
    assert _credential_literals('      "MINIO_ROOT_PASSWORD": hunter2')  # quoted key
    assert _credential_literals("      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:-minioadmin}")
    assert _credential_literals("      MINIO_ROOT_PASSWORD: ${A}literal${B}")
    assert not _credential_literals('      - "MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}"')
    assert not _credential_literals("      AWS_ACCESS_KEY_ID: ${MLFLOW_S3_ACCESS_KEY}")


def _env_ref(name: str) -> re.Pattern[str]:
    return re.compile(rf"\$\{{\{{\s*env\.{name}\s*\}}\}}")


_MINIO_IMAGE = re.compile(r"^ghcr\.io/mycookie/minio:(RELEASE\.[^@\s]+)@sha256:[0-9a-f]{64}$")


def test_minio_image_pin_is_consistent():
    """INF-U-008: compose minio image is GHCR tag+digest; its tag is the workflow's release tag
    plus `-hardened-<run id>` (the form env.IMAGE publishes; the bare release tag is rejected);
    minio.Dockerfile ARGs are default-free and its final stage is non-root; every FROM in every
    docker/*.Dockerfile is digest-pinned;
    the full string is verbatim in the 05 §1 table and the operations guide, and 05 §1 names the
    workflow's source commit."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yaml").read_text())
    image = compose["services"]["minio"]["image"]
    match = _MINIO_IMAGE.match(image)
    assert match, (
        f"minio image is not ghcr.io/mycookie/minio:RELEASE.<tag>@sha256:<digest>: {image}"
    )

    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/minio-image.yml").read_text())
    env = workflow["env"]
    release_tag = "RELEASE." + env["VERSION"].replace(":", "-")
    assert re.fullmatch(
        rf"ghcr\.io/mycookie/minio:{re.escape(release_tag)}-hardened-\$\{{\{{\s*github\.run_id\s*\}}\}}",
        env["IMAGE"],
    ), f"env.IMAGE is not <release tag>-hardened-<run id>: {env['IMAGE']}"
    assert re.fullmatch(rf"{re.escape(release_tag)}-hardened-[0-9]+", match.group(1)), (
        f"compose tag {match.group(1)} is not {release_tag}-hardened-<run id>"
    )
    steps = workflow["jobs"]["publish"]["steps"]
    (src,) = [s for s in steps if s.get("with", {}).get("repository") == "MyCookie/minio"]
    assert _env_ref("COMMIT").fullmatch(src["with"]["ref"])
    (build,) = [s for s in steps if s.get("id") == "build"]
    assert re.search(rf"^VERSION={_env_ref('VERSION').pattern}$", build["with"]["build-args"], re.M)
    assert re.search(rf"^COMMIT={_env_ref('COMMIT').pattern}$", build["with"]["build-args"], re.M)

    dockerfile = (REPO_ROOT / "docker/minio.Dockerfile").read_text()
    for arg in ("VERSION", "COMMIT"):
        assert re.search(rf"^ARG {arg}$", dockerfile, re.M), f"Dockerfile needs a bare ARG {arg}"
    froms = re.findall(r"^FROM\s.*$", dockerfile, re.M)
    assert froms, "Dockerfile has no FROM"
    for line in froms:
        assert re.search(r"@sha256:[0-9a-f]{64}(\s|$)", line), f"FROM is not digest-pinned: {line}"
    for path in sorted((REPO_ROOT / "docker").glob("*.Dockerfile")):
        for line in re.findall(r"^FROM\s.*$", path.read_text(), re.M):
            assert re.search(r"@sha256:[0-9a-f]{64}(\s|$)", line), (
                f"{path.name}: FROM is not digest-pinned: {line}"
            )
    final_stage = dockerfile[dockerfile.rindex("\nFROM ") :]
    users = re.findall(r"^USER\s+(\S+)", final_stage, re.M)
    assert users and users[-1].split(":")[0] not in ("root", "0"), (
        f"final stage must end as a non-root USER, got {users}"
    )

    for doc in ("docs/spec/05-infrastructure.md", "docs/04-operations.md"):
        assert image in (REPO_ROOT / doc).read_text(), f"{doc} lacks the compose minio image"
    source = f"MyCookie/minio@{env['COMMIT']}"
    assert source in (REPO_ROOT / "docs/spec/05-infrastructure.md").read_text()


_MLFLOW_CLIENT_PACKAGES = ("mlflow", "mlflow-skinny", "mlflow-tracing")


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _mlflow_pin_violations(dockerfile: str, lock: str) -> list[str]:
    """Reasons the server tag in `dockerfile` and the client pins in `lock` break 05 §1."""
    tags = re.findall(r"^FROM\s+ghcr\.io/mlflow/mlflow:v(\d+(?:\.\d+)*)[@\s]", dockerfile, re.M)
    if len(tags) != 1:
        return [f"expected one ghcr.io/mlflow/mlflow:v<version> FROM line, found {len(tags)}"]
    locked = {p["name"]: p["version"] for p in tomllib.loads(lock).get("package", [])}
    missing = [name for name in _MLFLOW_CLIENT_PACKAGES if name not in locked]
    if missing:
        return [f"uv.lock has no {name}" for name in missing]
    problems = []
    if len({locked[name] for name in _MLFLOW_CLIENT_PACKAGES}) != 1:
        problems.append(
            "client packages differ: "
            + ", ".join(f"{name} {locked[name]}" for name in _MLFLOW_CLIENT_PACKAGES)
        )
    if _version_tuple(tags[0]) < max(_version_tuple(locked[n]) for n in _MLFLOW_CLIENT_PACKAGES):
        problems.append(f"server v{tags[0]} is older than the locked client")
    return problems


def _lock(mlflow: str, skinny: str, tracing: str) -> str:
    return "".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n\n'
        for name, version in zip(_MLFLOW_CLIENT_PACKAGES, (mlflow, skinny, tracing), strict=True)
    )


def _mlflow_from(tag: str) -> str:
    return f"FROM ghcr.io/mlflow/mlflow:v{tag}@sha256:{'0' * 64}\n"


def test_mlflow_server_is_not_older_than_locked_client():
    """INF-U-019: docker/mlflow.Dockerfile's server tag is >= the locked mlflow client, and
    mlflow, mlflow-skinny and mlflow-tracing are locked at one version (05 §1)."""
    assert (
        _mlflow_pin_violations(
            (REPO_ROOT / "docker/mlflow.Dockerfile").read_text(),
            (REPO_ROOT / "uv.lock").read_text(),
        )
        == []
    )

    same = _lock("3.18.0", "3.18.0", "3.18.0")
    for dockerfile, lock, expected in [
        (_mlflow_from("3.18.0"), same, []),  # server equals client
        (_mlflow_from("3.19.0"), same, []),  # server ahead is the supported direction
        (_mlflow_from("3.9.0"), _lock("3.10.0", "3.10.0", "3.10.0"), ["older"]),  # not lexical
        (_mlflow_from("3.18.0"), _lock("3.18.1", "3.18.1", "3.18.1"), ["older"]),  # patch bump
        (_mlflow_from("3.18.0"), _lock("3.19.0", "3.19.0", "3.19.0"), ["older"]),  # minor bump
        (_mlflow_from("3.18.0"), _lock("3.18.0", "3.18.1", "3.18.0"), ["differ", "older"]),
        (_mlflow_from("3.19.0"), _lock("3.18.0", "3.18.0", "3.18.1"), ["differ"]),
        (_mlflow_from("3.18.0"), same.split("[[package]]", 1)[0], ["no mlflow"]),
        (f"FROM python:3.11@sha256:{'0' * 64}\n", same, ["expected one"]),
    ]:
        found = "\n".join(_mlflow_pin_violations(dockerfile, lock))
        assert bool(found) == bool(expected), (dockerfile, lock, found)
        for word in expected:
            assert word in found, (dockerfile, lock, found)


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
