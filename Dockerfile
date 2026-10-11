# Container for Google Cloud Run (also works on any container host).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first (cached layer), exactly as pinned in uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY server.py ./

RUN useradd --create-home --uid 10001 app
USER app

# Cloud Run injects PORT (default 8080) and K_SERVICE; server.py then binds 0.0.0.0:$PORT,
# runs stateless, and does not write images to disk.
CMD ["python", "server.py"]
