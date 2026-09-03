# Mock-judge sidecar (docs/spec/06-testing.md §4) -- test infrastructure only, never
# deployed to a real environment. Same non-root pattern as base.Dockerfile, but
# installs the `dev` extra (fastapi/uvicorn) instead of `train`/nothing, and copies
# tests/mock_judge/ instead of just src/tuner -- this image has no product code in it.
FROM python:3.11-slim

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin tuner

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY tests/mock_judge/ tests/mock_judge/

RUN pip install --no-cache-dir "uv>=0.4" \
    && uv sync --frozen --extra dev \
    && chown -R tuner:tuner /app

ENV PATH="/app/.venv/bin:${PATH}"
USER tuner

ENTRYPOINT ["python", "-m", "uvicorn", "tests.mock_judge.app:app", "--host", "0.0.0.0", "--port", "8088"]
