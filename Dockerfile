# syntax=docker/dockerfile:1

# ---- build a slim, non-root image for the agent ----
FROM python:3.12-slim AS base

# No .pyc, unbuffered logs (so kubectl logs is live).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY app/ ./app/

# Run as an unprivileged user — the agent never needs root, and the
# restricted PodSecurity standard on the namespace requires it.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8080

# `python -m app.main` so package-relative imports resolve.
ENTRYPOINT ["python", "-m", "app.main"]
