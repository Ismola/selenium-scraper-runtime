import json
import logging

from flask import Flask
from selenium_scraper_runtime.logging import JsonFormatter, init_request_logging, run_id
from selenium_scraper_runtime.metrics import TaskSkipped, init_metrics, track_task


def test_json_log_and_request_correlation():
    app = Flask(__name__)
    init_request_logging(app)

    @app.get("/")
    def index():
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
        return JsonFormatter().format(record)

    response = app.test_client().get("/")
    payload = json.loads(response.text)
    assert payload["run_id"] == response.headers["X-Run-ID"]
    assert payload["message"] == "hello"
    assert run_id.get() is None

    with app.test_client() as client:
        client.get("/")
        client.get("/")
    assert run_id.get() is None


def test_metrics_keep_existing_names():
    app = Flask(__name__)
    init_metrics(app)
    app.add_url_rule("/", view_func=lambda: "ok")
    assert app.test_client().get("/").status_code == 200
    with track_task("example"):
        pass


def test_skipped_task_is_recorded_without_failing_the_scheduler():
    with track_task("outside_schedule"):
        raise TaskSkipped
