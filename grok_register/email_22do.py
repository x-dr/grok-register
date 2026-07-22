"""22.do Outlook temporary mailbox channel (ported from mytest/email_22do.py).

Public API mirrors cfmail:
  create_mailbox() -> {email, token, provider, ...}
  Do22Receiver(email=..., token=...).wait_for_code(timeout)

token format: "22do:<email>". 22.do does not require a password; knowing the
address is enough to re-login and read mail. Sessions are cached in-module.
"""

from __future__ import annotations

import re
import time
from email import policy
from email.parser import BytesParser
from typing import Any, Callable

from curl_cffi import requests as cffi_requests

from .proxyutil import proxies_dict, resolve_proxy_from_env

BASE = "https://22.do"
LANG = "zh"
IMPERSONATE = "chrome"

# email -> {"session": Session, "inbox_url": str}
_SESSIONS: dict[str, dict[str, Any]] = {}

_LogFn = Callable[[str], None] | None


def _proxy_kwargs(proxy: str | None = None) -> dict[str, Any]:
    p = (proxy or "").strip() or resolve_proxy_from_env()
    d = proxies_dict(p)
    if not d:
        return {}
    return {"proxies": d}


def _new_session(proxy: str | None = None):
    return cffi_requests.Session(impersonate=IMPERSONATE, **_proxy_kwargs(proxy))


# ---------------- HTML / eml parsing ----------------


def _decode_cf_email(hex_str: str) -> str:
    if not hex_str or len(hex_str) < 2:
        return ""
    key = int(hex_str[:2], 16)
    return "".join(
        chr(int(hex_str[i : i + 2], 16) ^ key)
        for i in range(2, len(hex_str), 2)
    )


def _text(html_frag: str) -> str:
    s = re.sub(
        r'<(?:a|span)\b[^>]*data-cfemail="([0-9a-fA-F]+)"[^>]*>[\s\S]*?</(?:a|span)>',
        lambda m: _decode_cf_email(m.group(1)),
        html_frag,
        flags=re.I,
    )
    s = re.sub(r"<script[\s\S]*?</script>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (
        s.replace("&quot;", '"')
        .replace("&#039;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&nbsp;", " ")
    )
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    return re.sub(r"\s+", " ", s).strip()


_ROW_RE = re.compile(
    r'<div class="tr">\s*'
    r'<div class="item subject"[^>]*viewEml\(\'([^\']+)\'\)[^>]*>([\s\S]*?)</div>\s*'
    r'<div class="item from">([\s\S]*?)</div>\s*'
    r'<div class="item time receive-time" data-bs-time="(\d+)">([\s\S]*?)</div>',
    re.I,
)


def _parse_inbox(html_text: str) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for m in _ROW_RE.finditer(html_text or ""):
        msgs.append(
            {
                "message_id": m.group(1),
                "subject": _text(m.group(2)),
                "from": _text(m.group(3)),
                "timestamp": int(m.group(4)),
            }
        )
    return msgs


def _parse_content(html_text: str) -> str:
    m = re.search(r"viewId:\s*'([^']+)'", html_text or "", re.I)
    return m.group(1) if m else ""


def _parse_eml(raw: bytes, limit: int = 2000) -> str:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    try:
        if msg.is_multipart():
            body = msg.get_body(preferencelist=("plain", "html"))
            text = body.get_content() if body else ""
        else:
            text = msg.get_content()
    except Exception:
        text = raw.decode("utf-8", errors="ignore")
    if not isinstance(text, str):
        text = str(text)
    return " ".join(text.split())[:limit]


# ---------------- verification code ----------------


_CODE_PATTERNS = (
    # subject: "LSQ-OPU xAI" style
    re.compile(r"(?i)^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI"),
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9])"),
    re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])"),
    re.compile(
        r"(?i)(?:code|otp|验证码|verification|verify)\s*[:：]?\s*"
        r"([A-Z0-9]{3}-[A-Z0-9]{3})"
    ),
    re.compile(
        r"(?i)(?:code|otp|验证码|verification|verify)\s*[:：]?\s*([A-Z0-9]{6})"
    ),
    re.compile(
        r"(?i)(?:verification|confirm(?:ation)?|your)\s+code[:\s]+(\d{4,8})"
    ),
)


