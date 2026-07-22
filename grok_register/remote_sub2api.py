"""Push accounts into a remote sub2api (Wei-Shaw/sub2api) instance.

Admin API:
  POST /api/v1/auth/login
  GET  /api/v1/admin/groups
  POST /api/v1/admin/groups
  POST /api/v1/admin/accounts          platform=grok type=oauth
  POST /api/v1/admin/grok/sso-to-oauth
"""

from __future__ import annotations

import json
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


from .export_formats import account_auth_id, decode_jwt_payload, normalize_result

_DEFAULT_TIMEOUT = 45.0
_USER_AGENT = "grok-register-sub2api/1.0"


def default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "base_url": "",
        "email": "",
        "password": "",
        "group_id": None,
        "group_name": "grok-register",
        "auto_create_group": True,
        "auto_push_on_register": False,
        "concurrency": 4,
        "account_concurrency": 3,
        "account_priority": 50,
        "account_rate_multiplier": 1.0,
        "notes_prefix": "grok-register",
        "token": "",
        "token_expires_at": 0,
    }


def normalize_config(raw: Any) -> dict[str, Any]:
    base = default_config()
    if not isinstance(raw, dict):
        return base
    out = dict(base)
    out["enabled"] = bool(raw.get("enabled", False))
    out["base_url"] = str(raw.get("base_url") or raw.get("url") or "").strip().rstrip("/")
    out["email"] = str(raw.get("email") or raw.get("username") or "").strip()
    out["password"] = "" if raw.get("password") is None else str(raw.get("password"))
    gid = raw.get("group_id")
    if gid in (None, "", 0, "0"):
        out["group_id"] = None
    else:
        try:
            out["group_id"] = int(gid)
        except (TypeError, ValueError):
            out["group_id"] = None
    out["group_name"] = str(raw.get("group_name") or base["group_name"]).strip() or base["group_name"]
    out["auto_create_group"] = bool(raw.get("auto_create_group", True))
    auto = raw.get("auto_push_on_register")
    if auto is None:
        auto = raw.get("auto_import_on_register")
    out["auto_push_on_register"] = bool(auto)
    for k, lo, hi, default in (
        ("concurrency", 1, 16, 4),
        ("account_concurrency", 1, 100, 3),
        ("account_priority", 0, 100, 50),
    ):
        try:
            out[k] = max(lo, min(hi, int(raw.get(k) if raw.get(k) is not None else default)))
        except (TypeError, ValueError):
            out[k] = default
    try:
        out["account_rate_multiplier"] = max(
            0.1, min(10.0, float(raw.get("account_rate_multiplier") or 1.0))
        )
    except (TypeError, ValueError):
        out["account_rate_multiplier"] = 1.0
    out["notes_prefix"] = str(raw.get("notes_prefix") or base["notes_prefix"]).strip()
    out["token"] = str(raw.get("token") or "").strip()
    try:
        out["token_expires_at"] = float(raw.get("token_expires_at") or 0)
    except (TypeError, ValueError):
        out["token_expires_at"] = 0.0
    return out


def _urljoin(base: str, path: str) -> str:
    return urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[int, Any, str]:
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with _urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200) or 200)
            try:
                parsed = json.loads(raw) if raw.strip() else None
            except json.JSONDecodeError:
                parsed = raw
            return status, parsed, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            parsed = raw
        return int(e.code), parsed, raw
    except Exception as e:  # noqa: BLE001
        return 0, None, str(e)


def _api_error_message(status: int, parsed: Any, raw: str) -> str:
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if code not in (None, 0, "0", 200, "200"):
            msg = parsed.get("message") or parsed.get("msg") or parsed.get("error")
            if isinstance(msg, dict):
                msg = msg.get("message") or msg.get("msg") or str(msg)
            if msg:
                return f"HTTP {status}: {msg}"
            return f"HTTP {status}: code={code}"
        for k in ("message", "error", "detail", "msg"):
            v = parsed.get(k)
            if v and not isinstance(v, (dict, list)):
                return f"HTTP {status}: {v}"
    text = (raw or "").strip()
    if text:
        return f"HTTP {status}: {text[:300]}"
    return f"HTTP {status}"


