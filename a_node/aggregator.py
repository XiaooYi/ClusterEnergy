#!/usr/bin/env python3
"""Aggregate compute-node power and provide mTLS snapshot and replay APIs."""

import argparse
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
import random
import signal
import sys
import threading
import time

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from c_node.collector import init_baseline, step_metrics
from common.config import DEFAULT_AGGREGATOR_PORT, DEFAULT_EXPORTER_PORT, DEFAULT_POLL_INTERVAL_SEC, TREND_SECONDS
from common.samples import since, stamp
from common.storage import SampleStore
from common.tls import create_client_ssl_context, create_server_ssl_context
from common.transport import JsonHandler, feed_page, get_json, poll_loop, read_json_file, replay


def build_snapshot(samples, racks, now, freshness, mode):
    earliest = since(now, freshness)
    rack_power = defaultdict(float)
    rack_counts = defaultdict(int)
    total = 0.0
    online = 0
    mapped_online = 0
    sampled_at = None
    nodes = []
    for sample in sorted(samples, key=lambda item: item["node_id"]):
        node = sample["node_id"]
        ts = sample["collected_at"]
        active = earliest <= ts <= now
        rack = racks.get(node) or sample.get("rack_id")
        watts = sample["watts"] if active else None
        nodes.append({"node_id": node, "rack_id": rack, "collected_at": ts,
                      "status": "online" if active else "stale",
                      "summary": {"total_power": watts}})
        if active:
            sampled_at = max(sampled_at or ts, ts)
            total += watts
            online += 1
            if rack:
                rack_power[rack] += watts
                rack_counts[rack] += 1
                mapped_online += 1
    return {
        "updated_at": now,
        "sampled_at": sampled_at,
        "mode": mode,
        "cluster": {"total_power": round(total, 3) if online else None,
                    "online_nodes": online, "total_nodes": len(nodes),
                    "mapped_online_nodes": mapped_online},
        "nodes": nodes,
        "racks": sorted(({"rack_id": rack, "watts": round(power, 3), "online_nodes": rack_counts[rack]}
                         for rack, power in rack_power.items()),
                        key=lambda item: (-item["watts"], item["rack_id"])),
    }


class AggregatorState:
    def __init__(self, targets, context, store, racks, interval, sim_nodes=0):
        self.targets = targets
        self.context = context
        self.store = store
        self.racks = racks
        self.interval = interval
        self.mode = "simulated" if sim_nodes else "real"
        self.current = {sample["node_id"]: sample for sample in store.latest()}
        self.snapshot = build_snapshot(self.current.values(), racks, stamp(), 3 * interval, self.mode)
        self.history = deque()
        self.lock = threading.Lock()
        self.simulators = []
        self.poll_round = 0
        for index in range(sim_nodes):
            rng = random.Random(1000 + index)
            self.simulators.append((rng, init_baseline(rng)))

    def _read_source(self, source, address):
        gap = replay(source, address, self.context, self.store)
        live = get_json(address, self.context, "/api/current")["samples"]
        return live, gap

    def poll_once(self, pool):
        self.poll_round += 1
        updates = []
        statuses = {}
        if self.simulators:
            collected_at = stamp()
            for index, (rng, state) in enumerate(self.simulators):
                metrics = step_metrics(state, self.poll_round, rng, index)
                updates.append({"node_id": f"sim-cn{index + 1:03d}",
                                "rack_id": f"SIM-R{index // 4 + 1:02d}",
                                "collected_at": collected_at, "watts": round(sum(metrics.values()), 3),
                                "metrics": metrics})
            self.store.ingest("simulator", updates)
        futures = {source: pool.submit(self._read_source, source, address)
                   for source, address in self.targets.items()}
        for source, future in futures.items():
            try:
                live, gap = future.result()
                statuses[source] = {"ok": True, "history_gap": gap}
                updates.extend(live)
            except Exception as exc:
                statuses[source] = {"ok": False, "error": str(exc), "history_gap": self.store.has_gap(source)}
        for sample in updates:
            node = sample["node_id"]
            previous = self.current.get(node)
            if previous is None or sample["collected_at"] >= previous["collected_at"]:
                self.current[node] = sample
        now = stamp()
        result = build_snapshot(self.current.values(), self.racks, now, 3 * self.interval, self.mode)
        result["sources"] = statuses
        result["poll_round"] = self.poll_round
        total = result["cluster"]["total_power"]
        with self.lock:
            self.snapshot = result
            self.history.append({"ts": now, "cluster_total": total})
            earliest = since(now, TREND_SECONDS)
            while self.history and self.history[0]["ts"] < earliest:
                self.history.popleft()

    def read_history(self):
        with self.lock:
            return {"updated_at": self.snapshot["updated_at"], "points": list(self.history)}


