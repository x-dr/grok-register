"""Load config.json and map into process environment + CLI defaults.

Priority (high → low):
  CLI flags  >  environment  >  config.json  >  built-in defaults

config.json is preferred for local secrets (gitignored).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Package/repo root: .../grok-register
ROOT = Path(__file__).resolve().parents[1]

# env key ← config path (dot notation via nested dict walk is handled in apply)
# Values are applied only when env is currently empty (unless force=True).
_ENV_MAP: list[tuple[str, tuple[str, ...]]] = [
    # captcha
    ("GROK_REGISTER_CAPTCHA", ("captcha", "provider")),
    ("YESCAPTCHA_API_KEY", ("captcha", "yescaptcha_key")),
    ("YESCAPTCHA_API_KEY", ("yescaptcha_key",)),  # flat alias
    ("YESCAPTCHA_ENDPOINT", ("captcha", "yescaptcha_endpoint")),
    ("GROK_REGISTER_SOLVER_URL", ("captcha", "solver_url")),
    ("GROK_REGISTER_SOLVER_URL", ("solver_url",)),
    # cfmail (cloudflare_temp_email HTTP)
    ("CFMAIL_BASE_URL", ("cfmail", "base_url")),
    ("CFMAIL_ADMIN_PASSWORD", ("cfmail", "admin_password")),
    ("CFMAIL_SITE_PASSWORD", ("cfmail", "site_password")),
    ("CFMAIL_DOMAIN", ("cfmail", "domain")),
    ("CFMAIL_BASE_URL", ("cfmail_base_url",)),
    ("CFMAIL_ADMIN_PASSWORD", ("cfmail_admin_password",)),
    ("CFMAIL_SITE_PASSWORD", ("cfmail_site_password",)),
    ("CFMAIL_DOMAIN", ("cfmail_domain",)),
    # tempmail.lol
    ("TEMPMAIL_API_KEY", ("tempmail", "api_key")),
    ("TEMPMAIL_API_KEY", ("tempmail_api_key",)),
    # cloudflare D1 (alias_mail)
    ("CLOUDFLARE_API_TOKEN", ("cloudflare", "api_token")),
    ("CLOUDFLARE_ACCOUNT_ID", ("cloudflare", "account_id")),
    ("CLOUDFLARE_D1_DB_ID", ("cloudflare", "d1_db_id")),
    ("ALIAS_MAIL_DOMAINS", ("cloudflare", "domains")),
    ("CLOUDFLARE_API_TOKEN", ("cloudflare_d1", "api_token")),
    ("CLOUDFLARE_ACCOUNT_ID", ("cloudflare_d1", "account_id")),
    ("CLOUDFLARE_D1_DB_ID", ("cloudflare_d1", "d1_db_id")),
    ("ALIAS_MAIL_DOMAINS", ("cloudflare_d1", "domains")),
    # proxy
    ("HTTPS_PROXY", ("proxy",)),
    ("HTTP_PROXY", ("proxy",)),
    ("HTTPS_PROXY", ("https_proxy",)),
    ("HTTP_PROXY", ("http_proxy",)),
]


def _dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).strip()
    return s if s != "" else None


def default_config_paths() -> list[Path]:
    """Search order for config files."""
    paths: list[Path] = []
    env_path = (os.environ.get("GROK_REGISTER_CONFIG") or "").strip()
    if env_path:
        paths.append(Path(env_path).expanduser())
    paths.extend(
        [
            Path.cwd() / "config.json",
            Path.cwd() / "config.local.json",
            ROOT / "config.json",
            ROOT / "config.local.json",
        ]
    )
    # dedupe preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def load_config_file(path: str | Path | None = None) -> tuple[dict[str, Any], Path | None]:
    """Load JSON config. Returns (data, resolved_path_or_None)."""
    candidates: list[Path]
    if path:
        candidates = [Path(path).expanduser()]
    else:
        candidates = default_config_paths()

    for p in candidates:
        if not p.is_file():
            continue
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"config.json 解析失败 ({p}): {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"config.json 根节点必须是对象 ({p})")
        return data, p.resolve()
    return {}, None


def apply_config_to_env(data: dict[str, Any], *, force: bool = False) -> list[str]:
    """Write mapped keys into os.environ. Returns list of applied env names."""
    applied: list[str] = []
    for env_key, path in _ENV_MAP:
        val = _as_str(_dig(data, path))
        if val is None:
            continue
        if not force and (os.environ.get(env_key) or "").strip():
            continue
        os.environ[env_key] = val
        if env_key not in applied:
            applied.append(env_key)
    return applied


def cli_defaults_from_config(data: dict[str, Any]) -> dict[str, Any]:
    """Extract argparse-friendly defaults from config (non-secret runtime knobs)."""
    out: dict[str, Any] = {}

    def set_if(key: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        out[key] = value

    set_if("count", data.get("count") or data.get("n"))
    set_if("threads", data.get("threads") or data.get("t"))
    set_if("email", data.get("email") or data.get("email_backend") or data.get("mail"))
    set_if("proxy", data.get("proxy") or data.get("https_proxy") or data.get("http_proxy"))

    captcha = data.get("captcha") if isinstance(data.get("captcha"), dict) else {}
    set_if("captcha", captcha.get("provider") or data.get("captcha_provider"))
    set_if("solver_url", captcha.get("solver_url") or data.get("solver_url"))
    set_if(
        "yescaptcha_key",
        captcha.get("yescaptcha_key") or data.get("yescaptcha_key"),
    )

    if "no_oauth" in data:
        out["no_oauth"] = bool(data.get("no_oauth"))
    if "oauth" in data and data.get("oauth") is False:
        out["no_oauth"] = True

    oauth = data.get("oauth") if isinstance(data.get("oauth"), dict) else {}
    set_if(
        "cliproxyapi_auth_dir",
        oauth.get("cliproxyapi_auth_dir") or data.get("cliproxyapi_auth_dir"),
    )
    set_if(
        "cliproxyapi_base_url",
        oauth.get("cliproxyapi_base_url") or data.get("cliproxyapi_base_url"),
    )
    set_if(
        "accounts_output_dir",
        data.get("accounts_output_dir")
        or oauth.get("accounts_output_dir")
        or data.get("output_dir"),
    )
    if data.get("no_save") is not None:
        out["no_save"] = bool(data.get("no_save"))
    if data.get("json") is not None:
        out["json_out"] = bool(data.get("json"))

    exp = data.get("export") if isinstance(data.get("export"), dict) else {}
    formats = exp.get("formats") or data.get("export_formats")
    if formats:
        if isinstance(formats, str):
            formats = [x.strip() for x in formats.split(",") if x.strip()]
        if isinstance(formats, list):
            out["export_formats"] = [str(x).strip() for x in formats if str(x).strip()]
    set_if("export_dir", exp.get("dir") or exp.get("export_dir") or data.get("export_dir"))
    if exp.get("enabled") is False:
        out["export_formats"] = []
    if data.get("export_enabled") is False:
        out["export_formats"] = []

    # coerce ints
    for k in ("count", "threads"):
        if k in out:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError):
                out.pop(k, None)
    return out


def bootstrap(
    config_path: str | Path | None = None,
    *,
    load_dotenv_file: bool = True,
) -> tuple[dict[str, Any], Path | None]:
    """Load .env (optional) then config.json → env. Safe to call multiple times."""
    if load_dotenv_file:
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
            # cwd .env overrides
            load_dotenv(Path.cwd() / ".env", override=False)
        except Exception:
            pass

    data, path = load_config_file(config_path)
    if data:
        apply_config_to_env(data, force=False)
    return data, path
