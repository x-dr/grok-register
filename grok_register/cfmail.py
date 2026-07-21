"""HTTP client for dreamhunter2333/cloudflare_temp_email.

Uses Workers API origin (e.g. https://xxxxx.xyz), not Cloudflare D1 tokens.
Preferred automation path: admin password → POST /admin/new_address (x-admin-auth).
"""

from __future__ import annotations

import email
import os
import random
import re
import secrets
import time
from email import policy
from typing import Any
from urllib.parse import urlparse

import requests


def normalize_base_url(base_url: str | None = None) -> str:
    raw = (
        base_url
        or os.environ.get("CFMAIL_BASE_URL")
        or os.environ.get("GROK_REGISTER_CFMAIL_BASE_URL")
        or os.environ.get("GROK2API_CFMAIL_BASE_URL")
        or ""
    ).strip()
    if not raw:
        raise ValueError(
            "CFMAIL_BASE_URL 未设置（例如 https://xxxxx.xyz ）。"
            "填 Workers / 后端 API 域名，不要用 Pages 前端路径。"
        )
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}".rstrip("/")
    if not parsed.netloc:
        raise ValueError(f"无效的 CFMAIL_BASE_URL: {raw!r}")
    return origin


def _admin_password(explicit: str | None = None) -> str:
    return (
        explicit
        or os.environ.get("CFMAIL_ADMIN_PASSWORD")
        or os.environ.get("GROK_REGISTER_CFMAIL_ADMIN_PASSWORD")
        or os.environ.get("GROK2API_CFMAIL_ADMIN_PASSWORD")
        or os.environ.get("CFMAIL_API_KEY")  # alias
        or ""
    ).strip()


def _site_password(explicit: str | None = None) -> str:
    return (
        explicit
        or os.environ.get("CFMAIL_SITE_PASSWORD")
        or os.environ.get("GROK_REGISTER_CFMAIL_SITE_PASSWORD")
        or ""
    ).strip()


def _domain_pref(explicit: str | None = None) -> str:
    return (
        explicit
        or os.environ.get("CFMAIL_DOMAIN")
        or os.environ.get("GROK_REGISTER_CFMAIL_DOMAIN")
        or os.environ.get("ALIAS_MAIL_DOMAINS")  # first entry if multi
        or ""
    ).strip().split(",")[0].strip().lstrip("@").strip(".")


