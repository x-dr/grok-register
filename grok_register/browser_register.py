"""Browser (cloakbrowser) registration method.

Adapted from mytest/app.py. Uses a real anti-detect Chromium to complete the
x.ai sign-up UI flow (email → code → profile → Turnstile → sso cookie).

Unlike the protocol path, this method does **not** need an external Turnstile
solver — the widget is clicked inside the browser.
"""

from __future__ import annotations

import gc
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .browser_js import (
    AccountRetryNeeded,
    BrowserDeadError,
    RegistrationCancelled,
    build_profile,
    click_button_by_text,
    human_pause,
    page_run_js,
    parse_browser_proxy,
    raise_if_cancelled,
    sleep_with_cancel,
)
from .browser_turnstile import get_turnstile_token
from .register import (
    CLIPROXYAPI_GROK_BASE_URL,
    make_email_provider,
    resolve_proxy,
    save_account_bundle,
)
from xconsole_client.xai_oauth import (
    default_cliproxyapi_auth_dir,
    save_cliproxyapi_auth_record,
)

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

LogFn = Optional[Callable[[int, str], None]]
Progress = Optional[dict[str, Any]]
CancelFn = Optional[Callable[[], bool]]


# ---------------------------------------------------------------------------
# platform / UA
# ---------------------------------------------------------------------------

def _detect_platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _platform_default_ua(platform_name: str) -> str:
    # cloakbrowser free Chromium 146.0.7680.177.5
    if platform_name == "windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.7680.177 Safari/537.36"
        )
    if platform_name == "macos":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.7680.177 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.7680.177 Safari/537.36"
    )


def _require_cloakbrowser():
    try:
        from cloakbrowser import launch_context  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "browser 注册方式需要 cloakbrowser。请安装: pip install cloakbrowser\n"
            "或: pip install 'grok-register[browser]'"
        ) from exc
    return launch_context


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------

