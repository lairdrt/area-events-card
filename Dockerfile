# Area Events discovery service: hourly refresh + read-only HTTP feed.
# Standard library only, so there is nothing to pip install.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AREA_EVENTS_DATA_DIR=/data \
    AREA_EVENTS_PORT=8080

# Fixed unprivileged user; /data is the only writable location.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin events \
    && mkdir /data && chown events:events /data

WORKDIR /app
COPY discovery/ ./discovery/
COPY discovery.config.json ./

USER events
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]

CMD ["python", "-m", "discovery.service"]
