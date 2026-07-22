"""Core single-account registration flow (protocol + captcha + SSO + optional OAuth)."""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional

# Repo root (parent of package) so `import xconsole_client` works when installed
# as editable or run via `python -m grok_register`.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from .config import bootstrap

    bootstrap()
except Exception:
    try:
        from dotenv import load_dotenv

        load_dotenv(_ROOT / ".env")
    except Exception:
        pass

from xconsole_client import XConsoleAuthClient, YesCaptchaSolver, config as C
from xconsole_client.oauth_protocol import extract_cookies_from_auth_client
from xconsole_client.xai_oauth import (
    CLIPROXYAPI_GROK_BASE_URL,
    complete_build_oauth,
    default_cliproxyapi_auth_dir,
    save_cliproxyapi_auth_record,
)

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_results_lock = threading.Lock()
_cf_lock = threading.Lock()
_oauth_lock = threading.Lock()

LogFn = Callable[[int, str], None]


def _default_log(index: int, msg: str, *, done: int = 0, total: int = 0, t0: float = 0.0) -> None:
    elapsed = time.time() - t0 if t0 else 0.0
    bar = f"[{done}/{total}]" if total > 1 else ""
    print(f"  {bar} [#{index}] {msg}  ({elapsed:.0f}s)", flush=True)


def resolve_proxy() -> str:
    try:
        from .proxyutil import resolve_proxy_from_env, apply_proxy_env

        p = resolve_proxy_from_env()
        if p:
            # Keep env normalized (socks5→socks5h) + NO_PROXY for local services.
            return apply_proxy_env(p, force=True)
        return ""
    except Exception:
        return (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
            or ""
        ).strip()


def resolve_captcha(
    *,
    provider: str | None = None,
    yescaptcha_key: str | None = None,
    solver_url: str | None = None,
) -> tuple[str, str, str | None, bool]:
    """Return (provider, api_key, endpoint, auto_fallback).

    provider: yescaptcha | local

    ``solver_url`` only applies to ``local`` (turnstile-solver).
    YesCaptcha cloud uses ``YESCAPTCHA_ENDPOINT`` / yescaptcha_endpoint only —
    never the local 5072 solver URL.
    """
    prov = (
        provider
        or os.environ.get("GROK_REGISTER_CAPTCHA")
        or os.environ.get("GROK2API_CAPTCHA_PROVIDER")
        or os.environ.get("CAPTCHA_PROVIDER")
        or "yescaptcha"
    ).strip().lower()
    if prov not in {"yescaptcha", "local"}:
        prov = "yescaptcha"

    def _looks_local(url: str | None) -> bool:
        u = (url or "").strip().lower()
        return any(
            x in u
            for x in (
                "127.0.0.1",
                "localhost",
                "0.0.0.0",
                ":5072",
            )
        )

    if prov == "local":
        endpoint = (
            solver_url
            or os.environ.get("GROK_REGISTER_SOLVER_URL")
            or os.environ.get("GROK2API_LOCAL_SOLVER_URL")
            or os.environ.get("LOCAL_SOLVER_URL")
            or "http://127.0.0.1:5072"
        ).strip().rstrip("/")
        # Local turnstile-solver is YesCaptcha-compatible; key is ignored.
        key = (
            yescaptcha_key
            or os.environ.get("GROK2API_YESCAPTCHA_KEY")
            or os.environ.get("YESCAPTCHA_API_KEY")
            or "local"
        ).strip() or "local"
        return prov, key, endpoint, False

    key = (
        yescaptcha_key
        or os.environ.get("GROK2API_YESCAPTCHA_KEY")
        or os.environ.get("YESCAPTCHA_API_KEY")
        or ""
    ).strip()
    # Cloud YesCaptcha only — do NOT fall back to solver_url / local 5072.
    endpoint = (
        os.environ.get("GROK2API_YESCAPTCHA_ENDPOINT")
        or os.environ.get("YESCAPTCHA_ENDPOINT")
        or os.environ.get("YESCAPTCHA_API_BASE")
        or None
    )
    # Allow explicit solver_url only if it is clearly a remote YesCaptcha host.
    if solver_url and not _looks_local(solver_url):
        endpoint = solver_url
    if endpoint:
        endpoint = endpoint.strip().rstrip("/")
        if _looks_local(endpoint):
            endpoint = None
    # Placeholder keys from config.example.json
    if key in {"你的YesCaptcha_Key", "your_yescaptcha_key", "xxx", "changeme"}:
        key = ""
    return "yescaptcha", key, endpoint, True


