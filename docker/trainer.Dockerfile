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

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin tuner

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
