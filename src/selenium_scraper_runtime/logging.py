"""JSON logs on stdout with per-request correlation."""

import contextvars
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

from flask import request

run_id = contextvars.ContextVar("scraper_run_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        event = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": os.getenv("SCRAPER_SERVICE_NAME", "selenium-scraper"),
            "logger": record.name,
            "message": record.getMessage(),
        }
        correlation = run_id.get()
        if correlation:
            event["run_id"] = correlation
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event, ensure_ascii=False)


def configure_logging(level=None):
    """Install one JSON stdout handler on the root logger, idempotently."""
    root = logging.getLogger()
    root.setLevel(level or os.getenv("LOG_LEVEL", "INFO").upper())
    for handler in root.handlers:
        if getattr(handler, "_scraper_runtime", False):
            return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._scraper_runtime = True
    root.addHandler(handler)


def init_request_logging(app):
    """Add a request ID to logs without changing response bodies."""
    configure_logging()

    @app.before_request
    def set_run_id():
        request._scraper_run_id_token = run_id.set(uuid.uuid4().hex)

    @app.after_request
    def add_run_id_header(response):
        response.headers["X-Run-ID"] = run_id.get()
        return response

    @app.teardown_request
    def clear_run_id(_exception):
        token = getattr(request, "_scraper_run_id_token", None)
        if token is not None:
            run_id.reset(token)