class BrowserSession:
    """Owns one cloakbrowser context + page for a registration attempt."""

    def __init__(
        self,
        *,
        proxy: str = "",
        headless: bool = False,
        user_agent: str | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.proxy = (proxy or "").strip()
        self.headless = bool(headless)
        self.user_agent = user_agent
        self._log = log or (lambda _m: None)
        self.context = None
        self.page = None

    def log(self, msg: str) -> None:
        self._log(msg)

    def start(self) -> None:
        launch_context = _require_cloakbrowser()
        platform_name = _detect_platform_name()
        configured_ua = (self.user_agent or "").strip() or _platform_default_ua(platform_name)
        if (
            (platform_name == "windows" and "Windows" not in configured_ua)
            or (platform_name == "macos" and "Macintosh" not in configured_ua)
            or (
                platform_name == "linux"
                and "Linux" not in configured_ua
                and "X11" not in configured_ua
            )
        ):
            ua = _platform_default_ua(platform_name)
            self.log(f"[browser] config UA mismatches OS({platform_name}), using platform default")
        else:
            ua = configured_ua

        try:
            from .proxyutil import playwright_proxy

            browser_proxy = playwright_proxy(self.proxy) or parse_browser_proxy(self.proxy)
        except Exception:
            browser_proxy = parse_browser_proxy(self.proxy)
        if browser_proxy:
            self.log(f"[browser] proxy={browser_proxy.get('server')}")
        else:
            self.log("[browser] no proxy (direct)")
        self.log(f"[browser] headless={self.headless} platform={platform_name}")

        last_exc: Exception | None = None
        for attempt in range(1, 5):
            try:
                self.stop()
                kwargs: dict[str, Any] = dict(
                    headless=self.headless,
                    user_agent=ua,
                    viewport={"width": 1280, "height": 720},
                    locale="en-US",
                    timezone="America/Los_Angeles",
                )
                if browser_proxy:
                    kwargs["proxy"] = browser_proxy
                self.context = launch_context(**kwargs)
                self.page = self.context.new_page()
                self.page.set_default_timeout(30000)
                self.page.set_default_navigation_timeout(60000)
                self.log("[browser] cloakbrowser started")
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.log(f"[browser] start failed ({attempt}/4): {exc}")
                self.stop()
                time.sleep(1.5 * attempt)
        raise RuntimeError(f"browser start failed after 4 tries: {last_exc}")

    def stop(self) -> None:
        page, ctx = self.page, self.context
        self.page = None
        self.context = None
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass

    def restart(self) -> None:
        self.stop()
        self.start()

    def alive(self, deep: bool = False) -> bool:
        if self.page is None or self.context is None:
            return False
        try:
            if self.page.is_closed():
                return False
        except Exception:
            return False
        if not deep:
            return True
        try:
            self.page.evaluate("1+1")
            return True
        except Exception:
            return False

    def ensure_alive(self, restart: bool = True) -> bool:
        if self.alive(deep=True):
            return True
        self.log("[browser] disconnected")
        if restart:
            try:
                self.restart()
                return self.alive(deep=True)
            except Exception:
                return False
        return False

    def url(self) -> str:
        try:
            return self.page.url if self.page else ""
        except Exception:
            return ""

    def cookies(self) -> list[dict[str, Any]]:
        if self.context is None:
            return []
        try:
            return self.context.cookies()
        except Exception:
            return []

    def find_sso(self) -> str | None:
        for c in self.cookies():
            if str(c.get("name") or "").lower() == "sso":
                val = str(c.get("value") or "").strip()
                if val:
                    return val
        return None

    def html_snippet(self, limit: int = 500) -> str:
        try:
            html = self.page.content() if self.page else ""
            return (html or "")[:limit]
        except Exception as exc:
            return f"<html error: {exc}>"


# ---------------------------------------------------------------------------
# Registration steps
# ---------------------------------------------------------------------------

def open_signup_page(session: BrowserSession, cancel: CancelFn = None) -> None:
    raise_if_cancelled(cancel)
    if session.page is None:
        session.start()
    last_err: Exception | None = None
    for attempt in range(1, 4):
        raise_if_cancelled(cancel)
        try:
            session.page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            session.log(f"[browser] open URL failed ({attempt}/3): {e}")
            sleep_with_cancel(1.5 * attempt, cancel)
            try:
                session.restart()
            except Exception as re:  # noqa: BLE001
                session.log(f"[browser] restart failed: {re}")
    if last_err is not None:
        raise last_err
    try:
        session.page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    sleep_with_cancel(2, cancel)
    session.log(f"[browser] url={session.url()}")
    click_email_signup_button(session, cancel=cancel)


def click_email_signup_button(
    session: BrowserSession, timeout: float = 10, cancel: CancelFn = None
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel)
        clicked = page_run_js(
            session.page,
            """
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => {
        const compact = textOf(node).replace(/\\s+/g, '');
        const lower = compact.toLowerCase();
        let score = 0;
        if (compact.includes('使用邮箱注册')) score = 100;
        else if (lower.includes('signupwithemail')) score = 95;
        else if (lower.includes('continuewithemail')) score = 90;
        else if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) score = 80;
        else if (lower === 'email' || lower.includes('邮箱')) score = 70;
        return { node, score, text: textOf(node) };
    })
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
const target = candidates[0]?.node || null;
if (!target) return false;
target.click();
return candidates[0].text || true;
""",
        )
        if clicked:
            detail = f": {clicked}" if isinstance(clicked, str) else ""
            session.log(f"[browser] clicked email signup{detail}")
            sleep_with_cancel(2, cancel)
            return True
        sleep_with_cancel(1, cancel)
    raise RuntimeError("email signup button not found")


def _email_page_advanced_once(session: BrowserSession, email: str) -> bool:
    try:
        return bool(
            page_run_js(
                session.page,
                """
const codeInput = Array.from(document.querySelectorAll('input')).find((node) => {
    if (!isVisible(node)) return false;
    const type = (node.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file'].includes(type)) return false;
    const meta = textOf(node).toLowerCase();
    const inMode = (node.getAttribute('inputmode') || '').toLowerCase();
    return meta.includes('code') || meta.includes('otp') || meta.includes('verif') ||
        meta.includes('验证') || meta.includes('one-time') || inMode === 'numeric' ||
        node.getAttribute('autocomplete') === 'one-time-code';
});
if (codeInput) return true;
const emailInput = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'))
    .find((node) => isVisible(node) && !node.disabled && !node.readOnly);
if (!emailInput) return true;
const v = String(emailInput.value || '').trim().toLowerCase();
return v === String(arguments[0] || '').trim().toLowerCase() && document.querySelector('input[name="code"], input[autocomplete="one-time-code"]');
""",
                email,
            )
        )
    except Exception:
        return False


def _wait_email_page_advanced(
    session: BrowserSession, email: str, wait: float = 4.0, cancel: CancelFn = None
) -> bool:
    deadline = time.time() + wait
    while time.time() < deadline:
        raise_if_cancelled(cancel)
        if _email_page_advanced_once(session, email):
            return True
        sleep_with_cancel(0.4, cancel)
    return _email_page_advanced_once(session, email)


def fill_email_and_submit(
    session: BrowserSession,
    email: str,
    *,
    timeout: float = 45,
    cancel: CancelFn = None,
) -> str:
    deadline = time.time() + timeout
    last_reclick = 0.0
    while time.time() < deadline:
        raise_if_cancelled(cancel)
        filled = page_run_js(
            session.page,
            r"""
const email = arguments[0];
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) return { state: 'not-ready', url: location.href };
input.focus(); input.click();
const ok = setInputValue(input, email);
if (!ok) return { state: 'fill-failed', value: input.value || '', url: location.href };
return { state: 'filled', url: location.href };
""",
            email,
        )
        state = filled.get("state") if isinstance(filled, dict) else filled
        if state == "not-ready":
            now = time.time()
            if now - last_reclick >= 3:
                page_run_js(
                    session.page,
                    """
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => {
        const compact = textOf(node).replace(/\\s+/g,'').toLowerCase();
        let score = 0;
        if (compact.includes('使用邮箱注册')) score = 100;
        else if (compact.includes('signupwithemail')) score = 95;
        else if (compact.includes('continuewithemail')) score = 90;
        return { node, score };
    })
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
if (candidates.length) candidates[0].node.click();
return true;
""",
                )
                last_reclick = now
                session.log("[browser] email input missing; re-clicked signup entry")
            sleep_with_cancel(0.5, cancel)
            continue
        if state != "filled":
            session.log(f"[browser] email fill failed: {filled}")
            sleep_with_cancel(0.5, cancel)
            continue
        sleep_with_cancel(0.8, cancel)
        clicked = click_button_by_text(
            session.page,
            [
                "注册",
                "继续",
                "下一步",
                "确认",
                "signup",
                "sign up",
                "continue",
                "next",
                "createaccount",
                "submit",
            ],
        )
        if clicked and _wait_email_page_advanced(session, email, cancel=cancel):
            session.log(f"[browser] email submitted: {email}")
            return email
        sleep_with_cancel(0.5, cancel)
    raise RuntimeError("email input/submit failed")


def fill_code_and_submit(
    session: BrowserSession,
    code: str,
    *,
    timeout: float = 180,
    cancel: CancelFn = None,
) -> str:
    clean_code = str(code).replace("-", "").strip()
    if not clean_code:
        raise RuntimeError("empty verification code")
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel)
        filled = page_run_js(
            session.page,
            r"""
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';
const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);
if (aggregate) {
    aggregate.focus(); aggregate.click();
    const ok = setInputValue(aggregate, code);
    return ok ? 'filled-aggregate' : 'aggregate-failed';
}
const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});
if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus(); box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}
return 'not-ready';
""",
            clean_code,
        )
        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel)
            continue
        if "failed" in str(filled):
            session.log(f"[browser] code fill failed: {filled}")
            sleep_with_cancel(0.5, cancel)
            continue
        try:
            click_button_by_text(
                session.page,
                ["确认邮箱", "继续", "下一步", "confirm", "continue", "next", "verify"],
                selectors='button[type="submit"], button',
            )
        except Exception:
            pass
        advanced_deadline = time.time() + 15
        while time.time() < advanced_deadline:
            raise_if_cancelled(cancel)
            advanced = page_run_js(
                session.page,
                """
const given = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[placeholder*="First"], input[id*="firstName"], input[id*="givenName"]');
const family = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[placeholder*="Last"], input[id*="lastName"], input[id*="familyName"]');
const password = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');
return !!(isVisible(given) && isVisible(family) && isVisible(password));
""",
            )
            if advanced:
                session.log("[browser] code accepted; profile page ready")
                return clean_code
            sleep_with_cancel(0.5, cancel)
        sleep_with_cancel(0.5, cancel)
    raise RuntimeError("code obtained but auto-fill/submit failed")


