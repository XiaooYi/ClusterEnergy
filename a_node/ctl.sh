#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${BASE_DIR}/runtime/ln3"
CERTS_DIR="${BASE_DIR}/certs"
SSH_KEY="${RUNTIME_DIR}/id_ed25519_b"

mkdir -p "${RUNTIME_DIR}"

AGG_PID="${RUNTIME_DIR}/aggregator.pid"
TUN_PID="${RUNTIME_DIR}/tunnel.pid"

is_running() {
  local pid_file="$1"
  if [[ -f "${pid_file}" ]]; then
    local pid
    pid="$(cat "${pid_file}")"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

start_proc() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"
  shift 3
  if is_running "${pid_file}"; then
    echo "${name} already running (pid $(cat "${pid_file}"))"
    return 0
  fi
  nohup "$@" >> "${log_file}" 2>&1 < /dev/null &
  echo $! > "${pid_file}"
  echo "started ${name} (pid $!)"
}

stop_proc() {
  local name="$1"
  local pid_file="$2"
  if is_running "${pid_file}"; then
    local pid
    pid="$(cat "${pid_file}")"
    kill "${pid}" 2>/dev/null || true
    sleep 0.5
    kill -9 "${pid}" 2>/dev/null || true
    rm -f "${pid_file}"
    echo "stopped ${name} (pid ${pid})"
  else
    rm -f "${pid_file}"
    echo "${name} not running"
  fi
}

case "${1:-status}" in
  start)
    start_proc "aggregator" "${AGG_PID}" "${RUNTIME_DIR}/aggregator.log" \
      python3 "${BASE_DIR}/a_node/aggregator.py" \
      --bind 127.0.0.1 --port 19200 \
      --target ln2=25.8.100.22:19100 \
      --target ln4=25.8.100.24:19100 \
      --interval 2.0 \
      --ca "${CERTS_DIR}/ca.crt" \
      --client-cert "${CERTS_DIR}/a_client.crt" \
      --client-key "${CERTS_DIR}/a_client.key" \
      --server-cert "${CERTS_DIR}/a_server.crt" \
      --server-key "${CERTS_DIR}/a_server.key" \
      --sim-nodes 8
    if [[ -f "${SSH_KEY}" ]]; then
      chmod 600 "${SSH_KEY}"
      start_proc "tunnel" "${TUN_PID}" "${RUNTIME_DIR}/tunnel.log" \
        python3 "${BASE_DIR}/a_node/tunnel.py" \
        --remote-user user --remote-host 192.168.251.226 \
        --remote-port 19200 --local-port 19200 --ssh-key "${SSH_KEY}"
    else
      echo "tunnel skipped: ${SSH_KEY} not found"
    fi
    ;;
  stop)
    stop_proc "tunnel" "${TUN_PID}"
    stop_proc "aggregator" "${AGG_PID}"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    for pair in "aggregator:${AGG_PID}" "tunnel:${TUN_PID}"; do
      n="${pair%%:*}"; p="${pair##*:}"
      if is_running "${p}"; then
        echo "ln3 ${n}: RUNNING (pid $(cat "${p}"))"
      else
        echo "ln3 ${n}: STOPPED"
      fi
    done
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
