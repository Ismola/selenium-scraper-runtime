# Selenium scraper runtime

Public Python library and Docker base image for Selenium scraper services.

## Install

```bash
pip install 'selenium-scraper-runtime[browser] @ git+https://github.com/Ismola/selenium-scraper-runtime.git@v0.1.0'
```

Use `ghcr.io/ismola/selenium-scraper-runtime:v0.1.0` as the Docker base image. Pin a released tag in each scraper and update it through its test pipeline. The image supplies Python 3.10, Chromium with its matching driver, Firefox, fonts, a non-root user, and the Python package. Keep scraper-specific dependencies in the consuming repository.

```dockerfile
FROM ghcr.io/ismola/selenium-scraper-runtime:v0.1.0
USER root
COPY --chown=scraper:scraper requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=scraper:scraper . .
USER scraper
ENTRYPOINT ["/app/docker-entrypoint.sh"]
```

## Logging and metrics

```python
from selenium_scraper_runtime import init_metrics, init_request_logging, track_task

init_request_logging(app)
init_metrics(app)
with track_task("download_documents"):
    run_scraper()
```

Application logs are JSON lines on stdout. Docker exposes them to Alloy; Alloy writes them to Loki for Grafana. `X-Run-ID` also appears in every HTTP response for correlation. The library preserves existing `scraper_*` Prometheus metric names and labels.

The first rollout is limited to Starnaliza scrapers. Other scraper services can migrate after the quickstarter and its Siryus fork pass their test suites.
