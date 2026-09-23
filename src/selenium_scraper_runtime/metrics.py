import os
import time
from contextlib import contextmanager
from functools import wraps

from flask import g, request
from prometheus_client import Counter, Gauge, Histogram


def _ensure_multiprocess_directory():
    """Create Prometheus' temporary directory when the container cleared /tmp."""
    path = os.getenv("PROMETHEUS_MULTIPROC_DIR")
    if path:
        os.makedirs(path, exist_ok=True)


_ensure_multiprocess_directory()


SERVICE_NAME = os.getenv("SCRAPER_SERVICE_NAME", "selenium-scraper-quickstarter")

HTTP_REQUESTS = Counter(
    "scraper_http_requests_total",
    "Total HTTP requests handled by the scraper API.",
    ("service", "method", "endpoint", "status", "outcome"),
)
HTTP_REQUEST_DURATION = Histogram(
    "scraper_http_request_duration_seconds",
    "Scraper API request duration in seconds.",
    ("service", "method", "endpoint"),
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "scraper_http_requests_in_progress",
    "Scraper API requests currently in progress.",
    ("service",),
    multiprocess_mode="livesum",
)
UNHANDLED_EXCEPTIONS = Counter(
    "scraper_unhandled_exceptions_total",
    "Unhandled exceptions raised while serving scraper API requests.",
    ("service", "endpoint"),
)
BUILD_INFO = Gauge(
    "scraper_build_info",
    "Scraper service identity.",
    ("service",),
    multiprocess_mode="livemax",
)
BUILD_INFO.labels(service=SERVICE_NAME).set(1)
HTTP_LAST_REQUEST = Gauge(
    "scraper_http_last_request_timestamp_seconds",
    "Unix timestamp of the last completed HTTP request.",
    ("service", "outcome"),
    multiprocess_mode="livemax",
)
HTTP_LAST_SUCCESS = Gauge(
    "scraper_http_last_success_timestamp_seconds",
    "Unix timestamp of the last successful HTTP request.",
    ("service",),
    multiprocess_mode="livemax",
)

TASK_RUNS = Counter(
    "scraper_task_runs_total",
    "Total scraper task executions, including scheduled and internal work.",
    ("service", "task", "outcome"),
)
TASK_DURATION = Histogram(
    "scraper_task_duration_seconds",
    "Scraper task execution duration in seconds.",
    ("service", "task"),
    buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200),
)
TASKS_IN_PROGRESS = Gauge(
    "scraper_tasks_in_progress",
    "Scraper tasks currently running.",
    ("service", "task"),
    multiprocess_mode="livesum",
)
TASK_LAST_RUN = Gauge(
    "scraper_task_last_run_timestamp_seconds",
    "Unix timestamp of the last scraper task completion.",
    ("service", "task", "outcome"),
    multiprocess_mode="livemax",
)
TASK_LAST_SUCCESS = Gauge(
    "scraper_task_last_success_timestamp_seconds",
    "Unix timestamp of the last successful scraper task completion.",
    ("service", "task"),
    multiprocess_mode="livemax",
)


@contextmanager
def track_task(task):
    """Track scheduled or internal work that does not pass through Flask."""
    started = time.monotonic()
    outcome = "success"
    TASKS_IN_PROGRESS.labels(service=SERVICE_NAME, task=task).inc()
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        finished_at = time.time()
        TASKS_IN_PROGRESS.labels(service=SERVICE_NAME, task=task).dec()
        TASK_RUNS.labels(service=SERVICE_NAME, task=task, outcome=outcome).inc()
        TASK_DURATION.labels(service=SERVICE_NAME, task=task).observe(
            time.monotonic() - started
        )
        TASK_LAST_RUN.labels(
            service=SERVICE_NAME, task=task, outcome=outcome
        ).set(finished_at)
        if outcome == "success":
            TASK_LAST_SUCCESS.labels(service=SERVICE_NAME, task=task).set(finished_at)


def tracked_task(task):
    """Decorator form of :func:`track_task` for scheduler callbacks."""
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with track_task(task):
                return function(*args, **kwargs)

        return wrapped

    return decorator


def _endpoint():
    return request.url_rule.rule if request.url_rule else "unmatched"


def init_metrics(app):
    @app.before_request
    def observe_request_start():
        g.metrics_start_time = time.monotonic()
        HTTP_REQUESTS_IN_PROGRESS.labels(service=SERVICE_NAME).inc()

    @app.after_request
    def observe_request_end(response):
        start_time = getattr(g, "metrics_start_time", None)
        endpoint = _endpoint()
        outcome = "success" if response.status_code < 400 else "error"
        HTTP_REQUESTS.labels(
            service=SERVICE_NAME,
            method=request.method,
            endpoint=endpoint,
            status=str(response.status_code),
            outcome=outcome,
        ).inc()
        finished_at = time.time()
        HTTP_LAST_REQUEST.labels(service=SERVICE_NAME, outcome=outcome).set(finished_at)
        if outcome == "success":
            HTTP_LAST_SUCCESS.labels(service=SERVICE_NAME).set(finished_at)
        if start_time is not None:
            HTTP_REQUEST_DURATION.labels(
                service=SERVICE_NAME,
                method=request.method,
                endpoint=endpoint,
            ).observe(time.monotonic() - start_time)
            HTTP_REQUESTS_IN_PROGRESS.labels(service=SERVICE_NAME).dec()
            g.metrics_start_time = None
        return response

    @app.teardown_request
    def observe_unhandled_exception(exception):
        if exception is not None:
            UNHANDLED_EXCEPTIONS.labels(
                service=SERVICE_NAME,
                endpoint=_endpoint(),
            ).inc()
            if getattr(g, "metrics_start_time", None) is not None:
                HTTP_REQUESTS_IN_PROGRESS.labels(service=SERVICE_NAME).dec()
                g.metrics_start_time = None
