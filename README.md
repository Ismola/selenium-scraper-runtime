# Selenium scraper runtime

Public Python library and Docker base image for Selenium scraper services.

## Install

```bash
pip install 'selenium-scraper-runtime[browser] @ git+https://github.com/Ismola/selenium-scraper-runtime.git@v0.2.1'
```

Use `ghcr.io/ismola/selenium-scraper-runtime:v0.2.1` as the Docker base image. Pin a released tag in each scraper and update it through its test pipeline. The image supplies Python 3.10, Chromium with its matching driver, Firefox, fonts, a non-root user, and the Python package. Keep scraper-specific dependencies in the consuming repository.

```dockerfile
FROM ghcr.io/ismola/selenium-scraper-runtime:v0.2.1
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

## Browser and element helpers

```python
from selenium.webdriver.common.by import By
from selenium_scraper_runtime.browser import browser_session
from selenium_scraper_runtime.elements import search_element, write_element, click_element

with browser_session(url="https://example.com/login") as driver:
    username = search_element(driver, (By.NAME, "username"))
    write_element(driver, username, "user@example.com")
    button = search_element(driver, (By.CSS_SELECTOR, "button[type=submit]"))
    click_element(driver, button)
```

`create_driver`, `get_page`, `get_wait`, `reload_driver`, and `close_driver` are also available from `selenium_scraper_runtime.browser`. `hover_element` and `make_element_interactable` are available from `selenium_scraper_runtime.elements`; the latter is an explicit workaround, since changing a site's disabled controls should never be the default. The browser helper reads `BASE_URL`, `HEADLESS_MODE`, `BROWSER_LANGUAGE`, `DOWNLOAD_DIR`, `PAGE_MAX_TIMEOUT`, and `SELENIUM_URL` from the environment. `SELENIUM_STEALTH=true` enables optional Chrome stealth settings. Site-specific selectors and workflow steps stay in each scraper.

The library sets finite WebDriver command, page, and script timeouts (60, 60, and 30 seconds by default). `close_driver` quits the session and terminates only its surviving local process tree. A separate guard notices worker crashes and caps each local session at `WEBDRIVER_MAX_LIFETIME` (720 seconds by default); increase this value for legitimate longer jobs. Use `browser_session` or a `try/finally` block so normal work closes promptly. `WEBDRIVER_COMMAND_TIMEOUT`, `PAGE_LOAD_TIMEOUT`, and `SCRIPT_TIMEOUT` override the other limits.
