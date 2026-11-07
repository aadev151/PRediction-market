# PRediction Market server image (PLAN §8.5). One service, state on the /data volume.
# Secrets (ANTHROPIC_API_KEY, GITHUB_TOKEN, HOLDOUTS_TOKEN, GITHUB_WEBHOOK_SECRET, SETTLE_HMAC_SECRET,
# PRM_ADMIN_TOKEN) are injected by the platform at run time; none are baked in (.dockerignore excludes .env).
FROM python:3.12-slim

# git: settlement clones targets and holdouts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md prm.server.toml ./
COPY prm ./prm
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 prm \
    && mkdir -p /data/cache \
    && chown -R prm:prm /data

USER prm
ENV PRM_SERVER_CONFIG=/app/prm.server.toml \
    PRM_DB=/data/prm.db \
    PRM_CACHE=/data/cache \
    PORT=8000
VOLUME ["/data"]
EXPOSE 8000

# Shell form so $PORT expands (platforms like Fly/Render/Railway set it). `prm serve` lands with WP E.
CMD prm serve --host 0.0.0.0 --port ${PORT}
