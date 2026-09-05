"""Slow-lane container structure (INF-S-020, docs/spec/08-test-specs/infra.md).

Needs a working `docker` CLI with build + run permissions (no compose services
required -- these are plain `docker build`/`docker run` invocations against the two
Dockerfiles directly, not the compose topology). Not marked `integration` -- see
tests/slow/test_scale.py's module docstring for why `slow` alone is the right marker.

Builds real images every run rather than trusting a cached tag: the point of this
case is catching drift in the Dockerfiles themselves (a stray `USER root`, an
accidentally-baked `.env`, a base image whose Python slipped below 3.11), which a
stale cached image would silently hide.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]

_IMAGES = {
    "base": ("docker/base.Dockerfile", "tuner-base:inf-s-020"),
    "trainer": ("docker/trainer.Dockerfile", "tuner-trainer:inf-s-020"),
}


def _build(dockerfile: str, tag: str) -> None:
    result = subprocess.run(
        ["docker", "build", "-f", dockerfile, "-t", tag, "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert result.returncode == 0, (
        f"docker build -f {dockerfile} failed:\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
    )


def _run(
    tag: str, args: list[str], entrypoint: str | None = None, user: str | None = None
) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm"]
    if user is not None:
        cmd += ["--user", user]
    if entrypoint is not None:
        cmd += ["--entrypoint", entrypoint]
    cmd += [tag, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="module", params=sorted(_IMAGES))
def built_image(request):
    """Builds one image once per module (session-shared across every check below,
    per this suite's own 'shared session-scoped fixture to keep runtime down'
    convention, e.g. tests/integration/test_trainer.py's `tokenized_run_id`)."""
    name = request.param
    dockerfile, tag = _IMAGES[name]
    _build(dockerfile, tag)
    return name, tag


@pytest.mark.slow
def test_container_runs_as_non_root_uid_1000(built_image):
    """INF-S-020: both images run as uid 1000 non-root -- `whoami` != root."""
    name, tag = built_image
    result = _run(tag, [], entrypoint="whoami")
    assert result.returncode == 0, result.stderr
    whoami = result.stdout.strip()
    assert whoami != "root", f"{name} image runs as root"
    assert whoami == "tuner", f"{name} image whoami: {whoami!r}"

    uid_result = _run(tag, ["-u"], entrypoint="id")
    assert uid_result.stdout.strip() == "1000", f"{name} image uid: {uid_result.stdout!r}"


@pytest.mark.slow
def test_container_entrypoint_runs_tuner_help(built_image):
    """INF-S-020: `tuner --help` works as the container entrypoint."""
    name, tag = built_image
    result = subprocess.run(
        ["docker", "run", "--rm", tag, "--help"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"{name}: {result.stdout}\n{result.stderr}"
    assert "Tuner" in result.stdout or "Usage" in result.stdout, result.stdout


@pytest.mark.slow
def test_container_python_at_least_3_11(built_image):
    """INF-S-020: image Python is >= 3.11."""
    name, tag = built_image
    result = _run(tag, ["--version"], entrypoint="python3")
    assert result.returncode == 0, result.stderr
    # "Python 3.11.9" / "Python 3.12.3" -> (3, 11) / (3, 12)
    version_str = result.stdout.strip().removeprefix("Python ")
    major, minor = (int(part) for part in version_str.split(".")[:2])
    assert (major, minor) >= (3, 11), f"{name} image Python {version_str}"


# Literal well-known credential-STORE file names/paths -- deliberately not a
# substring/glob match on "credentials" or "*.pem": both `train`/`base` images
# install real cloud SDKs (boto3/botocore, google-auth, minio, docker) whose own
# *source code* ships modules named exactly `credentials.py`, and the base image's
# CA trust bundle is hundreds of legitimately-public `*.pem` root certificates --
# an earlier version of this check matched both and flagged them as "baked-in
# secrets," a false positive discovered running this suite for real in T15. This
# list matches actual credential-bearing file names a Dockerfile mistake (or a
# stray host mount) could plausibly leak in, not any file whose name merely
# contains a credential-adjacent word.
_CREDENTIAL_FILENAMES = (
    ".env",
    ".env.*",  # .env.local, .env.production, etc. -- glob, not a literal filename
    "credentials",  # ~/.aws/credentials
    ".git-credentials",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    ".pgpass",
)


@pytest.mark.slow
def test_container_has_no_baked_credentials(built_image):
    """INF-S-020: image has no `.env` or credential files baked in.

    Runs the scan as `--user 0` (root) inside the container, not as the image's
    own uid-1000 user: `find` run as uid 1000 gets `Permission denied` on
    root-owned directories (`/root`, `/etc/ssl/private`, ...) and silently skips
    them, which would make this check blind to exactly the paths a leaked
    credential is most likely to land in (`~/.aws/credentials` for a root-built
    image is `/root/.aws/credentials`) -- discovered running a deliberately
    poisoned control image in T15 round-1 review. `--user 0` only changes what
    the *scanner* can read; it says nothing about what uid the image itself runs
    as, which `test_container_runs_as_non_root_uid_1000` already proves separately.
    """
    name, tag = built_image
    # -type f: both `minio` and `docker` (real, expected dependencies of this repo's
    # own StorageClient/registry code) ship a *subpackage directory* literally named
    # `credentials/` -- a directory can't hold a leaked secret by itself, only a file
    # under it could, and none of the exact filenames below match anything real
    # inside either (spot-checked against tuner-base:inf-s-020 in T15).
    find_args = ["/", "-xdev", "-type", "f", "("]
    for i, filename in enumerate(_CREDENTIAL_FILENAMES):
        if i:
            find_args.append("-o")
        find_args += ["-iname", filename]
    find_args.append(")")

    result = _run(tag, find_args, entrypoint="find", user="0")
    # A non-zero exit (or any stderr) means the scan itself failed to cover the
    # filesystem -- e.g. a permission error -- which must fail loud, not be
    # indistinguishable from "scanned everything, found nothing" (T15 round-1
    # review: the uid-1000 version of this check failed exactly this way).
    assert result.returncode == 0, (
        f"{name} credential scan did not complete cleanly (exit {result.returncode}): "
        f"{result.stderr}"
    )
    hits = [line for line in result.stdout.splitlines() if line.strip()]
    assert hits == [], f"{name} image bakes in credential-shaped file(s): {hits}"
