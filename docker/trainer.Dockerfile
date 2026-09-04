# CUDA-runtime image for GPU stages (trainer, smoke). 05-infrastructure.md §2.
# Non-root uid 1000, uv-installed deps incl. the `train` extra (torch/transformers/
# peft/bitsandbytes/accelerate), src/tuner installed. Never bakes data or credentials.
#
# Ubuntu 24.04's base CUDA runtime satisfies requires-python>=3.11 with its stock
# python3 (3.12) -- no deadsnakes/PPA needed, unlike the 22.04-based tags.
FROM nvidia/cuda:12.6.2-runtime-ubuntu24.04

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

# The nvidia/cuda:*-ubuntu24.04 base images ship a default non-root `ubuntu` user
# already sitting on uid/gid 1000 (Canonical's own Ubuntu 24.04 base-image policy) --
# a plain `useradd --uid 1000` fails outright ("UID 1000 is not unique", exit 4)
# against this specific base, unlike python:3.11-slim (base.Dockerfile), which ships
# no such user (T15/INF-S-020 finding: this build had never actually been run for
# real before T15). Remove it first so `tuner` can claim uid/gid 1000 instead --
# still exactly one non-root uid-1000 user in the final image, just renamed.
RUN userdel -r ubuntu \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin tuner

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY scripts/ scripts/

RUN pip install --no-cache-dir --break-system-packages "uv>=0.4" \
    && uv sync --frozen --no-dev --extra train \
    && chown -R tuner:tuner /app

ENV PATH="/app/.venv/bin:${PATH}"
USER tuner

ENTRYPOINT ["tuner"]
