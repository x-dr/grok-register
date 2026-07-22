# turnstile-solver

`grok-register` 内置的本地 Cloudflare Turnstile 过盾服务。

提供 **YesCaptcha 兼容** API，默认监听 `http://127.0.0.1:5072`。

## 快速启动（推荐）

在仓库根目录：

```bash
# 本机进程（默认，无需 Docker）
./scripts/start-solver.sh
# 或
./scripts/start-solver.sh --local

# Docker（可选）
./scripts/start-solver.sh --docker

# 停止
./scripts/stop-solver.sh
```

也可在本目录直接：

```bash
./start.sh    # 首次自动 venv + 依赖 + 浏览器
./stop.sh
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `TURNSTILE_HOST` | `0.0.0.0` | 监听地址 |
| `TURNSTILE_PORT` | `5072` | 端口 |
| `TURNSTILE_THREAD` | `2`（本机 `start.sh`）/ compose 默认 `1` | 浏览器线程；小机器建议 `1` |
| `TURNSTILE_BROWSER_TYPE` | `camoufox` | `camoufox` / `chromium` 等 |
| `SOLVER_MODE` | `local` | 根脚本：`local` 或 `docker` |

## 协议

- `POST /createTask`
- `POST /getTaskResult`

与 YesCaptcha 任务协议兼容；`grok-register` 配置：

```json
"captcha": {
  "provider": "local",
  "solver_url": "http://127.0.0.1:5072"
}
```

## 日志

```bash
# 本机
tail -n 100 logs/turnstile_solver.log

# Docker
docker logs -f grok-register-turnstile
```

## Windows

可尝试 `TurnstileSolver.bat`，或在 Git Bash / WSL 中运行 `start.sh`。