def fill_profile_and_submit(
    session: BrowserSession,
    *,
    timeout: float = 120,
    cancel: CancelFn = None,
) -> dict[str, str]:
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since: float | None = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel)
        if not form_filled_once:
            inputs = page_run_js(
                session.page,
                """
const selectors = {
    given: 'input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="First"], input[placeholder*="First"], input[id*="firstName"], input[id*="givenName"]',
    family: 'input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="Last"], input[placeholder*="Last"], input[id*="lastName"], input[id*="familyName"]',
    password: 'input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"], input[aria-label*="Password"], input[placeholder*="Password"]'
};
const result = {};
for (const key of Object.keys(selectors)) {
    result[key] = pickInput(selectors[key]);
}
result.cfInput = !!document.querySelector('input[name="cf-turnstile-response"]') || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
return result;
""",
            )
            if not inputs or not (
                inputs.get("given") and inputs.get("family") and inputs.get("password")
            ):
                sleep_with_cancel(0.5, cancel)
                continue

            fields = [
                ("given", given_name),
                ("family", family_name),
                ("password", password),
            ]
            session.log("[browser] filling profile fields…")
            fill_ok = True
            for field_key, field_value in fields:
                ok = page_run_js(
                    session.page,
                    """
const key = arguments[0];
const value = arguments[1];
const selectors = {
    given: 'input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="First"], input[placeholder*="First"], input[id*="firstName"], input[id*="givenName"]',
    family: 'input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="Last"], input[placeholder*="Last"], input[id*="lastName"], input[id*="familyName"]',
    password: 'input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"], input[aria-label*="Password"], input[placeholder*="Password"]'
};
const el = pickInput(selectors[key]);
if (!el) return false;
return setInputValue(el, value);
""",
                    field_key,
                    field_value,
                )
                if not ok:
                    fill_ok = False
                    break
                sleep_with_cancel(random.uniform(0.8, 1.5), cancel)
            if not fill_ok:
                sleep_with_cancel(0.5, cancel)
                continue
            form_filled_once = True
            session.log("[browser] profile fields filled")

        # Turnstile
        try:
            token = get_turnstile_token(
                session.page,
                log_callback=session.log,
                cancel_callback=cancel,
                timeout=40,
            )
            if token:
                session.log(f"[browser] turnstile ok ({len(token)} chars)")
        except RegistrationCancelled:
            raise
        except Exception as ts_exc:
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if now - last_cf_retry_at >= 8:
                session.log(f"[browser] turnstile still pending: {ts_exc}")
                last_cf_retry_at = now
            if now - (wait_cf_since or now) > 90:
                raise AccountRetryNeeded(f"turnstile timeout: {ts_exc}")
            sleep_with_cancel(1.0, cancel)
            continue

        # Submit
        clicked = click_button_by_text(
            session.page,
            [
                "完成注册",
                "创建账户",
                "创建账号",
                "注册",
                "completesignup",
                "create account",
                "createaccount",
                "sign up",
                "signup",
                "submit",
                "continue",
            ],
            selectors='button[type="submit"], button, [role="button"], input[type="submit"]',
        )
        if not clicked:
            sleep_with_cancel(0.8, cancel)
            continue
        session.log(f"[browser] submit clicked: {clicked}")
        # Wait for navigation away from profile / toward grok
        nav_deadline = time.time() + 40
        while time.time() < nav_deadline:
            raise_if_cancelled(cancel)
            url = session.url() or ""
            if "grok.com" in url or ("accounts.x.ai" not in url and url):
                break
            # profile fields gone?
            still = page_run_js(
                session.page,
                """
const password = document.querySelector('input[type="password"], input[name="password"]');
return isVisible(password);
""",
            )
            if still is False:
                break
            sleep_with_cancel(0.8, cancel)
        return {
            "given_name": given_name,
            "family_name": family_name,
            "password": password,
        }
    raise RuntimeError("profile fill/submit timeout")


