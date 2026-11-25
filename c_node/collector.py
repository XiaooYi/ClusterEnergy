#!/usr/bin/env python3
"""Simulated C-node energy collector writing atomic latest.json every 2 seconds."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
import random
import signal
import socket
import sys
import time
from typing import Dict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.config import (
    DEFAULT_POLL_INTERVAL_SEC,
    HVM_OUT_FIELDS,
    N_CORE_FIELDS,
    N_DDQ_FIELDS,
    NI_OUT_FIELDS,
    SCHEMA_VERSION,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def init_baseline(rng: random.Random) -> Dict[str, float]:
    """Initialize realistic baseline wattage per rail."""
    state: Dict[str, float] = {}
    for k in N_DDQ_FIELDS:
        state[k] = rng.uniform(11.0, 14.5)
    for k in N_CORE_FIELDS:
        state[k] = rng.uniform(38.0, 52.0)
    for k in NI_OUT_FIELDS:
        state[k] = rng.uniform(7.0, 10.5)
    for k in HVM_OUT_FIELDS:
        state[k] = rng.uniform(5.5, 8.5)
    return state


def step_metrics(state: Dict[str, float], seq: int, rng: random.Random, node_offset: float) -> Dict[str, float]:
    """Advance simulated power readings smoothly."""
    wave = math.sin((seq + node_offset) * 0.15)
    metrics: Dict[str, float] = {}
    for k, base in state.items():
        if k.endswith("_CORE"):
            delta = rng.uniform(-1.8, 1.8) + 2.5 * wave
            val = max(20.0, min(85.0, base + delta))
        elif k.endswith("_DDQ"):
            delta = rng.uniform(-0.4, 0.4) + 0.5 * wave
            val = max(6.0, min(25.0, base + delta))
        else:
            delta = rng.uniform(-0.3, 0.3) + 0.3 * wave
            val = max(3.0, min(18.0, base + delta))
        state[k] = val
        metrics[k] = round(val, 3)
    return metrics


def atomic_write_json(path: str, payload: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="C-node simulated energy collector")
    parser.add_argument("--node-id", default=socket.gethostname().split(".")[0])
    parser.add_argument("--host-ip", default="127.0.0.1")
    parser.add_argument("--output", required=True, help="Output latest.json path")
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    args = parser.parse_args()

    running = True

    def handle_sig(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    seed = sum(ord(ch) for ch in args.node_id)
    rng = random.Random(seed)
    node_offset = float(seed % 10)
    state = init_baseline(rng)
    seq = 0

    while running:
        t0 = time.monotonic()
        seq += 1
        ts_now = now_iso()
        metrics = step_metrics(state, seq, rng, node_offset)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "node_id": args.node_id,
            "host_ip": args.host_ip,
            "seq": seq,
            "collected_at": ts_now,
            "sources": {
                "table1_ts": ts_now,
                "table2_ts": ts_now,
            },
            "metrics": metrics,
        }
        atomic_write_json(args.output, payload)

        elapsed = time.monotonic() - t0
        sleep_sec = max(0.1, args.interval - elapsed)
        time.sleep(sleep_sec)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
