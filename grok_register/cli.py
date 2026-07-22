#!/usr/bin/env python3
"""grok-register CLI — standalone x.ai / Grok protocol registration machine.

Extracted from HM2899/grokcli-2api (grok-build-auth + registration sidecar).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from xconsole_client.xai_oauth import (
    CLIPROXYAPI_GROK_BASE_URL,
    default_cliproxyapi_auth_dir,
)

from . import __version__
from .config import bootstrap, cli_defaults_from_config
from .export_formats import ALL_FORMATS, export_results, load_results_from_paths
from . import remote_sub2api as sub2api
from . import remote_cliproxyapi as cpa
from .config import get_section
from .register import resolve_captcha, run_batch


def build_parser(defaults: dict | None = None) -> argparse.ArgumentParser:
    d = defaults or {}
    default_auth = str(
        d.get("cliproxyapi_auth_dir") or default_cliproxyapi_auth_dir()
    )
    default_out = str(
        d.get("accounts_output_dir") or (Path.cwd() / "accounts_output")
    )

    p = argparse.ArgumentParser(
        prog="grok-register",
        description=(
            "独立注册机 CLI：协议注册 x.ai 账号 → 提取 SSO → "
            "可选 Grok Build OAuth / CLIProxyAPI auth 导出。"
            "优先读 config.json（见 config.example.json）。"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "-c",
        "--config",
        default=None,
        help="配置文件路径（默认: ./config.json 或项目根 config.json）",
    )
    p.add_argument(
        "-n",
        "--count",
        type=int,
        default=int(d.get("count", 1)),
        help="注册账号数量",
    )
    p.add_argument(
        "-t",
        "--threads",
        type=int,
        default=int(d.get("threads", 1)),
        help="并发线程数（注册阶段；OAuth 串行）",
    )
    p.add_argument(
        "-e",
        "--email",
        choices=["tempmail", "cfmail", "cloudflare"],
        default=str(d.get("email") or "tempmail"),
        help="邮箱: tempmail | cfmail(cloudflare_temp_email HTTP) | cloudflare(D1)",
    )
    p.add_argument(
        "--captcha",
        choices=["yescaptcha", "local"],
        default=d.get("captcha"),
        help="过盾：yescaptcha（云打码）或 local（本地 YesCaptcha 兼容服务）",
    )
    p.add_argument(
        "--solver-url",
        default=d.get("solver_url"),
        help="过盾 API 地址（local 默认 http://127.0.0.1:5072）",
    )
    p.add_argument(
        "--yescaptcha-key",
        default=d.get("yescaptcha_key"),
        help="YesCaptcha clientKey（默认读 config / YESCAPTCHA_API_KEY）",
    )
    p.add_argument(
        "--no-oauth",
        action="store_true",
        default=False,
        help="只注册 + 提取 SSO，不做 Build OAuth / CLIProxyAPI 导出",
    )
    p.add_argument(
        "--oauth",
        action="store_true",
        default=False,
        help="强制走 Build OAuth（覆盖 config.json 里 no_oauth: true）",
    )
    p.add_argument(
        "--cliproxyapi-auth-dir",
        default=default_auth,
        help="CLIProxyAPI auth 输出目录",
    )
    p.add_argument(
        "--cliproxyapi-base-url",
        default=d.get("cliproxyapi_base_url") or CLIPROXYAPI_GROK_BASE_URL,
        help="Build 上游 base_url",
    )
    p.add_argument(
        "--oauth-headed",
        action="store_true",
        help="Playwright 有头模式（仅非协议回退时）",
    )
    p.add_argument("--oauth-timeout", type=float, default=180.0, help="OAuth 超时秒数")
    p.add_argument(
        "--no-oauth-protocol",
        action="store_true",
        help="禁用纯协议 OAuth",
    )
    p.add_argument(
        "--oauth-interactive-fallback",
        action="store_true",
        help="协议/Playwright 失败时回退系统浏览器手动登录",
    )
    p.add_argument("--oauth-debug", action="store_true", help="打印协议 OAuth 调试日志")
    p.add_argument(
        "--accounts-output-dir",
        default=default_out,
        help="合并账号 JSON 输出目录",
    )
    p.add_argument(
        "--no-save",
        action="store_true",
        default=bool(d.get("no_save", False)),
        help="不写入 accounts_output",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_out",
        default=bool(d.get("json_out", False)),
        help="结束时向 stdout 打印完整 JSON 结果列表",
    )
    p.add_argument(
        "--proxy",
        default=d.get("proxy"),
        help="HTTP/HTTPS/SOCKS5 代理，如 socks5h://127.0.0.1:40000（WARP）或 http://127.0.0.1:7890",
    )
    default_fmts = d.get("export_formats")
    if not default_fmts:
        default_fmts = ["auth", "sub2api", "cliproxyapi", "bundle", "sso"]
    p.add_argument(
        "--export-formats",
        default=",".join(default_fmts),
        help="导出格式，逗号分隔: auth,sub2api,cliproxyapi,bundle,sso（空=不导出）",
    )
    p.add_argument(
        "--export-dir",
        default=str(d.get("export_dir") or (Path.cwd() / "exports")),
        help="多格式导出目录",
    )
    p.add_argument(
        "--no-export",
        action="store_true",
        help="跳过多格式导出",
    )
    return p


def _cmd_export(argv: list[str]) -> int:
    """Export existing account JSON files into multi formats."""
    p = argparse.ArgumentParser(
        prog="grok-register export",
        description="从已有账号 JSON 导出 auth / sub2api / cliproxyapi 等格式",
    )
    p.add_argument(
        "inputs",
        nargs="+",
        help="输入文件或目录（account bundle / auth.json / sub2api-data / 原始结果）",
    )
    p.add_argument(
        "--export-formats",
        default="auth,sub2api,cliproxyapi,bundle,sso",
        help="导出格式，逗号分隔",
    )
    p.add_argument("--export-dir", default=str(Path.cwd() / "exports"), help="输出目录")
    p.add_argument(
        "--cliproxyapi-auth-dir",
        default=str(Path.cwd() / "cliproxyapi_auth"),
        help="CLIProxyAPI 单文件目录",
    )
    p.add_argument(
        "--cliproxyapi-base-url",
        default="https://cli-chat-proxy.grok.com/v1",
        help="CPA base_url",
    )
    p.add_argument("-c", "--config", default=None, help="可选 config.json")
    args = p.parse_args(argv)
    bootstrap(args.config)
    rows = load_results_from_paths(args.inputs)
    if not rows:
        print("no accounts loaded from inputs", file=sys.stderr)
        return 1
    fmts = [x.strip() for x in args.export_formats.split(",") if x.strip()]
    written = export_results(
        rows,
        formats=fmts,
        export_dir=args.export_dir,
        cliproxyapi_auth_dir=args.cliproxyapi_auth_dir,
        cliproxyapi_base_url=args.cliproxyapi_base_url,
    )
    print(f"loaded {len(rows)} account(s)")
    print(f"export → {written.get('export_dir')}")
    for k, v in (written.get("files") or {}).items():
        if isinstance(v, list):
            print(f"  {k}: {len(v)} file(s)")
        else:
            print(f"  {k}: {v}")
    return 0


def _cmd_sso_oauth(argv: list[str]) -> int:
    """Convert existing SSO cookie(s) to OIDC tokens via device flow."""
    p = argparse.ArgumentParser(
        prog="grok-register oauth",
        description="用已有 SSO cookie 走 device-flow 换 access/refresh（无需 Playwright）",
    )
    p.add_argument("--sso", default=None, help="单个 SSO JWT")
    p.add_argument("--email", default="", help="邮箱（写入导出）")
    p.add_argument("--password", default="", help="密码（写入导出）")
    p.add_argument(
        "--from-json",
        default=None,
        help="从 account bundle / 结果 JSON 读取 sso/email/password",
    )
    p.add_argument("--export-dir", default=str(Path.cwd() / "exports"))
    p.add_argument(
        "--export-formats",
        default="auth,sub2api,cliproxyapi,bundle,sso",
    )
    p.add_argument(
        "--cliproxyapi-auth-dir",
        default=str(Path.cwd() / "cliproxyapi_auth"),
    )
    p.add_argument("-c", "--config", default=None)
    args = p.parse_args(argv)
    bootstrap(args.config)

    rows: list[dict] = []
    if args.from_json:
        rows = load_results_from_paths([args.from_json])
    elif args.sso:
        rows = [{"email": args.email, "password": args.password, "sso": args.sso}]
    else:
        p.error("需要 --sso 或 --from-json")

    from .sso_device import sso_to_token
    from xconsole_client.xai_oauth import save_cliproxyapi_auth_record
    from .export_formats import decode_jwt_payload
    import time as _time

    out_rows: list[dict] = []
    ok = 0
    for r in rows:
        sso = str(r.get("sso") or r.get("sso_cookie") or "").strip()
        email = str(r.get("email") or args.email or "").strip()
        password = str(r.get("password") or r.get("register_password") or args.password or "")
        if not sso:
            print(f"skip (no sso): {email or '?'}")
            continue
        print(f"device-flow for {email or sso[:20]}...")
        token = sso_to_token(sso, quiet=False)
        if not token or not token.get("access_token"):
            print("  FAIL: no access_token")
            out_rows.append({**r, "error": "device-flow failed"})
            continue
        claims = decode_jwt_payload(str(token.get("access_token") or ""))
        row = {
            "email": email,
            "password": password,
            "sso": sso,
            "oauth_access_token": token.get("access_token"),
            "oauth_refresh_token": token.get("refresh_token"),
            "user_id": str(claims.get("principal_id") or claims.get("sub") or ""),
            "team_id": str(claims.get("team_id") or ""),
            "expires_at": int(claims["exp"]) if claims.get("exp") is not None else None,
            "error": None,
        }
        try:
            cpath = save_cliproxyapi_auth_record(
                {
                    "access_token": token.get("access_token"),
                    "refresh_token": token.get("refresh_token"),
                    "expires_in": token.get("expires_in"),
                    "token_type": token.get("token_type") or "Bearer",
                },
                userinfo={"email": email},
                auth_dir=args.cliproxyapi_auth_dir,
            )
            row["cliproxyapi_auth"] = str(cpath)
            print(f"  OK → {cpath}")
        except Exception as e:
            print(f"  tokens OK, cliproxy save fail: {e}")
        out_rows.append(row)
        ok += 1

    fmts = [x.strip() for x in args.export_formats.split(",") if x.strip()]
    if out_rows and fmts:
        written = export_results(
            out_rows,
            formats=fmts,
            export_dir=args.export_dir,
            cliproxyapi_auth_dir=args.cliproxyapi_auth_dir,
        )
        print(f"export → {written.get('export_dir')}")
    print(f"done: {ok}/{len(rows)} converted")
    return 0 if ok else 1




def _load_rows_for_push(args) -> list[dict]:
    rows: list[dict] = []
    if getattr(args, "from_json", None):
        paths = args.from_json if isinstance(args.from_json, list) else [args.from_json]
        rows.extend(load_results_from_paths([p for p in paths if p]))
    if getattr(args, "inputs", None):
        rows.extend(load_results_from_paths(args.inputs))
    seen = set()
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = (
            str(r.get("email") or ""),
            str(r.get("sso") or r.get("sso_cookie") or "")[:24],
            str(r.get("oauth_access_token") or r.get("access_token") or r.get("key") or "")[:24],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _cmd_test_sub2api(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="grok-register test-sub2api")
    p.add_argument("-c", "--config", default=None)
    args = p.parse_args(argv)
    data, path = bootstrap(args.config)
    cfg = sub2api.normalize_config(get_section(data, "sub2api"))
    print(f"config: {path}")
    print(f"base_url: {cfg.get('base_url')}")
    r = sub2api.test_connection(cfg)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


def _cmd_test_cpa(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="grok-register test-cpa")
    p.add_argument("-c", "--config", default=None)
    args = p.parse_args(argv)
    data, path = bootstrap(args.config)
    cfg = cpa.normalize_config(get_section(data, "cliproxyapi"))
    print(f"config: {path}")
    print(f"base_url: {cfg.get('base_url')}")
    r = cpa.test_connection(cfg)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


def _cmd_push_sub2api(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="grok-register push-sub2api",
        description="远程导入账号到 sub2api",
    )
    p.add_argument("inputs", nargs="*", help="账号 JSON 文件/目录（export/auth/bundle）")
    p.add_argument("--from-json", action="append", default=[], help="额外输入路径，可重复")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--group-id", type=int, default=None)
    p.add_argument("--group-name", default=None)
    args = p.parse_args(argv)
    data, path = bootstrap(args.config)
    cfg = sub2api.normalize_config(get_section(data, "sub2api"))
    if args.group_id is not None:
        cfg["group_id"] = args.group_id
    if args.group_name:
        cfg["group_name"] = args.group_name
    rows = _load_rows_for_push(args)
    if not rows:
        print("no accounts loaded; pass export/auth.json or accounts_output/", file=sys.stderr)
        return 1
    print(f"config: {path}")
    print(f"push {len(rows)} account(s) → {cfg.get('base_url')}")
    result = sub2api.push_many(rows, cfg=cfg, concurrency=args.concurrency)
    print(result.get("message"))
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, ensure_ascii=False, indent=2))
    fails = [r for r in result.get("results") or [] if not r.get("ok")]
    for f in fails[:20]:
        print(f"  FAIL {f.get('email') or f.get('account_id')}: {f.get('error')}")
    return 0 if result.get("ok") else 1


def _cmd_push_cpa(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="grok-register push-cpa",
        description="远程导入账号到 CLIProxyAPI management API",
    )
    p.add_argument("inputs", nargs="*", help="账号 JSON 文件/目录")
    p.add_argument("--from-json", action="append", default=[], help="额外输入路径，可重复")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("--concurrency", type=int, default=None)
    args = p.parse_args(argv)
    data, path = bootstrap(args.config)
    cfg = cpa.normalize_config(get_section(data, "cliproxyapi"))
    rows = _load_rows_for_push(args)
    if not rows:
        print("no accounts loaded; pass export/auth.json or accounts_output/", file=sys.stderr)
        return 1
    print(f"config: {path}")
    print(f"push {len(rows)} account(s) → {cfg.get('base_url')}")
    result = cpa.push_many(rows, cfg=cfg, concurrency=args.concurrency)
    print(result.get("message"))
    print(json.dumps({k: v for k, v in result.items() if k != "results"}, ensure_ascii=False, indent=2))
    fails = [r for r in result.get("results") or [] if not r.get("ok")]
    for f in fails[:20]:
        print(f"  FAIL {f.get('email') or f.get('filename')}: {f.get('error')}")
    return 0 if result.get("ok") else 1



def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Subcommand: export
    if argv and argv[0] == "export":
        return _cmd_export(argv[1:])

    # Subcommand: convert SSO → tokens (device flow)
    if argv and argv[0] in {"oauth", "sso-oauth", "device-oauth"}:
        return _cmd_sso_oauth(argv[1:])

    # Remote import
    if argv and argv[0] in {"push-sub2api", "import-sub2api", "sub2api"}:
        return _cmd_push_sub2api(argv[1:])
    if argv and argv[0] in {"push-cpa", "push-cliproxyapi", "import-cpa", "cliproxyapi", "cpa"}:
        return _cmd_push_cpa(argv[1:])
    if argv and argv[0] in {"test-sub2api", "sub2api-test"}:
        return _cmd_test_sub2api(argv[1:])
    if argv and argv[0] in {"test-cpa", "cpa-test", "test-cliproxyapi"}:
        return _cmd_test_cpa(argv[1:])

    # Pre-parse --config only, so config.json can supply defaults before full parse.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("-c", "--config", default=None)
    pre_args, _ = pre.parse_known_args(argv)

    cfg_data, cfg_path = bootstrap(pre_args.config)
    defaults = cli_defaults_from_config(cfg_data)

    parser = build_parser(defaults)
    args = parser.parse_args(argv)

    # Re-apply if user passed an explicit config path (already loaded) — no-op.
    if cfg_path is None and args.config:
        cfg_data, cfg_path = bootstrap(args.config)
        # env already set; defaults already baked into parse — OK

    provider, key, endpoint, _ = resolve_captcha(
        provider=args.captcha,
        yescaptcha_key=args.yescaptcha_key,
        # local 才用 solver_url；yescaptcha 只用官方/endpoint，避免 127.0.0.1:5072 误伤
        solver_url=args.solver_url,
    )
    # CLI flags override config; config no_oauth is default when neither flag set.
    if args.oauth:
        do_oauth = True
    elif args.no_oauth:
        do_oauth = False
    else:
        do_oauth = not bool(defaults.get("no_oauth", False))
    out_dir = None if args.no_save else args.accounts_output_dir

    print(
        f"grok-register v{__version__}: {args.count} account(s), "
        f"threads={min(args.threads, args.count)}, email={args.email}, "
        f"captcha={provider}, oauth={'on' if do_oauth else 'off'}",
        flush=True,
    )
    if cfg_path:
        print(f"  config: {cfg_path}", flush=True)
    else:
        print("  config: (none — using env / flags; see config.example.json)", flush=True)
    if provider == "local":
        print(f"  solver-url: {endpoint}", flush=True)
    elif endpoint:
        print(f"  yescaptcha-endpoint: {endpoint}", flush=True)
    if args.email == "cfmail":
        import os

        print(
            f"  cfmail: {os.environ.get('CFMAIL_BASE_URL', '?')} "
            f"domain={os.environ.get('CFMAIL_DOMAIN') or '(auto)'}",
            flush=True,
        )
    if do_oauth:
        print(f"  cliproxyapi-auth-dir: {args.cliproxyapi_auth_dir}", flush=True)
        print(f"  build-base-url:       {args.cliproxyapi_base_url}", flush=True)
    if out_dir:
        print(f"  accounts-output-dir:  {out_dir}", flush=True)
    # Proxy diagnostics (SOCKS without PySocks / dead WARP look like "没网")
    try:
        from .proxyutil import (
            apply_proxy_env,
            format_proxy_startup_lines,
            normalize_proxy,
            socks_dependency_error,
        )

        px = normalize_proxy(args.proxy) if args.proxy else ""
        if px:
            apply_proxy_env(px, force=True)
        for line in format_proxy_startup_lines(px or None):
            print(line, flush=True)
        dep_err = socks_dependency_error(px or None)
        if dep_err:
            print(dep_err, flush=True)
            return 2
    except Exception as e:
        print(f"  proxy: (diagnostic failed: {e})", flush=True)
    print(flush=True)

    t0 = time.time()
    results = run_batch(
        count=args.count,
        threads=args.threads,
        email_backend=args.email,
        captcha_provider=provider,
        yescaptcha_key=key if provider == "yescaptcha" else args.yescaptcha_key,
        solver_url=(args.solver_url or endpoint) if provider == "local" else None,
        do_oauth=do_oauth,
        oauth_headless=not args.oauth_headed,
        oauth_timeout=args.oauth_timeout,
        oauth_interactive_fallback=args.oauth_interactive_fallback,
        oauth_protocol=not args.no_oauth_protocol,
        oauth_debug=args.oauth_debug,
        cliproxyapi_auth_dir=args.cliproxyapi_auth_dir,
        cliproxyapi_base_url=args.cliproxyapi_base_url,
        accounts_output_dir=out_dir,
        proxy=args.proxy,
    )

    ok_sso = [r for r in results if r.get("sso")]
    ok_build = [r for r in results if r.get("cliproxyapi_auth")]
    fail = [r for r in results if r.get("error")]

    print(f"\n{'=' * 50}", flush=True)
    print(
        f"Done in {time.time() - t0:.0f}s  |  "
        f"SSO OK: {len(ok_sso)}  BUILD OK: {len(ok_build)}  FAIL: {len(fail)}",
        flush=True,
    )
    print(f"{'=' * 50}", flush=True)
    for r in results:
        email = r.get("email") or "?"
        if r.get("cliproxyapi_auth"):
            print(f"  {email:40s}  BUILD  {r['cliproxyapi_auth']}", flush=True)
        elif r.get("sso") and not do_oauth:
            sso = str(r["sso"])
            print(f"  {email:40s}  SSO    {sso[:36]}...", flush=True)
        elif r.get("sso") and r.get("error"):
            print(f"  {email:40s}  SSO-ok OAuth-FAIL: {r.get('error')}", flush=True)
        else:
            print(f"  {email:40s}  FAIL: {r.get('error', '?')}", flush=True)

    if args.json_out:
        print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)

    # Multi-format export for successful (and partial) rows
    if not args.no_export and (ok_sso or ok_build or any(r.get("email") for r in results)):
        fmts = [x.strip() for x in str(args.export_formats or "").split(",") if x.strip()]
        if fmts:
            try:
                written = export_results(
                    results,
                    formats=fmts,
                    export_dir=args.export_dir,
                    accounts_output_dir=out_dir,
                    cliproxyapi_auth_dir=args.cliproxyapi_auth_dir,
                    cliproxyapi_base_url=args.cliproxyapi_base_url,
                )
                print(f"\nExport → {written.get('export_dir')}", flush=True)
                for k, v in (written.get("files") or {}).items():
                    if isinstance(v, list):
                        print(f"  {k}: {len(v)} file(s)", flush=True)
                    else:
                        print(f"  {k}: {v}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: export failed: {exc}", flush=True)

    # Optional remote auto-push
    pushable = [r for r in results if isinstance(r, dict) and (r.get("sso") or r.get("oauth_access_token"))]
    if pushable:
        try:
            s2 = sub2api.normalize_config(get_section(cfg_data, "sub2api"))
            if s2.get("enabled") and s2.get("auto_push_on_register") and s2.get("base_url"):
                print("\nAuto-push → sub2api …", flush=True)
                pr = sub2api.push_many(pushable, cfg=s2)
                print(pr.get("message"), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: sub2api auto-push failed: {exc}", flush=True)
        try:
            cp = cpa.normalize_config(get_section(cfg_data, "cliproxyapi"))
            if cp.get("enabled") and cp.get("auto_push_on_register") and cp.get("base_url"):
                print("\nAuto-push → CLIProxyAPI …", flush=True)
                pr = cpa.push_many(pushable, cfg=cp)
                print(pr.get("message"), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: CPA auto-push failed: {exc}", flush=True)

    if ok_sso or ok_build:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
