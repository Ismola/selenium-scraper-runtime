import os
import subprocess
import sys
from unittest.mock import Mock

import psutil
import pytest

from selenium_scraper_runtime import browser
from selenium_scraper_runtime.watchdog import supervise


def test_navigation_failure_closes_its_driver(monkeypatch):
    driver = Mock()
    driver.get.side_effect = RuntimeError("navigation failed")
    close = Mock()
    monkeypatch.setattr(browser, "create_driver", lambda **kwargs: driver)
    monkeypatch.setattr(browser, "close_driver", close)

    with pytest.raises(RuntimeError, match="navigation failed"):
        browser.get_page(browser="firefox", url="https://example.com")
    close.assert_called_once_with(driver)


def test_close_driver_ends_its_own_process_only(monkeypatch):
    owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        driver = Mock()
        driver.quit.side_effect = RuntimeError("browser unresponsive")
        monkeypatch.setattr(browser, "_owned_processes", lambda _: [psutil.Process(owned.pid)])
        browser.close_driver(driver, timeout=0.1)
        owned.wait(timeout=5)
        assert unrelated.poll() is None
    finally:
        for process in (owned, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait()


@pytest.mark.skipif(os.name != "posix", reason="The browser watchdog runs on POSIX")
def test_watchdog_cleans_orphaned_browser_without_touching_other_process():
    owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        process = psutil.Process(owned.pid)
        supervise(os.getpid(), 0, owned.pid, process.create_time(), 5,
                  [(owned.pid, process.create_time())])
        owned.wait(timeout=5)
        assert unrelated.poll() is None
    finally:
        for process in (owned, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait()
