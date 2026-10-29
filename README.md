# Selenium scraper runtime

Public Python library and Docker base image for Selenium scraper services.

## Install

```bash
pip install 'selenium-scraper-runtime[browser] @ https://github.com/Ismola/selenium-scraper-runtime/archive/refs/heads/main.tar.gz'
```

Use `ghcr.io/ismola/selenium-scraper-runtime:latest` as the Docker base image. Consumer projects install the Python package from `main`, so rebuilding them picks up the current shared runtime. The image supplies Python 3.10, Chromium with its matching driver, Firefox, fonts, a non-root user, and the Python package. Keep scraper-specific dependencies in the consuming repository.

```dockerfile
FROM ghcr.io/ismola/selenium-scraper-runtime:latest
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

init_request_logging(app, log_json_body=True)
init_metrics(app)
with track_task("download_documents"):
    run_scraper()
```

Application logs are JSON lines on stdout. Docker exposes them to Alloy; Alloy writes them to Loki for Grafana. `X-Run-ID` also appears in every HTTP response for correlation. The library preserves existing `scraper_*` Prometheus metric names and labels.

JSON request bodies are checked for a `username` or `usuario` field and that value is logged at INFO level for request correlation. With `log_json_body=True`, the complete JSON body is also logged. Passwords and other sensitive values are recursively replaced with `[REDACTED]` when their field names contain terms such as `password`, `passwd`, `pwd`, `contraseña`, `email`, `login`, `secret`, `token`, `authorization`, `credential`, or `cookie`. Username fields are retained; generic `user` and `login` fields remain redacted. Non-JSON request bodies are not logged.

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

`create_driver`, `get_page`, `get_wait`, `reload_driver`, and `close_driver` are also available from `selenium_scraper_runtime.browser`. `hover_element` and `make_element_interactable` are available from `selenium_scraper_runtime.elements`; the latter is an explicit workaround, since enable it for a site with `SELENIUM_FORCE_INTERACTABLE=true`. The browser helper reads `BASE_URL`, `HEADLESS_MODE`, `BROWSER_LANGUAGE`, `DOWNLOAD_DIR`, `PAGE_MAX_TIMEOUT`, and `SELENIUM_URL` from the environment. `SELENIUM_STEALTH=true` enables optional Chrome stealth settings. Site-specific selectors and workflow steps stay in each scraper.

For local development the runtime detects `chromium`, `chromium-browser`, `google-chrome-stable`, or `google-chrome`, in that order. `CHROME_BIN` and `CHROMEDRIVER_BIN` can select explicit executables. Devcontainers should use this runtime image and set `DOCKERIZED=false` so `HEADLESS_MODE=auto` can display browser windows.

The library sets finite WebDriver command, page, and script timeouts (60, 60, and 30 seconds by default). `close_driver` quits the session and terminates only its surviving local process tree. A separate guard notices worker crashes and caps each local session at `WEBDRIVER_MAX_LIFETIME` (720 seconds by default); increase this value for legitimate longer jobs. Use `browser_session` or a `try/finally` block so normal work closes promptly. `WEBDRIVER_COMMAND_TIMEOUT`, `PAGE_LOAD_TIMEOUT`, and `SCRIPT_TIMEOUT` override the other limits.

## Failure reports shared by scrapers

Opt in without changing API response bodies:

```python
from selenium_scraper_runtime.logging import init_request_logging
from selenium_scraper_runtime.diagnostics import capture_failure

init_request_logging(app, log_json_body=True, failure_diagnostics=True)
# In each controller, inside except and BEFORE closing its browser:
# capture_failure(driver)
```

Set `REQUEST_DIAGNOSTICS_DIR` to a persistent spool shared with one worker running
`python -m selenium_scraper_runtime.notifications` from this same image.
An existing screenshot helper can be passed as `screenshot=take_screenshot` to
`init_request_logging`; it must accept `directory` and return a PNG path there.
The shared `diagnostics.take_screenshot` preserves that signature.

Every failed HTTP response (400–599) queues its own report using the generated
`X-Run-ID`. The email includes all application log records emitted at the configured
log level during that request, traceback, HTTP context, an inline browser screenshot,
and a complete `.log` attachment. Request credentials are redacted. Successful
requests discard their evidence. Requests that fail before a browser starts or with
an unresponsive browser still send logs and explain why a screenshot is unavailable.
`get_page` navigation failures and `browser_session` also capture before cleanup.

Worker configuration: `SCRAPER_SMTP_HOST`, `SCRAPER_SMTP_PORT` (465),
`SCRAPER_SMTP_SECURITY` (`ssl`, `starttls`, or `plain` for local tests),
`SCRAPER_SMTP_USER`, `SCRAPER_SMTP_PASSWORD`, `SCRAPER_ALERT_FROM`, and
comma-separated `SCRAPER_ALERT_TO`. Keep credentials in deployment secrets.
The worker takes a spool lock, retries with exponential backoff, and marks reports
sent only after SMTP accepts them. Delivery is at least once: a crash after SMTP
acceptance can resend, with the same Message-ID. No pending report is removed by
retention; sent and abandoned evidence expires after
`REQUEST_DIAGNOSTICS_RETENTION_DAYS` (7). Run the worker under a user that can read
and update the scraper's private spool. `.heartbeat` is updated each scan; also
monitor the age of pending reports to detect mail delivery problems.

## Scheduled task failure evidence (v0.4)

Use `@tracked_task("task_name", failure_diagnostics=True)` (or the equivalent
`track_task` context manager) for cron/worker flows. Every cycle receives a new
correlation ID. A failed cycle queues its complete logs, traceback and screenshots
in the same durable spool used for HTTP errors, without needing a Flask context.
Call `capture_failure(driver)` before closing a failed browser. Caught extraction
errors should log at ERROR with their traceback and capture at the failing step;
`raise_if_task_errors()` at the end of a cycle ensures cleanup and failure metrics.
An opt-in cycle that logs ERROR is reported as failed even if the caller swallowed
the exception. Successful or intentionally skipped cycles log their outcome but do
not send email. Environment credentials and explicitly supplied `secrets=` values
are redacted, and parallel cycles retain separate reports.

Unmatched HTTP routes (404) and disallowed methods (405) never queue email reports.
A legitimate endpoint returning 404 still does. The notification worker also
ignores legacy 404/405 scanner reports queued before this version; these are
marked `ignored.json` and expire under the normal retention policy. This filter
does not suppress business failures, HTTP 5xx, or scheduled task failures.
