"""Push accounts into a remote CLIProxyAPI management API.

  GET  /v0/management/auth-files
  POST /v0/management/auth-files?name=...
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

def _is_private_or_local_url(url: str) -> bool:
    """True if URL host is loopback / private — must not use outbound SOCKS."""
    try:
        from urllib.parse import urlparse
        import ipaddress
        host = (urlparse(url).hostname or "").strip().lower()
        if not host:
            return False
        if host in {"localhost", "0.0.0.0"} or host.endswith(".local"):
            return True
        try:
            ip = ipaddress.ip_address(host)
            return bool(ip.is_private or ip.is_loopback or ip.is_link_local)
        except ValueError:
            return False
    except Exception:
        return False


def _urlopen(req: "urllib.request.Request", timeout: float):
    """urlopen that skips HTTP(S)_PROXY for private/local management APIs."""
    import urllib.request
    url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
    if _is_private_or_local_url(url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


from .export_formats import to_cliproxyapi_record, normalize_result

_DEFAULT_TIMEOUT = 45.0
_USER_AGENT = "grok-register-cpa/1.0"


def default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "base_url": "",
        "management_key": "",
        "auto_push_on_register": False,
        "concurrency": 4,
        "auth_type": "xai",
        "base_upstream": "https://cli-chat-proxy.grok.com/v1",
        "notes_prefix": "grok-register",
    }


def normalize_config(raw: Any) -> dict[str, Any]:
    base = default_config()
    if not isinstance(raw, dict):
        return base
    out = dict(base)
    out["enabled"] = bool(raw.get("enabled", False))
    out["base_url"] = str(raw.get("base_url") or raw.get("url") or "").strip().rstrip("/")
    out["management_key"] = str(
        raw.get("management_key") or raw.get("api_key") or raw.get("key") or ""
    ).strip()
    auto = raw.get("auto_push_on_register")
    if auto is None:
        auto = raw.get("auto_import_on_register")
    out["auto_push_on_register"] = bool(auto)
    try:
        out["concurrency"] = max(1, min(16, int(raw.get("concurrency") or 4)))
    except (TypeError, ValueError):
        out["concurrency"] = 4
    out["auth_type"] = str(raw.get("auth_type") or "xai").strip().lower() or "xai"
    out["base_upstream"] = str(
        raw.get("base_upstream") or raw.get("upstream") or base["base_upstream"]
    ).strip()
    out["notes_prefix"] = str(raw.get("notes_prefix") or base["notes_prefix"]).strip()
    return out


def _urljoin(base: str, path: str) -> str:
    return urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _auth_headers(cfg: dict[str, Any]) -> dict[str, str]:
    key = str(cfg.get("management_key") or "").strip()
    if not key:
        raise ValueError("未配置 CLIProxyAPI management key")
    return {
        "Authorization": f"Bearer {key}",
        "X-Management-Key": key,
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method.upper())
    try:
        with _urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = int(getattr(resp, "status", 200) or 200)
            return status, body, dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp else b""
        return int(e.code), body, {}
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode("utf-8", errors="replace"), {}


def _safe_email_filename(email: str) -> str:
    raw = (email or "").strip().lower()
    if not raw:
        return "unknown"
    safe = re.sub(r"[^a-z0-9@._+-]+", "_", raw).strip("._") or "unknown"
    return safe[:180]


def _record_filename(record: dict[str, Any]) -> str:
    email = str(record.get("email") or "").strip()
    safe = _safe_email_filename(email)
    lower = safe.lower()
    if lower.startswith("xai-") or lower.startswith("xai_") or lower.startswith("xai"):
        fname = safe
    else:
        t = str(record.get("type") or "xai").strip().lower() or "xai"
        fname = f"xai-{safe}" if t in ("xai", "grok", "x-ai", "x.ai") else f"{t}-{safe}"
    if not fname.endswith(".json"):
        fname = f"{fname}.json"
    return fname


def test_connection(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = normalize_config(cfg)
    base = cfg.get("base_url") or ""
    if not base:
        return {"ok": False, "error": "请填写 CLIProxyAPI URL（如 http://127.0.0.1:8317）"}
    try:
        headers = _auth_headers(cfg)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    url = _urljoin(base, "/v0/management/auth-files")
    code, body, _ = _http("GET", url, headers=headers, timeout=15.0)
    text = body.decode("utf-8", errors="replace") if body else ""
    if code >= 400 or code == 0:
        return {"ok": False, "error": f"HTTP {code}: {text[:300]}", "url": url}
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {}
    files = []
    if isinstance(data, dict):
        files = data.get("files") or data.get("auths") or []
    n = len(files) if isinstance(files, list) else 0
    return {
        "ok": True,
        "status_code": code,
        "url": url,
        "auth_files": n,
        "message": f"连接成功，CPA 当前约 {n} 个 auth 文件",
    }


def _upload_one(cfg: dict[str, Any], *, filename: str, record: dict[str, Any]) -> dict[str, Any]:
    base = cfg.get("base_url") or ""
    headers = _auth_headers(cfg)
    raw = json.dumps(record, ensure_ascii=False).encode("utf-8")
    q = urllib.parse.urlencode({"name": filename})
    url = _urljoin(base, f"/v0/management/auth-files?{q}")
    hdrs = dict(headers)
    hdrs["Content-Type"] = "application/json"
    code, body, _ = _http("POST", url, headers=hdrs, data=raw, timeout=_DEFAULT_TIMEOUT)
    text = body.decode("utf-8", errors="replace") if body else ""
    if code >= 400 or code == 0:
        boundary = f"----gr{int(time.time() * 1000)}"
        parts = [
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: application/json\r\n\r\n"
            ).encode("utf-8")
            + raw
            + b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
        mbody = b"".join(parts)
        murl = _urljoin(base, "/v0/management/auth-files")
        mhdrs = dict(headers)
        mhdrs["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        code2, body2, _ = _http("POST", murl, headers=mhdrs, data=mbody, timeout=_DEFAULT_TIMEOUT)
        text2 = body2.decode("utf-8", errors="replace") if body2 else ""
        if code2 >= 400 or code2 == 0:
            return {
                "ok": False,
                "filename": filename,
                "error": f"HTTP {code}: {text[:200]} | multipart HTTP {code2}: {text2[:200]}",
                "status_code": code2,
            }
        return {"ok": True, "filename": filename, "status_code": code2, "via": "multipart"}
    return {"ok": True, "filename": filename, "status_code": code, "via": "json-body"}


def push_one(result: dict[str, Any], *, cfg: dict[str, Any]) -> dict[str, Any]:
    n = normalize_result(result)
    rec = to_cliproxyapi_record(n, base_url=str(cfg.get("base_upstream") or ""))
    if not rec:
        return {
            "ok": False,
            "email": n.get("email"),
            "error": "missing access_token (需要 OAuth / device-flow 后的 token)",
        }
    pref = str(cfg.get("auth_type") or "xai").strip().lower() or "xai"
    if pref in ("xai", "grok", "x-ai", "x.ai"):
        rec["type"] = "xai"
    if cfg.get("base_upstream"):
        rec["base_url"] = str(cfg.get("base_upstream"))
    note_prefix = str(cfg.get("notes_prefix") or "grokcli-2api")
    # Prefer local_account_id-style note: grokcli-2api:https://auth.x.ai::<sub>
    if rec.get("local_account_id"):
        rec["note"] = f"{note_prefix}:{rec['local_account_id']}"
    else:
        rec["note"] = f"{note_prefix}:{n.get('user_id') or n.get('email') or ''}"
    fname = _record_filename(rec)
    try:
        r = _upload_one(cfg, filename=fname, record=rec)
        r["email"] = rec.get("email")
        return r
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "filename": fname, "email": rec.get("email"), "error": str(e)[:300]}


def push_many(
    results: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    concurrency: int | None = None,
) -> dict[str, Any]:
    cfg = normalize_config(cfg)
    if not cfg.get("base_url"):
        raise ValueError("请先配置 cliproxyapi.base_url")
    if not cfg.get("management_key"):
        raise ValueError("请先配置 cliproxyapi.management_key")
    rows = [r for r in results if isinstance(r, dict)]
    jobs = rows
    if not jobs:
        return {
            "ok": True,
            "total": 0,
            "success": 0,
            "failed": 0,
            "results": [],
            "message": "没有可推送的账号",
        }
    try:
        workers = int(concurrency if concurrency is not None else cfg.get("concurrency") or 4)
    except (TypeError, ValueError):
        workers = 4
    workers = max(1, min(16, workers, len(jobs)))
    results_out: list[dict[str, Any]] = []
    ok_n = fail_n = 0

    def _one(r: dict[str, Any]) -> dict[str, Any]:
        return push_one(r, cfg=cfg)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_one, r) for r in jobs]
        for fut in as_completed(futs):
            r = fut.result()
            results_out.append(r)
            if r.get("ok"):
                ok_n += 1
            else:
                fail_n += 1
    return {
        "ok": fail_n == 0,
        "total": len(jobs),
        "success": ok_n,
        "failed": fail_n,
        "concurrency": workers,
        "results": results_out,
        "message": f"CLIProxyAPI 导入完成：成功 {ok_n} / 失败 {fail_n} / 共 {len(jobs)}",
    }
