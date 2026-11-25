#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_ID="$(hostname -s)"
RUNTIME_DIR="${BASE_DIR}/runtime/${NODE_ID}"
CERTS_DIR="${BASE_DIR}/certs"
DATA_FILE="${RUNTIME_DIR}/latest.json"

case "${NODE_ID}" in
  ln2) HOST_IP="25.8.100.22" ;;
  ln4) HOST_IP="25.8.100.24" ;;
  *)   HOST_IP="$(hostname -I | awk '{print $1}')" ;;
esac

mkdir -p "${RUNTIME_DIR}"

COLLECTOR_PID="${RUNTIME_DIR}/collector.pid"
EXPORTER_PID="${RUNTIME_DIR}/exporter.pid"

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
    start_proc "collector-${NODE_ID}" "${COLLECTOR_PID}" "${RUNTIME_DIR}/collector.log" \
      python3 "${BASE_DIR}/c_node/collector.py" \
      --node-id "${NODE_ID}" --host-ip "${HOST_IP}" --output "${DATA_FILE}" --interval 2.0
    sleep 0.5
    start_proc "exporter-${NODE_ID}" "${EXPORTER_PID}" "${RUNTIME_DIR}/exporter.log" \
      python3 "${BASE_DIR}/c_node/exporter.py" \
      --bind 0.0.0.0 --port 19100 --node-id "${NODE_ID}" --data-file "${DATA_FILE}" \
      --cert "${CERTS_DIR}/${NODE_ID}.crt" --key "${CERTS_DIR}/${NODE_ID}.key" --ca "${CERTS_DIR}/ca.crt"
    ;;
  stop)
    stop_proc "exporter-${NODE_ID}" "${EXPORTER_PID}"
    stop_proc "collector-${NODE_ID}" "${COLLECTOR_PID}"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    for pair in "collector:${COLLECTOR_PID}" "exporter:${EXPORTER_PID}"; do
      n="${pair%%:*}"; p="${pair##*:}"
      if is_running "${p}"; then
        echo "${NODE_ID} ${n}: RUNNING (pid $(cat "${p}"))"
      else
        echo "${NODE_ID} ${n}: STOPPED"
      fi
    done
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
