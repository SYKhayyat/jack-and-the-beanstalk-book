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

ENV IMAGE_PROVIDER=mock \
    APP_ENV=production \
    STORAGE_DIR=/app/storage

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
