# syntax=docker/dockerfile:1.7
# ─── Stage 1: builder ─────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc curl unixodbc-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ─── Stage 2: runtime ─────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 unixodbc curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 app && useradd -u 1000 -g app -s /bin/bash -m app

COPY --from=builder /install /usr/local
WORKDIR /app
# CACHEBUST: forces Docker to never cache the COPY layer
ARG CACHEBUST=1780114236
COPY --chown=app:app . /app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS\" = \"true\" ]; then alembic -c app/migrations/alembic.ini upgrade head; fi && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
