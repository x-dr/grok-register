# grok-register

从 [HM2899/grokcli-2api](https://github.com/HM2899/grokcli-2api) **分离出来的独立注册机 CLI**。

核心协议客户端来自上游 vendored 的 `grok-build-auth` / `xconsole_client`，去掉了 Go 主进程、PG/Redis、管理台与 HTTP sidecar，只保留：

1. 协议注册（邮箱验证 + Turnstile + create_account）
2. SSO 提取
3. 可选 Grok Build OAuth → CLIProxyAPI 兼容 auth JSON

> 使用前请阅读 [`NOTICE`](NOTICE)。本工具按 AS-IS 提供，仅供研究与自用；请遵守 x.ai / 第三方服务条款与当地法律。

---

## 安装

```bash
cd grok-register
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 可选：可编辑安装，获得 `grok-register` 命令
pip install -e .
```

```bash
cp .env.example .env
# 编辑 .env：至少配置打码 + 邮箱
```

---

## 配置（推荐 config.json）

复制示例并编辑真实密钥（`config.json` 已在 `.gitignore`）：

```bash
cp config.example.json config.json
# 编辑 config.json
```

针对你的 CF Temp Email（`https://xxxxx.xyz`）最小配置：

```json
{
  "email": "cfmail",
  "count": 1,
  "threads": 1,
  "no_oauth": true,
  "captcha": {
    "provider": "yescaptcha",
    "yescaptcha_key": "你的YesCaptcha_Key"
  },
  "cfmail": {
    "base_url": "https://xxxxx.xyz",
    "admin_password": "部署时的 ADMIN_PASSWORDS",
    "site_password": "",
    "domain": ""
  },
  "accounts_output_dir": "./accounts_output"
}
```

字段说明：

| 字段 | 说明 |
|------|------|
| `email` | `cfmail` / `tempmail` / `cloudflare` |
| `count` / `threads` | 数量与并发 |
| `no_oauth` | `true` = 只注册+SSO |
| `captcha.provider` | `yescaptcha` 或 `local` |
| `captcha.yescaptcha_key` | YesCaptcha Key |
| `captcha.solver_url` | 本地过盾地址 |
| `cfmail.base_url` | Workers API，如 `https://xxxxx.xyz` |
| `cfmail.admin_password` | 对应 `ADMIN_PASSWORDS` |
| `cfmail.domain` | 可选收信域名，空则自动选 |
| `cfmail.site_password` | 可选，站点 `PASSWORDS` |
| `proxy` | 可选 HTTP/HTTPS/SOCKS5 代理（如 `socks5h://127.0.0.1:40000`） |
| `tempmail.api_key` | `-e tempmail` 时 |
| `cloudflare.*` | 仅 D1 后端时 |

优先级：**命令行参数 > 环境变量 > config.json > 默认值**

```bash
# 自动读 ./config.json
python -m grok_register

# 指定文件
python -m grok_register -c /path/to/config.json

# 命令行覆盖数量
python -m grok_register -n 5 -t 2
```

---

## 配置（环境变量，可选）


| 变量 | 说明 |
|------|------|
| `YESCAPTCHA_API_KEY` | YesCaptcha 云打码 Key（`--captcha yescaptcha`，默认） |
| `TEMPMAIL_API_KEY` | [Tempmail.lol](https://tempmail.lol) Key（`-e tempmail`） |
| `CFMAIL_BASE_URL` | [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) API 源站，如 `https://xxxxx.xyz` |
| `CFMAIL_ADMIN_PASSWORD` | 部署的 `ADMIN_PASSWORDS`（请求头 `x-admin-auth`） |
| `CFMAIL_DOMAIN` | 可选，收信域名；不填则读 `/open_api/settings` |
| `CFMAIL_SITE_PASSWORD` | 可选，站点 `PASSWORDS`（`x-custom-auth`） |
| `GROK_REGISTER_CAPTCHA` | `yescaptcha` / `local` |
| `GROK_REGISTER_SOLVER_URL` | 本地过盾地址，默认 `http://127.0.0.1:5072` |
| `HTTPS_PROXY` / `HTTP_PROXY` | 可选代理 |
| `CLOUDFLARE_*` / `ALIAS_MAIL_DOMAINS` | 仅 `-e cloudflare`（直连 D1）时需要 |

本地过盾可使用上游项目的 `turnstile-solver`（YesCaptcha 兼容 `/createTask` API），本仓库不强制捆绑浏览器栈。

---


## 本地过盾（Turnstile Solver）

```bash
# 1) 启动本地过盾（Docker，监听 127.0.0.1:5072）
./scripts/start-solver.sh

# 2) config.json 使用 local
# "captcha": { "provider": "local", "solver_url": "http://127.0.0.1:5072" }

# 3) 注册
python -m grok_register

# 停止
./scripts/stop-solver.sh
```

首次 `docker compose` 构建会安装 Camoufox，较慢（数分钟）。  
小机器建议 `TURNSTILE_THREAD=1`。

---

## WARP 本地代理（可选）

一键安装 Cloudflare WARP，并以 **SOCKS5** 形式提供本机代理（默认 `127.0.0.1:40000`），供 `proxy` / `HTTPS_PROXY` 使用：

```bash
# 仅生成脚本在仓库内；安装需你自行执行（需 root）
sudo bash scripts/install-warp-proxy.sh
# 指定端口
sudo bash scripts/install-warp-proxy.sh --port 40000
# 状态 / 卸载
sudo bash scripts/install-warp-proxy.sh --status
sudo bash scripts/install-warp-proxy.sh --uninstall
```

安装后示例（推荐 **socks5h**，DNS 走代理；`socks5://` 也会被自动规范化为 `socks5h://`）：

```bash
export HTTPS_PROXY=socks5h://127.0.0.1:40000
export HTTP_PROXY=socks5h://127.0.0.1:40000
# 本地过盾 / 管理面板不要走代理（程序也会自动设置 NO_PROXY）
export NO_PROXY=localhost,127.0.0.1,::1

# 或 config.json:
# "proxy": "socks5h://127.0.0.1:40000"
```

**填了 proxy 却“没网”时请检查：**

1. WARP 是否在听端口：`sudo bash scripts/install-warp-proxy.sh --status`（需能 curl 出 `warp=on`）
2. 依赖：`pip install PySocks`（`requests` 走 SOCKS 必需；缺了会报 `Missing dependencies for SOCKS support`）
3. 用 `socks5h://` 而不是错误的 HTTP 代理格式
4. 本地 captcha solver（`127.0.0.1:5072`）与局域网 sub2api 会绕过代理

支持 Debian/Ubuntu 与 RHEL/CentOS/Fedora 系（官方 `cloudflare-warp` 包 + `warp-cli` proxy 模式）。无完整 systemd 的容器会自动回退：`service warp-svc start` → 直接 `nohup warp-svc`。

---
## 用法

```bash
# 查看帮助
python -m grok_register -h
# 或安装后
grok-register -h

# 注册 1 个号：SSO + Build OAuth（默认）
python -m grok_register -n 1

# 只要 SSO，不要 OAuth
python -m grok_register -n 3 -t 2 --no-oauth

# 本地过盾 + JSON 输出
python -m grok_register -n 1 --captcha local --solver-url http://127.0.0.1:5072 --json

# CF Temp Email（dreamhunter2333）
python -m grok_register -e cfmail --no-oauth

# 直连 Cloudflare D1（一般不用）
python -m grok_register -e cloudflare --no-oauth
```

### CF Temp Email（你的 Base URL）

`.env` 示例：

```env
CFMAIL_BASE_URL=https://xxxxx.xyz
CFMAIL_ADMIN_PASSWORD=你的ADMIN_PASSWORDS
# CFMAIL_DOMAIN=你的收信域名   # 可选
YESCAPTCHA_API_KEY=...
```

```bash
python -m grok_register -e cfmail -n 1 --no-oauth
```

说明：
- Base URL 填 **Workers 后端 API 域名**（你给的 `https://xxxxx.xyz/`），不要带 `/admin` 页面路径
- `CFMAIL_ADMIN_PASSWORD` = 部署文档里的 **ADMIN_PASSWORDS**（不是 Cloudflare API Token）
- 自动化走 `POST /admin/new_address` + 地址 JWT 收信，无需 D1 Token

### 常用参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `-n` / `--count` | `1` | 数量 |
| `-t` / `--threads` | `1` | 并发（注册阶段；OAuth 串行） |
| `-e` / `--email` | `tempmail` | `tempmail` \| `cfmail` \| `cloudflare` |
| `--captcha` | env / `yescaptcha` | `yescaptcha` \| `local` |
| `--solver-url` | env / `http://127.0.0.1:5072` | 过盾 API |
| `--no-oauth` | off | 只注册 + SSO |
| `--accounts-output-dir` | `./accounts_output` | 账号 JSON 目录 |
| `--cliproxyapi-auth-dir` | `./cliproxyapi_auth` | CLIProxyAPI auth 目录 |
| `--json` | off | 结束后打印完整 JSON |
| `--no-save` | off | 不落盘 accounts_output |

---



## 远程导入 sub2api / CLIProxyAPI

在 `config.json` 配置：

```json
{
  "sub2api": {
    "enabled": true,
    "base_url": "http://127.0.0.1:8080",
    "email": "admin@example.com",
    "password": "your-password",
    "group_name": "grok-register",
    "auto_create_group": true,
    "auto_push_on_register": false,
    "concurrency": 4,
    "account_concurrency": 3,
    "account_priority": 50
  },
  "cliproxyapi": {
    "enabled": true,
    "base_url": "http://127.0.0.1:8317",
    "management_key": "your-management-key",
    "auto_push_on_register": false,
    "concurrency": 4,
    "base_upstream": "https://cli-chat-proxy.grok.com/v1"
  }
}
```

### 命令

```bash
# 连通性
python -m grok_register test-sub2api
python -m grok_register test-cpa

# 从导出目录 / auth.json 推送
python -m grok_register push-sub2api ./exports
python -m grok_register push-sub2api ./exports/auth/auth.json
python -m grok_register push-cpa ./exports ./cliproxyapi_auth

# 注册成功后自动推送：把对应 auto_push_on_register 设为 true
```

| 目标 | API |
|------|-----|
| sub2api | `POST /api/v1/auth/login` → `POST /api/v1/admin/accounts`（oauth）；无 token 时走 `sso-to-oauth` 或本地 device-flow |
| CLIProxyAPI | `POST /v0/management/auth-files`（management key） |

> CPA 推送需要 access_token；仅 SSO 的账号请先 `python -m grok_register oauth --from-json ...` 再 push-cpa。

---
## 多格式导出

注册成功后默认写入 `exports/<格式>/`（可在 `config.json` → `export` 配置）：

| 格式 | 路径 | 用途 |
|------|------|------|
| `auth` | `exports/auth/auth.json` / `auth-<ts>.json` | grokcli-2api 号池：`{"auth":{"https://auth.x.ai::<uid>":{...}}}` |
| `sub2api` | `exports/sub2api/sub2api-data-<ts>.json` | sub2api「导入数据」：`type=sub2api-data` |
| `cliproxyapi` | `exports/cliproxyapi/cliproxyapi-bundle-*.json` + `cliproxyapi_auth/xai-*.json`（或 `exports/cliproxyapi/auth/`） | CLIProxyAPI 单账号 auth |
| `bundle` | `accounts_output/account_*.json`（或 `exports/bundle/`） | 完整原始结果 |
| `sso` | `exports/sso/sso-list-*.json` / `.txt`、`sso_*.txt` | `sso-list`: `email----password----sso`；`sso_*.txt`: 纯 SSO 每行一个 |

```json
"export": {
  "enabled": true,
  "dir": "./exports",
  "formats": ["auth", "sub2api", "cliproxyapi", "bundle", "sso"]
}
```

```bash
# 注册并自动导出（需 OAuth 才会有 access/refresh；--no-oauth 仍可导出 sso）
python -m grok_register

# 从已有 JSON 再导出
python -m grok_register export ./accounts_output --export-dir ./exports
python -m grok_register export ./exports/auth/auth.json --export-formats sub2api,cliproxyapi
```

> 完整 OIDC 字段（`key` / `refresh_token` / sub2api oauth credentials）需要 **不要** `--no-oauth`（或 config `no_oauth: false`）。

---
## 输出

成功时每个账号一份 JSON（`accounts_output/account_<email>_<ts>.json`），字段包括：

- `email` / `password`
- `sso` — Grok SSO JWT
- `oauth_access_token` / `oauth_refresh_token`（未 `--no-oauth` 时）
- `cliproxyapi_auth` — CLIProxyAPI 兼容 auth 文件路径

---

## 目录结构

```
grok-register/
├── grok_register/          # CLI 入口与编排
│   ├── cli.py
│   └── register.py
├── xconsole_client/        # 协议客户端（来自 grok-build-auth）
├── alias_mail/             # Cloudflare D1 邮箱可选后端
├── requirements.txt
├── pyproject.toml
├── .env.example
└── NOTICE
```

与完整 `grokcli-2api` 的关系：

| 原路径 | 本仓库 |
|--------|--------|
| `grok-build-auth/xconsole_client/*` | `xconsole_client/*` |
| `grok-build-auth/run.py` | `grok_register/register.py` + `cli.py` |
| `scripts/registration_service.py` | **不包含**（HTTP sidecar） |
| `grok2api/upstream/grok_build_adapter.py` | **不包含**（池化 / Redis / 管理台编排） |
| `turnstile-solver/` | **不包含**（可用外部进程，`--captcha local`） |

---

## 与原项目差异

- **纯 CLI**，无 FastAPI 内网服务、无 Go 管理台 facade
- 过盾顺序：先 Turnstile，再发邮箱验证码（降低验证码过期）
- 邮箱提供方：`tempmail` / `cfmail`（cloudflare_temp_email）/ `cloudflare`（D1）（完整版管理台还支持 MoeMail / YYDS / GPTMail 等，未迁入以保持精简）
- 不自动写 Postgres 号池；结果落本地 JSON / CLIProxyAPI auth 目录

---

## 许可与免责

见 [`NOTICE`](NOTICE)。使用本工具即表示你已阅读并接受其中条款。
