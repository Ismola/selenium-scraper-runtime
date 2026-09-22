from unittest.mock import Mock, patch

import pytest
from selenium.webdriver.common.by import By
from selenium.common.exceptions import StaleElementReferenceException

from selenium_scraper_runtime.elements import (
    ElementActionError, click_element, hover_element, search_element, write_element,
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


def test_hover_refinds_stale_element():
    driver = Mock()
    old, fresh = Mock(), Mock()
    driver.find_element.return_value = fresh
    chain = Mock()
    chain.move_to_element.return_value = chain
    chain.pause.return_value = chain
    chain.perform.side_effect = [StaleElementReferenceException(), None]
    with patch("selenium_scraper_runtime.elements.ActionChains", return_value=chain):
        assert hover_element(driver, old, locator=(By.ID, "menu"), retries=2) is driver
    driver.find_element.assert_called_once_with(By.ID, "menu")
    assert chain.move_to_element.call_count == 2


def test_force_interactable_is_opt_in(monkeypatch):
    driver, element = Mock(), Mock()
    element.click.side_effect = [RuntimeError("blocked"), None]
    chain = Mock()
    chain.move_to_element.return_value = chain
    chain.click.return_value = chain
    chain.perform.side_effect = RuntimeError("blocked")
    monkeypatch.setenv("SELENIUM_FORCE_INTERACTABLE", "true")
    with patch("selenium_scraper_runtime.elements.ActionChains", return_value=chain):
        assert click_element(driver, element) is driver
    assert any("removeAttribute('disabled')" in call.args[0]
               for call in driver.execute_script.call_args_list)
