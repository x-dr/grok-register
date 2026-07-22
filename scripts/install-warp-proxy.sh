#!/usr/bin/env bash
# 一键安装 Cloudflare WARP，并配置为本地 SOCKS5 代理（默认 127.0.0.1:40000）
# 供 grok-register 的 proxy / HTTPS_PROXY 使用。
#
# 用法:
#   bash scripts/install-warp-proxy.sh              # 安装并启动 proxy 模式
#   bash scripts/install-warp-proxy.sh --port 1080  # 指定 SOCKS5 端口
#   bash scripts/install-warp-proxy.sh --status     # 仅查看状态
#   bash scripts/install-warp-proxy.sh --uninstall  # 卸载 WARP
#
# 服务启动顺序（兼容无完整 systemd 的容器）:
#   1) systemctl（若真正可用）
#   2) service warp-svc start
#   3) nohup 直接后台运行 warp-svc
#
# 安装完成后示例:
#   export HTTPS_PROXY=socks5h://127.0.0.1:40000
#   # 或 config.json: "proxy": "socks5h://127.0.0.1:40000"
#
# 注意: 本脚本只负责生成/安装 WARP 代理环境；请遵守 Cloudflare 与当地法规。

set -euo pipefail

PROXY_PORT="${WARP_PROXY_PORT:-40000}"
ACTION="install"

log()  { echo "[warp] $*"; }
err()  { echo "[warp] ERROR: $*" >&2; }
die()  { err "$*"; exit 1; }

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --port)
      PROXY_PORT="${2:-}"
      [[ -n "$PROXY_PORT" ]] || die "--port 需要端口号"
      shift 2
      ;;
    --status) ACTION="status"; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    *) die "未知参数: $1（见 --help）" ;;
  esac
done

[[ "$PROXY_PORT" =~ ^[0-9]+$ ]] && (( PROXY_PORT >= 1 && PROXY_PORT <= 65535 )) \
  || die "无效端口: $PROXY_PORT"

need_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "请使用 root 运行: sudo bash $0 $*"
  fi
}

detect_os() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    OS_ID="${ID:-}"
    OS_LIKE="${ID_LIKE:-}"
  else
    OS_ID=""
    OS_LIKE=""
  fi
}

has_cmd() { command -v "$1" >/dev/null 2>&1; }

wait_warp_ready() {
  local i
  for i in $(seq 1 30); do
    if warp-cli --accept-tos status >/dev/null 2>&1 \
      || warp-cli status >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

warp_cli() {
  # 新版可能需要 --accept-tos；旧版没有该参数时回退
  if warp-cli --accept-tos "$@" 2>/dev/null; then
    return 0
  fi
  warp-cli "$@"
}

show_status() {
  if ! has_cmd warp-cli; then
    log "未安装 warp-cli"
    return 1
  fi
  log "warp-cli 版本: $(warp-cli --version 2>/dev/null || echo unknown)"
  log "状态:"
  warp_cli status 2>/dev/null || warp-cli status || true
  log "设置:"
  warp_cli settings 2>/dev/null || true
  log "本机 SOCKS5 代理: socks5://127.0.0.1:${PROXY_PORT}"
  if has_cmd curl; then
    log "出口 IP 探测（经 WARP SOCKS5，超时 15s）:"
    curl -fsS -m 15 --socks5-hostname "127.0.0.1:${PROXY_PORT}" \
      https://cloudflare.com/cdn-cgi/trace 2>/dev/null \
      | grep -E '^(ip|warp|colo)=' || log "探测失败（服务可能未就绪）"
  fi
}

uninstall_warp() {
  need_root
  log "停止 WARP..."
  stop_warp_service || true

  detect_os
  if has_cmd apt-get && { [[ "$OS_ID" == "debian" || "$OS_ID" == "ubuntu" ]] || [[ "$OS_LIKE" == *debian* ]]; }; then
    apt-get remove -y cloudflare-warp 2>/dev/null || true
    rm -f /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    rm -f /etc/apt/sources.list.d/cloudflare-client.list
    apt-get update -y || true
  elif has_cmd dnf || has_cmd yum; then
    (dnf remove -y cloudflare-warp || yum remove -y cloudflare-warp) 2>/dev/null || true
    rm -f /etc/yum.repos.d/cloudflare-warp.repo
  else
    die "无法自动卸载：不支持的包管理器"
  fi
  log "卸载完成"
}

install_debian() {
  need_root
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y curl gnupg lsb-release ca-certificates apt-transport-https

  local codename
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
  if [[ -z "$codename" ]] && has_cmd lsb_release; then
    codename="$(lsb_release -cs)"
  fi
  [[ -n "$codename" ]] || die "无法检测 Debian/Ubuntu codename"

  curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
    | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg

  echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ ${codename} main" \
    > /etc/apt/sources.list.d/cloudflare-client.list

  apt-get update -y
  apt-get install -y cloudflare-warp
}

install_rhel() {
  need_root
  local releasever
  releasever="$(rpm -E %rhel 2>/dev/null || rpm -E %centos 2>/dev/null || echo 8)"
  # Cloudflare 仓库常用 8/9 等大版本号
  if [[ ! "$releasever" =~ ^[0-9]+$ ]]; then
    releasever=8
  fi

  cat > /etc/yum.repos.d/cloudflare-warp.repo <<REPO
[cloudflare-client]
name=Cloudflare Client
baseurl=https://pkg.cloudflareclient.com/rpm
enabled=1
gpgcheck=1
gpgkey=https://pkg.cloudflareclient.com/pubkey.gpg
REPO

  if has_cmd dnf; then
    dnf install -y cloudflare-warp
  else
    yum install -y cloudflare-warp
  fi
}

# 判断 systemctl 是否真正可用（很多容器里 systemctl 是 stub）
systemd_usable() {
  has_cmd systemctl || return 1
  # stub 常见输出含 "is not running in this container"
  local out
  out="$(systemctl is-system-running 2>&1 || true)"
  case "$out" in
    *not\ running*|*Failed\ to\ connect*|*System\ has\ not\ been\ booted*)
      return 1
      ;;
  esac
  # running / degraded / starting 等视为可用
  case "$out" in
    running|degraded|starting|initializing|maintenance)
      return 0
      ;;
  esac
  # 再试一次 list-units；stub 通常会失败或打印提示
  if systemctl list-units --type=service >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