class ApiHandler(JsonHandler):
    state = None

    def do_GET(self):
        path, params = self.route()
        try:
            if path == "/healthz":
                self.send_json(200, {"status": "ok", "mode": self.state.mode})
            elif path == "/api/snapshot":
                self.send_json(200, self.state.snapshot)
            elif path == "/api/history":
                self.send_json(200, self.state.read_history())
            elif path == "/api/samples":
                self.send_json(200, feed_page(self.state.store, params))
            else:
                self.send_json(404, {"error": "not found"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})


def parse_targets(values):
    targets = {}
    for value in values:
        name, separator, address = value.partition("=")
        if not separator or not name.strip() or not address.strip() or name in targets:
            raise ValueError("use one --target name=host:port for each C source")
        address = address.strip()
        if "://" not in address:
            if ":" not in address:
                address = f"{address}:{DEFAULT_EXPORTER_PORT}"
            address = "https://" + address
        if not address.startswith("https://"):
            raise ValueError("C targets must use HTTPS")
        targets[name] = address
    return targets


def main():
    parser = argparse.ArgumentParser(description="A-node mTLS energy aggregator")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_AGGREGATOR_PORT)
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--client-cert")
    parser.add_argument("--client-key")
    parser.add_argument("--server-cert", required=True)
    parser.add_argument("--server-key", required=True)
    parser.add_argument("--sim-nodes", type=int, default=0)
    parser.add_argument("--db")
    parser.add_argument("--racks")
    parser.add_argument("--retention-days", type=float, default=15)
    args = parser.parse_args()
    if args.sim_nodes < 0 or bool(args.target) == bool(args.sim_nodes):
        parser.error("choose --sim-nodes with a positive count or one or more --target values")
    if args.interval <= 0 or args.retention_days <= 0:
        parser.error("interval and retention must be positive")
    if args.target and not (args.client_cert and args.client_key):
        parser.error("real mode requires the C client certificate and key")
    try:
        targets = parse_targets(args.target)
        racks = read_json_file(args.racks) if args.racks else {}
        if not isinstance(racks, dict) or any(not isinstance(rack, str) for rack in racks.values()):
            raise ValueError("racks must map node identifiers to rack names")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    client = (create_client_ssl_context(args.ca, args.client_cert, args.client_key)
              if targets else None)
    server_tls = create_server_ssl_context(args.server_cert, args.server_key, args.ca)
    mode = "simulated" if args.sim_nodes else "real"
    store = SampleStore(args.db or f"runtime/a-{mode}.sqlite")
    state = AggregatorState(targets, client, store, racks, args.interval, args.sim_nodes)
    ApiHandler.state = state
    server = ThreadingHTTPServer((args.bind, args.port), ApiHandler)
    server.socket = server_tls.wrap_socket(server.socket, server_side=True)
    stop = threading.Event()
    next_cleanup = 0.0

    def collect():
        nonlocal next_cleanup
        state.poll_once(pool)
        if time.monotonic() >= next_cleanup:
            store.maintain(stamp(), args.retention_days)
            next_cleanup = time.monotonic() + 60

    def stop_server(*_args):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        thread = threading.Thread(target=poll_loop, args=(collect, args.interval, stop), daemon=True)
        thread.start()
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            stop.set()
            server.server_close()
            thread.join()
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
