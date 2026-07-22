#!/usr/bin/env bash
# 通过重新注册 Cloudflare WARP 尝试更换本地 SOCKS5 出口 IP。
# 默认端口与 install-warp-proxy.sh / config 一致：127.0.0.1:40000
#
# 用法:
#   sudo bash scripts/rotate-warp-ip.sh
#   sudo bash scripts/rotate-warp-ip.sh --port 40000
#   sudo bash scripts/rotate-warp-ip.sh --max-tries 8
#   PORT=40000 MAX_TRIES=5 sudo -E bash scripts/rotate-warp-ip.sh
#
# 环境变量:
#   WARP_PROXY_PORT / PORT   SOCKS5 端口（默认 40000）
#   MAX_TRIES               最多尝试次数（默认 5）
#
# 注意:
#   - 需 root；需已安装 cloudflare-warp（可用 install-warp-proxy.sh）
#   - 会 disconnect → registration delete → 重启 warp-svc → 重新注册并 connect
#   - 完成后端口仍保持为本脚本使用的端口，与注册机 proxy 配置保持一致
#   - 请遵守 Cloudflare 与当地法规，勿滥用

set -u

PROXY_PORT="${WARP_PROXY_PORT:-${PORT:-40000}}"
MAX_TRIES="${MAX_TRIES:-5}"
TRACE_URL="https://www.cloudflare.com/cdn-cgi/trace"
WARP_LOG="${WARP_LOG_FILE:-/var/log/warp-svc.log}"
WARP_PID_FILE="${WARP_PID_FILE:-/var/run/warp-svc.pid}"

log() { printf '[%s] [warp-rotate] %s\n' "$(date '+%F %T')" "$*"; }
err() { printf '[%s] [warp-rotate] ERROR: %s\n' "$(date '+%F %T')" "$*" >&2; }
die() { err "$*"; exit 1; }

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --port)
      PROXY_PORT="${2:-}"
      [[ -n "${PROXY_PORT}" ]] || die "--port 需要端口号"
      shift 2
      ;;
    --max-tries)
      MAX_TRIES="${2:-}"
      [[ -n "${MAX_TRIES}" ]] || die "--max-tries 需要正整数"
      shift 2
      ;;
    *) die "未知参数: $1（见 --help）" ;;
  esac
done

[[ "${PROXY_PORT}" =~ ^[0-9]+$ ]] && (( PROXY_PORT >= 1 && PROXY_PORT <= 65535 )) \
  || die "无效端口: ${PROXY_PORT}"
[[ "${MAX_TRIES}" =~ ^[0-9]+$ ]] && (( MAX_TRIES >= 1 )) \
  || die "无效 MAX_TRIES: ${MAX_TRIES}"

PROXY="socks5h://127.0.0.1:${PROXY_PORT}"

# 检测 warp-cli 是否支持 --accept-tos
if warp-cli --help 2>&1 | grep -q -- '--accept-tos'; then
  WARP_CLI=(warp-cli --accept-tos)
else
  WARP_CLI=(warp-cli)
fi

warp_run() {
  "${WARP_CLI[@]}" "$@"
}

get_ip() {
  curl -fsS \
    --proxy "${PROXY}" \
    --connect-timeout 10 \
    --max-time 20 \
    "${TRACE_URL}" 2>/dev/null |
    sed -n 's/^ip=//p' |
    head -n1
}

show_trace() {
  curl -fsS \
    --proxy "${PROXY}" \
    --connect-timeout 10 \
    --max-time 20 \
    "${TRACE_URL}" 2>/dev/null |
    grep -E '^(ip|loc|colo|warp)='
}

start_dbus() {
  if [[ -S /run/dbus/system_bus_socket ]]; then
    return 0
  fi
  if ! command -v dbus-daemon >/dev/null 2>&1; then
    log "未安装 dbus-daemon，跳过 D-Bus 启动"
    return 0
  fi
  log "启动 system D-Bus"
  mkdir -p /run/dbus
  rm -f /run/dbus/pid
  dbus-daemon --system --fork >/dev/null 2>&1 || true
}

start_warp_svc() {
  local i svc_bin

  if pgrep -x warp-svc >/dev/null 2>&1; then
    log "warp-svc 已经运行"
    return 0
  fi

  svc_bin="$(command -v warp-svc || true)"
  for c in /bin/warp-svc /usr/bin/warp-svc /usr/local/bin/warp-svc; do
    if [[ -z "${svc_bin}" && -x "$c" ]]; then
      svc_bin="$c"
    fi
  done

  if [[ -z "${svc_bin}" ]]; then
    log "未找到 warp-svc"
    return 1
  fi

  start_dbus
  mkdir -p "$(dirname "${WARP_LOG}")" "$(dirname "${WARP_PID_FILE}")"
  touch "${WARP_LOG}"

  log "直接启动 warp-svc：${svc_bin}"
  nohup "${svc_bin}" >>"${WARP_LOG}" 2>&1 </dev/null &
  echo "$!" >"${WARP_PID_FILE}"

  for i in $(seq 1 20); do
    if pgrep -x warp-svc >/dev/null 2>&1; then
      log "warp-svc 启动成功"
      return 0
    fi
    sleep 1
  done

  log "warp-svc 启动失败，最近日志："
  tail -n 30 "${WARP_LOG}" 2>/dev/null || true
  return 1
}

