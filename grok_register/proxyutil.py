"""Proxy helpers for SOCKS5 / HTTP proxies used by grok-register.

Common failure mode: config sets ``proxy: "socks5://127.0.0.1:40000"`` (WARP),
which is written to ``HTTPS_PROXY``. Then:

1. ``requests`` needs **PySocks** for socks schemes — without it every request
   raises ``Missing dependencies for SOCKS support`` (looks like "no network").
2. Local services (captcha solver ``127.0.0.1:5072``, sub2api on LAN) must not
   go through the proxy — we set a sensible ``NO_PROXY``.
3. Prefer ``socks5h://`` so DNS is resolved via the proxy (needed for many
   SOCKS/WARP setups).
"""

from __future__ import annotations

import os
import socket
from typing import Any
from urllib.parse import urlparse

# Hosts / patterns that must never use the outbound proxy.
# Note: Python urllib/requests NO_PROXY does NOT honor CIDR ranges.
# List exact hosts (or suffixes like .local). For LAN sub2api/CLIProxyAPI,
# remote_* helpers also force-bypass when base_url is private/loopback.
DEFAULT_NO_PROXY = "localhost,127.0.0.1,::1,0.0.0.0,.local,*.local,*.localhost"


def is_socks_proxy(url: str | None) -> bool:
    u = (url or "").strip().lower()
    return u.startswith("socks5://") or u.startswith("socks5h://") or u.startswith(
        "socks4://"
    ) or u.startswith("socks4a://") or u.startswith("socks://")


def normalize_proxy(url: str | None, *, prefer_socks5h: bool = True) -> str:
    """Strip + normalize a proxy URL.

    - empty → ""
    - ``socks5://`` → ``socks5h://`` when *prefer_socks5h* (DNS via proxy)
    - bare ``host:port`` is left unchanged (caller should use full URL)
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if prefer_socks5h and raw.lower().startswith("socks5://"):
        # socks5h = remote DNS through proxy (libcurl / PySocks convention)
        return "socks5h://" + raw[len("socks5://") :]
    return raw


def has_socks_support() -> bool:
    """True if ``requests`` can use SOCKS proxies (PySocks installed)."""
    try:
        import socks  # noqa: F401  # type: ignore

        return True
    except Exception:
        return False


def proxies_dict(url: str | None) -> dict[str, str]:
    """``{"http": url, "https": url}`` for requests / curl_cffi, or {}."""
    p = normalize_proxy(url)
    if not p:
        return {}
    return {"http": p, "https": p}


def ensure_no_proxy(*, extra: str | None = None) -> str:
    """Merge default local/LAN bypass into ``NO_PROXY`` / ``no_proxy``."""
    parts: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        cur = (os.environ.get(key) or "").strip()
        if cur:
            parts.extend(x.strip() for x in cur.split(",") if x.strip())
    parts.extend(x.strip() for x in DEFAULT_NO_PROXY.split(",") if x.strip())
    if extra:
        parts.extend(x.strip() for x in extra.split(",") if x.strip())
    # de-dupe case-insensitively, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    merged = ",".join(out)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged
    return merged


def apply_proxy_env(url: str | None, *, force: bool = False) -> str:
    """Normalize *url* and write HTTP(S)/ALL_PROXY + NO_PROXY.

    Returns the normalized proxy string (may be empty).
    """
    p = normalize_proxy(url)
    if not p:
        return ""
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        if force or not (os.environ.get(key) or "").strip():
            os.environ[key] = p
    ensure_no_proxy()
    return p


def resolve_proxy_from_env() -> str:
    for key in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        v = (os.environ.get(key) or "").strip()
        if v:
            return normalize_proxy(v)
    return ""


def socks_dependency_error(url: str | None) -> str | None:
    """Human-readable error if SOCKS proxy is configured without PySocks."""
    p = normalize_proxy(url) or resolve_proxy_from_env()
    if not p or not is_socks_proxy(p):
        return None
    if has_socks_support():
        return None
    return (
        f"已配置 SOCKS 代理 ({p})，但未安装 PySocks。\n"
        "  请执行: pip install PySocks\n"
        "  或:     pip install 'requests[socks]'\n"
        "  否则 requests 会报 Missing dependencies for SOCKS support（表现为没网）。"
    )


def parse_proxy_host_port(url: str | None) -> tuple[str, int] | None:
    p = normalize_proxy(url)
    if not p:
        return None
    # urlparse needs scheme; socks5h is fine
    try:
        u = urlparse(p if "://" in p else f"http://{p}")
    except Exception:
        return None
    host = u.hostname
    if not host:
        return None
    port = u.port
    if port is None:
        # common defaults
        scheme = (u.scheme or "").lower()
        port = 1080 if scheme.startswith("socks") else 8080
    return host, int(port)


def probe_proxy(url: str | None, *, timeout: float = 2.0) -> tuple[bool, str]:
    """TCP connect to proxy listen address. Does not verify SOCKS handshake."""
    p = normalize_proxy(url)
    if not p:
        return True, "no proxy"
    parsed = parse_proxy_host_port(p)
    if not parsed:
        return False, f"无法解析代理地址: {p}"
    host, port = parsed
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} 可达"
    except OSError as e:
        return False, (
            f"无法连接代理 {host}:{port} ({e}).\n"
            "  若使用 WARP: sudo bash scripts/install-warp-proxy.sh --status\n"
            "  确认 warp-cli 为 proxy 模式且已 connect，端口与配置一致。"
        )


def format_proxy_startup_lines(url: str | None) -> list[str]:
    """Lines suitable for CLI banner."""
    p = normalize_proxy(url) or resolve_proxy_from_env()
    if not p:
        return ["  proxy: (none)"]
    lines = [f"  proxy: {p}"]
    dep = socks_dependency_error(p)
    if dep:
        lines.append(f"  WARNING: {dep.replace(chr(10), ' | ')}")
    ok, msg = probe_proxy(p)
    if ok:
        lines.append(f"  proxy-probe: OK ({msg})")
    else:
        lines.append(f"  proxy-probe: FAIL — {msg.replace(chr(10), ' | ')}")
    return lines


def requests_kwargs_for_proxy(url: str | None = None) -> dict[str, Any]:
    """Kwargs to pass into ``requests.*``: proxies dict when configured."""
    p = normalize_proxy(url) if url is not None else resolve_proxy_from_env()
    d = proxies_dict(p)
    return {"proxies": d} if d else {}
