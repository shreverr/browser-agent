# Annotation server image (Render Docker web service). Build from the repo root:
#   docker build -f browser_agent/evals/annotate/Dockerfile -t browser-agent-annotate .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first so code changes don't bust the layer cache. The core agent deps come
# along (they are the project's own), but no browser is installed: the server never runs one.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra annotate --no-install-project

COPY browser_agent ./browser_agent
RUN uv sync --frozen --no-dev --extra annotate

RUN useradd --system --uid 10001 --no-create-home annotate
USER annotate

EXPOSE 8000
# Runs Alembic migrations, then uvicorn on $PORT (Render sets it; defaults to 8000).
CMD ["browser-agent-annotate", "serve"]
