# ── Stage 1: dependency builder ───────────────────────────────────────────
# Use a full image to compile any C extensions (e.g. yfinance deps),
# then copy only the installed packages to the final slim image.
FROM python:3.12-slim AS builder

WORKDIR /build

# System deps needed to compile Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="tumbot"
LABEL description="tumbot — Polymarket prediction market trading bot"

# Non-root user for security
RUN groupadd -r botuser && useradd -r -g botuser botuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY src/       ./src/
COPY tests/     ./tests/
COPY main.py    .
COPY requirements.txt .

# SQLite database directory — persisted via volume mount
RUN mkdir -p /data && chown botuser:botuser /data

# The bot writes bot.db to /data (set via BOT_DB env var)
ENV BOT_DB=/data/bot.db

# Unbuffered output so logs appear immediately in docker logs
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

USER botuser

# Health check: verify the Python environment is intact
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import yfinance, requests, rich; print('ok')" || exit 1

CMD ["python", "main.py"]
