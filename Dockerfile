# syntax=docker/dockerfile:1

# --- Build stage: install the app and its dependencies into /app/.venv ---
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Dependencies first, so this layer stays cached until uv.lock changes
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Runtime stage: plain Python plus the virtualenv, running as a non-root user ---
FROM python:3.13-slim-bookworm
RUN useradd --create-home --uid 1000 bot
COPY --from=build --chown=bot:bot /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER bot
WORKDIR /home/bot
CMD ["earnings-bot"]