def interact_with_grok_before_sso(
    session: BrowserSession, cancel: CancelFn = None
) -> None:
    """Optional age-gate / first chat interaction (from mytest). Non-fatal."""
    questions = [
        "What is the weather today in New York?",
        "What are the latest news on X today?",
        "What is trending on X right now?",
        "What are the top headlines in the US today?",
    ]
    session.log("[browser] waiting for grok.com…")
    for _ in range(30):
        raise_if_cancelled(cancel)
        if "grok.com" in (session.url() or ""):
            break
        sleep_with_cancel(1.0, cancel)
    try:
        session.page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        try:
            session.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
    sleep_with_cancel(2.0, cancel)

    question = random.choice(questions)
    editor_selector = (
        '[data-testid="chat-input"] div[contenteditable="true"][role="textbox"], '
        'div[contenteditable="true"][role="textbox"][aria-label="Ask Grok anything"], '
        'div[contenteditable="true"][role="textbox"][aria-label*="Grok"], '
        'div[contenteditable="true"][role="textbox"]'
    )
    try:
        session.page.wait_for_selector(editor_selector, timeout=20000)
    except Exception:
        session.log("[browser] chat input not found; skip pre-sso interaction")
        sleep_with_cancel(5.0, cancel)
        return

    session.log(f"[browser] typing question: {question}")
    try:
        session.page.locator(editor_selector).first.click()
        sleep_with_cancel(0.5, cancel)
        session.page.keyboard.type(question, delay=30)
        sleep_with_cancel(0.5, cancel)
        session.page.keyboard.press("Enter")
    except Exception as exc:
        session.log(f"[browser] chat type failed: {exc}")
        return

    dialog_selector = '[role="dialog"][data-analytics-name="age_verification"]'
    year_input_selector = (
        'input[placeholder="YYYY"][aria-label="Year of birth"], '
        'input[placeholder="YYYY"][aria-label*="出生年份"], '
        'input[placeholder="YYYY"]'
    )
    found_dialog = False
    for _ in range(20):
        raise_if_cancelled(cancel)
        try:
            dlg = session.page.query_selector(dialog_selector)
            if dlg and dlg.query_selector(year_input_selector):
                found_dialog = True
                break
        except Exception:
            pass
        sleep_with_cancel(1.0, cancel)

    if not found_dialog:
        session.log("[browser] no age verification dialog")
    else:
        birth_year = str(random.randint(1960, 2000))
        session.log(f"[browser] age dialog year={birth_year}")
        try:
            session.page.locator(dialog_selector).locator(year_input_selector).first.fill(
                birth_year
            )
            sleep_with_cancel(0.5, cancel)
            for btn_text in ("Continue", "继续"):
                try:
                    btn = session.page.locator(dialog_selector).locator(
                        f'button:has-text("{btn_text}")'
                    ).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        session.log(f"[browser] clicked {btn_text}")
                        break
                except Exception:
                    pass
            sleep_with_cancel(2.0, cancel)
            for _ in range(15):
                raise_if_cancelled(cancel)
                try:
                    for save_text in ("Save", "保存"):
                        btn = session.page.locator(dialog_selector).locator(
                            f'button:has-text("{save_text}")'
                        )
                        if btn.count() > 0 and btn.first.is_visible(timeout=1000):
                            btn.first.click()
                            session.log(f"[browser] clicked {save_text}")
                            raise StopIteration
                except StopIteration:
                    break
                except Exception:
                    pass
                sleep_with_cancel(1.0, cancel)
        except Exception as exc:
            session.log(f"[browser] age dialog handling failed: {exc}")

    sleep_with_cancel(8.0, cancel)


