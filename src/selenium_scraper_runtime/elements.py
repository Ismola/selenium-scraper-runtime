"""Reusable Selenium element lookup, clicking, and typing."""

import logging
import os
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


def _force_interactable(value):
    if value is not None:
        return value
    return os.getenv("SELENIUM_FORCE_INTERACTABLE", "false").lower() in {"true", "1", "yes"}


def click_element(driver, element, max_attempts=3, force_interactable=None):
    """Click with bounded fallbacks and return the driver for existing callers."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    force_interactable = _force_interactable(force_interactable)
    last_error = None
    for attempt in range(max_attempts):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            for action in (
                lambda: element.click(),
                lambda: ActionChains(driver).move_to_element(element).click().perform(),
            ):
                try:
                    action()
                    return driver
                except Exception as error:
                    last_error = error
            if force_interactable:
                make_element_interactable(driver, element)
                try:
                    element.click()
                    return driver
                except Exception as error:
                    last_error = error
            for action in (
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
    if value is None:
        value = element.text
    return str(text) in value


def write_element(driver, element, text, clear=True, slow=False, max_attempts=3,
                  force_interactable=None, javascript_fallback=True):
    """Type without logging the value, including passwords and other secrets."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    force_interactable = _force_interactable(force_interactable)
    last_error = None
    for attempt in range(max_attempts):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            if force_interactable and attempt > 0:
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
    if javascript_fallback and not isinstance(last_error, StaleElementReferenceException):
        try:
            driver.execute_script("""
                const element = arguments[0];
                const text = arguments[1];
                const clear = arguments[2];
                if (element.isContentEditable) {
                    element.textContent = clear ? text : element.textContent + text;
                } else {
                    const value = clear ? text : (element.value || '') + text;
                    const prototype = Object.getPrototypeOf(element);
                    const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
                    if (setter) setter.call(element, value);
                    else element.value = value;
                }
                element.dispatchEvent(new Event('input', {bubbles: true}));
                element.dispatchEvent(new Event('change', {bubbles: true}));
            """, element, text, clear)
            if _value_matches(element, text):
                return driver
        except Exception as error:
            last_error = error
    raise ElementActionError("Failed to write to element") from last_error


def hover_element(driver, element=None, pause_time=0.5, *, locator=None, retries=3):
    """Hover, refinding stale elements when a locator is available."""
    if element is None and locator is None:
        raise ValueError("element or locator is required")
    if retries < 1:
        raise ValueError("retries must be at least 1")
    last_error = None
    for attempt in range(retries):
        try:
            current = element if attempt == 0 and element is not None else driver.find_element(*locator)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", current)
            ActionChains(driver).move_to_element(current).pause(pause_time).perform()
            return driver
        except StaleElementReferenceException as error:
            last_error = error
            if locator is None:
                break
        except Exception as error:
            last_error = error
            break
        time.sleep(0.2)
    try:
        current = driver.find_element(*locator) if locator is not None else element
        driver.execute_script(
            "arguments[0].dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));",
            current,
        )
        return driver
    except Exception as error:
        raise ElementActionError("Failed to hover over element") from (last_error or error)
