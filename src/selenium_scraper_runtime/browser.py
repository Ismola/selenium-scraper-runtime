"""Browser setup and lifecycle shared by Selenium scrapers."""

import logging
import os
import shutil
import subprocess
import sys
import threading
import weakref
import atexit
from contextlib import contextmanager

import psutil
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as conditions
from selenium.webdriver.remote.client_config import ClientConfig


_active_drivers = weakref.WeakSet()
_registry_lock = threading.Lock()


class _GuardedService:
    def _start_process(self, path):
        super()._start_process(path)
        if os.name == "posix":
            try:
                self._scraper_watchdog = _launch_watchdog(self.process)
            except Exception:
                self.stop()
                raise


class _ChromeService(_GuardedService, ChromeService):
    pass


class _FirefoxService(_GuardedService, FirefoxService):
    pass


def _headless(value):
    if value is not None:
        return value
    mode = os.getenv("HEADLESS_MODE", "auto").lower()
    if mode in {"true", "1", "yes"}:
        return True
    if mode in {"false", "0", "no"}:
        return False
    return os.getenv("DOCKERIZED", "").lower() == "true" or not bool(os.getenv("DISPLAY"))


def _find_executable(env_name, candidates):
    """Resolve an explicit executable or the first installed candidate."""
    configured = os.getenv(env_name)
    if configured:
        resolved = shutil.which(configured) if os.path.sep not in configured else configured
        if resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return resolved
        raise FileNotFoundError(f"{env_name} does not point to an executable: {configured}")
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _visible_chrome_arguments(is_headless, extra_arguments):
    """Choose a deterministic display backend for visible container sessions."""
    if is_headless:
        return []
    arguments = []
    if os.getenv("DISPLAY") and not any(
        argument.startswith("--ozone-platform=") for argument in extra_arguments
    ):
        arguments.append(f"--ozone-platform={os.getenv('CHROME_OZONE_PLATFORM', 'x11')}")
    disable_gpu = os.getenv("CHROME_DISABLE_GPU")
    if disable_gpu is None:
        # VS Code forwards a Wayland socket which Chromium cannot use reliably
        # from these containers. Software rendering avoids Xwayland GPU glitches.
        disable_gpu = os.getenv("WAYLAND_DISPLAY", "").startswith("vscode-wayland-")
    else:
        disable_gpu = disable_gpu.lower() in {"true", "1", "yes"}
    if disable_gpu and "--disable-gpu" not in extra_arguments:
        arguments.append("--disable-gpu")
    return arguments


