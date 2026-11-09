# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: install the locked runtime environment into /app/.venv.
#
# uv reads pyproject.toml + uv.lock, so the image resolves exactly the
# dependency set committed to the repository (including the libp2p git fork).
# Build tools and git stay in this stage and never reach the final image.
# ---------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.13.5

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# git is required by the locked libp2p git source; build-essential covers any
# dependency without a prebuilt wheel. Both are discarded with this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        libgmp-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./

# Runtime dependencies only; the application source is copied below so code
# changes do not invalidate the dependency layer.
RUN uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Runtime: minimal Python image, unprivileged user, application on /app,
# durable state on the /data volume.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    UC_DATA_DIR=/data \
    UC_HEALTHCHECK_URL=http://127.0.0.1:8765/api/v1/service/status

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgmp10 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 uc \
    && useradd --uid 10001 --gid uc --create-home \
        --home-dir /home/uc --shell /usr/sbin/nologin uc

WORKDIR /app

# Runtime environment and application code. The React frontend is a separate
# project and is deliberately not included; it talks to this container's API.
COPY --from=builder --chown=uc:uc /app/.venv /app/.venv
COPY --chown=uc:uc main.py ./
COPY --chown=uc:uc core ./core
COPY --chown=uc:uc app ./app
COPY --chown=uc:uc api ./api
COPY --chown=uc:uc llm ./llm

# Durable application state lives only here. A named volume inherits this
# ownership on first use; a bind mount must be chowned by the operator.
RUN mkdir -p /data \
    && chown uc:uc /data

USER uc

# TCP only: the peer currently runs a TCP transport and the API is HTTP.
# UDP is not exposed because QUIC/WebTransport are not enabled.
EXPOSE 4001/tcp 8765/tcp

VOLUME ["/data"]

# The check parses /api/v1/service/status and requires ready + running, so it
# does not report healthy before the libp2p host is actually up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/core/healthcheck.py"]

ENTRYPOINT ["python", "main.py"]

# Headless runtime + HTTP/WebSocket API. The API binds 0.0.0.0 inside the
# container so Docker can route to it; publish it deliberately (Compose maps
# it to host loopback by default). Override --announce-host for your network.
CMD ["--api", "--api-host", "0.0.0.0", "--nick", "docker-peer"]
