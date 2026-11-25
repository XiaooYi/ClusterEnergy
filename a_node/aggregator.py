#!/usr/bin/env python3
"""A-node concurrent mTLS puller, energy aggregator, and mTLS API server."""

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import random
import signal
import ssl
import sys
import threading
import time
import urllib.request
from typing import Any, Deque, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.config import (
    DEFAULT_AGGREGATOR_PORT,
    DEFAULT_EXPORTER_PORT,
    DEFAULT_HISTORY_LEN,
    DEFAULT_POLL_INTERVAL_SEC,
    HVM_OUT_FIELDS,
    N_CORE_FIELDS,
    N_DDQ_FIELDS,
    NI_OUT_FIELDS,
)
from common.tls import create_client_ssl_context, create_server_ssl_context


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def translate_error(err: Optional[str]) -> str:
    text = (err or "").lower()
    if "timed out" in text:
        return "节点响应超时"
    if "connection refused" in text:
        return "节点服务未就绪"
    if "name or service not known" in text or "no route" in text:
        return "网络不可达"
    return f"采集通信异常 ({err})" if err else "采集通信异常"


def sum_fields(metrics: Dict[str, Any], keys: List[str]) -> float:
    return round(sum(float(metrics.get(k, 0.0)) for k in keys), 3)


def compute_node_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
    metrics = raw.get("metrics") or {}
    ddq = sum_fields(metrics, N_DDQ_FIELDS)
    core = sum_fields(metrics, N_CORE_FIELDS)
    ni_out = sum_fields(metrics, NI_OUT_FIELDS)
    hvm_out = sum_fields(metrics, HVM_OUT_FIELDS)
    total = round(ddq + core + ni_out + hvm_out, 3)
    # Compute-to-total power efficiency ratio (placeholder until workload throughput is wired).
    efficiency_ratio = round(core / total, 4) if total > 0 else 0.0
    return {
        "ddq_power": ddq,
        "core_power": core,
        "ni_out_power": ni_out,
        "hvm_out_power": hvm_out,
        "total_power": total,
        "efficiency_ratio": efficiency_ratio,
    }


