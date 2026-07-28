# Multi-stage build: uv resolves/install in builder, slim runtime image, non-root user.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic ./alembic
COPY alembic.ini ./
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER app
CMD ["jobtrack", "serve"]