def wait_for_sso_cookie(
    session: BrowserSession, *, timeout: float = 150, cancel: CancelFn = None
) -> str:
    deadline = time.time() + timeout
    last_seen: set[str] = set()
    last_log = 0.0
    while time.time() < deadline:
        raise_if_cancelled(cancel)
        try:
            cookies = session.cookies()
            names = {str(c.get("name") or "") for c in cookies}
            last_seen |= names
            sso = session.find_sso()
            if sso:
                session.log("[browser] sso cookie acquired")
                return sso
            now = time.time()
            if now - last_log >= 10:
                session.log(
                    f"[browser] waiting sso… url={session.url()} cookies={sorted(names)[:12]}"
                )
                last_log = now
            # If still stuck on accounts.x.ai with error, fail early-ish
            if "error" in (session.url() or "").lower() and time.time() + 30 > deadline:
                break
        except BrowserDeadError as dead:
            sso = session.find_sso()
            if sso:
                return sso
            if not session.ensure_alive(restart=True):
                raise AccountRetryNeeded(f"browser dead while waiting sso: {dead}")
        except Exception as exc:
            if not session.alive():
                raise AccountRetryNeeded(f"browser lost while waiting sso: {exc}")
        sleep_with_cancel(1.0, cancel)
    raise RuntimeError(
        f"sso cookie timeout; seen cookies={sorted(last_seen)}; url={session.url()}"
    )




