FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKERIZED=true \
    CHROME_BIN=/usr/bin/chromium \
    FIREFOX_BIN=/usr/bin/firefox-esr \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus-multiproc

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver firefox-esr fonts-dejavu fonts-liberation \
    fonts-freefont-ttf ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system scraper \
    && useradd --system --create-home --gid scraper scraper \
    && mkdir -p /app/logs /app/temp_downloads /tmp/prometheus-multiproc \
    && chown -R scraper:scraper /app /tmp/prometheus-multiproc

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir '.[browser]'

USER scraper
EXPOSE 3000 9090
