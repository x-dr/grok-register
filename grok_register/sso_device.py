"""SSO cookie → OIDC access/refresh via xAI device flow.

Same approach as grokcli-2api ``scripts/sso_to_auth_json.py``:
no Turnstile / Playwright required when the SSO JWT is valid.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

OIDC_ISSUER = os.getenv("GROK2API_OIDC_ISSUER", "https://auth.x.ai")
GROK_CLI_CLIENT_ID = os.getenv(
    "GROK2API_OIDC_CLIENT_ID", "b1a00492-073a-47ea-816f-4c329264a828"
)
OIDC_SCOPES = os.getenv(
    "GROK2API_OIDC_SCOPES",
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write",
)

_DEVICE_FLOW_LOCK = threading.RLock()
_DEVICE_FLOW_LAST_TS = 0.0


def _http_timeout() -> float:
    try:
        return max(5.0, float(os.getenv("GROK2API_SSO_HTTP_TIMEOUT", "30") or 30))
    except (TypeError, ValueError):
        return 30.0


def _device_flow_gap_sec() -> float:
    try:
        return max(0.0, float(os.getenv("GROK2API_SSO_DEVICE_GAP_SEC", "0.85") or 0.85))
    except (TypeError, ValueError):
        return 0.85


def _device_flow_retries() -> int:
    try:
        return max(1, min(16, int(os.getenv("GROK2API_SSO_DEVICE_RETRIES", "8") or 8)))
    except (TypeError, ValueError):
        return 6


def _device_flow_backoff_sec(attempt: int) -> float:
    base = 1.4 * (1.45 ** max(0, attempt - 1))
    return max(0.8, min(25.0, base))


def _wait_device_flow_slot() -> None:
    global _DEVICE_FLOW_LAST_TS
    gap = _device_flow_gap_sec()
    with _DEVICE_FLOW_LOCK:
        now = time.time()
        wait = (_DEVICE_FLOW_LAST_TS + gap) - now
        if wait > 0:
            time.sleep(wait)
        _DEVICE_FLOW_LAST_TS = time.time()


def _is_rate_limited_payload(
    text: str | None = None, url: str | None = None, status: int | None = None
) -> bool:
    blob = f"{status or ''} {url or ''} {text or ''}".lower()
    return any(
        k in blob
        for k in ("slow_down", "rate_limited", "rate limit", "too many", "429")
    )


def _proxy_kwargs() -> dict[str, Any]:
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
        or ""
    ).strip()
    if not proxy:
        return {}
    return {"proxies": {"http": proxy, "https": proxy}}


def _poll_interval_sec(raw: Any = 1) -> float:
    env = (os.getenv("GROK2API_SSO_POLL_INTERVAL") or "").strip()
    if env:
        try:
            return max(0.2, min(10.0, float(env)))
        except ValueError:
            pass
    try:
        hinted = float(raw if raw is not None else 1)
    except (TypeError, ValueError):
        hinted = 1.0
    return max(0.4, min(hinted, 1.5))


def request_device_code(session: Any | None = None) -> dict | None:
    form = {"client_id": GROK_CLI_CLIENT_ID, "scope": OIDC_SCOPES}
    timeout = _http_timeout()
    retries = _device_flow_retries()
    last_err = ""
    for attempt in range(1, retries + 1):
        _wait_device_flow_slot()
        if session is not None:
            try:
                r = session.post(
                    f"{OIDC_ISSUER}/oauth2/device/code",
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate="chrome",
                    timeout=timeout,
                    **_proxy_kwargs(),
                )
                code = int(getattr(r, "status_code", 0) or 0)
                body = (getattr(r, "text", None) or "")[:300]
                if code >= 400:
                    last_err = f"HTTP {code}: {body[:200]}"
                    if _is_rate_limited_payload(body, status=code) and attempt < retries:
                        time.sleep(_device_flow_backoff_sec(attempt))
                        continue
                    return None
                data = r.json()
                return data if isinstance(data, dict) else None
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                if attempt < retries and _is_rate_limited_payload(str(e)):
                    time.sleep(_device_flow_backoff_sec(attempt))
                    continue
                return None

        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            f"{OIDC_ISSUER}/oauth2/device/code",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            last_err = f"HTTP {e.code}: {body[:200]}"
            if _is_rate_limited_payload(body, status=e.code) and attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            return None
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            return None
    return None


def poll_token(
    device_code: str,
    interval: int | float = 1,
    expires_in: int = 1800,
    timeout: int | float = 45,
    *,
    session: Any | None = None,
    immediate: bool = True,
) -> dict | None:
    interval_f = _poll_interval_sec(interval)
    deadline = time.time() + min(float(expires_in or 1800), float(timeout or 45))
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": GROK_CLI_CLIENT_ID,
        "device_code": device_code,
    }
    http_timeout = _http_timeout()
    first = True
    while time.time() < deadline:
        if not (first and immediate):
            time.sleep(interval_f)
        first = False

        if session is not None:
            try:
                r = session.post(
                    f"{OIDC_ISSUER}/oauth2/token",
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate="chrome",
                    timeout=http_timeout,
                    **_proxy_kwargs(),
                )
                if int(getattr(r, "status_code", 0) or 0) >= 400:
                    body = (getattr(r, "text", None) or "")[:300]
                    if "authorization_pending" in body or "slow_down" in body:
                        continue
                    return None
                data = r.json()
                if isinstance(data, dict) and data.get("access_token"):
                    return data
            except Exception:
                continue
            continue

        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            f"{OIDC_ISSUER}/oauth2/token",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                payload = json.loads(resp.read())
                if isinstance(payload, dict) and payload.get("access_token"):
                    return payload
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            if "authorization_pending" in body or "slow_down" in body:
                continue
            return None
        except Exception:
            continue
    return None


def sso_to_token(sso_cookie: str, *, quiet: bool = False) -> dict | None:
    """SSO cookie → token dict (access/refresh/expires_in)."""
    log = (lambda *a, **k: None) if quiet else print
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise RuntimeError("curl_cffi is required for SSO device flow") from exc

    s = requests.Session()
    s.cookies.set("sso", sso_cookie, domain=".x.ai")
    timeout = _http_timeout()
    proxy_kw = _proxy_kwargs()

    try:
        r = s.get(
            "https://accounts.x.ai/",
            impersonate="chrome",
            timeout=timeout,
            **proxy_kw,
        )
    except Exception as e:
        log(f"  [device] network error: {e}")
        return None
    if "sign-in" in (r.url or "") or "sign-up" in (r.url or ""):
        log("  [device] sso invalid")
        return None
    log("  [device] sso valid")

    retries = _device_flow_retries()
    for attempt in range(1, retries + 1):
        log(f"  [device] device flow try {attempt}/{retries}")
        dc = request_device_code(session=s)
        if not dc:
            if attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            return None
        log(f"  [device] user_code={dc.get('user_code')}")

        rate_limited = False
        try:
            s.get(
                dc["verification_uri_complete"],
                impersonate="chrome",
                timeout=timeout,
                **proxy_kw,
            )
            r = s.post(
                f"{OIDC_ISSUER}/oauth2/device/verify",
                data={"user_code": dc["user_code"]},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                impersonate="chrome",
                timeout=timeout,
                allow_redirects=True,
                **proxy_kw,
            )
            if "consent" not in (r.url or ""):
                log(f"  [device] verify failed: {r.url}")
                if _is_rate_limited_payload(
                    getattr(r, "text", None), r.url, getattr(r, "status_code", None)
                ):
                    rate_limited = True
                else:
                    return None
        except Exception as e:
            log(f"  [device] verify error: {e}")
            if _is_rate_limited_payload(str(e)):
                rate_limited = True
            else:
                return None
        if rate_limited:
            if attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            return None

        try:
            r = s.post(
                f"{OIDC_ISSUER}/oauth2/device/approve",
                data={
                    "user_code": dc["user_code"],
                    "action": "allow",
                    "principal_type": "User",
                    "principal_id": "",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                impersonate="chrome",
                timeout=timeout,
                allow_redirects=True,
                **proxy_kw,
            )
            if "done" not in (r.url or ""):
                log(f"  [device] approve failed: {r.url}")
                if _is_rate_limited_payload(
                    getattr(r, "text", None), r.url, getattr(r, "status_code", None)
                ):
                    if attempt < retries:
                        time.sleep(_device_flow_backoff_sec(attempt))
                        continue
                return None
            log("  [device] approved")
        except Exception as e:
            log(f"  [device] approve error: {e}")
            if _is_rate_limited_payload(str(e)) and attempt < retries:
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            return None

        token = poll_token(
            dc["device_code"],
            dc.get("interval", 1),
            dc.get("expires_in", 1800),
            timeout=float(os.getenv("GROK2API_SSO_POLL_TIMEOUT", "45") or 45),
            session=s,
            immediate=True,
        )
        if not token:
            if attempt < retries:
                log("  [device] token poll empty — retry")
                time.sleep(_device_flow_backoff_sec(attempt))
                continue
            return None
        log(
            f"  [device] access_token ok expires_in={token.get('expires_in')}"
            + (" + refresh" if token.get("refresh_token") else "")
        )
        return token
    return None