# ---------------------------------------------------------------------------
# Public entry: register_one_browser
# ---------------------------------------------------------------------------

def register_one_browser(
    index: int = 1,
    email_backend: str = "22do",
    *,
    do_oauth: bool = True,
    oauth_protocol: bool = True,
    oauth_debug: bool = False,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    accounts_output_dir: Optional[str | Path] = None,
    proxy: str | None = None,
    enable_nsfw: bool = True,
    headless: bool = False,
    user_agent: str | None = None,
    log: LogFn = None,
    progress: Progress = None,
) -> dict[str, Any]:
    """Run one account via cloakbrowser UI flow. Result shape matches register_one."""

    def _log(msg: str) -> None:
        if log:
            log(index, msg)
        elif progress is not None:
            done = int(progress.get("done", 0))
            total = int(progress.get("total", 0))
            t0 = float(progress.get("t0", 0.0))
            from .register import _default_log

            _default_log(index, msg, done=done, total=total, t0=t0)
        else:
            from .register import _default_log

            _default_log(index, msg)

    proxy = (proxy if proxy is not None else resolve_proxy()) or ""
    try:
        from .proxyutil import apply_proxy_env, normalize_proxy

        if proxy:
            proxy = normalize_proxy(proxy) or proxy
            apply_proxy_env(proxy, force=True)
    except Exception:
        pass

    session = BrowserSession(
        proxy=proxy, headless=headless, user_agent=user_agent, log=_log
    )
    email = ""
    password = ""
    try:
        session.start()
        _log("method=browser open signup…")
        open_signup_page(session)

        # Allocate mailbox after page is ready (same backends as protocol mode)
        email, receiver = make_email_provider(email_backend)
        _log(f"email: {email}")
        fill_email_and_submit(session, email)

        _log("wait verification code…")

        def _resend() -> None:
            try:
                page_run_js(
                    session.page,
                    r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g,'').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
