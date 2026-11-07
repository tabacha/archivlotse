FROM python:3.12-slim

# tzdata: die cron:-Zeitplaene in config/batch.yaml gelten in der lokalen
# Zeit des Containers (TZ, siehe docker-compose.yml).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY archivlotse ./archivlotse
RUN pip install --no-cache-dir .

# Vorgaben fuer steps.yaml, batch.yaml und Prompts (ohne Geheimnisse, siehe
# .dockerignore). Wird in docker-compose.yml durch ./config ersetzt, damit
# Prompts/Regeln ohne neues Image angepasst werden koennen.
COPY config ./config

RUN useradd --uid 1000 --create-home archivlotse \
    && mkdir -p /app/data \
    && chown archivlotse:archivlotse /app/data
USER archivlotse

ENV PYTHONUNBUFFERED=1 \
    ARCHIVLOTSE_PORT=8080
EXPOSE 8080
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('ARCHIVLOTSE_PORT', '8080'), timeout=4)"

ENTRYPOINT ["archivlotse"]
CMD ["serve"]
