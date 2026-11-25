#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")/.." && pwd)
certs="${CE_CERTS_DIR:-$root/certs}"
data="${CE_DATA_DIR:-$root/runtime}"
mode="${CE_MODE:-real}"
mkdir -p "$data"
pid="$data/aggregator-$mode.pid"
case "${1:-status}" in
    start)
        if [[ -f "$pid" ]] && kill -0 "$(<"$pid")" 2>/dev/null; then exit 0; fi
        args=(--bind "${CE_BIND:-127.0.0.1}" --port "${CE_A_PORT:-19200}" --interval "${CE_INTERVAL:-5}"
              --db "$data/a-$mode.sqlite" --retention-days "${CE_A_RETENTION_DAYS:-15}"
              --ca "$certs/ca.crt" --server-cert "$certs/a_server.crt" --server-key "$certs/a_server.key")
        if [[ "$mode" == simulated ]]; then
            args+=(--sim-nodes "${CE_SIM_NODES:-8}")
        elif [[ "$mode" == real ]]; then
            IFS=, read -r -a targets <<< "${CE_C_TARGETS:?Set CE_C_TARGETS to name=https://host:port}"
            args+=(--client-cert "$certs/a_client.crt" --client-key "$certs/a_client.key")
            for target in "${targets[@]}"; do args+=(--target "$target"); done
        else
            echo "CE_MODE must be real or simulated" >&2
            exit 2
        fi
        if [[ -n "${CE_RACKS:-}" ]]; then args+=(--racks "$CE_RACKS"); fi
        nohup python3 "$root/a_node/aggregator.py" "${args[@]}" > "$data/aggregator-$mode.log" 2>&1 &
        echo "$!" > "$pid" ;;
    stop)
        if [[ -f "$pid" ]]; then kill "$(<"$pid")" 2>/dev/null || true; rm -f -- "$pid"; fi ;;
    restart) bash "$0" stop; bash "$0" start ;;
    status)
        if [[ -f "$pid" ]] && kill -0 "$(<"$pid")" 2>/dev/null; then
            echo "running ($(<"$pid"))"
        else
            echo "stopped"
            exit 1
        fi ;;
    *) echo "Usage: bash $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
