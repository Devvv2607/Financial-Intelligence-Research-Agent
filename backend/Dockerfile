# syntax=docker/dockerfile:1
# Build context: repo root (needs the workspace pyproject.toml + uv.lock).

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

# ---- builder: resolve and install into /app/.venv -------------------------
FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, in their own layer: code edits don't re-download wheels.
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package fira-backend

# Then our own code, installed non-editable so the runtime only needs .venv.
COPY backend/src backend/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package fira-backend

# ---- runtime: slim image, non-root ---------------------------------------
FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app config config
RUN mkdir -p data/models && chown -R app:app data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER app
# Bake the embedding model into the image (data/models = Settings.models_dir), so a cold
# start on the host doesn't download it. Must match EMBEDDING_MODEL at runtime.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "import sys; from fastembed import TextEmbedding; TextEmbedding(sys.argv[1], cache_dir='data/models')" "$EMBEDDING_MODEL"
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://localhost:8000/health').status != 200)"
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
