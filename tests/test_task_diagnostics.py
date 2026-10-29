import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest
from flask import Flask, jsonify

from selenium_scraper_runtime.diagnostics import capture_failure, current_report, TaskExecutionError
from selenium_scraper_runtime.logging import init_request_logging, run_id
from selenium_scraper_runtime.metrics import track_task, TaskSkipped, TASK_RUNS
from selenium_scraper_runtime.notifications import build_message, process_pending


@pytest.fixture
def spool(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUEST_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setenv("SCRAPER_SERVICE_NAME", "proservice-scheduler-test")
    monkeypatch.setenv("PASSWORD", "cron-secret-never-email")
    monkeypatch.setenv("SCRAPER_ALERT_FROM", "ops@example.test")
    monkeypatch.setenv("SCRAPER_ALERT_TO", "ops@example.test")
    return tmp_path


def test_cron_failure_without_flask_has_full_logs_traceback_and_image(spool):
    driver = Mock()

    def screenshot(filename):
        Path(filename).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        return True

    driver.save_screenshot.side_effect = screenshot
    with pytest.raises(RuntimeError, match="controlled failure"):
        with track_task("scheduled_analysis", failure_diagnostics=True):
            logging.info("cycle beginning")
            logging.warning("secret: cron-secret-never-email")
            try:
                raise RuntimeError("controlled failure")
            except Exception:
                capture_failure(driver)
                raise
    manifests = list(spool.glob("*/pending.json"))
    assert len(manifests) == 1
    metadata = json.loads(manifests[0].read_text())
    assert metadata["kind"] == "task" and metadata["outcome"] == "error"
    assert metadata["task"] == "scheduled_analysis"
    logs = (manifests[0].parent / "request.log").read_text()
    assert all(text in logs for text in ["cycle beginning", "Traceback", "Task completed", "error"])
    assert "cron-secret-never-email" not in logs
    message = build_message(manifests[0].parent, "ops@example.test", ["ops@example.test"])
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Tarea programada: scheduled_analysis" in body and "Resultado: FALLIDA" in body
    assert "HTTP:" not in body and logs in body
    assert len([part for part in message.walk() if part.get_content_type() == "image/png"]) == 1
    assert current_report.get() is None and run_id.get() is None


def test_handled_error_makes_cycle_fail_and_metrics_report_failure(spool):
    before = TASK_RUNS.labels(service=__import__('selenium_scraper_runtime.metrics', fromlist=['SERVICE_NAME']).SERVICE_NAME,
                              task="partial_cycle", outcome="error")._value.get()
    with pytest.raises(TaskExecutionError, match="Failed to extract documents"):
        with track_task("partial_cycle", failure_diagnostics=True):
            logging.error("Failed to extract documents")
            logging.info("The remainder of the cycle completed")
    manifest = next(spool.glob("*/pending.json"))
    assert "Failed to extract documents" in json.loads(manifest.read_text())["error"]
    from selenium_scraper_runtime.metrics import SERVICE_NAME
    assert TASK_RUNS.labels(service=SERVICE_NAME, task="partial_cycle", outcome="error")._value.get() == before + 1


@pytest.mark.parametrize("skipped", [False, True])
def test_success_or_skip_discards_evidence_without_email(spool, skipped):
    with track_task("normal_cycle", failure_diagnostics=True):
        logging.info("normal work")
        if skipped:
            raise TaskSkipped()
    assert not list(spool.glob("*/*"))
    assert current_report.get() is None and run_id.get() is None


def test_cron_threads_have_independent_correlations_and_logs(spool):
    barrier = Barrier(2)

    def run(marker):
        with pytest.raises(RuntimeError):
            with track_task(marker, failure_diagnostics=True):
                logging.info("marker-%s", marker)
                barrier.wait(timeout=5)
                raise RuntimeError(marker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ["ascending", "descending"]))
    manifests = list(spool.glob("*/pending.json"))
    assert len(manifests) == 2
    for path in manifests:
        task = json.loads(path.read_text())["task"]
        logs = (path.parent / "request.log").read_text()
        assert "marker-" + task in logs
        other = "descending" if task == "ascending" else "ascending"
        assert "marker-" + other not in logs


def test_unmatched_scanner_and_wrong_method_do_not_queue_email(spool):
    app = Flask(__name__)
    init_request_logging(app, failure_diagnostics=True)
    app.add_url_rule("/business", view_func=lambda: (jsonify(message="Business object missing"), 404))
    client = app.test_client()
    assert client.get("/app/.env").status_code == 404
    assert client.post("/business").status_code == 405
    assert not list(spool.glob("*/pending.json"))
    response = client.get("/business")
    assert response.status_code == 404
    assert (spool / response.headers['X-Run-ID'] / 'pending.json').is_file()


def test_notifier_discards_old_scanner_queue_but_delivers_real_business_404(spool):
    app = Flask(__name__)
    init_request_logging(app, failure_diagnostics=True)
    app.add_url_rule("/business", view_func=lambda: (jsonify(message="Object missing"), 404))
    response = app.test_client().get("/business")
    source = spool / response.headers["X-Run-ID"]
    import shutil
    old = spool / "legacy-scanner"
    shutil.copytree(source, old)
    report = json.loads((old / "pending.json").read_text())
    report.pop("matched_route")
    report["path"] = "/app/.env"
    (old / "pending.json").write_text(json.dumps(report))
    deliver = Mock()
    assert process_pending(spool, deliver=deliver) == 1
    deliver.assert_called_once()
    assert (old / "ignored.json").exists()
    assert (source / "sent.json").exists()
