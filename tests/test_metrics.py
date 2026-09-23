import os
import subprocess
import sys


def test_metrics_import_recreates_missing_multiprocess_directory(tmp_path):
    metrics_directory = tmp_path / "cleared" / "prometheus"
    environment = os.environ.copy()
    environment["PROMETHEUS_MULTIPROC_DIR"] = str(metrics_directory)

    result = subprocess.run(
        [sys.executable, "-c", "import selenium_scraper_runtime.metrics"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert metrics_directory.is_dir()
