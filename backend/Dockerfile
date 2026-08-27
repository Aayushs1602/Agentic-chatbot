# syntax=docker/dockerfile:1
FROM python:3.12-slim

# git: the ingest CLI shallow-clones the transcripts repo.
# curl: healthcheck / debugging convenience.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1

# Non-root. /data holds the cloned corpus, ~/.cache/fastembed the ONNX weights;
# both are Compose volumes, so both must be writable by this user.
RUN useradd --create-home --uid 1000 app \
 && mkdir -p /data /home/app/.cache/fastembed \
 && chown -R app:app /data /home/app/.cache

WORKDIR /app

# Requirements first so the dependency layer caches across code edits.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --upgrade pip && pip install -r requirements-dev.txt

COPY --chown=app:app . .

USER app
EXPOSE 8000

# Migrations run in the app's lifespan, not here — one code path whether the
# backend starts under Compose, under uvicorn directly, or in a test.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
