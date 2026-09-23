import os
import subprocess
import sys
from unittest.mock import Mock

import psutil
import pytest

from selenium_scraper_runtime import browser
from selenium_scraper_runtime.watchdog import supervise


def test_local_chrome_detects_google_chrome_and_matching_driver(monkeypatch):
    paths = {
        "google-chrome-stable": "/usr/bin/google-chrome-stable",
        "chromedriver": "/usr/local/bin/chromedriver",
    }
    monkeypatch.delenv("CHROME_BIN", raising=False)
    monkeypatch.delenv("CHROMEDRIVER_BIN", raising=False)
    monkeypatch.setattr(browser.shutil, "which", lambda name: paths.get(name))
    monkeypatch.setattr(browser, "_prepare_driver", lambda driver: driver)
    service_factory = Mock(return_value=object())
    monkeypatch.setattr(browser, "_ChromeService", service_factory)
    driver = Mock()
    chrome_factory = Mock(return_value=driver)
    monkeypatch.setattr(browser.webdriver, "Chrome", chrome_factory)

    assert browser.create_driver("chrome", headless=False, stealth=False) is driver

    options = chrome_factory.call_args.kwargs["options"]
    assert options.binary_location == "/usr/bin/google-chrome-stable"
    assert "--start-maximized" in options.arguments
    service_factory.assert_called_once_with(
        "/usr/local/bin/chromedriver",
        popen_kw={"start_new_session": True} if os.name == "posix" else {},
    )


def test_invalid_explicit_chrome_binary_fails_clearly(monkeypatch):
    monkeypatch.setenv("CHROME_BIN", "/missing/google-chrome")

    with pytest.raises(FileNotFoundError, match="CHROME_BIN"):
        browser.create_driver("chrome", headless=False)


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
