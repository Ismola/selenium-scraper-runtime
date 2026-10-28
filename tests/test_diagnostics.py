import concurrent.futures
import json
import logging
import time
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify, request

from selenium_scraper_runtime.diagnostics import capture_failure, current_report
from selenium_scraper_runtime.logging import init_request_logging, run_id
from selenium_scraper_runtime.notifications import build_message, process_pending, send_message


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUEST_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setenv("SCRAPER_SERVICE_NAME", "botpl")
    monkeypatch.setenv("SCRAPER_ALERT_FROM", "grafana@example.test")
    monkeypatch.setenv("SCRAPER_ALERT_TO", "ops@example.test")
    app = Flask(__name__)
    init_request_logging(app, log_json_body=True, failure_diagnostics=True)
    return app, tmp_path


def test_full_logs_traceback_and_redacted_credentials(configured):
    app, root = configured

    @app.post("/fail")
    def fail():
        for index in range(1200):
            logging.info("step %s", index)
        try:
            raise RuntimeError("failure with password: very-secret")
        except RuntimeError:
            logging.exception("controller failed")
        return jsonify(message="failed"), 400

    response = app.test_client().post("/fail", json={"password": "very-secret", "username": "tester"},
                                    headers={"Authorization": "Bearer bearer-secret"})
    path = root / response.headers["X-Run-ID"]
    logs = (path / "request.log").read_text()
    assert "step 0" in logs and "step 1199" in logs
    assert "Traceback" in logs and "Request completed" in logs
    assert "very-secret" not in logs and "bearer-secret" not in logs
    assert "tester" in logs
    message = build_message(path, "grafana@example.test", ["ops@example.test"])
    assert "step 1199" in message.get_body(preferencelist=("plain",)).get_content()
    assert "no disponible" in message.get_body(preferencelist=("html",)).get_content()
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_payload(decode=True).decode() == logs
    assert current_report.get() is None and run_id.get() is None


def test_browser_screenshot_is_captured_before_close_and_inline(configured):
    app, root = configured
    driver = Mock()
    events = []

    def save(filename):
        events.append("capture")
        Path(filename).write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
        return True

    driver.save_screenshot.side_effect = save

    @app.post("/fail")
    def fail():
        try:
            raise RuntimeError("browser failure")
        except RuntimeError:
            capture_failure(driver)
            capture_failure(driver)  # Nested error handlers must not duplicate it.
            logging.exception("failure")
        finally:
            events.append("close")
        return jsonify(message="failed"), 500

    response = app.test_client().post("/fail", json={})
    assert events == ["capture", "close"]
    path = root / response.headers["X-Run-ID"]
    message = build_message(path, "ops@example.test", ["ops@example.test"])
    images = [part for part in message.walk() if part.get_content_type() == "image/png"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == f"<{response.headers['X-Run-ID']}-0>"
    assert images[0].get_content_disposition() == "inline"


def test_broken_browser_keeps_original_error_and_logs(configured):
    app, root = configured
    driver = Mock()
    driver.save_screenshot.side_effect = RuntimeError("browser is gone")

    @app.get("/fail")
    def fail():
        capture_failure(driver)
        return jsonify(message="original failure"), 400

    response = app.test_client().get("/fail")
    path = root / response.headers["X-Run-ID"]
    report = json.loads((path / "pending.json").read_text())
    assert report["error"] == "original failure"
    assert report["screenshot_errors"] == ["browser is gone"]
    assert "Could not capture" in (path / "request.log").read_text()


def test_successful_requests_leave_no_report(configured):
    app, root = configured
    app.add_url_rule("/ok", view_func=lambda: "ok")
    assert app.test_client().get("/ok").status_code == 200
    assert not list(root.iterdir())


def test_concurrent_requests_do_not_share_logs_or_images(configured):
    app, root = configured
    barrier = Barrier(4)

    @app.post("/fail")
    def fail():
        marker = request.json["marker"]
        logging.info("begin marker-%s", marker)
        barrier.wait(timeout=5)
        logging.error("end marker-%s", marker)
        return jsonify(message="failure"), 400

    def invoke(marker):
        response = app.test_client().post("/fail", json={"marker": marker})
        return marker, response.headers["X-Run-ID"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(invoke, range(4)))
    assert len({run for _, run in results}) == 4
    for marker, run in results:
        logs = (root / run / "request.log").read_text()
        for other in range(4):
            assert (f"marker-{other}" in logs) == (other == marker)


def test_unhandled_exception_produces_report(configured):
    app, root = configured

    @app.get("/fail")
    def fail():
        raise RuntimeError("unhandled")

    response = app.test_client().get("/fail")
    assert response.status_code == 500
    assert "unhandled" in (root / response.headers["X-Run-ID"] / "request.log").read_text()


def test_retry_preserves_pending_evidence_then_delivers_once(configured):
    app, root = configured
    app.add_url_rule("/fail", view_func=lambda: (jsonify(message="failed"), 400))
    response = app.test_client().get("/fail")
    path = root / response.headers["X-Run-ID"]
    now = time.time()
    deliver = Mock(side_effect=OSError("SMTP unavailable"))
    assert process_pending(root, deliver=deliver, now=now) == 0
    assert (path / "pending.json").exists() and (path / "request.log").exists()
    deliver.side_effect = None
    assert process_pending(root, deliver=deliver, now=now + 1) == 0
    assert process_pending(root, deliver=deliver, now=now + 60) == 1
    assert (path / "sent.json").exists()
    assert process_pending(root, deliver=deliver, now=now + 61) == 0
    assert deliver.call_count == 2
    assert deliver.call_args.args[0]["X-Scraper-Run-ID"] == response.headers["X-Run-ID"]


def test_email_is_delivered_over_real_smtp(configured, monkeypatch):
    from aiosmtpd.controller import Controller
    import socket

    received = []

    class Inbox:
        async def handle_DATA(self, server, session, envelope):
            received.append(envelope.content)
            return "250 Message accepted for delivery"

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = Controller(Inbox(), hostname="127.0.0.1", port=port)
    server.start()
    try:
        monkeypatch.setenv("SCRAPER_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("SCRAPER_SMTP_PORT", str(port))
        monkeypatch.setenv("SCRAPER_SMTP_SECURITY", "plain")
        monkeypatch.delenv("SCRAPER_SMTP_USER", raising=False)
        app, root = configured
        app.add_url_rule("/fail", view_func=lambda: (jsonify(message="failed"), 500))
        response = app.test_client().get("/fail")
        assert process_pending(root, deliver=send_message) == 1
        assert len(received) == 1
        assert response.headers["X-Run-ID"].encode() in received[0]
    finally:
        server.stop()


def test_retention_never_removes_unsent_reports(configured):
    app, root = configured
    app.add_url_rule("/fail", view_func=lambda: (jsonify(message="failed"), 500))
    response = app.test_client().get("/fail")
    path = root / response.headers["X-Run-ID"]
    future = time.time() + 30 * 86400
    assert process_pending(root, deliver=Mock(side_effect=OSError("offline")), now=future) == 0
    assert (path / "pending.json").exists()
    assert process_pending(root, deliver=Mock(), now=future + 60) == 1
    assert not path.exists()
