FROM python:3.12.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# uharfbuzz (real PDF text shaping) ships a native ext that needs libstdc++.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --requirement requirements.lock

COPY app ./app
COPY scripts ./scripts
COPY .env.example ./README.md ./

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/storage \
    && chown -R appuser:appuser /app
USER appuser

# MALLOC_ARENA_MAX: every asyncio.to_thread worker otherwise gets its own
# glibc malloc arena, and churning multi-MB image buffers (provider responses,
# PDF embeds) across them fragments memory that is never handed back. On the
# 512 MiB free plan that bloat alone OOM-killed the instance mid-render; the
# same workload measured 302 MiB resident by default and ~160 MiB with 2.
ENV APP_ENV=production \
    STORAGE_DIR=/app/storage \
    MALLOC_ARENA_MAX=2

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', 8000))"

# --proxy-headers makes request.client the real visitor address behind
# Render's proxy, so the rate limiter and login lockout are per client.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=*"]