def _headers(
    *,
    api_key: str | None = None,
    site_password: str | None = None,
    content_type: bool = False,
    as_admin: bool | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    key = (api_key or "").strip()
    site = (site_password or "").strip()
    if key:
        parts = key.split(".")
        is_jwt = len(parts) == 3 and all(parts) and not key.startswith("http")
        if is_jwt and as_admin is not True:
            headers["Authorization"] = f"Bearer {key}"
        else:
            headers["x-admin-auth"] = key
            if not site:
                headers["x-custom-auth"] = key
    if site:
        headers["x-custom-auth"] = site
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def list_domains(
    *,
    base_url: str | None = None,
    admin_password: str | None = None,
    site_password: str | None = None,
) -> list[str]:
    base = normalize_base_url(base_url)
    site = _site_password(site_password) or _admin_password(admin_password)
    headers = _headers(site_password=site)
    try:
        resp = requests.get(f"{base}/open_api/settings", headers=headers, timeout=20)
        if resp.status_code >= 400:
            return []
        data = resp.json() if resp.content else {}
    except Exception:
        return []
    body = data if isinstance(data, dict) else {}
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        body = {**data, **data["data"]}
    out: list[str] = []
    seen: set[str] = set()
    for key in (
        "defaultDomains",
        "default_domains",
        "domains",
        "randomSubdomainDomains",
        "random_subdomain_domains",
    ):
        items = body.get(key)
        if isinstance(items, str):
            items = [x.strip() for x in items.split(",") if x.strip()]
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                name = item.get("domain") or item.get("name") or item.get("value")
            else:
                name = item
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip().lstrip("@").strip(".")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


def _parse_raw_rfc822(raw: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    text = (raw or "").strip()
    if not text:
        return out
    try:
        msg = email.message_from_string(text, policy=policy.default)
    except Exception:
        out["text"] = text[:8000]
        return out
    out["subject"] = str(msg.get("subject") or "")
    out["from"] = str(msg.get("from") or "")
    texts: list[str] = []
    htmls: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = str(part.get_content_disposition() or "").lower()
            if disp == "attachment":
                continue
            try:
                payload = part.get_content()
            except Exception:
                try:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        payload = payload.decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                except Exception:
                    payload = None
            if not isinstance(payload, str):
                continue
            if ctype == "text/html":
                htmls.append(payload)
            elif ctype.startswith("text/"):
                texts.append(payload)
    else:
        try:
            payload = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                payload = payload.decode(
                    msg.get_content_charset() or "utf-8", errors="replace"
                )
        if isinstance(payload, str):
            if (msg.get_content_type() or "").lower() == "text/html":
                htmls.append(payload)
            else:
                texts.append(payload)
    if texts:
        out["text"] = "\n".join(texts)
    if htmls:
        out["html"] = "\n".join(htmls)
    if not texts and not htmls:
        out["text"] = text[:8000]
    return out


def create_mailbox(
    *,
    base_url: str | None = None,
    admin_password: str | None = None,
    site_password: str | None = None,
    domain: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Create address; return {email, token(jwt), id, provider}."""
    base = normalize_base_url(base_url)
    key = _admin_password(admin_password)
    site = _site_password(site_password)
    if not key and not site:
        raise ValueError(
            "缺少 CF Temp Email 管理密码。设置 CFMAIL_ADMIN_PASSWORD "
            "（对应部署里的 ADMIN_PASSWORDS，请求头 x-admin-auth）。"
        )
    auth_key = key or site
    dom = _domain_pref(domain)
    if not dom:
        domains = list_domains(
            base_url=base, admin_password=auth_key, site_password=site or key
        )
        if not domains:
            raise ValueError(
                f"无法从 {base}/open_api/settings 拿到域名。"
                "请设置 CFMAIL_DOMAIN=你的收信域名，或检查站点密码。"
            )
        dom = random.choice(domains)

    local = (name or "").strip().lower()
    if not local:
        local = f"xai{secrets.token_hex(4)}"
    local = re.sub(r"[^a-z0-9._+-]", "", local) or f"xai{secrets.token_hex(4)}"

    payload_admin = {
        "name": local,
        "domain": dom,
        "enablePrefix": False,
        "enableRandomSubdomain": False,
    }
    payload_public = {
        "name": local,
        "domain": dom,
        "enableRandomSubdomain": False,
    }
    headers = _headers(
        api_key=auth_key,
        site_password=site,
        content_type=True,
        as_admin=True if key else None,
    )
    use_admin = "x-admin-auth" in headers
    last_err = ""
    resp = None
    if use_admin:
        resp = requests.post(
            f"{base}/admin/new_address",
            json=payload_admin,
            headers=headers,
            timeout=30,
        )
        if resp.status_code >= 400:
            last_err = f"admin/new_address {resp.status_code}: {resp.text[:300]}"
            pub_headers = _headers(
                site_password=site or key, content_type=True
            )
            resp = requests.post(
                f"{base}/api/new_address",
                json=payload_public,
                headers=pub_headers,
                timeout=30,
            )
    else:
        resp = requests.post(
            f"{base}/api/new_address",
            json=payload_public,
            headers=headers,
            timeout=30,
        )
    if resp is None or resp.status_code >= 400:
        detail = (resp.text[:500] if resp is not None else last_err)
        raise RuntimeError(
            f"CF Temp Email 创建失败 ({base}): {detail or last_err}。"
            "确认 Base URL 是 Workers API 源站，ADMIN_PASSWORDS 正确，"
            "域名在 /open_api/settings 里。"
        )
    try:
        data = resp.json() if resp.content else {}
    except Exception as e:
        raise RuntimeError(
            f"CF Temp Email create 返回非 JSON: {resp.text[:300]}"
        ) from e

    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected CF Temp Email create response: {data}")
    address = body.get("address") or body.get("email") or body.get("mail") or body.get("name")
    jwt = (
        body.get("jwt")
        or body.get("token")
        or body.get("credential")
        or body.get("address_jwt")
        or ""
    )
    address_id = body.get("address_id") or body.get("id") or body.get("addressId") or address
    if (not address or "@" not in str(address)) and jwt:
        try:
            sresp = requests.get(
                f"{base}/api/settings",
                headers=_headers(api_key=str(jwt)),
                timeout=20,
            )
            if sresp.status_code < 400:
                sdata = sresp.json() if sresp.content else {}
                sbody = (
                    sdata.get("data")
                    if isinstance(sdata, dict) and "data" in sdata
                    else sdata
                )
                if isinstance(sbody, dict):
                    address = sbody.get("address") or address
        except Exception:
            pass
    if not address or "@" not in str(address):
        raise RuntimeError(f"Unexpected CF Temp Email create response: {data}")
    if not jwt:
        raise RuntimeError(
            "创建成功但未返回 address JWT，无法收信。"
            "请用 ADMIN_PASSWORDS 走 /admin/new_address。"
        )
    return {
        "id": str(address_id or address),
        "email": str(address).strip(),
        "token": str(jwt),
        "provider": "cfmail",
        "base_url": base,
    }


def fetch_messages(
    *,
    token: str,
    base_url: str | None = None,
    site_password: str | None = None,
    include_details: bool = True,
) -> list[dict[str, Any]]:
    jwt = (token or "").strip()
    if not jwt:
        return []
    base = normalize_base_url(base_url)
    headers = _headers(api_key=jwt, site_password=_site_password(site_password))
    items: list[Any] = []
    used_parsed = False
    resp = requests.get(
        f"{base}/api/parsed_mails",
        headers=headers,
        params={"limit": 20, "offset": 0},
        timeout=30,
    )
    if resp.status_code < 400:
        data = resp.json() if resp.content else {}
        body = data.get("data") if isinstance(data, dict) and "data" in data else data
        if isinstance(body, dict):
            items = body.get("results") or body.get("mails") or body.get("items") or []
        elif isinstance(body, list):
            items = body
        used_parsed = True
    else:
        resp = requests.get(
            f"{base}/api/mails",
            headers=headers,
            params={"limit": 20, "offset": 0},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"CF Temp Email list failed {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json() if resp.content else {}
        body = data.get("data") if isinstance(data, dict) and "data" in data else data
        if isinstance(body, dict):
            items = body.get("results") or body.get("mails") or body.get("items") or []
        elif isinstance(body, list):
            items = body
    if not isinstance(items, list):
        return []

    out: list[dict[str, Any]] = []
    for raw in items[:20]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        msg_id = item.get("id") or item.get("mail_id") or item.get("message_id")
        if include_details and msg_id and not used_parsed:
            detail = requests.get(
                f"{base}/api/mail/{msg_id}", headers=headers, timeout=30
            )
            if detail.status_code == 200:
                d = detail.json() if detail.content else {}
                msg = d.get("data") if isinstance(d, dict) and "data" in d else d
                if isinstance(msg, dict):
                    item.update(msg)
        if not item.get("text") and not item.get("html"):
            raw_rfc = (
                item.get("raw")
                or item.get("source")
                or item.get("message")
                or item.get("content")
                or ""
            )
            if isinstance(raw_rfc, str) and ("\n" in raw_rfc or "From:" in raw_rfc):
                for k, v in _parse_raw_rfc822(raw_rfc).items():
                    item.setdefault(k, v)
        out.append(item)
    return out


def extract_xai_code(text: str) -> str | None:
    """Extract xAI style verification code from mail body."""
    if not text:
        return None
    m = re.search(r"\b([A-Z0-9]{3})-([A-Z0-9]{3})\b", text, flags=re.I)
    if m:
        return "".join(m.groups()).upper()
    m2 = re.search(r"\b([A-Z0-9]{6})\b", text, flags=re.I)
    if m2 and "x.ai" in text.lower():
        return m2.group(1).upper()
    # plain 6-digit
    m3 = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if m3:
        return m3.group(1)
    return None


class CfmailReceiver:
    """Receiver with wait_for_code(timeout) compatible with register flow."""

    def __init__(
        self,
        *,
        email: str,
        token: str,
        base_url: str,
        site_password: str | None = None,
    ) -> None:
        self.email = email
        self.token = token
        self.base_url = base_url
        self.site_password = site_password
        self._seen: set[str] = set()

    def wait_for_code(self, timeout: float = 120.0) -> str:
        deadline = time.time() + float(timeout)
        poll = 1.0
        while time.time() < deadline:
            try:
                messages = fetch_messages(
                    token=self.token,
                    base_url=self.base_url,
                    site_password=self.site_password,
                )
            except Exception:
                messages = []
            for item in messages:
                text = "\n".join(
                    str(item.get(k) or "")
                    for k in (
                        "subject",
                        "content",
                        "text",
                        "html",
                        "body",
                        "from",
                        "sender",
                        "raw",
                    )
                )
                key = str(item.get("id") or "") + "|" + text[:80]
                if key in self._seen:
                    continue
                self._seen.add(key)
                code = extract_xai_code(text)
                if code:
                    return code
            time.sleep(poll)
            poll = min(3.0, poll + 0.25)
        raise RuntimeError("timeout waiting for xAI email verification code (cfmail)")
