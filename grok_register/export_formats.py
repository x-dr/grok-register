"""Multi-format account exporters for registration results.

Formats (written under ``export_dir/<format>/``):
  - auth       : grokcli-2api / pool style ``{"auth": { "https://auth.x.ai::<uid>": {...}}}``
  - sub2api    : sub2api import ``{"type":"sub2api-data","accounts":[...]}``
  - cliproxyapi: CLIProxyAPI per-file ``xai-<email>.json`` + optional bundle
  - bundle     : one combined JSON per account (accounts_output style)
  - sso        : simple list ``[{email,password,sso}, ...]`` plus
                 ``sso-list-<ts>.txt`` (email----password----sso) and ``sso_<ts>.txt`` (SSO only)
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_OIDC_ISSUER = "https://auth.x.ai"
DEFAULT_OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
CLIPROXYAPI_GROK_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CLIPROXYAPI_GROK_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
}

ALL_FORMATS = ("auth", "sub2api", "cliproxyapi", "bundle", "sso")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def decode_jwt_payload(token: str | None) -> dict[str, Any]:
    raw = (token or "").strip()
    if not raw or raw.count(".") < 2:
        return {}
    try:
        part = raw.split(".")[1]
        pad = "=" * (-len(part) % 4)
        data = base64.urlsafe_b64decode(part + pad)
        obj = json.loads(data.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _safe_email(email: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._@+-]+", "_", (email or "unknown").strip()) or "unknown"
    return s[:120]


def _as_unix_exp(value: Any, access_token: str = "") -> int | None:
    if value is None or value == "":
        claims = decode_jwt_payload(access_token)
        if claims.get("exp") is not None:
            try:
                return int(claims["exp"])
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
        # ISO
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        claims = decode_jwt_payload(access_token)
        try:
            return int(claims["exp"]) if claims.get("exp") is not None else None
        except Exception:
            return None


def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize a register_one result (or account bundle / auth entry) to a common shape."""
    if not isinstance(result, dict):
        return {}

    # Already an auth-map entry?
    access = (
        result.get("oauth_access_token")
        or result.get("access_token")
        or result.get("key")
        or result.get("token")
        or ""
    )
    refresh = result.get("oauth_refresh_token") or result.get("refresh_token") or ""
    email = str(result.get("email") or "").strip()
    password = str(
        result.get("password") or result.get("register_password") or ""
    ).strip()
    sso = str(
        result.get("sso") or result.get("sso_cookie") or result.get("sso_token") or ""
    ).strip()

    claims = decode_jwt_payload(str(access)) if access else {}
    sso_claims = decode_jwt_payload(sso) if sso else {}

    user_id = str(
        result.get("user_id")
        or result.get("principal_id")
        or claims.get("principal_id")
        or claims.get("sub")
        or ""
    ).strip()
    team_id = str(result.get("team_id") or claims.get("team_id") or "").strip()
    client_id = str(
        result.get("oidc_client_id")
        or claims.get("client_id")
        or DEFAULT_OIDC_CLIENT_ID
    ).strip()
    issuer = str(result.get("oidc_issuer") or claims.get("iss") or DEFAULT_OIDC_ISSUER).strip()

    expires_at = _as_unix_exp(result.get("expires_at"), str(access))
    if expires_at is None and claims.get("exp") is not None:
        try:
            expires_at = int(claims["exp"])
        except Exception:
            expires_at = None

    create_time = result.get("create_time")
    if not create_time:
        iat = claims.get("iat")
        try:
            ts = float(iat) if iat is not None else time.time()
            create_time = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except Exception:
            create_time = _utc_now_iso()

    if not email and claims.get("email"):
        email = str(claims.get("email"))

    session_id = ""
    if sso_claims.get("session_id"):
        session_id = str(sso_claims["session_id"])

    return {
        "email": email,
        "password": password,
        "sso": sso,
        "access_token": str(access or "").strip(),
        "refresh_token": str(refresh or "").strip(),
        "user_id": user_id,
        "team_id": team_id,
        "oidc_client_id": client_id,
        "oidc_issuer": issuer,
        "expires_at": expires_at,
        "create_time": create_time,
        "session_id": session_id,
        "registration_batch_id": result.get("registration_batch_id") or "",
        "registration_session_id": result.get("registration_session_id") or "",
        "source": result.get("source") or "grok-register",
        "error": result.get("error"),
        "raw": result,
    }


def account_auth_id(n: dict[str, Any]) -> str:
    uid = n.get("user_id") or "unknown"
    issuer = n.get("oidc_issuer") or DEFAULT_OIDC_ISSUER
    return f"{issuer}::{uid}"


