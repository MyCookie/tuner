# Base image for all CPU stages (ingestor, cleaner, judge, tokenizer) and
# infra tooling (minio-init). 05-infrastructure.md §2. Non-root uid 1000,
# uv-installed deps, src/tuner installed. Never bakes data or credentials.
FROM python:3.11-slim

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin tuner

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY scripts/ scripts/

RUN pip install --no-cache-dir "uv>=0.4" \
    && uv sync --frozen --no-dev \
    && chown -R tuner:tuner /app

ENV PATH="/app/.venv/bin:${PATH}"
USER tuner

ENTRYPOINT ["tuner"]
