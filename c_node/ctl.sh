#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
node="${CE_C_ID:-$(hostname -s)}"
data="${CE_DATA_DIR:-$root/runtime/$node}"
certs="${CE_CERTS_DIR:-$root/certs}"
source_file="${CE_SOURCE_FILE:-$data/latest.json}"
interval="${CE_INTERVAL:-5}"
mkdir -p "$data"

is_running() { [[ -f "$1" ]] && kill -0 "$(<"$1")" 2>/dev/null; }
start() {
    local name=$1
    shift
    if ! is_running "$data/$name.pid"; then
        nohup "$@" > "$data/$name.log" 2>&1 &
        echo "$!" > "$data/$name.pid"
    fi
}

case "${1:-status}" in
    start)
        if [[ "${CE_MODE:-real}" == simulated ]]; then
            start collector python3 "$root/c_node/collector.py" --node-id "$node" --output "$source_file" --interval "$interval"
        fi
        certificate="${CE_C_CERT_NAME:-c_server}"
        args=(--bind "${CE_BIND:-0.0.0.0}" --port "${CE_C_PORT:-19100}" --node-id "$node"
              --data-file "$source_file" --db "$data/c-replay.sqlite" --interval "$interval"
              --retention-days "${CE_C_RETENTION_DAYS:-1}" --ca "$certs/ca.crt"
              --cert "$certs/$certificate.crt" --key "$certs/$certificate.key")
        if [[ -n "${CE_SOURCE_SCHEMA:-}" ]]; then args+=(--schema "$CE_SOURCE_SCHEMA"); fi
        start exporter python3 "$root/c_node/exporter.py" "${args[@]}" ;;
    stop)
        for pid in "$data/exporter.pid" "$data/collector.pid"; do
            if is_running "$pid"; then kill "$(<"$pid")" 2>/dev/null || true; fi
            rm -f -- "$pid"
        done ;;
    restart) bash "$0" stop; bash "$0" start ;;
    status)
        if is_running "$data/exporter.pid"; then echo "exporter running"; else echo "exporter stopped"; exit 1; fi ;;
    *) echo "Usage: bash $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
