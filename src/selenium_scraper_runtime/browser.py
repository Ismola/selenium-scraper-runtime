"""Safe defaults for Chromium and Firefox inside the shared image."""

import os
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService


def create_driver(browser="chrome", download_dir="/app/temp_downloads", remote_url=None):
    remote_url = remote_url or os.getenv("SELENIUM_URL")
    if browser == "chrome":
        options = webdriver.ChromeOptions()
        options.binary_location = os.getenv("CHROME_BIN", "/usr/bin/chromium")
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_experimental_option("prefs", {"download.default_directory": download_dir})
        if remote_url:
            return webdriver.Remote(command_executor=remote_url, options=options)
        return webdriver.Chrome(service=ChromeService("/usr/bin/chromedriver"), options=options)
    if browser == "firefox":
        options = webdriver.FirefoxOptions()
        options.binary_location = os.getenv("FIREFOX_BIN", "/usr/bin/firefox-esr")
        options.add_argument("-headless")
        if remote_url:
            return webdriver.Remote(command_executor=remote_url, options=options)
        return webdriver.Firefox(options=options)
    raise ValueError(f"Unsupported browser: {browser}")
