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
# The app migrates its own database on boot (app/db.py: prepare_database), so
# the Alembic config and the migration chain ship in the image.
COPY alembic.ini ./
COPY migrations ./migrations
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
# MALLOC_MMAP_THRESHOLD_: glibc otherwise raises its mmap threshold to 32 MiB
# after the first large free, so every later decoded picture (4 to 8 MiB) is
# carved from the heap and its pages are kept after it is freed. Fixed at
# 1 MiB, a picture's buffer is its own mapping and goes back to the OS when
# freed: one book with the R2 codec on one CPU measured 380 MiB resident
# after it finished without this and 258 MiB with it (2026-11-10).
ENV APP_ENV=production \
    STORAGE_DIR=/app/storage \
    MALLOC_ARENA_MAX=2 \
    MALLOC_MMAP_THRESHOLD_=1048576

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', 8000))"

# --proxy-headers makes request.client the real visitor address behind
# Render's proxy, so the rate limiter and login lockout are per client.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=*"]
