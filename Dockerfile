# Brain — single-tenant FastAPI second brain.
#
# This Dockerfile demonstrates that the brain CAN be containerized for
# deployment on a fresh machine. It is NOT how Chris currently runs the
# brain (he runs natively via launchd on macOS for tighter ChromaDB I/O),
# but it documents the contract for anyone who wants to deploy on Linux
# or in a CI runner.
#
# Build:
#   docker build -t brain:latest .
#
# Run (assumes ChromaDB / Ollama / Neo4j on the host network):
#   docker run -d --name brain \
#     --network host \
#     -v $HOME/.brain/credentials:/credentials:ro \
#     -v $(pwd)/logs:/app/logs \
#     -e BRAIN_AUTOPILOT_DISABLED=1 \  # safe default for first boot
#     brain:latest
#
# Or via docker-compose.yml (recommended):
#   docker compose up -d

FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
        git \
        libpq-dev \
        libxml2-dev \
        libxslt-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml /app/
RUN pip install --upgrade pip uv && \
    uv venv /app/.venv --python 3.14 && \
    /app/.venv/bin/pip install -e . || true

COPY . /app

RUN /app/.venv/bin/pip install -e . || true

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8791

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8791/healthz || exit 1

CMD ["/app/.venv/bin/python", "/app/server.py"]