def create_driver(
    browser="chrome", download_dir=None, remote_url=None, headless=None,
    language=None, stealth=None, user_agent=None, extra_arguments=(),
    binary=None, driver_path=None,
):
    """Start one browser with container defaults and overridable site settings."""
    remote_url = remote_url or os.getenv("SELENIUM_URL")
    download_dir = os.path.abspath(download_dir or os.getenv("DOWNLOAD_DIR", "temp_downloads"))
    language = language or os.getenv("BROWSER_LANGUAGE", "en")
    if stealth is None:
        stealth = os.getenv("SELENIUM_STEALTH", "false").lower() in {"true", "1", "yes"}

    if browser == "chrome":
        options = webdriver.ChromeOptions()
        if binary is None and not remote_url:
            binary = _find_executable(
                "CHROME_BIN",
                ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome"),
            )
        if binary:
            options.binary_location = binary
            logging.info("Using Chrome-compatible browser: %s", binary)
        is_headless = _headless(headless)
        if is_headless:
            options.add_argument("--headless=new")
        visible_arguments = _visible_chrome_arguments(is_headless, extra_arguments)
        for argument in (
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-notifications",
            "--disable-extensions", "--no-first-run", "--no-default-browser-check",
            "--disable-popup-blocking", "--password-store=basic", "--window-size=1920,1080",
            *visible_arguments,
            *extra_arguments,
        ):
            options.add_argument(argument)
        if not is_headless:
            options.add_argument("--start-maximized")
        if stealth:
            options.add_argument("--disable-blink-features=AutomationControlled")
        if user_agent:
            options.add_argument(f"--user-agent={user_agent}")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("prefs", {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True,
            "intl.accept_languages": language,
            "profile.default_content_setting_values.notifications": 2,
            "profile.password_manager_enabled": False,
            "credentials_enable_service": False,
        })
        if remote_url:
            driver = webdriver.Remote(command_executor=remote_url, options=options,
                                     client_config=ClientConfig(remote_server_addr=remote_url,
                                                               timeout=_command_timeout()))
        else:
            driver_path = driver_path or _find_executable("CHROMEDRIVER_BIN", ("chromedriver",))
            if driver_path:
                logging.info("Using ChromeDriver: %s", driver_path)
            service = _ChromeService(driver_path, popen_kw={"start_new_session": True} if os.name == "posix" else {})
            driver = webdriver.Chrome(service=service, options=options)
        _prepare_driver(driver)
        if stealth:
            try:
                from selenium_stealth import stealth as apply_stealth
                apply_stealth(driver, languages=[language], vendor="Google Inc.",
                              platform="Win32", webgl_vendor="Intel Inc.",
                              renderer="Intel Iris OpenGL Engine", fix_hairline=True)
            except Exception:
                close_driver(driver)
                raise
        return driver

    if browser == "firefox":
        options = webdriver.FirefoxOptions()
        if binary is None and not remote_url:
            binary = _find_executable("FIREFOX_BIN", ("firefox-esr", "firefox"))
        if binary:
            options.binary_location = binary
        if _headless(headless):
            options.add_argument("-headless")
        for argument in extra_arguments:
            options.add_argument(argument)
        options.accept_insecure_certs = True
        options.set_preference("intl.accept_languages", language)
        if remote_url:
            driver = webdriver.Remote(command_executor=remote_url, options=options,
                                     client_config=ClientConfig(remote_server_addr=remote_url,
                                                               timeout=_command_timeout()))
        else:
            driver_path = driver_path or _find_executable("GECKODRIVER_BIN", ("geckodriver",))
            service = _FirefoxService(driver_path, popen_kw={"start_new_session": True} if os.name == "posix" else {})
            driver = webdriver.Firefox(service=service, options=options)
        return _prepare_driver(driver)

    raise ValueError(f"Unsupported browser: {browser}")


def _configure_timeouts(driver):
    """Bound requests so a dead browser cannot hold a worker forever."""
    command_timeout = _command_timeout()
    executor = getattr(driver, "command_executor", None)
    config = getattr(executor, "client_config", None)
    if config is not None:
        config.timeout = command_timeout
    driver.set_page_load_timeout(int(os.getenv("PAGE_LOAD_TIMEOUT", "60")))
    driver.set_script_timeout(int(os.getenv("SCRIPT_TIMEOUT", "30")))


def _command_timeout():
    return int(os.getenv("WEBDRIVER_COMMAND_TIMEOUT", "60"))


