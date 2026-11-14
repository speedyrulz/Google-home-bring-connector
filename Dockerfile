FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KEEP_TO_BRING_ENV_FILE=/data/.env \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && mkdir -p /data \
    && chown 1000:1000 /data

# Runs as an unprivileged user. docker-compose.yml lets you override the
# user id (PUID/PGID) to match the owner of the ./data folder.
USER 1000:1000
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=15s --start-period=180s --retries=3 \
    CMD ["keep-to-bring", "health"]

ENTRYPOINT ["keep-to-bring"]
CMD ["run"]