def make_email_provider(backend: str):
    """Return (email, receiver) — receiver has .wait_for_code(timeout)."""
    backend = (backend or "tempmail").strip().lower()
    if backend in {"tempmail", "tempmail.lol", "lol"}:
        key = (
            os.environ.get("TEMPMAIL_API_KEY")
            or os.environ.get("GROK2API_TEMPMAIL_API_KEY")
            or ""
        ).strip()
        if not key:
            raise RuntimeError("TEMPMAIL_API_KEY 环境变量未设置（邮箱后端 tempmail）")
        from xconsole_client.tempmail_transport import TempmailInbox

        inbox = TempmailInbox(api_key=key, prefix="xai", debug=False)
        email = inbox.create()
        return email, inbox

    # dreamhunter2333/cloudflare_temp_email HTTP API (Base URL + admin password)
    if backend in {"cfmail", "cf_temp", "cf-temp", "cloudflare_temp_email", "cf"}:
        from . import cfmail as cfm

        box = cfm.create_mailbox()
        receiver = cfm.CfmailReceiver(
            email=box["email"],
            token=box["token"],
            base_url=box["base_url"],
            site_password=cfm._site_password() or None,
        )
        return box["email"], receiver

    # Direct Cloudflare D1 (legacy alias_mail; needs CLOUDFLARE_* tokens)
    if backend in {"cloudflare", "d1", "alias_mail"}:
        from xconsole_client.mailbox import AliasMailAccount, AliasMailCodeReceiver

        with _cf_lock:
            cf = AliasMailAccount.ensure_cf()
            alloc = AliasMailAccount(cf)
            address = alloc.create(prefix="xai")
        receiver = AliasMailCodeReceiver(
            cf, address=address, timeout=120, interval=3, since_now=True
        )
        return address, receiver

    # 22.do Outlook temporary mailbox (no API key)
    if backend in {"22do", "22.do", "do22", "outlook22", "hotmail22"}:
        from . import email_22do as d22
        from .proxyutil import resolve_proxy_from_env

        proxy = resolve_proxy_from_env() or None
        box = d22.create_mailbox(proxy=proxy)
        receiver = d22.Do22Receiver(
            email=box["email"],
            token=box["token"],
            proxy=proxy,
        )
        return box["email"], receiver

    raise ValueError(
        f"unknown email backend: {backend} "
        f"(tempmail|cfmail|cloudflare|22do)"
    )


