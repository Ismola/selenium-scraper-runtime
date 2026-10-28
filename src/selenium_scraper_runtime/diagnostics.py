"""Durable, isolated evidence for failed HTTP requests (opt-in)."""

import contextvars
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from flask import request

current_report = contextvars.ContextVar("scraper_request_report", default=None)


def take_screenshot(driver, directory=None):
    """Save the current browser view, preserving the scraper helper's signature."""
    directory = Path(directory or os.getenv("DOWNLOAD_DIR", "temp_downloads"))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"screenshot_{uuid.uuid4().hex}.png"
    if not driver.save_screenshot(str(path)):
        path.unlink(missing_ok=True)
        raise RuntimeError("WebDriver could not save the screenshot")
    if not path.is_file() or not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        path.unlink(missing_ok=True)
        raise RuntimeError("WebDriver returned an invalid PNG screenshot")
    logging.info("Screenshot saved: %s", path)
    return str(path)


def _request_secrets():
    from .logging import _redact_sensitive_fields

    secrets = set()

    def visit(value):
        if isinstance(value, dict):
            redacted = _redact_sensitive_fields(value)
            for key, item in value.items():
                if redacted[key] == "[REDACTED]":
                    collect(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    def collect(value):
        if isinstance(value, str) and value:
            secrets.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    visit(request.get_json(silent=True) if request.is_json else None)
    for name in ("Authorization", "Cookie"):
        value = request.headers.get(name)
        if value:
            secrets.add(value)
            if name == "Authorization":
                secrets.add(value.split(" ")[-1])
    return sorted(secrets, key=len, reverse=True)


class RequestReport:
    def __init__(self, run_id, screenshot):
        self.path = Path(os.environ["REQUEST_DIAGNOSTICS_DIR"]) / run_id
        self.path.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.stream = (self.path / "request.log").open("x", encoding="utf-8")
        self.secrets = _request_secrets()
        self.screenshot = screenshot or take_screenshot
        self.captured_drivers = set()
        self.finished = False
        self.metadata = {
            "version": 1,
            "run_id": run_id,
            "service": os.getenv("SCRAPER_SERVICE_NAME", "selenium-scraper"),
            "method": request.method,
            "path": request.path,
            "started_at": time.time(),
            "screenshots": [],
            "screenshot_errors": [],
        }

    def redact(self, text):
        for secret in self.secrets:
            # Boundaries keep short passwords from corrupting every word.
            if len(secret) < 4:
                text = re.sub(r"(?<!\w)" + re.escape(secret) + r"(?!\w)", "[REDACTED]", text)
            else:
                text = text.replace(secret, "[REDACTED]")
                text = text.replace(json.dumps(secret)[1:-1], "[REDACTED]")
        return text


class RequestLogHandler(logging.Handler):
    """One handler per worker; ContextVar selects only the active request file."""
    _scraper_diagnostics = True

    def emit(self, record):
        report = current_report.get()
        if report is None or report.finished:
            return
        try:
            report.stream.write(report.redact(self.format(record)) + "\n")
            report.stream.flush()
        except Exception:
            self.handleError(record)


def configure_diagnostics():
    from .logging import JsonFormatter

    root = logging.getLogger()
    if not any(getattr(handler, "_scraper_diagnostics", False) for handler in root.handlers):
        handler = RequestLogHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)


def begin_request(run_id, screenshot=None):
    if not os.getenv("REQUEST_DIAGNOSTICS_DIR"):
        return
    try:
        report = RequestReport(run_id, screenshot)
        request._scraper_report_token = current_report.set(report)
        logging.info("Request started method=%s path=%s", request.method, request.path)
    except Exception:
        logging.exception("Could not start request diagnostics")


def capture_failure(driver):
    """Call inside except, before finally closes the browser. Never masks the error."""
    report = current_report.get()
    if report is None or report.finished or driver is None or id(driver) in report.captured_drivers:
        return
    report.captured_drivers.add(id(driver))
    try:
        filename = Path(report.screenshot(driver, directory=str(report.path)))
        if filename.parent.resolve() != report.path.resolve():
            raise ValueError("Screenshot must be saved in the request's evidence directory")
        report.metadata["screenshots"].append(filename.name)
    except Exception as error:
        report.metadata["screenshot_errors"].append(report.redact(str(error)))
        logging.exception("Could not capture the browser failure; request logs are preserved")


def finish_request(response=None, exception=None):
    report = current_report.get()
    if report is None or report.finished:
        return
    try:
        status = response.status_code if response is not None else 500
        payload = response.get_json(silent=True) if response is not None and response.is_json else None
        error = (payload.get("message") or payload.get("error")) if isinstance(payload, dict) else None
        error = str(error or exception or (response.status if response is not None else "Unhandled exception"))
        logging.log(logging.ERROR if status >= 400 else logging.INFO,
                    "Request completed status=%s error=%s", status, error if status >= 400 else "")
        if exception is not None:
            logging.error("Unhandled request exception", exc_info=(type(exception), exception, exception.__traceback__))
        report.stream.flush()
        os.fsync(report.stream.fileno())
        report.stream.close()
        report.finished = True
        if status >= 400:
            report.metadata.update(status=status, error=report.redact(error), finished_at=time.time())
            # Only a fully written manifest is visible to the notification worker.
            temporary = report.path / "report.tmp"
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(report.metadata, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            temporary.rename(report.path / "pending.json")
        else:
            shutil.rmtree(report.path)
    except Exception:
        logging.exception("Could not finalize request diagnostics")


def clear_request(exception=None):
    report = current_report.get()
    if report is not None and not report.finished:
        finish_request(exception=exception)
    token = getattr(request, "_scraper_report_token", None)
    if token is not None:
        request._scraper_report_token = None
        current_report.reset(token)