find_warp_svc_bin() {
  local c
  for c in /bin/warp-svc /usr/bin/warp-svc /usr/local/bin/warp-svc; do
    if [[ -x "$c" ]]; then
      echo "$c"
      return 0
    fi
  done
  if has_cmd warp-svc; then
    command -v warp-svc
    return 0
  fi
  return 1
}

WARP_PID_FILE="${WARP_PID_FILE:-/var/run/warp-svc.pid}"
WARP_LOG_FILE="${WARP_LOG_FILE:-/var/log/warp-svc.log}"

warp_svc_running() {
  if has_cmd pgrep && pgrep -x warp-svc >/dev/null 2>&1; then
    return 0
  fi
  if [[ -f "$WARP_PID_FILE" ]]; then
    local pid
    pid="$(cat "$WARP_PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

start_warp_svc_direct() {
  local bin
  bin="$(find_warp_svc_bin)" || die "找不到 warp-svc 可执行文件"
  if warp_svc_running; then
    log "warp-svc 已在运行"
    return 0
  fi
  mkdir -p "$(dirname "$WARP_PID_FILE")" "$(dirname "$WARP_LOG_FILE")"
  log "无可用 systemd，直接后台启动: $bin"
  # 官方 daemon；无 systemd 时 nohup 常驻
  nohup "$bin" >>"$WARP_LOG_FILE" 2>&1 &
  echo $! >"$WARP_PID_FILE"
  sleep 1
  if ! warp_svc_running; then
    # 部分版本会自行 daemonize，父进程立刻退出；再用进程名判断
    sleep 2
  fi
  if ! warp_svc_running && ! wait_warp_ready; then
    err "直接启动失败，最近日志:"
    tail -n 40 "$WARP_LOG_FILE" 2>/dev/null || true
    return 1
  fi
  return 0
}

stop_warp_service() {
  if systemd_usable; then
    systemctl stop warp-svc 2>/dev/null || true
    systemctl disable warp-svc 2>/dev/null || true
  fi
  if has_cmd service; then
    service warp-svc stop 2>/dev/null || true
  fi
  if [[ -f "$WARP_PID_FILE" ]]; then
    local pid
    pid="$(cat "$WARP_PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]]; then
      kill "$pid" 2>/dev/null || true
      rm -f "$WARP_PID_FILE"
    fi
  fi
  if has_cmd pkill; then
    pkill -x warp-svc 2>/dev/null || true
  fi
}

try_start_via_service() {
  has_cmd service || return 1
  # 捕获 stub/错误输出，避免误判成功
  local out
  out="$(service warp-svc start 2>&1)" || {
    [[ -n "$out" ]] && log "service: $out"
    return 1
  }
  if echo "$out" | grep -qiE 'not running in this container|unrecognized service|not found'; then
    log "service 不可用: $out"
    return 1
  fi
  [[ -n "$out" ]] && log "service: $out"
  return 0
}

try_start_via_systemctl() {
  systemd_usable || return 1
  log "尝试 systemctl 启动 warp-svc"
  systemctl enable warp-svc >/dev/null 2>&1 || true
  local out rc=0
  out="$(systemctl start warp-svc 2>&1)" || rc=$?
  if echo "$out" | grep -qiE 'not running in this container|Failed to connect to bus|System has not been booted'; then
    log "systemctl 在此环境不可用，将回退"
    return 1
  fi
  if [[ $rc -ne 0 ]]; then
    [[ -n "$out" ]] && log "systemctl start 失败: $out"
    return 1
  fi
  return 0
}

