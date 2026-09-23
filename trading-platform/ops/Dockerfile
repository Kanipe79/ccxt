FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e . || pip install \
    "ccxt>=4.4" pydantic numpy scipy pandas polars optuna aiohttp uvloop \
    fastapi uvicorn structlog prometheus-client clickhouse-driver asyncpg \
    redis nats-py pyyaml pyarrow websockets

COPY src/ ./src/
COPY config/ ./config/

# Never run as root; never bake credentials into the image.
RUN useradd -m -u 10001 ux && chown -R ux:ux /app
USER ux

# Only the ingest role serves /healthz (feed staleness); other roles are supervised via
# the heartbeat.* subjects the L2 watchdog consumes.

CMD ["python", "-m", "uxtrader.run", "--role", "all"]