def _unwrap_data(parsed: Any) -> Any:
    if not isinstance(parsed, dict):
        return parsed
    code = parsed.get("code")
    if code not in (None, 0, "0", 200, "200"):
        return parsed
    if "data" in parsed:
        return parsed.get("data")
    return parsed


def login(cfg: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    base = cfg.get("base_url") or ""
    if not base:
        raise ValueError("sub2api base_url is required")
    email = cfg.get("email") or ""
    password = cfg.get("password") or ""
    if not email or not password:
        raise ValueError("sub2api email/password is required")

    token = str(cfg.get("token") or "").strip()
    exp = float(cfg.get("token_expires_at") or 0)
    if not force and token and exp > time.time() + 60:
        return {"ok": True, "token": token, "cached": True, "expires_at": exp}

    status, parsed, raw = _http_json(
        "POST",
        _urljoin(base, "/api/v1/auth/login"),
        body={"email": email, "password": password},
        timeout=30,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(_api_error_message(status, parsed, raw) or "login failed")
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if code not in (None, 0, "0", 200, "200"):
            raise RuntimeError(_api_error_message(status, parsed, raw) or "login failed")
    data = _unwrap_data(parsed)
    if not isinstance(data, dict):
        data = parsed if isinstance(parsed, dict) else {}
    new_token = (
        data.get("access_token")
        or data.get("token")
        or (parsed.get("access_token") if isinstance(parsed, dict) else None)
        or (parsed.get("token") if isinstance(parsed, dict) else None)
        or ""
    )
    new_token = str(new_token).strip()
    if not new_token:
        raise RuntimeError(f"login response missing token: {raw[:200]}")
    expires_in = data.get("expires_in") if isinstance(data, dict) else None
    if expires_in is None and isinstance(parsed, dict):
        expires_in = parsed.get("expires_in")
    try:
        expires_in_f = float(expires_in if expires_in is not None else 12 * 3600)
    except (TypeError, ValueError):
        expires_in_f = 12 * 3600
    cfg["token"] = new_token
    cfg["token_expires_at"] = time.time() + max(60.0, expires_in_f)
    return {"ok": True, "token": new_token, "cached": False, "expires_at": cfg["token_expires_at"]}


def _request_authed(
    method: str,
    path: str,
    *,
    cfg: dict[str, Any],
    body: Any = None,
    timeout: float = _DEFAULT_TIMEOUT,
    retry_login: bool = True,
) -> tuple[int, Any, str]:
    base = cfg.get("base_url") or ""
    if not base:
        raise ValueError("sub2api base_url is required")
    auth = login(cfg, force=False)
    token = auth["token"]
    headers = {"Authorization": f"Bearer {token}"}
    status, parsed, raw = _http_json(
        method,
        _urljoin(base, path),
        headers=headers,
        body=body,
        timeout=timeout,
    )
    if status in (401, 403) and retry_login:
        auth = login(cfg, force=True)
        headers = {"Authorization": f"Bearer {auth['token']}"}
        status, parsed, raw = _http_json(
            method,
            _urljoin(base, path),
            headers=headers,
            body=body,
            timeout=timeout,
        )
    return status, parsed, raw


def list_groups(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    status, parsed, raw = _request_authed("GET", "/api/v1/admin/groups", cfg=cfg)
    if status < 200 or status >= 300:
        raise RuntimeError(_api_error_message(status, parsed, raw))
    data = _unwrap_data(parsed)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        items = data.get("items") or data.get("groups") or data.get("list") or []
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    return []


def create_group(name: str, *, cfg: dict[str, Any], platform: str = "grok") -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("group name is required")
    body = {
        "name": name,
        "platform": platform or "grok",
        "description": "created by grok-register",
        "rate_multiplier": 1.0,
        "is_exclusive": False,
    }
    status, parsed, raw = _request_authed("POST", "/api/v1/admin/groups", cfg=cfg, body=body)
    if status < 200 or status >= 300:
        raise RuntimeError(_api_error_message(status, parsed, raw))
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if code not in (None, 0, "0", 200, "200"):
            raise RuntimeError(_api_error_message(status, parsed, raw))
    data = _unwrap_data(parsed)
    return data if isinstance(data, dict) else {"raw": data}


def resolve_group_id(cfg: dict[str, Any]) -> int:
    if cfg.get("group_id"):
        return int(cfg["group_id"])
    name = str(cfg.get("group_name") or "").strip() or "grok-register"
    groups = list_groups(cfg)
    for g in groups:
        gname = str(g.get("name") or "").strip()
        if gname == name or gname.lower() == name.lower():
            gid = int(g["id"])
            cfg["group_id"] = gid
            return gid
    if not cfg.get("auto_create_group", True):
        raise RuntimeError(f"group not found: {name}")
    created = create_group(name, cfg=cfg)
    gid = created.get("id")
    if gid is None:
        for g in list_groups(cfg):
            if str(g.get("name") or "").strip() == name:
                gid = g.get("id")
                break
    if gid is None:
        raise RuntimeError(f"failed to create group {name}: {created}")
    cfg["group_id"] = int(gid)
    return int(gid)


def test_connection(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        auth = login(cfg, force=True)
        groups = list_groups(cfg)
        return {
            "ok": True,
            "cached": auth.get("cached"),
            "groups": len(groups),
            "group_names": [g.get("name") for g in groups[:10]],
            "message": f"sub2api 登录成功，{len(groups)} 个 group",
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def sso_to_oauth(sso_tokens: list[str], *, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    tokens = [str(x).strip() for x in sso_tokens if str(x).strip()]
    if not tokens:
        return []
    status, parsed, raw = _request_authed(
        "POST",
        "/api/v1/admin/grok/sso-to-oauth",
        cfg=cfg,
        body={"sso_tokens": tokens, "proxy_id": None},
        timeout=120,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(_api_error_message(status, parsed, raw))
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if code not in (None, 0, "0", 200, "200"):
            raise RuntimeError(_api_error_message(status, parsed, raw))
    data = _unwrap_data(parsed)
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data, list):
        return data
    return []


def create_grok_oauth_account(
    *,
    name: str,
    group_id: int,
    access_token: str,
    refresh_token: str = "",
    email: str = "",
    expires_at: str | None = None,
    notes: str = "",
    cfg: dict[str, Any],
) -> dict[str, Any]:
    credentials: dict[str, Any] = {"access_token": access_token, "email": email or ""}
    if refresh_token:
        credentials["refresh_token"] = refresh_token
    if expires_at:
        credentials["expires_at"] = expires_at
    body: dict[str, Any] = {
        "name": (name or email or "grok-account")[:200],
        "platform": "grok",
        "type": "oauth",
        "credentials": credentials,
        "extra": {},
        "proxy_id": None,
        "group_ids": [int(group_id)],
        "concurrency": int(cfg.get("account_concurrency") or 3),
        "priority": int(cfg.get("account_priority") if cfg.get("account_priority") is not None else 50),
        "rate_multiplier": float(cfg.get("account_rate_multiplier") or 1.0),
        "notes": notes or "",
    }
    status, parsed, raw = _request_authed(
        "POST", "/api/v1/admin/accounts", cfg=cfg, body=body, timeout=60
    )
    if status < 200 or status >= 300:
        raise RuntimeError(_api_error_message(status, parsed, raw))
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if code not in (None, 0, "0", 200, "200"):
            raise RuntimeError(_api_error_message(status, parsed, raw))
    data = _unwrap_data(parsed)
    return data if isinstance(data, dict) else {"raw": data}


def _expires_at_iso(n: dict[str, Any]) -> str | None:
    exp = n.get("expires_at")
    if exp is None:
        claims = decode_jwt_payload(str(n.get("access_token") or ""))
        exp = claims.get("exp")
    if exp is None:
        return None
    try:
        if isinstance(exp, str) and not exp.isdigit():
            return exp
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(exp)))
    except Exception:
        return None


def push_one(result: dict[str, Any], *, cfg: dict[str, Any], group_id: int | None = None) -> dict[str, Any]:
    """Push one registration/export result dict to sub2api."""
    n = normalize_result(result)
    email = n.get("email") or ""
    access = n.get("access_token") or ""
    refresh = n.get("refresh_token") or ""
    sso = n.get("sso") or ""
    aid = account_auth_id(n) if n.get("user_id") else (email or "unknown")
    notes = f"{cfg.get('notes_prefix') or 'grok-register'}:{aid}"
    name = email or aid
    gid = int(group_id or resolve_group_id(cfg))

    if access:
        try:
            created = create_grok_oauth_account(
                name=name,
                group_id=gid,
                access_token=access,
                refresh_token=refresh,
                email=email,
                expires_at=_expires_at_iso(n),
                notes=notes,
                cfg=cfg,
            )
            return {
                "ok": True,
                "account_id": aid,
                "email": email,
                "method": "oauth_token",
                "group_id": gid,
                "remote": {"id": created.get("id"), "name": created.get("name")},
            }
        except Exception as e:  # noqa: BLE001
            if not sso:
                return {
                    "ok": False,
                    "account_id": aid,
                    "email": email,
                    "error": str(e),
                    "method": "oauth_token",
                }
            token_err = str(e)
    else:
        token_err = "missing access_token"
        if not sso:
            return {
                "ok": False,
                "account_id": aid,
                "email": email,
                "error": token_err,
                "method": "none",
            }

    # SSO path (server-side convert or local device-flow already done preferred)
    try:
        results = sso_to_oauth([sso], cfg=cfg)
    except Exception as e:  # noqa: BLE001
        # Fallback: local device-flow then create
        try:
            from .sso_device import sso_to_token

            token = sso_to_token(sso, quiet=True)
            if not token or not token.get("access_token"):
                return {
                    "ok": False,
                    "account_id": aid,
                    "email": email,
                    "error": f"sso-to-oauth failed: {e}; local device-flow empty",
                    "method": "sso",
                }
            created = create_grok_oauth_account(
                name=email or name,
                group_id=gid,
                access_token=str(token.get("access_token") or ""),
                refresh_token=str(token.get("refresh_token") or ""),
                email=email,
                expires_at=None,
                notes=notes,
                cfg=cfg,
            )
            return {
                "ok": True,
                "account_id": aid,
                "email": email,
                "method": "sso_local_device",
                "group_id": gid,
                "remote": {"id": created.get("id"), "name": created.get("name")},
            }
        except Exception as e2:  # noqa: BLE001
            return {
                "ok": False,
                "account_id": aid,
                "email": email,
                "error": f"sso-to-oauth failed: {e}; local: {e2}",
                "method": "sso",
            }

    cred = None
    for r in results:
        if not isinstance(r, dict) or r.get("success") is False:
            continue
        c = r.get("credentials") if isinstance(r.get("credentials"), dict) else r
        if c.get("access_token") or c.get("AccessToken"):
            cred = c
            if not email:
                email = str(c.get("email") or r.get("email") or "").strip()
            break
    if not cred:
        return {
            "ok": False,
            "account_id": aid,
            "email": email,
            "error": "sso-to-oauth produced no credentials",
            "method": "sso",
        }
    try:
        created = create_grok_oauth_account(
            name=email or name,
            group_id=gid,
            access_token=str(cred.get("access_token") or cred.get("AccessToken") or ""),
            refresh_token=str(cred.get("refresh_token") or ""),
            email=email,
            expires_at=str(cred.get("expires_at") or "") or None,
            notes=notes,
            cfg=cfg,
        )
        return {
            "ok": True,
            "account_id": aid,
            "email": email,
            "method": "sso",
            "group_id": gid,
            "remote": {"id": created.get("id"), "name": created.get("name")},
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "account_id": aid,
            "email": email,
            "error": str(e),
            "method": "sso",
        }


def push_many(
    results: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    concurrency: int | None = None,
) -> dict[str, Any]:
    cfg = normalize_config(cfg)
    rows = [r for r in results if isinstance(r, dict)]
    if not rows:
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
    workers = max(1, min(16, workers, len(rows)))
    # Resolve group once (serial) before fan-out
    gid = resolve_group_id(cfg)
    out: list[dict[str, Any]] = []
    ok_n = fail_n = 0

    def _one(r: dict[str, Any]) -> dict[str, Any]:
        return push_one(r, cfg=cfg, group_id=gid)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_one, r) for r in rows]
        for fut in as_completed(futs):
            r = fut.result()
            out.append(r)
            if r.get("ok"):
                ok_n += 1
            else:
                fail_n += 1
    return {
        "ok": fail_n == 0,
        "total": len(rows),
        "success": ok_n,
        "failed": fail_n,
        "group_id": gid,
        "concurrency": workers,
        "results": out,
        "message": f"sub2api 导入完成：成功 {ok_n} / 失败 {fail_n} / 共 {len(rows)}",
    }
