from unittest.mock import Mock

import pytest
from selenium.webdriver.common.by import By

from selenium_scraper_runtime.elements import (
    ElementActionError, click_element, search_element, write_element,
)


def test_search_element_optional_failure(monkeypatch):
    driver = Mock()
    driver.find_element.side_effect = RuntimeError("missing")
    assert search_element(driver, (By.ID, "missing"), wait_to_search=False,
                          raise_exception=False) is None
    with pytest.raises(ElementActionError):
        search_element(driver, (By.ID, "missing"), wait_to_search=False)


def test_click_element_uses_javascript_fallback():
    driver = Mock()
    element = Mock()
    element.click.side_effect = RuntimeError("intercepted")
    element.send_keys.side_effect = RuntimeError("intercepted")
    # The scroll and JS click share execute_script; the second call succeeds.
    assert click_element(driver, element) is driver
    assert driver.execute_script.call_count == 2


def test_write_element_does_not_log_secret(caplog):
    driver = Mock()
    element = Mock()
    element.get_attribute.return_value = "secret-password"
    assert write_element(driver, element, "secret-password") is driver
    element.send_keys.assert_called_once_with("secret-password")
    assert "secret-password" not in caplog.text


def test_write_element_uses_javascript_after_typing_fails():
    driver = Mock()
    element = Mock()
    element.send_keys.side_effect = RuntimeError("input intercepted")
    element.get_attribute.return_value = "typed"
    assert write_element(driver, element, "typed", max_attempts=1) is driver
    assert driver.execute_script.call_count == 2
