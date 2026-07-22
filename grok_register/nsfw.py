# -*- coding: utf-8 -*-
"""Enable Grok NSFW feature controls for an account after SSO is available.

Mirrors the mytest flow (ToS → birth date → always_show_nsfw_content) using
curl_cffi + xconsole_client.grpcweb framing.
"""

from __future__ import annotations

import datetime as dt
import random
import re
import time
from typing import Any, Callable, Optional

from xconsole_client import grpcweb
from xconsole_client import config as xc_config

LogFn = Callable[[str], None]

SET_TOS_URL = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
SET_BIRTH_DATE_URL = "https://grok.com/rest/auth/set-birth-date"
UPDATE_NSFW_URL = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
NSFW_FEATURE_KEY = "always_show_nsfw_content"


def generate_random_birthdate(*, age_min: int = 20, age_max: int = 40) -> str:
    """Random adult birthdate in the shape Grok's set-birth-date API expects."""
    today = dt.date.today()
    age = random.randint(age_min, age_max)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def encode_set_tos_accepted_body() -> bytes:
    """gRPC-Web frame: field 2 varint = 1 (SetTosAcceptedVersion)."""
    return grpcweb.frame_request(grpcweb.encode_varint_field(2, 1))


def encode_update_nsfw_body() -> bytes:
    """gRPC-Web frame for UpdateUserFeatureControls(always_show_nsfw_content).

    Wire layout (matches mytest byte-for-byte):
      field1 { field2: 1 }          # enable control bit
      field2 { field1: "always_show_nsfw_content" }
    """
    field1 = grpcweb.encode_bytes(1, grpcweb.encode_varint_field(2, 1))
    field2 = grpcweb.encode_bytes(2, grpcweb.encode_string(1, NSFW_FEATURE_KEY))
    return grpcweb.frame_request(field1 + field2)


def _response_preview(res: Any, limit: int = 200) -> str:
    try:
        raw = getattr(res, "content", None)
        if raw is None:
            raw = (getattr(res, "text", None) or "").encode("utf-8", errors="replace")
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw)
        return re.sub(r"\s+", " ", text).strip()[:limit]
    except Exception:
        return ""


def _is_cloudflare_block_response(res: Any) -> bool:
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(getattr(res, "text", None) or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False


def _normalize_proxy(proxy: str | None) -> str:
    px = (proxy or "").strip()
    if not px:
        return ""
    if px.lower().startswith("socks5://"):
        px = "socks5h://" + px[len("socks5://") :]
    return px


def _request_with_retry(
    session: Any,
    method: str,
    url: str,
    *,
    log: LogFn | None = None,
    label: str = "",
    max_retries: int = 4,
    **kwargs: Any,
) -> Any:
    last_res = None
    for attempt in range(1, max_retries + 1):
        res = session.request(method, url, **kwargs)
        last_res = res
        if res.status_code not in (429, 503):
            return res
        retry_after = res.headers.get("Retry-After") or res.headers.get("retry-after")
        try:
            wait_s = min(float(retry_after), 30.0)
        except (TypeError, ValueError):
            wait_s = min(2**attempt, 20) + random.uniform(0.3, 1.2)
        if log:
            log(
                f"[nsfw] {label or url} HTTP {res.status_code}, "
                f"retry in {wait_s:.1f}s ({attempt}/{max_retries})"
            )
        time.sleep(wait_s)
    return last_res


def _set_tos_accepted(session: Any, log: LogFn | None = None) -> tuple[bool, str]:
    headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": getattr(xc_config, "CONNECT_ES_VERSION", "connect-es/2.1.1"),
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    data = encode_set_tos_accepted_body()
    try:
        res = _request_with_retry(
            session,
            "POST",
            SET_TOS_URL,
            data=data,
            headers=headers,
            timeout=15,
            log=log,
            label="set_tos_accepted",
        )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if _is_cloudflare_block_response(res):
            return False, f"set_tos_accepted blocked by Cloudflare, HTTP {res.status_code}"
        return False, f"set_tos_accepted HTTP {res.status_code}: {_response_preview(res)}"
    except Exception as e:  # noqa: BLE001
        return False, f"set_tos_accepted error: {e}"


def _set_birth_date(session: Any, log: LogFn | None = None) -> tuple[bool, str]:
    headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = _request_with_retry(
            session,
            "POST",
            SET_BIRTH_DATE_URL,
            json=payload,
            headers=headers,
            timeout=15,
            log=log,
            label="set_birth_date",
        )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if _is_cloudflare_block_response(res):
            return False, f"set_birth_date blocked by Cloudflare, HTTP {res.status_code}"
        return False, f"set_birth_date HTTP {res.status_code}: {_response_preview(res)}"
    except Exception as e:  # noqa: BLE001
        return False, f"set_birth_date error: {e}"


def _update_nsfw_settings(session: Any, log: LogFn | None = None) -> tuple[bool, str]:
    headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    data = encode_update_nsfw_body()
    try:
        res = _request_with_retry(
            session,
            "POST",
            UPDATE_NSFW_URL,
            data=data,
            headers=headers,
            timeout=15,
            log=log,
            label="update_nsfw",
        )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if _is_cloudflare_block_response(res):
            return False, f"update_nsfw blocked by Cloudflare, HTTP {res.status_code}"
        return False, f"update_nsfw HTTP {res.status_code}: {_response_preview(res)}"
    except Exception as e:  # noqa: BLE001
        return False, f"update_nsfw error: {e}"


def enable_nsfw_for_sso(
    sso: str,
    *,
    proxy: str | None = None,
    user_agent: str | None = None,
    cf_clearance: str | None = None,
    log: LogFn | None = None,
    impersonate: str = "chrome131",
) -> tuple[bool, str]:
    """Enable NSFW for an account identified by its ``sso`` JWT cookie.

    Returns ``(True, message)`` on success, ``(False, reason)`` on failure.
    """
    token = (sso or "").strip()
    if not token:
        return False, "empty sso token"

    try:
        from curl_cffi import requests as creq
    except ImportError as exc:
        return False, f"curl_cffi required: {exc}"

    ua = (user_agent or "").strip() or getattr(
        xc_config,
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    px = _normalize_proxy(proxy)
    session_kwargs: dict[str, Any] = {"impersonate": impersonate}
    if px:
        session_kwargs["proxy"] = px

    cookie_parts = [f"sso={token}", f"sso-rw={token}"]
    if cf_clearance:
        cookie_parts.append(f"cf_clearance={cf_clearance.strip()}")

    try:
        with creq.Session(**session_kwargs) as session:
            session.headers.update(
                {
                    "user-agent": ua,
                    "cookie": "; ".join(cookie_parts),
                }
            )
            ok, message = _set_tos_accepted(session, log)
            if not ok:
                return False, message
            time.sleep(random.uniform(0.8, 1.6))
            ok, message = _set_birth_date(session, log)
            if not ok:
                return False, message
            time.sleep(random.uniform(0.8, 1.6))
            ok, message = _update_nsfw_settings(session, log)
            if not ok:
                return False, message
            return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"error: {e}"


__all__ = [
    "NSFW_FEATURE_KEY",
    "encode_set_tos_accepted_body",
    "encode_update_nsfw_body",
    "enable_nsfw_for_sso",
    "generate_random_birthdate",
]
