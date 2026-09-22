"""Reusable Selenium element lookup, clicking, and typing."""

import logging
import random
import time

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as conditions

from .browser import get_wait


class ElementActionError(RuntimeError):
    """An element could not be used after the configured attempts."""


def search_element(driver, locator, wait_to_search=True, raise_exception=True, timeout=None):
    """Find a visible element, or return None when requested."""
    try:
        if not wait_to_search:
            return driver.find_element(*locator)
        wait = get_wait(driver, timeout)
        return wait.until(lambda current: (
            conditions.element_to_be_clickable(locator)(current)
            or conditions.visibility_of_element_located(locator)(current)
        ))
    except Exception as error:
        if not raise_exception:
            return None
        raise ElementActionError(f"Failed to locate element {locator}") from error


def make_element_interactable(driver, element):
    """Opt-in workaround for sites that incorrectly mark controls disabled."""
    return bool(driver.execute_script("""
        const element = arguments[0];
        element.removeAttribute('disabled');
        element.removeAttribute('readonly');
        element.style.pointerEvents = 'auto';
        return true;
    """, element))


def click_element(driver, element, max_attempts=3, force_interactable=False):
    """Click with bounded fallbacks and return the driver for existing callers."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    last_error = None
    for attempt in range(max_attempts):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            if force_interactable:
                make_element_interactable(driver, element)
            for action in (
                lambda: element.click(),
                lambda: ActionChains(driver).move_to_element(element).click().perform(),
                lambda: driver.execute_script("arguments[0].click();", element),
                lambda: element.send_keys(Keys.ENTER),
            ):
                try:
                    action()
                    return driver
                except Exception as error:
                    last_error = error
        except StaleElementReferenceException as error:
            last_error = error
            break  # A stale WebElement must be found again by its caller.
        except Exception as error:
            last_error = error
        if attempt + 1 < max_attempts:
            time.sleep(0.2)
    raise ElementActionError("Failed to click element") from last_error


def _value_matches(element, text):
    value = element.get_attribute("value")
    return value is None or str(text) in value


def write_element(driver, element, text, clear=True, slow=False, max_attempts=3,
                  force_interactable=False):
    """Type without logging the value, including passwords and other secrets."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    last_error = None
    for attempt in range(max_attempts):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            if force_interactable:
                make_element_interactable(driver, element)
            if clear:
                element.clear()
            if slow:
                for character in str(text):
                    element.send_keys(character)
                    time.sleep(random.uniform(0.05, 0.2))
            else:
                element.send_keys(text)
            if _value_matches(element, text):
                return driver
            last_error = ValueError("The field value does not contain the text")
        except StaleElementReferenceException as error:
            last_error = error
            break
        except Exception as error:
            last_error = error
        if attempt + 1 < max_attempts:
            try:
                ActionChains(driver).click(element).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).perform()
            except Exception:
                logging.debug("Could not focus the element for another write attempt")
            time.sleep(0.2)
    raise ElementActionError("Failed to write to element") from last_error


def hover_element(driver, element, pause_time=0.5):
    if element is None:
        raise ValueError("element cannot be None")
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    ActionChains(driver).move_to_element(element).pause(pause_time).perform()
    return driver
