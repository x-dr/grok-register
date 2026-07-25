"""Browser registration helpers (JS helpers + cancel/sleep utilities).

Adapted from mytest/utils.py for integration into the main package.
"""

from __future__ import annotations

import json
import random
import string
import time
from typing import Any, Callable, Optional


class RegistrationCancelled(Exception):
    """User cancelled registration."""


class BrowserDeadError(Exception):
    """Browser/page disconnected."""


class AccountRetryNeeded(Exception):
    """Transient account-slot failure; caller may retry."""


CancelFn = Optional[Callable[[], bool]]
LogFn = Optional[Callable[[str], None]]


def raise_if_cancelled(cancel_callback: CancelFn = None) -> None:
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("cancelled")


def sleep_with_cancel(seconds: float, cancel_callback: CancelFn = None) -> None:
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        raise_if_cancelled(cancel_callback)
        time.sleep(min(0.2, end - time.time()))


def human_pause(lo: float = 0.3, hi: float = 1.0, cancel_callback: CancelFn = None) -> None:
    sleep_with_cancel(random.uniform(lo, hi), cancel_callback)


_GIVEN_NAME_POOL = [
    "Alex", "Blake", "Cameron", "Dylan", "Elliot", "Finley", "Gray", "Harper",
    "Ivan", "Jordan", "Kai", "Logan", "Morgan", "Noah", "Owen", "Parker",
    "Quinn", "Riley", "Sam", "Taylor", "Uri", "Victor", "Wesley", "Xavier",
    "Yuri", "Zane", "Felix", "Aaron", "Damian", "Chris", "Drew", "Eden",
]

_FAMILY_NAME_POOL = [
    "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun", "Guo", "He",
    "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi", "Fang", "Peng", "Cao", "Deng",
    "Fan", "Fu", "Gao", "Han", "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie",
    "Pan", "Qiao", "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
]


def build_profile() -> tuple[str, str, str]:
    """Random given/family name + strong password."""
    given = random.choice(_GIVEN_NAME_POOL)
    family = random.choice(_FAMILY_NAME_POOL)
    # x.ai password rules: length + mixed classes
    core = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    password = f"{core[0].upper()}{core[1:]}!a#A"
    return given, family, password


JS_HELPERS = r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    if (!node) return '';
    return [node.innerText, node.textContent, node.getAttribute('aria-label'),
            node.getAttribute('title'), node.getAttribute('placeholder'),
            node.getAttribute('data-testid'), node.getAttribute('name'),
            node.getAttribute('id'), node.getAttribute('autocomplete'),
            node.getAttribute('href'), node.getAttribute('value')]
           .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value); else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}
function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}
"""


def page_run_js(page, script: str, *args: Any) -> Any:
    """Evaluate JS with helpers prepended. Supports arguments[N] style scripts."""
    if page is None:
        raise BrowserDeadError("page not ready")
    args_json = json.dumps(list(args), ensure_ascii=False)
    full = f"(function(){{ {JS_HELPERS}\n{script}\n}}).apply(null, {args_json})"
    try:
        return page.evaluate(full)
    except Exception as exc:
        msg = str(exc).lower()
        if any(
            k in msg
            for k in (
                "target closed",
                "browser has been closed",
                "page has been closed",
                "connection refused",
            )
        ):
            raise BrowserDeadError(str(exc)) from exc
        raise


def click_button_by_text(
    page,
    keywords: list[str],
    selectors: str = 'button, a, [role="button"], input[type="submit"]',
) -> Any:
    script = f"""
    const keywords = {json.dumps(keywords)};
    const candidates = Array.from(document.querySelectorAll({json.dumps(selectors)}))
        .filter(n => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true')
        .map(n => ({{ node: n, text: textOf(n).replace(/\\s+/g,'').toLowerCase() }}))
        .filter(x => keywords.some(k => x.text.includes(k.toLowerCase())))
        .sort((a, b) => b.text.length - a.text.length);
    if (!candidates.length) return false;
    candidates[0].node.click();
    return textOf(candidates[0].node) || true;
    """
    return page_run_js(page, script)


def parse_browser_proxy(proxy_str: str | None) -> dict[str, str] | None:
    """Convert proxy URL to Playwright/cloakbrowser proxy settings."""
    from urllib.parse import unquote, urlparse

    if not proxy_str:
        return None
    parsed = urlparse(str(proxy_str).strip())
    if not parsed.scheme or not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    result: dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}:{port}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result
