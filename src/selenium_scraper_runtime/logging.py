"""JSON logs on stdout with per-request correlation."""

import contextvars
import json
import logging
import os
import re
import sys
import unicodedata
import uuid
from datetime import datetime, timezone

from flask import request

run_id = contextvars.ContextVar("scraper_run_id", default=None)

_SENSITIVE_KEY_PARTS = (
    "password",
    "pasword",
    "passwd",
    "passphrase",
    "passcode",
    "pwd",
    "contrasena",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "privatekey",
    "authorization",
    "credential",
    "cookie",
    "sessionkey",
    "totp",
)


def _normalized_key(key):
    decomposed = unicodedata.normalize("NFKD", str(key)).casefold()
    without_accents = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]", "", without_accents)


def _redact_sensitive_fields(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized_key = _normalized_key(key)
            is_sensitive = (
                "pass" in normalized_key
                or any(part in normalized_key for part in _SENSITIVE_KEY_PARTS)
            )
            redacted[key] = "[REDACTED]" if is_sensitive else _redact_sensitive_fields(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_fields(item) for item in value]
    return value


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


def init_request_logging(app, log_json_body=False):
    """Add request IDs and optionally log redacted JSON request bodies."""
    configure_logging()

    @app.before_request
    def set_run_id():
        request._scraper_run_id_token = run_id.set(uuid.uuid4().hex)
        if log_json_body and request.is_json:
            body = request.get_json(silent=True)
            logging.info(
                "Incoming request body method=%s path=%s body=%s",
                request.method,
                request.path,
                json.dumps(
                    _redact_sensitive_fields(body),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )

    @app.after_request
    def add_run_id_header(response):
        response.headers["X-Run-ID"] = run_id.get()
        return response

    @app.teardown_request
    def clear_run_id(_exception):
        token = getattr(request, "_scraper_run_id_token", None)
        if token is not None:
            request._scraper_run_id_token = None
            run_id.reset(token)