class AggregatorState:
    def __init__(self, targets: List[Dict[str, str]], client_ctx: ssl.SSLContext, sim_nodes: int = 0) -> None:
        self.targets = targets
        self.client_ctx = client_ctx
        self.lock = threading.Lock()
        self.sim_configs: List[Dict[str, Any]] = []
        for idx in range(sim_nodes):
            nid = f"ln{5 + idx}"
            rng = random.Random(1000 + idx)
            base: Dict[str, float] = {}
            for k in N_DDQ_FIELDS:
                base[k] = rng.uniform(11.0, 14.5)
            for k in N_CORE_FIELDS:
                base[k] = rng.uniform(36.0, 54.0)
            for k in NI_OUT_FIELDS:
                base[k] = rng.uniform(7.0, 10.5)
            for k in HVM_OUT_FIELDS:
                base[k] = rng.uniform(5.5, 8.5)
            self.sim_configs.append({"node_id": nid, "host_ip": f"25.8.100.{25 + idx}", "rng": rng, "state": base, "offset": float(idx)})

        all_meta = [{"node_id": t["node_id"], "host_ip": t["host_ip"]} for t in targets] + [
            {"node_id": s["node_id"], "host_ip": s["host_ip"]} for s in self.sim_configs
        ]
        self.nodes: Dict[str, Dict[str, Any]] = {
            t["node_id"]: {
                "node_id": t["node_id"],
                "host_ip": t["host_ip"],
                "status": "initializing",
                "seq": 0,
                "stale_ticks": 0,
                "fail_count": 0,
                "collected_at": None,
                "fetched_at": None,
                "sources": {},
                "summary": {
                    "ddq_power": 0.0,
                    "core_power": 0.0,
                    "ni_out_power": 0.0,
                    "hvm_out_power": 0.0,
                    "total_power": 0.0,
                    "efficiency_ratio": 0.0,
                },
                "metrics": {},
                "error": None,
            }
            for t in all_meta
        }
        self.history: Deque[Dict[str, Any]] = deque(maxlen=DEFAULT_HISTORY_LEN)
        self.alerts: Deque[Dict[str, str]] = deque(maxlen=50)
        self.poll_round = 0

    def _fetch_one(self, target: Dict[str, str]) -> Dict[str, Any]:
        url = f"https://{target['host_ip']}:{target['port']}/latest.json"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, context=self.client_ctx, timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def poll_once(self, pool: ThreadPoolExecutor) -> None:
        futures = {t["node_id"]: pool.submit(self._fetch_one, t) for t in self.targets}
        fetched_at = now_iso()

        results: Dict[str, Optional[Dict[str, Any]]] = {}
        errors: Dict[str, Optional[str]] = {}
        for node_id, fut in futures.items():
            try:
                results[node_id] = fut.result(timeout=1.8)
                errors[node_id] = None
            except Exception as exc:
                results[node_id] = None
                errors[node_id] = str(exc)

        with self.lock:
            self.poll_round += 1
            for t in self.targets:
                nid = t["node_id"]
                prev = self.nodes[nid]
                raw = results[nid]
                err = errors[nid]

                if raw is not None:
                    new_seq = int(raw.get("seq", 0))
                    prev_seq = int(prev.get("seq", 0))
                    stale_ticks = prev["stale_ticks"] + 1 if (new_seq == prev_seq and prev_seq > 0) else 0
                    status = "stale" if stale_ticks >= 3 else "online"
                    if prev["status"] == "offline":
                        self.alerts.appendleft({"ts": fetched_at, "level": "info", "node": nid, "msg": "节点恢复在线"})
                    elif status == "stale" and prev["status"] != "stale":
                        self.alerts.appendleft({"ts": fetched_at, "level": "warn", "node": nid, "msg": f"采集序号停滞 (seq={new_seq})"})

                    self.nodes[nid] = {
                        "node_id": nid,
                        "host_ip": t["host_ip"],
                        "status": status,
                        "seq": new_seq,
                        "stale_ticks": stale_ticks,
                        "fail_count": 0,
                        "collected_at": raw.get("collected_at"),
                        "fetched_at": fetched_at,
                        "sources": raw.get("sources") or {},
                        "summary": compute_node_summary(raw),
                        "metrics": raw.get("metrics") or {},
                        "error": None,
                    }
                else:
                    fail_count = int(prev.get("fail_count", 0)) + 1
                    prev["fail_count"] = fail_count
                    prev["error"] = err
                    if fail_count >= 3:
                        if prev["status"] != "offline":
                            self.alerts.appendleft(
                                {"ts": fetched_at, "level": "error", "node": nid, "msg": f"连续{fail_count}次拉取失败: {translate_error(err)}"}
                            )
                        prev["status"] = "offline"
                        prev["fetched_at"] = fetched_at

            for sim in self.sim_configs:
                snid = sim["node_id"]
                rng = sim["rng"]
                wave = math.sin((self.poll_round + sim["offset"]) * 0.16)
                smetrics: Dict[str, float] = {}
                for k, val in sim["state"].items():
                    if k.endswith("_CORE"):
                        nv = max(20.0, min(85.0, val + rng.uniform(-1.8, 1.8) + 2.5 * wave))
                    elif k.endswith("_DDQ"):
                        nv = max(6.0, min(25.0, val + rng.uniform(-0.4, 0.4) + 0.5 * wave))
                    else:
                        nv = max(3.0, min(18.0, val + rng.uniform(-0.3, 0.3) + 0.3 * wave))
                    sim["state"][k] = nv
                    smetrics[k] = round(nv, 3)
                raw_sim = {"metrics": smetrics}
                self.nodes[snid] = {
                    "node_id": snid,
                    "host_ip": sim["host_ip"],
                    "status": "online",
                    "seq": self.poll_round,
                    "stale_ticks": 0,
                    "fail_count": 0,
                    "collected_at": fetched_at,
                    "fetched_at": fetched_at,
                    "sources": {"table1_ts": fetched_at, "table2_ts": fetched_at},
                    "summary": compute_node_summary(raw_sim),
                    "metrics": smetrics,
                    "error": None,
                }

            cluster_total = round(sum(n["summary"]["total_power"] for n in self.nodes.values()), 3)
            cluster_core = round(sum(n["summary"]["core_power"] for n in self.nodes.values()), 3)
            cluster_ddq = round(sum(n["summary"]["ddq_power"] for n in self.nodes.values()), 3)
            cluster_ni = round(sum(n["summary"]["ni_out_power"] for n in self.nodes.values()), 3)
            cluster_hvm = round(sum(n["summary"]["hvm_out_power"] for n in self.nodes.values()), 3)

            hist_point: Dict[str, Any] = {
                "ts": fetched_at,
                "cluster_total": cluster_total,
                "nodes": {nid: n["summary"]["total_power"] for nid, n in self.nodes.items()},
            }
            self.history.append(hist_point)
            self._last_cluster = {
                "total_power": cluster_total,
                "core_power": cluster_core,
                "ddq_power": cluster_ddq,
                "ni_out_power": cluster_ni,
                "hvm_out_power": cluster_hvm,
                "efficiency_ratio": round(cluster_core / cluster_total, 4) if cluster_total > 0 else 0.0,
                "online_nodes": sum(1 for n in self.nodes.values() if n["status"] == "online"),
                "total_nodes": len(self.nodes),
            }

    def snapshot_json(self) -> bytes:
        with self.lock:
            payload = {
                "updated_at": now_iso(),
                "poll_round": self.poll_round,
                "cluster": getattr(self, "_last_cluster", {}),
                "nodes": list(self.nodes.values()),
                "alerts": list(self.alerts),
            }
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def history_json(self) -> bytes:
        with self.lock:
            payload = {
                "updated_at": now_iso(),
                "points": list(self.history),
            }
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: AggregatorState = None  # type: ignore

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, b'{"status":"ok"}')
        elif path == "/api/snapshot":
            self._send_json(HTTPStatus.OK, self.state.snapshot_json())
        elif path == "/api/history":
            self._send_json(HTTPStatus.OK, self.state.history_json())
        else:
            self._send_json(HTTPStatus.NOT_FOUND, b'{"error":"not found"}')