def _launch_watchdog(process):
    lifetime = int(os.getenv("WEBDRIVER_MAX_LIFETIME", "720"))
    if lifetime < 1:
        raise ValueError("WEBDRIVER_MAX_LIFETIME must be positive")
    owner = psutil.Process(os.getpid())
    service = psutil.Process(process.pid)
    initial = service.children(recursive=True) + [service]
    return subprocess.Popen(
        [sys.executable, "-m", "selenium_scraper_runtime.watchdog",
         str(owner.pid), str(owner.create_time()),
         str(service.pid), str(service.create_time()), str(lifetime),
         *(f"{item.pid}:{item.create_time()}" for item in initial)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )


def _prepare_driver(driver):
    try:
        _configure_timeouts(driver)
        service = getattr(driver, "service", None)
        if service is not None:
            driver._scraper_watchdog = getattr(service, "_scraper_watchdog", None)
        with _registry_lock:
            _active_drivers.add(driver)
        return driver
    except Exception:
        close_driver(driver)
        raise


def get_page(browser="chrome", url=None, **driver_options):
    """Open a page, closing the new driver if navigation fails."""
    url = url or os.getenv("BASE_URL", "https://www.google.com/")
    driver = create_driver(browser=browser, **driver_options)
    try:
        driver.get(url)
        return driver
    except Exception:
        close_driver(driver)
        raise


def get_wait(driver, timeout=None):
    return WebDriverWait(driver, timeout if timeout is not None else int(os.getenv("PAGE_MAX_TIMEOUT", "7")))


def reload_driver(driver, timeout=None):
    """Reload a page, accepting an already open alert when present."""
    driver.execute_script("window.onbeforeunload = null;")
    try:
        WebDriverWait(driver, 1).until(conditions.alert_is_present()).accept()
    except Exception:
        pass
    driver.execute_script("window.location.reload();")
    get_wait(driver, timeout).until(
        lambda current: current.execute_script("return document.readyState") == "complete"
    )
    return driver


def _owned_processes(driver):
    """Find this driver's process tree, including detached Firefox children."""
    processes = {}
    try:
        process = driver.service.process
        if process is not None:
            root = psutil.Process(process.pid)
            for item in root.children(recursive=True) + [root]:
                processes[(item.pid, item.create_time())] = item
            if os.name == "posix":
                created = root.create_time()
                for item in psutil.process_iter(["pid"]):
                    try:
                        if (os.getpgid(item.pid) == root.pid
                                and item.create_time() >= created - 1):
                            processes[(item.pid, item.create_time())] = item
                    except (OSError, psutil.Error):
                        pass
    except (AttributeError, psutil.Error):
        pass
    try:
        profile = driver.capabilities.get("moz:profile")
    except (AttributeError, TypeError):
        profile = None
    if profile:
        for item in psutil.process_iter(["pid", "cmdline"]):
            try:
                if profile in (item.info["cmdline"] or []):
                    processes[(item.pid, item.create_time())] = item
            except psutil.Error:
                pass
    return list(processes.values())


def close_driver(driver, timeout=10):
    """Quit one session and stop its surviving local processes."""
    if driver is None:
        return
    with _registry_lock:
        _active_drivers.discard(driver)
    processes = _owned_processes(driver)
    errors = []

    def quit_session():
        try:
            driver.quit()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=quit_session, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        logging.warning("WebDriver quit timed out")
    elif errors:
        logging.warning("WebDriver quit failed: %s", errors[0])
    # Firefox can reparent a content process while geckodriver exits.
    processes = list({(item.pid, item.create_time()): item
                      for item in processes + _owned_processes(driver)}.values())
    if processes:
        try:
            _, alive = psutil.wait_procs(processes, timeout=2)
            for process in alive:
                try:
                    process.terminate()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(alive, timeout=2)
            for process in alive:
                try:
                    process.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs(alive, timeout=2)
        except psutil.Error as error:
            logging.warning("Could not inspect the browser process tree: %s", error)
    thread.join(timeout=2)
    watchdog = getattr(driver, "_scraper_watchdog", None)
    if watchdog is not None:
        try:
            watchdog.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # The guard still owns a surviving process; let it finish cleanup.
            logging.warning("Browser watchdog is still cleaning the session")
            threading.Thread(target=watchdog.wait, daemon=True).start()


def close_all_drivers():
    """Release sessions still owned by this worker during graceful shutdown."""
    with _registry_lock:
        drivers = list(_active_drivers)
    for driver in drivers:
        try:
            close_driver(driver)
        except Exception:
            logging.exception("Could not close a browser during worker shutdown")


@contextmanager
def browser_session(browser="chrome", url=None, **driver_options):
    driver = get_page(browser=browser, url=url, **driver_options)
    try:
        yield driver
    finally:
        close_driver(driver)


atexit.register(close_all_drivers)