ensure_service() {
  need_root
  log "启动 warp-svc..."

  # 已在跑则跳过
  if wait_warp_ready; then
    log "warp-svc 已在运行"
    return 0
  fi

  local started=0
  if try_start_via_systemctl; then
    started=1
  elif try_start_via_service; then
    log "已通过 service 启动 warp-svc"
    started=1
  elif start_warp_svc_direct; then
    log "已直接后台启动 warp-svc"
    started=1
  fi

  if [[ "$started" -ne 1 ]]; then
    die "无法启动 warp-svc（systemctl / service / 直接启动均失败）"
  fi

  if wait_warp_ready; then
    log "warp-svc 已就绪"
    return 0
  fi

  # systemctl/service 可能“假成功”，再强制直接拉起一次
  log "服务未就绪，强制直接启动 warp-svc 重试..."
  start_warp_svc_direct || true
  if wait_warp_ready; then
    log "warp-svc 已就绪（直接启动）"
    return 0
  fi

  err "warp-svc 启动超时"
  systemd_usable && systemctl status warp-svc --no-pager 2>/dev/null || true
  has_cmd service && service warp-svc status 2>/dev/null || true
  tail -n 40 "$WARP_LOG_FILE" 2>/dev/null || true
  die "请检查日志后重试。容器内可手动: service warp-svc start 或 nohup warp-svc &"
}

configure_proxy_mode() {
  log "注册 WARP 客户端（若已注册会跳过）..."
  # registration new 在已注册时可能失败，忽略
  warp_cli registration new 2>/dev/null || true

  log "切换为 proxy 模式，端口 ${PROXY_PORT}..."
  # 不同版本子命令略有差异，尽量兼容
  if ! warp_cli mode proxy 2>/dev/null; then
    warp_cli set-mode proxy 2>/dev/null || die "无法设置 proxy 模式，请检查 warp-cli 版本"
  fi

  if ! warp_cli proxy port "${PROXY_PORT}" 2>/dev/null; then
    if ! warp_cli set-proxy-port "${PROXY_PORT}" 2>/dev/null; then
      # 极旧版本可能无不同命令
      warp_cli mode proxy >/dev/null 2>&1 || true
      log "警告: 未能通过 CLI 设置端口，将使用 WARP 默认代理端口（多为 40000）"
      PROXY_PORT=40000
    fi
  fi

  log "连接 WARP..."
  warp_cli connect 2>/dev/null || true

  sleep 2
  local st
  st="$(warp_cli status 2>/dev/null || true)"
  log "当前状态摘要:"
  echo "$st" | head -n 20 || true
}

print_usage_hint() {
  cat <<HINT

========================================
WARP SOCKS5 代理已配置
  地址: socks5://127.0.0.1:${PROXY_PORT}
========================================

在当前 shell 使用:

  export HTTPS_PROXY=socks5h://127.0.0.1:${PROXY_PORT}
  export HTTP_PROXY=socks5h://127.0.0.1:${PROXY_PORT}
  export ALL_PROXY=socks5h://127.0.0.1:${PROXY_PORT}
  export NO_PROXY=localhost,127.0.0.1,::1

或写入 grok-register 配置 (config.json):

  "proxy": "socks5h://127.0.0.1:${PROXY_PORT}"

或 .env:

  HTTPS_PROXY=socks5h://127.0.0.1:${PROXY_PORT}
  HTTP_PROXY=socks5h://127.0.0.1:${PROXY_PORT}

常用命令:

  sudo bash scripts/install-warp-proxy.sh --status
  sudo bash scripts/rotate-warp-ip.sh          # 换出口 IP（默认端口同上 40000）
  warp-cli status
  warp-cli disconnect
  warp-cli connect

HINT
}

install_warp() {
  detect_os
  if has_cmd warp-cli; then
    log "已检测到 warp-cli，跳过包安装"
  else
    log "安装 Cloudflare WARP 客户端..."
    if has_cmd apt-get && { [[ "$OS_ID" == "debian" || "$OS_ID" == "ubuntu" ]] || [[ "$OS_LIKE" == *debian* || "$OS_LIKE" == *ubuntu* ]]; }; then
      install_debian
    elif has_cmd dnf || has_cmd yum; then
      install_rhel
    else
      die "暂不支持的系统（需要 Debian/Ubuntu 或 RHEL/CentOS/Fedora 系）"
    fi
    has_cmd warp-cli || die "安装后仍找不到 warp-cli"
  fi

  ensure_service
  configure_proxy_mode
  show_status || true
  print_usage_hint
}

case "$ACTION" in
  install) install_warp ;;
  status) show_status ;;
  uninstall) uninstall_warp ;;
  *) die "未知 action: $ACTION" ;;
esac
