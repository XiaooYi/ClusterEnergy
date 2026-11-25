#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${BASE_DIR}"

docker build -t cluster-energy-gateway ./gateway
docker build -t cluster-energy-web ./frontend

docker rm -f cluster-energy-gateway cluster-energy-web >/dev/null 2>&1 || true

mkdir -p "${BASE_DIR}/data"

docker run -d \
  --name cluster-energy-gateway \
  --network host \
  --restart unless-stopped \
  -e TZ=Asia/Shanghai \
  -v "${BASE_DIR}/certs:/certs:ro" \
  -v "${BASE_DIR}/data:/data" \
  cluster-energy-gateway \
  --bind 127.0.0.1 \
  --port 18081 \
  --upstream https://127.0.0.1:19200 \
  --interval 2.0 \
  --db /data/energy_history.db \
  --ca /certs/ca.crt \
  --cert /certs/b_client.crt \
  --key /certs/b_client.key

docker run -d \
  --name cluster-energy-web \
  --network host \
  --restart unless-stopped \
  cluster-energy-web

docker ps --filter "name=cluster-energy"
