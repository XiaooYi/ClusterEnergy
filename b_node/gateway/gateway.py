#!/usr/bin/env python3
"""B-node mTLS pull gateway with SQLite minute/daily persistence for Nginx/Vue."""

import argparse
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import sqlite3
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


TZ_CST = timezone(timedelta(hours=8))


def local_now() -> datetime:
    return datetime.now(TZ_CST)


def now_iso() -> str:
    return local_now().isoformat(timespec="milliseconds")


def create_client_ssl_context(ca_file: str, cert_file: str, key_file: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.load_verify_locations(cafile=ca_file)
    ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return ctx


class EnergyStore:
    """Thread-safe SQLite store for minute-level and daily aggregated cluster energy."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS minute_stats (
                    ts_minute TEXT PRIMARY KEY,
                    date_str  TEXT NOT NULL,
                    sum_power REAL NOT NULL,
                    max_power REAL NOT NULL,
                    sum_eff   REAL NOT NULL,
                    energy_kwh REAL NOT NULL,
                    samples   INTEGER NOT NULL
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_minute_date ON minute_stats(date_str)"
            )
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            migrated = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'tz_utc8_migrated'"
            ).fetchone()
            if not migrated:
                cnt = self.conn.execute("SELECT COUNT(*) AS c FROM minute_stats").fetchone()["c"]
                if cnt > 0:
                    self.conn.execute(
                        """
                        CREATE TABLE minute_stats_new (
                            ts_minute TEXT PRIMARY KEY,
                            date_str  TEXT NOT NULL,
                            sum_power REAL NOT NULL,
                            max_power REAL NOT NULL,
                            sum_eff   REAL NOT NULL,
                            energy_kwh REAL NOT NULL,
                            samples   INTEGER NOT NULL
                        )
                        """
                    )
                    self.conn.execute(
                        """
                        INSERT OR REPLACE INTO minute_stats_new
                        SELECT
                            strftime('%Y-%m-%dT%H:%M', replace(ts_minute, 'T', ' '), '+8 hours'),
                            strftime('%Y-%m-%d', replace(ts_minute, 'T', ' '), '+8 hours'),
                            sum_power, max_power, sum_eff, energy_kwh, samples
                        FROM minute_stats
                        """
                    )
                    self.conn.execute("DROP TABLE minute_stats")
                    self.conn.execute("ALTER TABLE minute_stats_new RENAME TO minute_stats")
                    self.conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_minute_date ON minute_stats(date_str)"
                    )
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('tz_utc8_migrated', '1')"
                )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_events (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       TEXT NOT NULL,
                    date_str TEXT NOT NULL,
                    node     TEXT NOT NULL,
                    level    TEXT NOT NULL,
                    msg      TEXT NOT NULL,
                    UNIQUE(ts, node, msg)
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_date ON alert_events(date_str)"
            )
            self.conn.commit()

    def record_sample(self, dt: datetime, total_power: float, eff_ratio: float, interval_sec: float) -> None:
        ts_minute = dt.strftime("%Y-%m-%dT%H:%M")
        date_str = dt.strftime("%Y-%m-%d")
        # Energy in kWh = Power(W) * time(h) / 1000
        delta_kwh = (max(0.0, total_power) * max(0.1, interval_sec)) / 3600000.0
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO minute_stats (ts_minute, date_str, sum_power, max_power, sum_eff, energy_kwh, samples)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(ts_minute) DO UPDATE SET
                    sum_power  = sum_power + excluded.sum_power,
                    max_power  = MAX(max_power, excluded.max_power),
                    sum_eff    = sum_eff + excluded.sum_eff,
                    energy_kwh = energy_kwh + excluded.energy_kwh,
                    samples    = samples + 1
                """,
                (ts_minute, date_str, total_power, total_power, eff_ratio, delta_kwh),
            )
            cutoff = (dt - timedelta(days=35)).strftime("%Y-%m-%d")
            self.conn.execute("DELETE FROM minute_stats WHERE date_str < ?", (cutoff,))
            self.conn.commit()

    def get_today_kwh(self) -> float:
        today_str = local_now().strftime("%Y-%m-%d")
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(energy_kwh), 0.0) AS kwh FROM minute_stats WHERE date_str = ?",
                (today_str,),
            ).fetchone()
            return round(float(row["kwh"]), 4) if row else 0.0

    def query_stats(self, range_name: str) -> Dict[str, Any]:
        now_dt = local_now()
        today_str = now_dt.strftime("%Y-%m-%d")
        with self.lock:
            if range_name == "today":
                rows = self.conn.execute(
                    """
                    SELECT ts_minute, sum_power / samples AS avg_power, max_power, energy_kwh
                    FROM minute_stats
                    WHERE date_str = ?
                    ORDER BY ts_minute ASC
                    """,
                    (today_str,),
                ).fetchall()
                points = [
                    {
                        "ts": r["ts_minute"],
                        "avg_power": round(float(r["avg_power"]), 2),
                        "max_power": round(float(r["max_power"]), 2),
                        "energy_kwh": round(float(r["energy_kwh"]), 5),
                    }
                    for r in rows
                ]
                total_kwh = round(sum(p["energy_kwh"] for p in points), 4)
                avg_p = round(sum(p["avg_power"] for p in points) / len(points), 2) if points else 0.0
                peak_p = round(max((p["max_power"] for p in points), default=0.0), 2)
                return {
                    "range": "today",
                    "total_kwh": total_kwh,
                    "avg_power": avg_p,
                    "peak_power": peak_p,
                    "points": points,
                }

            days_n = 7 if range_name == "7d" else 30
            start_date = (now_dt - timedelta(days=days_n - 1)).strftime("%Y-%m-%d")
            rows = self.conn.execute(
                """
                SELECT date_str,
                       SUM(energy_kwh) AS total_kwh,
                       SUM(sum_power) / SUM(samples) AS avg_power,
                       MAX(max_power) AS peak_power,
                       SUM(sum_eff) / SUM(samples) AS avg_eff
                FROM minute_stats
                WHERE date_str >= ?
                GROUP BY date_str
                ORDER BY date_str ASC
                """,
                (start_date,),
            ).fetchall()
            days = [
                {
                    "date": r["date_str"],
                    "energy_kwh": round(float(r["total_kwh"]), 4),
                    "avg_power": round(float(r["avg_power"]), 2),
                    "peak_power": round(float(r["peak_power"]), 2),
                    "avg_eff": round(float(r["avg_eff"]), 4),
                }
                for r in rows
            ]
            total_kwh = round(sum(d["energy_kwh"] for d in days), 4)
            avg_p = round(sum(d["avg_power"] for d in days) / len(days), 2) if days else 0.0
            peak_p = round(max((d["peak_power"] for d in days), default=0.0), 2)
            return {
                "range": f"{days_n}d",
                "total_kwh": total_kwh,
                "avg_power": avg_p,
                "peak_power": peak_p,
                "days": days,
            }

    def record_alerts(self, alerts: List[Dict[str, str]]) -> None:
        if not alerts:
            return
        now_dt = local_now()
        cutoff = (now_dt - timedelta(days=35)).strftime("%Y-%m-%d")
        with self.lock:
            for a in alerts:
                ts = str(a.get("ts") or "")
                node = str(a.get("node") or "")
                level = str(a.get("level") or "info")
                msg = str(a.get("msg") or "")
                if not ts or not node:
                    continue
                date_str = ts[:10]
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO alert_events (ts, date_str, node, level, msg)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ts, date_str, node, level, msg),
                )
            self.conn.execute("DELETE FROM alert_events WHERE date_str < ?", (cutoff,))
            self.conn.commit()

    def get_today_alerts_count(self) -> int:
        today_str = local_now().strftime("%Y-%m-%d")
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM alert_events WHERE date_str = ?",
                (today_str,),
            ).fetchone()
            return int(row["c"]) if row else 0

    def query_alerts(
        self,
        range_name: str,
        node: str,
        level: str,
        page: int,
        page_size: int,
    ) -> Dict[str, Any]:
        now_dt = local_now()
        if range_name == "today":
            start_date = now_dt.strftime("%Y-%m-%d")
        elif range_name == "7d":
            start_date = (now_dt - timedelta(days=6)).strftime("%Y-%m-%d")
        else:
            start_date = (now_dt - timedelta(days=29)).strftime("%Y-%m-%d")

        where = ["date_str >= ?"]
        params: List[Any] = [start_date]
        if node and node != "all":
            where.append("node = ?")
            params.append(node)
        if level and level != "all":
            where.append("level = ?")
            params.append(level)
        where_sql = " AND ".join(where)

        page = max(1, page)
        page_size = max(1, min(100, page_size))
        offset = (page - 1) * page_size

        with self.lock:
            total = int(
                self.conn.execute(
                    f"SELECT COUNT(*) AS c FROM alert_events WHERE {where_sql}",
                    params,
                ).fetchone()["c"]
            )
            rows = self.conn.execute(
                f"""
                SELECT ts, node, level, msg
                FROM alert_events
                WHERE {where_sql}
                ORDER BY ts DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                params + [page_size, offset],
            ).fetchall()

        total_pages = max(1, (total + page_size - 1) // page_size)
        return {
            "range": range_name,
            "total": total,
            "page": min(page, total_pages),
            "page_size": page_size,
            "total_pages": total_pages,
            "items": [
                {"ts": r["ts"], "node": r["node"], "level": r["level"], "msg": r["msg"]}
                for r in rows
            ],
        }

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class GatewayState:
    def __init__(self, upstream_base: str, ssl_ctx: ssl.SSLContext, store: EnergyStore, interval: float) -> None:
        self.upstream_base = upstream_base.rstrip("/")
        self.ssl_ctx = ssl_ctx
        self.store = store
        self.interval = interval
        self.lock = threading.Lock()
        self.snapshot: Dict[str, Any] = {
            "updated_at": None,
            "poll_round": 0,
            "cluster": {
                "total_power": 0.0,
                "core_power": 0.0,
                "ddq_power": 0.0,
                "ni_out_power": 0.0,
                "hvm_out_power": 0.0,
                "efficiency_ratio": 0.0,
                "online_nodes": 0,
                "total_nodes": 0,
                "today_kwh": 0.0,
            },
            "nodes": [],
            "alerts": [],
        }
        self.history: Dict[str, Any] = {"updated_at": None, "points": []}
        self.upstream_ok = False
        self.upstream_error: Optional[str] = "initializing"
        self.last_pull_at: Optional[str] = None
        self.last_sample_mono: Optional[float] = None

    def _get_json(self, path: str) -> Dict[str, Any]:
        req = urllib.request.Request(f"{self.upstream_base}{path}", method="GET")
        with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=2.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def poll_once(self) -> None:
        now_dt = local_now()
        ts = now_dt.isoformat(timespec="milliseconds")
        mono_now = time.monotonic()
        try:
            snap = self._get_json("/api/snapshot")
            hist = self._get_json("/api/history")
            cluster = snap.get("cluster") or {}
            total_power = float(cluster.get("total_power", 0.0))
            eff_ratio = float(cluster.get("efficiency_ratio", 0.0))
            dt_sec = self.interval if self.last_sample_mono is None else min(10.0, max(0.2, mono_now - self.last_sample_mono))
            self.last_sample_mono = mono_now
            if total_power > 0:
                self.store.record_sample(now_dt, total_power, eff_ratio, dt_sec)

            self.store.record_alerts(snap.get("alerts") or [])
            cluster["today_kwh"] = self.store.get_today_kwh()
            cluster["today_alerts"] = self.store.get_today_alerts_count()
            snap["cluster"] = cluster

            with self.lock:
                self.snapshot = snap
                self.history = hist
                self.upstream_ok = True
                self.upstream_error = None
                self.last_pull_at = ts
        except Exception as exc:
            with self.lock:
                self.upstream_ok = False
                self.upstream_error = str(exc)
                self.last_pull_at = ts

    def get_snapshot_bytes(self) -> bytes:
        with self.lock:
            out = dict(self.snapshot)
            out["gateway"] = {
                "upstream_ok": self.upstream_ok,
                "upstream_error": self.upstream_error,
                "last_pull_at": self.last_pull_at,
            }
            return json.dumps(out, ensure_ascii=False).encode("utf-8")

    def get_history_bytes(self) -> bytes:
        with self.lock:
            return json.dumps(self.history, ensure_ascii=False).encode("utf-8")


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: GatewayState = None  # type: ignore

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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            ok = self.state.upstream_ok
            body = json.dumps({"status": "ok", "upstream_ok": ok}).encode("utf-8")
            self._send_json(HTTPStatus.OK, body)
        elif path == "/api/snapshot":
            self._send_json(HTTPStatus.OK, self.state.get_snapshot_bytes())
        elif path == "/api/history":
            self._send_json(HTTPStatus.OK, self.state.get_history_bytes())
        elif path == "/api/stats":
            qs = urllib.parse.parse_qs(parsed.query)
            rng = (qs.get("range") or ["today"])[0]
            if rng not in ("today", "7d", "30d"):
                rng = "today"
            data = self.state.store.query_stats(rng)
            self._send_json(HTTPStatus.OK, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        elif path == "/api/alerts":
            qs = urllib.parse.parse_qs(parsed.query)
            rng = (qs.get("range") or ["7d"])[0]
            if rng not in ("today", "7d", "30d"):
                rng = "7d"
            node = (qs.get("node") or ["all"])[0]
            level = (qs.get("level") or ["all"])[0]
            try:
                page = int((qs.get("page") or ["1"])[0])
            except ValueError:
                page = 1
            try:
                page_size = int((qs.get("page_size") or ["12"])[0])
            except ValueError:
                page_size = 12
            data = self.state.store.query_alerts(rng, node, level, page, page_size)
            self._send_json(HTTPStatus.OK, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        else:
            self._send_json(HTTPStatus.NOT_FOUND, b'{"error":"not found"}')


def main() -> int:
    parser = argparse.ArgumentParser(description="B-node mTLS pull gateway")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--upstream", default="https://127.0.0.1:19200")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--db", default="/data/energy_history.db")
    parser.add_argument("--ca", required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()

    ssl_ctx = create_client_ssl_context(args.ca, args.cert, args.key)
    store = EnergyStore(args.db)
    state = GatewayState(args.upstream, ssl_ctx, store, args.interval)
    GatewayHandler.state = state

    stop_event = threading.Event()

    def loop() -> None:
        while not stop_event.is_set():
            t0 = time.monotonic()
            state.poll_once()
            stop_event.wait(max(0.1, args.interval - (time.monotonic() - t0)))

    threading.Thread(target=loop, daemon=True).start()

    server = ThreadingHTTPServer((args.bind, args.port), GatewayHandler)
    server.daemon_threads = True

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
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