def save_account_bundle(result: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    email = str(result.get("email") or "unknown")
    safe = "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in email) or "unknown"
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = output_dir / f"account_{safe}_{ts}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def register_one(
    index: int = 1,
    email_backend: str = "tempmail",
    *,
    captcha_provider: str | None = None,
    yescaptcha_key: str | None = None,
    solver_url: str | None = None,
    do_oauth: bool = True,
    oauth_headless: bool = True,
    oauth_timeout: float = 180.0,
    oauth_interactive_fallback: bool = False,
    oauth_protocol: bool = True,
    oauth_debug: bool = False,
    cliproxyapi_auth_dir: Optional[str | Path] = None,
    cliproxyapi_base_url: str = CLIPROXYAPI_GROK_BASE_URL,
    accounts_output_dir: Optional[str | Path] = None,
    proxy: str | None = None,
    enable_nsfw: bool = True,
    log: LogFn | None = None,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run signup (+ optional Build OAuth export). Thread-safe."""

    def _log(msg: str) -> None:
        if log:
            log(index, msg)
        elif progress is not None:
            done = int(progress.get("done", 0))
            total = int(progress.get("total", 0))
            t0 = float(progress.get("t0", 0.0))
            _default_log(index, msg, done=done, total=total, t0=t0)
        else:
            _default_log(index, msg)

    provider, solver_key, endpoint, auto_fallback = resolve_captcha(
        provider=captcha_provider,
        yescaptcha_key=yescaptcha_key,
        solver_url=solver_url,
    )
    if provider == "yescaptcha" and not solver_key:
        return {
            "email": "",
            "password": "",
            "sso": None,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "error": "YESCAPTCHA_API_KEY 未设置（或改用 --captcha local + 本地过盾）",
        }

    proxy = (proxy if proxy is not None else resolve_proxy()) or ""
    try:
        from .proxyutil import apply_proxy_env, socks_dependency_error, normalize_proxy

        proxy = apply_proxy_env(proxy, force=bool(proxy)) if proxy else resolve_proxy()
        proxy = normalize_proxy(proxy)
        err = socks_dependency_error(proxy)
        if err:
            _log(err.replace("\n", " "))
    except Exception:
        pass
    c = XConsoleAuthClient(debug=False, signup_url=SIGNUP_URL, proxy=proxy or None)
    email = ""
    password = ""
    sso = None

    try:
        # 1. warm-up + scrape
        c.visit_home()
        c.load_signup_page()
        sitekey = getattr(c, "turnstile_sitekey", None) or C.TURNSTILE_SITEKEY
        _log("cookie + scrape OK")

        # 2. allocate mailbox first (cheap), solve captcha BEFORE email code
        #    so validation codes do not expire during slow Turnstile solves.
        email, receiver = make_email_provider(email_backend)
        password = f"Pw{os.urandom(6).hex()}!a#A"
        _log(f"email: {email}")

        solver = YesCaptchaSolver(
            solver_key,
            endpoint=endpoint,
            timeout=float(os.environ.get("GROK_REGISTER_CAPTCHA_TIMEOUT", "180") or 180),
            poll_interval=float(os.environ.get("GROK_REGISTER_CAPTCHA_POLL", "2") or 2),
            debug=False,
            auto_fallback_endpoint=auto_fallback,
        )
        label = "本地过盾" if provider == "local" else "YesCaptcha"
        _log(f"Turnstile via {label}…")
        turnstile = solver.solve_turnstile(
            website_url=SIGNUP_URL,
            website_key=sitekey,
            premium=(provider != "local"),
        )
        _log(f"Turnstile OK ({len(turnstile)} chars)")

        # 3. email code after captcha
        c.create_email_validation_code(email)
        code = receiver.wait_for_code(timeout=120)
        _log(f"code: {code}")
        c.verify_email_validation_code(email, code)
        c.validate_password(email, password)
        _log("email verified")

        # 4. create account
        res = c.create_account(
            email=email,
            given_name="Test",
            family_name="User",
            password=password,
            email_validation_code=code,
            turnstile_token=turnstile,
            castle_request_token="",
            conversion_id=str(uuid.uuid4()),
        )
        if not res.ok:
            _log(f"FAIL create_account HTTP {res.http_status}")
            return {
                "email": email,
                "password": password,
                "sso": None,
                "oauth_access_token": None,
                "cliproxyapi_auth": None,
                "error": f"HTTP {res.http_status}",
            }
        _log("account created")

        # 5. SSO
        sso = c.fetch_sso_token(email=email, password=password, save=True, retries=3)
        if not sso:
            _log("FAIL SSO extraction")
            return {
                "email": email,
                "password": password,
                "sso": None,
                "oauth_access_token": None,
                "cliproxyapi_auth": None,
                "error": "SSO failed",
            }
        try:
            payload = json.loads(base64.urlsafe_b64decode(sso.split(".")[1] + "=="))
            sid = str(payload.get("session_id", "?"))[:12]
            _log(f"SSO saved  session_id={sid}...")
        except Exception:
            _log("SSO saved")

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
            "error": None,
        }

        # 5b. optional NSFW feature enable (non-blocking)
        if enable_nsfw:
            _log("enable NSFW…")
            try:
                from .nsfw import enable_nsfw_for_sso

                nsfw_ok, nsfw_msg = enable_nsfw_for_sso(
                    sso,
                    proxy=proxy,
                    log=_log,
                )
            except Exception as e:  # noqa: BLE001
                nsfw_ok, nsfw_msg = False, str(e)
            if nsfw_ok:
                result["nsfw_enabled"] = True
                result["nsfw_error"] = None
                _log(f"NSFW enabled: {nsfw_msg}")
            else:
                result["nsfw_enabled"] = False
                result["nsfw_error"] = nsfw_msg
                _log(f"NSFW not enabled (account kept): {nsfw_msg}")
        else:
            result["nsfw_enabled"] = False
            result["nsfw_error"] = "skipped"
            _log("NSFW skipped (--no-enable-nsfw)")

        # 6. optional OAuth tokens for auth.json / sub2api / CLIProxyAPI
        # Prefer SSO device-flow (same as grokcli-2api) — no Turnstile/Playwright.
        # Fall back to protocol Build OAuth (local captcha or YesCaptcha).
        if do_oauth:
            auth_dir = (
                Path(cliproxyapi_auth_dir)
                if cliproxyapi_auth_dir
                else default_cliproxyapi_auth_dir()
            )
            oauth_errors: list[str] = []

            # --- path A: SSO → device flow ---
            if sso:
                _log("OAuth via SSO device-flow…")
                try:
                    from .sso_device import sso_to_token

                    token = sso_to_token(sso, quiet=False)
                except Exception as e:  # noqa: BLE001
                    token = None
                    oauth_errors.append(f"device-flow: {e}")
                    _log(f"device-flow error: {e}")
                if token and token.get("access_token"):
                    result["oauth_access_token"] = token.get("access_token")
                    result["oauth_refresh_token"] = token.get("refresh_token")
                    exp_in = token.get("expires_in")
                    try:
                        if exp_in is not None:
                            result["expires_at"] = int(time.time()) + int(exp_in)
                    except Exception:
                        pass
                    # Decode user_id for export
                    try:
                        from .export_formats import decode_jwt_payload

                        claims = decode_jwt_payload(str(token.get("access_token") or ""))
                        if claims.get("principal_id") or claims.get("sub"):
                            result["user_id"] = str(
                                claims.get("principal_id") or claims.get("sub")
                            )
                        if claims.get("team_id"):
                            result["team_id"] = str(claims.get("team_id"))
                        if claims.get("client_id"):
                            result["oidc_client_id"] = str(claims.get("client_id"))
                        if claims.get("exp") is not None:
                            result["expires_at"] = int(claims["exp"])
                    except Exception:
                        pass
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
                        _log(f"OAuth tokens OK but cliproxy save failed: {e}")
                elif not result.get("oauth_access_token"):
                    oauth_errors.append("device-flow returned no access_token")

            # --- path B: protocol Build OAuth (needs captcha for CreateSession) ---
            if not result.get("oauth_access_token"):
                session_cookies = extract_cookies_from_auth_client(c)
                if sso:
                    session_cookies = dict(session_cookies or {})
                    session_cookies.setdefault("sso", sso)
                # Wire local solver into YesCaptcha client via env + key
                yc_key = solver_key
                if provider == "local":
                    yc_key = yc_key or "local"
                    if endpoint:
                        os.environ.setdefault("YESCAPTCHA_ENDPOINT", endpoint)
                        os.environ.setdefault("GROK2API_YESCAPTCHA_ENDPOINT", endpoint)
                    os.environ.setdefault("YESCAPTCHA_API_KEY", yc_key)
                _log(f"OAuth Build path fallback → {auth_dir}")
                try:
                    with _oauth_lock:
                        oauth = complete_build_oauth(
                            email,
                            password,
                            cliproxyapi_auth_dir=auth_dir,
                            cliproxyapi_base_url=cliproxyapi_base_url,
                            headless=oauth_headless,
                            timeout=oauth_timeout,
                            proxy=proxy,
                            interactive_fallback=oauth_interactive_fallback,
                            yescaptcha_key=yc_key,
                            protocol=oauth_protocol,
                            debug=oauth_debug,
                            session_cookies=session_cookies,
                            auth_client=c,
                        )
                    result["oauth_access_token"] = oauth.access_token
                    result["oauth_refresh_token"] = oauth.refresh_token
                    result["oauth_record"] = str(oauth.path) if oauth.path else None
                    result["cliproxyapi_auth"] = (
                        str(oauth.cliproxyapi_path) if oauth.cliproxyapi_path else None
                    )
                    name = oauth.cliproxyapi_path.name if oauth.cliproxyapi_path else "?"
                    tok = (oauth.access_token or "")[:20]
                    _log(f"Build OAuth OK  access={tok}...  cliproxy={name}")
                except Exception as e:  # noqa: BLE001
                    oauth_errors.append(str(e))
                    try:
                        from .proxyutil import shorten_error
                        _log(f"Build OAuth failed: {shorten_error(e)}")
                    except Exception:
                        _log(f"Build OAuth failed: {str(e)[:160]}")

            if not result.get("oauth_access_token"):
                # Keep SSO success; surface OAuth error without discarding account
                full_err = "; ".join(oauth_errors) or "OAuth failed"
                try:
                    from .proxyutil import shorten_error
                    result["error"] = shorten_error(full_err, max_len=200)
                    result["error_detail"] = full_err[:2000]
                except Exception:
                    result["error"] = full_err[:200]
                    result["error_detail"] = full_err[:2000]
                _log(f"OAuth failed (SSO kept): {result['error']}")
        else:
            _log("OAuth skipped (--no-oauth)")

        if accounts_output_dir:
            bundle = save_account_bundle(result, Path(accounts_output_dir))
            result["account_bundle"] = str(bundle)
            _log(f"saved {bundle}")

        return result

    except Exception as e:  # noqa: BLE001
        try:
            from .proxyutil import shorten_error
            short = shorten_error(e, max_len=200)
        except Exception:
            short = str(e)[:200]
        _log(f"ERROR: {short}")
        return {
            "email": email,
            "password": password,
            "sso": sso,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "error": short,
            "error_detail": str(e)[:2000],
        }
    finally:
        try:
            c.close()
        except Exception:
            pass
        if progress is not None:
            with _results_lock:
                progress["done"] = int(progress.get("done", 0)) + 1


def run_batch(
    count: int,
    threads: int,
    email_backend: str = "tempmail",
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Register `count` accounts with up to `threads` concurrent workers."""
    count = max(1, int(count))
    threads = max(1, min(int(threads), count))
    progress = {"done": 0, "total": count, "t0": time.time()}
    results: list[dict[str, Any]] = []

    if count == 1:
        results.append(
            register_one(1, email_backend=email_backend, progress=progress, **kwargs)
        )
        return results

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futures = [
            ex.submit(
                register_one,
                i,
                email_backend,
                progress=progress,
                **kwargs,
            )
            for i in range(1, count + 1)
        ]
        for f in as_completed(futures):
            results.append(f.result())
    return results