def to_auth_entry(n: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Build one grokcli-2api auth-map entry. Requires access_token or at least sso+email."""
    email = n.get("email") or ""
    access = n.get("access_token") or ""
    sso = n.get("sso") or ""
    if not access and not sso:
        return None
    if not n.get("user_id") and access:
        claims = decode_jwt_payload(access)
        n = dict(n)
        n["user_id"] = str(claims.get("principal_id") or claims.get("sub") or "").strip()
    aid = account_auth_id(n)
    entry: dict[str, Any] = {
        "auth_mode": "oidc",
        "create_time": n.get("create_time") or _utc_now_iso(),
        "email": email,
        "id": aid,
        "oidc_client_id": n.get("oidc_client_id") or DEFAULT_OIDC_CLIENT_ID,
        "oidc_issuer": n.get("oidc_issuer") or DEFAULT_OIDC_ISSUER,
        "principal_type": "User",
        "source": n.get("source") or "grok-register",
    }
    if n.get("user_id"):
        entry["user_id"] = n["user_id"]
        entry["principal_id"] = n["user_id"]
    if access:
        entry["key"] = access
    if n.get("refresh_token"):
        entry["refresh_token"] = n["refresh_token"]
    if n.get("expires_at") is not None:
        entry["expires_at"] = n["expires_at"]
    if n.get("password"):
        entry["password"] = n["password"]
        entry["register_password"] = n["password"]
    if sso:
        entry["sso"] = sso
        entry["sso_cookie"] = sso
    if n.get("team_id"):
        entry["team_id"] = n["team_id"]
    if n.get("registration_batch_id"):
        entry["registration_batch_id"] = n["registration_batch_id"]
    if n.get("registration_session_id"):
        entry["registration_session_id"] = n["registration_session_id"]
    return aid, entry


def to_sub2api_account(n: dict[str, Any], *, concurrency: int = 3, priority: int = 50) -> dict[str, Any] | None:
    email = n.get("email") or "unknown"
    access = n.get("access_token") or ""
    refresh = n.get("refresh_token") or ""
    sso = n.get("sso") or ""
    if not access and not sso:
        return None
    aid = account_auth_id(n) if n.get("user_id") else ""
    acc: dict[str, Any] = {
        "name": email,
        "notes": f"grok-register:{aid}" if aid else "grok-register",
        "platform": "grok",
        "type": "oauth" if access else "sso",
        "credentials": {},
        "extra": {
            "email": email,
            "source": n.get("source") or "grok-register",
        },
        "concurrency": concurrency,
        "priority": priority,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }
    if access:
        acc["credentials"] = {
            "access_token": access,
            "email": email,
            "refresh_token": refresh,
        }
    else:
        acc["credentials"] = {
            "email": email,
            "sso": sso,
            "password": n.get("password") or "",
        }
    if aid:
        acc["extra"]["local_account_id"] = aid
    if n.get("expires_at") is not None:
        acc["expires_at"] = n["expires_at"]
    if n.get("password"):
        acc["extra"]["password"] = n["password"]
    if sso:
        acc["extra"]["sso"] = sso
    return acc


def to_cliproxyapi_record(n: dict[str, Any], *, base_url: str = CLIPROXYAPI_GROK_BASE_URL) -> dict[str, Any] | None:
    """Build CLIProxyAPI / grokcli-2api compatible xAI OAuth auth JSON.

    Shape matches:
      type/auth_kind/email/sub/access_token/refresh_token/id_token/token_type
      expired/last_refresh/base_url/disabled/headers
      local_account_id/account_id/note
    """
    access = n.get("access_token") or ""
    if not access:
        return None
    exp = n.get("expires_at")
    expired_iso = ""
    if exp is not None:
        try:
            expired_iso = datetime.fromtimestamp(float(exp), tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except Exception:
            expired_iso = ""
    claims = decode_jwt_payload(access)
    sub = str(
        n.get("user_id")
        or claims.get("sub")
        or claims.get("principal_id")
        or ""
    ).strip()
    issuer = str(n.get("oidc_issuer") or claims.get("iss") or DEFAULT_OIDC_ISSUER).rstrip("/")
    local_account_id = f"{issuer}::{sub}" if sub else ""
    account_id = sub
    note = f"grokcli-2api:{local_account_id}" if local_account_id else "grokcli-2api"
    return {
        "type": "xai",
        "auth_kind": "oauth",
        "email": n.get("email") or "",
        "sub": sub,
        "access_token": access,
        "refresh_token": n.get("refresh_token") or "",
        "id_token": "",
        "token_type": "Bearer",
        "expired": expired_iso,
        "last_refresh": _utc_now_iso(),
        "base_url": base_url or CLIPROXYAPI_GROK_BASE_URL,
        "disabled": False,
        "headers": dict(CLIPROXYAPI_GROK_HEADERS),
        "local_account_id": local_account_id,
        "account_id": account_id,
        "note": note,
    }


def build_auth_payload(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    auth: dict[str, Any] = {}
    for r in results:
        n = normalize_result(r)
        if n.get("error") and not n.get("access_token") and not n.get("sso"):
            continue
        pair = to_auth_entry(n)
        if not pair:
            continue
        aid, entry = pair
        auth[aid] = entry
    return {
        "auth": auth,
        "count": len(auth),
        "exported_at": _utc_now_iso(),
        "source": "grok-register",
    }


def build_sub2api_payload(
    results: Iterable[dict[str, Any]],
    *,
    concurrency: int = 3,
    priority: int = 50,
) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    for r in results:
        n = normalize_result(r)
        if n.get("error") and not n.get("access_token") and not n.get("sso"):
            continue
        acc = to_sub2api_account(n, concurrency=concurrency, priority=priority)
        if acc:
            accounts.append(acc)
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": _utc_now_iso(),
        "proxies": [],
        "accounts": accounts,
        "source": "grok-register",
    }


def build_cliproxyapi_bundle(
    results: Iterable[dict[str, Any]],
    *,
    base_url: str = CLIPROXYAPI_GROK_BASE_URL,
) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    for r in results:
        n = normalize_result(r)
        rec = to_cliproxyapi_record(n, base_url=base_url)
        if rec:
            accounts.append(rec)
    return {
        "type": "cliproxyapi-auth-bundle",
        "version": 1,
        "exported_at": _utc_now_iso(),
        "source": "grok-register",
        "accounts": accounts,
    }


def build_sso_list(results: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in results:
        n = normalize_result(r)
        if not n.get("sso") and not n.get("email"):
            continue
        if n.get("error") and not n.get("sso"):
            continue
        out.append(
            {
                "email": n.get("email") or "",
                "password": n.get("password") or "",
                "sso": n.get("sso") or "",
                "user_id": n.get("user_id") or "",
            }
        )
    return out


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _format_dir(export_dir: Path, fmt: str) -> Path:
    """Return ``export_dir/<fmt>/`` and ensure it exists."""
    d = Path(export_dir) / fmt
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def export_results(
    results: list[dict[str, Any]],
    *,
    formats: list[str] | None = None,
    export_dir: str | Path = "./exports",
    accounts_output_dir: str | Path | None = None,
    cliproxyapi_auth_dir: str | Path | None = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    stamp: str | None = None,
) -> dict[str, Any]:
    """Write selected formats under type subfolders; return {format: path_or_paths}.

    Layout (under ``export_dir``)::

        auth/auth.json, auth/auth-<ts>.json
        sub2api/sub2api-data-<ts>.json
        cliproxyapi/cliproxyapi-bundle-<ts>.json
        cliproxyapi/auth/xai-*.json          (when cliproxyapi_auth_dir is omitted)
        bundle/account_*.json                (when accounts_output_dir is omitted)
        sso/sso-list-<ts>.json|.txt, sso/sso_<ts>.txt
    """
    wanted = [f.strip().lower() for f in (formats or list(ALL_FORMATS)) if f and f.strip()]
    wanted = [f for f in wanted if f in ALL_FORMATS]
    if not wanted:
        wanted = list(ALL_FORMATS)

    # Keep successful-ish rows
    rows = [r for r in results if isinstance(r, dict)]
    out_dir = Path(export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    written: dict[str, Any] = {"export_dir": str(out_dir), "stamp": ts, "files": {}}

    if "auth" in wanted:
        payload = build_auth_payload(rows)
        auth_dir = _format_dir(out_dir, "auth")
        path = _write_json(auth_dir / f"auth-{ts}.json", payload)
        # also maintain a rolling auth.json merge (prefer type dir; fall back to legacy flat path)
        rolling = auth_dir / "auth.json"
        existing = _load_json_dict(rolling)
        if not existing.get("auth"):
            legacy = _load_json_dict(out_dir / "auth.json")
            if legacy.get("auth"):
                existing = legacy
        auth_map = dict(existing.get("auth") or {}) if isinstance(existing, dict) else {}
        auth_map.update(payload.get("auth") or {})
        _write_json(
            rolling,
            {
                "auth": auth_map,
                "count": len(auth_map),
                "exported_at": _utc_now_iso(),
                "source": "grok-register",
            },
        )
        written["files"]["auth"] = str(path)
        written["files"]["auth_rolling"] = str(rolling)
        written["files"]["auth_dir"] = str(auth_dir)

    if "sub2api" in wanted:
        payload = build_sub2api_payload(rows)
        sub_dir = _format_dir(out_dir, "sub2api")
        path = _write_json(sub_dir / f"sub2api-data-{ts}.json", payload)
        written["files"]["sub2api"] = str(path)
        written["files"]["sub2api_dir"] = str(sub_dir)

    if "cliproxyapi" in wanted:
        bundle = build_cliproxyapi_bundle(rows, base_url=cliproxyapi_base_url)
        cpa_dir = _format_dir(out_dir, "cliproxyapi")
        path = _write_json(cpa_dir / f"cliproxyapi-bundle-{ts}.json", bundle)
        written["files"]["cliproxyapi_bundle"] = str(path)
        written["files"]["cliproxyapi_export_dir"] = str(cpa_dir)
        cdir = Path(cliproxyapi_auth_dir or (cpa_dir / "auth"))
        cdir.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        for rec in bundle.get("accounts") or []:
            if not isinstance(rec, dict):
                continue
            email = str(rec.get("email") or "unknown")
            safe = _safe_email(email)
            lower = safe.lower()
            fname = safe if lower.startswith("xai") else f"xai-{safe}"
            fp = _write_json(cdir / f"{fname}.json", rec)
            files.append(str(fp))
        written["files"]["cliproxyapi_dir"] = str(cdir)
        written["files"]["cliproxyapi_files"] = files

    if "bundle" in wanted:
        bdir = Path(accounts_output_dir or _format_dir(out_dir, "bundle"))
        bdir.mkdir(parents=True, exist_ok=True)
        files = []
        for r in rows:
            n = normalize_result(r)
            if n.get("error") and not n.get("sso") and not n.get("access_token"):
                continue
            email = _safe_email(n.get("email") or "unknown")
            path = _write_json(bdir / f"account_{email}_{ts}.json", r if "email" in r else n)
            files.append(str(path))
        written["files"]["bundle_dir"] = str(bdir)
        written["files"]["bundle_files"] = files

    if "sso" in wanted:
        payload = build_sso_list(rows)
        sso_dir = _format_dir(out_dir, "sso")
        path = _write_json(sso_dir / f"sso-list-{ts}.json", payload)
        # plaintext: email----password----sso
        txt = sso_dir / f"sso-list-{ts}.txt"
        list_lines = []
        pure_sso_lines = []
        for item in payload:
            email = item.get("email") or ""
            password = item.get("password") or ""
            sso = item.get("sso") or ""
            list_lines.append(f"{email}----{password}----{sso}")
            if sso:
                pure_sso_lines.append(sso)
        txt.write_text("\n".join(list_lines) + ("\n" if list_lines else ""), encoding="utf-8")
        # pure SSO tokens only: one token per line
        pure_txt = sso_dir / f"sso_{ts}.txt"
        pure_txt.write_text(
            "\n".join(pure_sso_lines) + ("\n" if pure_sso_lines else ""),
            encoding="utf-8",
        )
        written["files"]["sso"] = str(path)
        written["files"]["sso_txt"] = str(txt)
        written["files"]["sso_pure_txt"] = str(pure_txt)
        written["files"]["sso_dir"] = str(sso_dir)

    return written


def load_results_from_paths(paths: list[str | Path]) -> list[dict[str, Any]]:
    """Load account results from JSON files/dirs (bundle / auth / sub2api / raw)."""
    rows: list[dict[str, Any]] = []
    files: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(sorted(path.glob("**/*.json")))
        elif path.is_file():
            files.append(path)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, list):
            rows.extend([x for x in data if isinstance(x, dict)])
        elif isinstance(data, dict):
            if data.get("type") == "sub2api-data" and isinstance(data.get("accounts"), list):
                for acc in data["accounts"]:
                    if not isinstance(acc, dict):
                        continue
                    cred = acc.get("credentials") if isinstance(acc.get("credentials"), dict) else {}
                    extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
                    rows.append(
                        {
                            "email": acc.get("name") or cred.get("email") or extra.get("email"),
                            "oauth_access_token": cred.get("access_token"),
                            "oauth_refresh_token": cred.get("refresh_token"),
                            "sso": extra.get("sso") or cred.get("sso"),
                            "password": extra.get("password") or cred.get("password"),
                            "expires_at": acc.get("expires_at"),
                            "user_id": str(extra.get("local_account_id") or "")
                            .split("::")[-1],
                        }
                    )
            elif isinstance(data.get("auth"), dict):
                for aid, entry in data["auth"].items():
                    if not isinstance(entry, dict):
                        continue
                    e = dict(entry)
                    e.setdefault("id", aid)
                    rows.append(e)
            elif data.get("email") or data.get("sso") or data.get("key") or data.get("access_token"):
                rows.append(data)
            elif isinstance(data.get("accounts"), list):
                rows.extend([x for x in data["accounts"] if isinstance(x, dict)])
    return rows