stop_warp_svc() {
  if ! pgrep -x warp-svc >/dev/null 2>&1; then
    return 0
  fi

  log "停止 warp-svc"
  pkill -TERM -x warp-svc >/dev/null 2>&1 || true

  for _ in $(seq 1 10); do
    if ! pgrep -x warp-svc >/dev/null 2>&1; then
      rm -f "${WARP_PID_FILE}"
      return 0
    fi
    sleep 1
  done

  pkill -KILL -x warp-svc >/dev/null 2>&1 || true
  rm -f "${WARP_PID_FILE}"
}

restart_warp_svc() {
  stop_warp_svc
  sleep 2
  start_warp_svc
}

set_proxy_mode() {
  if warp_run mode proxy >/dev/null 2>&1; then
    return 0
  fi
  warp_run set-mode proxy >/dev/null 2>&1
}

set_proxy_port() {
  if warp_run proxy port "${PROXY_PORT}" >/dev/null 2>&1; then
    return 0
  fi
  warp_run set-proxy-port "${PROXY_PORT}" >/dev/null 2>&1
}

set_masque_protocol() {
  # 新版 Proxy 模式优先使用 MASQUE；旧版本不支持时忽略
  warp_run tunnel protocol set MASQUE >/dev/null 2>&1 || true
}

wait_for_cli() {
  local i
  for i in $(seq 1 20); do
    if warp_run status >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_proxy() {
  local i ip
  for i in $(seq 1 40); do
    ip="$(get_ip || true)"
    if [[ -n "${ip}" ]]; then
      printf '%s\n' "${ip}"
      return 0
    fi
    sleep 1
  done
  return 1
}

# ---- main ----

[[ "$(id -u)" -eq 0 ]] || die "请使用 root 用户运行: sudo bash $0 $*"

command -v warp-cli >/dev/null 2>&1 ||
  die "未找到 warp-cli，请先执行: sudo bash scripts/install-warp-proxy.sh"

command -v warp-svc >/dev/null 2>&1 ||
  die "未找到 warp-svc，请检查 cloudflare-warp 是否完整安装"

command -v curl >/dev/null 2>&1 ||
  die "未安装 curl"

command -v pgrep >/dev/null 2>&1 ||
  die "未安装 pgrep，请执行: apt-get update && apt-get install -y procps"

log "目标 SOCKS5: ${PROXY}（与 install-warp-proxy.sh / 注册机默认一致）"
log "最多尝试 ${MAX_TRIES} 次"

start_warp_svc || die "warp-svc 启动失败"

wait_for_cli || {
  tail -n 50 "${WARP_LOG}" 2>/dev/null || true
  die "warp-cli 无法连接到 warp-svc"
}

# 换 IP 前先保证当前也是统一端口，避免读到旧端口上的 IP
if set_proxy_mode && set_proxy_port; then
  warp_run connect >/dev/null 2>&1 || true
fi

OLD_IP="$(get_ip || true)"
if [[ -n "${OLD_IP}" ]]; then
  log "当前 WARP IP：${OLD_IP}"
else
  log "当前无法通过 127.0.0.1:${PROXY_PORT} 获取 WARP IP（将继续尝试重新注册）"
fi

for TRY in $(seq 1 "${MAX_TRIES}"); do
  log "开始第 ${TRY}/${MAX_TRIES} 次换 IP"

  warp_run disconnect >/dev/null 2>&1 || true
  sleep 1

  warp_run registration delete >/dev/null 2>&1 || true

  if ! restart_warp_svc; then
    log "warp-svc 重启失败"
    sleep 3
    continue
  fi

  if ! wait_for_cli; then
    log "warp-cli 无法连接到 warp-svc"
    tail -n 20 "${WARP_LOG}" 2>/dev/null || true
    continue
  fi

  log "重新注册 WARP"
  if ! warp_run registration new; then
    log "WARP 注册失败"
    sleep 5
    continue
  fi

  set_masque_protocol

  if ! set_proxy_mode; then
    log "设置 Proxy 模式失败"
    continue
  fi

  if ! set_proxy_port; then
    log "设置 SOCKS5 端口 ${PROXY_PORT} 失败"
    continue
  fi

  if ! warp_run connect; then
    log "WARP 连接失败"
    continue
  fi

  NEW_IP="$(wait_for_proxy || true)"

  if [[ -z "${NEW_IP}" ]]; then
    log "等待 SOCKS5 代理启动超时"
    warp_run status || true
    log "warp-svc 最近日志："
    tail -n 30 "${WARP_LOG}" 2>/dev/null || true
    continue
  fi

  log "本次出口 IP：${NEW_IP}"

  if [[ -z "${OLD_IP}" || "${NEW_IP}" != "${OLD_IP}" ]]; then
    log "换 IP 成功：${OLD_IP:-未知} -> ${NEW_IP}"
    log "注册机请使用: socks5h://127.0.0.1:${PROXY_PORT}"
    echo
    show_trace || true
    exit 0
  fi

  log "IP 没有变化，准备再次注册"
  sleep 5
done

log "尝试 ${MAX_TRIES} 次后，出口 IP 仍未变化（代理仍应监听 ${PROXY_PORT}）"
echo
warp_run status || true
echo
show_trace || true
exit 1