def extract_xai_code(text: str, subject: str = "") -> str | None:
    """Extract x.ai-style verification code from subject + body."""
    for blob in (subject or "", text or ""):
        if not blob:
            continue
        for pat in _CODE_PATTERNS:
            m = pat.search(blob)
            if not m:
                continue
            raw = m.group(1) if m.groups() else m.group(0)
            # Prefer non-pure-digit alnum codes (current x.ai format).
            if raw.replace("-", "").isdigit() and "-" not in raw and len(raw) == 6:
                # still accept plain 6-digit if nothing else matches later
                continue
            return raw.upper()
    # fallback: plain 6-digit
    m3 = re.search(r"(?<!\d)(\d{6})(?!\d)", f"{subject}\n{text}")
    if m3:
        return m3.group(1)
    return None


# ---------------- 22.do operations ----------------


def _gen_email(s, *, prefer_hotmail: bool = True) -> str:
    email = None
    if prefer_hotmail:
        for _ in range(15):
            r = s.post(
                f"{BASE}/action/mailbox/microsoft",
                json={"type": "random"},
                headers={"origin": BASE, "referer": f"{BASE}/temporary-hotmail"},
                timeout=15,
            )
            r.raise_for_status()
            d = r.json()
            if d.get("status") and d.get("data", {}).get("domain") == "hotmail.com":
                email = d["data"]["email"]
                break
    if not email:
        r = s.post(
            f"{BASE}/action/mailbox/microsoft",
            json={"type": "random"},
            headers={"origin": BASE, "referer": f"{BASE}/temporary-hotmail"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        email = data.get("email")
    if not email:
        raise RuntimeError("22.do 未能生成临时邮箱")
    return str(email).strip()


def _login(s, email: str) -> str:
    r = s.post(
        f"{BASE}/action/mailbox/login",
        json={"email": email, "language": LANG},
        headers={"origin": BASE, "referer": f"{BASE}/temporary-hotmail"},
        timeout=15,
    )
    r.raise_for_status()
    redirect = (r.json() or {}).get("redirect", "") or ""
    return redirect.split("#")[0] or f"{BASE}/inbox/"


def _ensure_session(email: str, *, proxy: str | None = None) -> dict[str, Any]:
    cached = _SESSIONS.get(email)
    if cached and cached.get("session") and cached.get("inbox_url"):
        return cached
    s = _new_session(proxy)
    inbox_url = _login(s, email)
    entry = {"session": s, "inbox_url": inbox_url, "proxy": proxy or ""}
    _SESSIONS[email] = entry
    return entry


def _fetch_messages(email: str, *, proxy: str | None = None) -> list[dict[str, Any]]:
    cached = _ensure_session(email, proxy=proxy)
    s = cached["session"]
    inbox_url = cached["inbox_url"]
    html = s.get(inbox_url, headers={"referer": inbox_url}, timeout=15).text
    return _parse_inbox(html)


def _fetch_detail(
    email: str, message_id: str, *, proxy: str | None = None
) -> str:
    cached = _ensure_session(email, proxy=proxy)
    s = cached["session"]
    inbox_url = cached["inbox_url"]
    content_html = s.get(
        f"{BASE}/{LANG}/content/{message_id}",
        headers={"referer": inbox_url},
        timeout=15,
    ).text
    view_id = _parse_content(content_html)
    if not view_id:
        raise RuntimeError("22.do 未能解析邮件 viewId")
    eml_raw = s.post(
        f"{BASE}/action/mailbox/download",
        json={"viewId": view_id},
        headers={
            "origin": BASE,
            "referer": f"{BASE}/{LANG}/content/{message_id}",
        },
        timeout=15,
    ).content
    return _parse_eml(eml_raw, limit=2000)


# ---------------- public API ----------------


def create_mailbox(
    *,
    proxy: str | None = None,
    prefer_hotmail: bool = True,
    log: _LogFn = None,
) -> dict[str, Any]:
    """Create a 22.do Microsoft temp mailbox. Returns {email, token, provider}."""
    s = _new_session(proxy)
    email = _gen_email(s, prefer_hotmail=prefer_hotmail)
    inbox_url = _login(s, email)
    _SESSIONS[email] = {"session": s, "inbox_url": inbox_url, "proxy": proxy or ""}
    if log:
        log(f"22.do mailbox: {email}")
    return {
        "email": email,
        "token": f"22do:{email}",
        "provider": "22do",
        "inbox_url": inbox_url,
    }


class Do22Receiver:
    """Receiver with wait_for_code(timeout) compatible with register flow."""

    def __init__(
        self,
        *,
        email: str,
        token: str | None = None,
        proxy: str | None = None,
        poll_interval: float = 5.0,
        log: _LogFn = None,
    ) -> None:
        self.email = (email or "").strip()
        tok = (token or "").strip()
        if tok.startswith("22do:"):
            self.email = tok.split(":", 1)[1].strip() or self.email
        self.token = tok or (f"22do:{self.email}" if self.email else "")
        self.proxy = proxy
        self.poll_interval = float(poll_interval)
        self.log = log
        self._seen: set[str] = set()
        if self.email:
            try:
                _ensure_session(self.email, proxy=proxy)
            except Exception:
                pass

    def wait_for_code(self, timeout: float = 120.0) -> str:
        if not self.email:
            raise RuntimeError("22.do receiver has no email")
        deadline = time.time() + float(timeout)
        poll = max(2.0, float(self.poll_interval))
        while time.time() < deadline:
            try:
                messages = _fetch_messages(self.email, proxy=self.proxy)
            except Exception as exc:
                if self.log:
                    self.log(f"22.do list mail failed: {exc}")
                # force re-login next round
                _SESSIONS.pop(self.email, None)
                time.sleep(min(3.0, poll))
                continue
            for msg in messages:
                msg_id = str(msg.get("message_id") or "")
                if not msg_id or msg_id in self._seen:
                    continue
                self._seen.add(msg_id)
                subject = str(msg.get("subject") or "")
                if self.log:
                    self.log(f"22.do mail: {subject}")
                try:
                    body = _fetch_detail(self.email, msg_id, proxy=self.proxy)
                except Exception as exc:
                    if self.log:
                        self.log(f"22.do mail detail failed: {exc}")
                    continue
                code = extract_xai_code(f"{subject}\n{body}", subject=subject)
                if code:
                    if self.log:
                        self.log(f"22.do code: {code}")
                    return code
            time.sleep(poll)
            poll = min(8.0, poll + 0.5)
        raise RuntimeError(
            f"timeout waiting for xAI email verification code (22do / {self.email})"
        )


# Back-compat helpers matching mytest/email_22do.py public names
def get_email_and_token(
    config: dict | None = None,
    log_callback: _LogFn = None,
    cancel_callback=None,  # noqa: ARG001 — kept for API compat
) -> tuple[str, str]:
    proxy = ""
    if isinstance(config, dict):
        proxy = str(
            (config.get("register") or {}).get("proxy")
            or config.get("proxy")
            or ""
        )
    box = create_mailbox(proxy=proxy or None, log=log_callback)
    return box["email"], box["token"]


def get_verification_code(
    token: str,
    email: str,
    config: dict | None = None,
    timeout: float = 180,
    poll_interval: float = 7,
    log_callback: _LogFn = None,
    cancel_callback=None,  # noqa: ARG001
    resend_callback=None,  # noqa: ARG001
    max_polls: int | None = None,  # noqa: ARG001 — ignored; poll until timeout
) -> str:
    proxy = ""
    if isinstance(config, dict):
        proxy = str(
            (config.get("register") or {}).get("proxy")
            or config.get("proxy")
            or ""
        )
    receiver = Do22Receiver(
        email=email,
        token=token,
        proxy=proxy or None,
        poll_interval=poll_interval,
        log=log_callback,
    )
    return receiver.wait_for_code(timeout=timeout)