def parse_targets(raw_targets: List[str]) -> List[Dict[str, str]]:
    parsed = []
    for item in raw_targets:
        # Format: node_id=host_ip[:port]
        nid, addr = item.split("=", 1)
        if ":" in addr:
            host, port = addr.rsplit(":", 1)
        else:
            host, port = addr, str(DEFAULT_EXPORTER_PORT)
        parsed.append({"node_id": nid.strip(), "host_ip": host.strip(), "port": port.strip()})
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="A-node mTLS aggregator & API")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_AGGREGATOR_PORT)
    parser.add_argument("--target", action="append", required=True, help="e.g. ln2=25.8.100.22:19100")
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--client-cert", required=True)
    parser.add_argument("--client-key", required=True)
    parser.add_argument("--server-cert", required=True)
    parser.add_argument("--server-key", required=True)
    parser.add_argument("--sim-nodes", type=int, default=0, help="Extra simulated nodes for multi-node UI testing")
    args = parser.parse_args()

    targets = parse_targets(args.target)
    client_ctx = create_client_ssl_context(
        ca_file=args.ca,
        cert_file=args.client_cert,
        key_file=args.client_key,
        check_hostname=True,
    )
    state = AggregatorState(targets=targets, client_ctx=client_ctx, sim_nodes=args.sim_nodes)
    ApiHandler.state = state

    stop_event = threading.Event()

    def poller_loop() -> None:
        with ThreadPoolExecutor(max_workers=max(2, len(targets))) as pool:
            while not stop_event.is_set():
                t0 = time.monotonic()
                state.poll_once(pool)
                elapsed = time.monotonic() - t0
                stop_event.wait(max(0.1, args.interval - elapsed))

    poller_thread = threading.Thread(target=poller_loop, daemon=True)
    poller_thread.start()

    server = ThreadingHTTPServer((args.bind, args.port), ApiHandler)
    server.daemon_threads = True
    server.socket = create_server_ssl_context(
        cert_file=args.server_cert,
        key_file=args.server_key,
        ca_file=args.ca,
        require_client_cert=True,
    ).wrap_socket(server.socket, server_side=True)

    def handle_sig(_signum, _frame):
        stop_event.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