""",
                )
            except Exception:
                pass

        # Poll with optional resend on slow mailboxes
        code = ""
        code_deadline = time.time() + 120
        last_resend = 0.0
        while time.time() < code_deadline:
            try:
                # Prefer short polls so we can resend
                if hasattr(receiver, "wait_for_code"):
                    # Many receivers block for full timeout; use shorter slices when possible
                    try:
                        code = receiver.wait_for_code(timeout=20)
                    except TypeError:
                        code = receiver.wait_for_code(timeout=120)
                        break
                if code:
                    break
            except Exception as mail_exc:
                _log(f"mail poll: {mail_exc}")
            now = time.time()
            if now - last_resend >= 35:
                _log("resend verification code…")
                _resend()
                last_resend = now
            sleep_with_cancel(2.0)
        if not code:
            raise RuntimeError("verification code timeout")
        _log(f"code: {code}")
        fill_code_and_submit(session, code)

        _log("fill profile + turnstile…")
        profile = fill_profile_and_submit(session)
        password = profile.get("password") or ""
        _log(f"profile: {profile.get('given_name')} {profile.get('family_name')}")

        sleep_with_cancel(5.0)
        try:
            interact_with_grok_before_sso(session)
        except Exception as interact_exc:
            _log(f"pre-sso interaction skipped: {interact_exc}")

        sso = wait_for_sso_cookie(session)
        result: dict[str, Any] = {
            "email": email,
            "password": password,
            "sso": sso,
            "oauth_access_token": None,
            "oauth_refresh_token": None,
            "oauth_record": None,
            "cliproxyapi_auth": None,
            "build_base_url": cliproxyapi_base_url,
            "nsfw_enabled": False,
            "nsfw_error": None,
            "register_method": "browser",
            "error": None,
        }

        # NSFW (reuse protocol helper)
        if enable_nsfw:
            _log("enable NSFW…")
            try:
                from .nsfw import enable_nsfw_for_sso

                nsfw_ok, nsfw_msg = enable_nsfw_for_sso(sso, proxy=proxy, log=_log)
            except Exception as e:  # noqa: BLE001
                nsfw_ok, nsfw_msg = False, str(e)
            if nsfw_ok:
                result["nsfw_enabled"] = True
                _log(f"NSFW enabled: {nsfw_msg}")
            else:
                result["nsfw_error"] = nsfw_msg
                _log(f"NSFW not enabled (account kept): {nsfw_msg}")
        else:
            result["nsfw_error"] = "skipped"
            _log("NSFW skipped")

        # OAuth device-flow / protocol fallback — same as register_one
        if do_oauth and sso:
            auth_dir = (
                Path(cliproxyapi_auth_dir)
                if cliproxyapi_auth_dir
                else default_cliproxyapi_auth_dir()
            )
            oauth_errors: list[str] = []
            _log("OAuth via SSO device-flow…")
            try:
                from .sso_device import sso_to_token
                from xconsole_client.xai_oauth import build_cliproxyapi_auth_record

                token = sso_to_token(sso, quiet=False)
            except Exception as e:  # noqa: BLE001
                token = None
                oauth_errors.append(f"device-flow: {e}")
                _log(f"device-flow failed: {e}")

            if token and token.get("access_token"):
                result["oauth_access_token"] = token.get("access_token")
                result["oauth_refresh_token"] = token.get("refresh_token")
                try:
                    cpath = save_cliproxyapi_auth_record(
                        {
                            "access_token": token.get("access_token"),
                            "refresh_token": token.get("refresh_token"),
                            "expires_in": token.get("expires_in"),
                            "token_type": token.get("token_type") or "Bearer",
                        },
                        userinfo={"email": email},
                        auth_dir=auth_dir,
                        base_url=cliproxyapi_base_url,
                    )
                    result["cliproxyapi_auth"] = str(cpath)
                    result["oauth_record"] = str(cpath)
                    _log(
                        f"OAuth OK (device-flow) access="
                        f"{str(token.get('access_token'))[:20]}...  "
                        f"cliproxy={cpath.name}"
                    )
                except Exception as e:  # noqa: BLE001
                    oauth_errors.append(f"write auth: {e}")
                    _log(f"write CPA auth failed: {e}")
            else:
                _log("device-flow failed; browser method keeps account with SSO only")
                if oauth_errors:
                    result["error"] = "; ".join(oauth_errors)

        if accounts_output_dir and not result.get("error"):
            try:
                path = save_account_bundle(result, Path(accounts_output_dir))
                result["account_bundle"] = str(path)
                _log(f"saved → {path}")
            except Exception as e:  # noqa: BLE001
                _log(f"save bundle failed: {e}")

        _log("OK")
        return result
    except AccountRetryNeeded as e:
        _log(f"RETRY: {e}")
        return {
            "email": email,
            "password": password,
            "sso": None,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "register_method": "browser",
            "error": f"retry: {e}",
        }
    except RegistrationCancelled:
        return {
            "email": email,
            "password": password,
            "sso": None,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "register_method": "browser",
            "error": "cancelled",
        }
    except Exception as e:  # noqa: BLE001
        _log(f"FAIL: {e}")
        return {
            "email": email,
            "password": password,
            "sso": None,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "register_method": "browser",
            "error": str(e),
        }
    finally:
        try:
            session.stop()
        except Exception:
            pass
        try:
            gc.collect()
        except Exception:
            pass


def run_batch_browser(
    count: int,
    *,
    email_backend: str = "22do",
    threads: int = 1,
    progress: Progress = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Browser mode is effectively serial (one Chromium at a time)."""
    if threads and int(threads) > 1:
        # Keep API compatible but force serial for browser stability.
        if progress is not None:
            # no direct log; register_one_browser will log per account
            pass
        threads = 1

    results: list[dict[str, Any]] = []
    if progress is None:
        progress = {"done": 0, "total": count, "t0": time.time()}
    else:
        progress.setdefault("done", 0)
        progress.setdefault("total", count)
        progress.setdefault("t0", time.time())

    for i in range(1, count + 1):
        r = register_one_browser(
            i, email_backend=email_backend, progress=progress, **kwargs
        )
        results.append(r)
        progress["done"] = int(progress.get("done", 0)) + 1
        if i < count:
            time.sleep(random.uniform(2.0, 4.0))
    return results
