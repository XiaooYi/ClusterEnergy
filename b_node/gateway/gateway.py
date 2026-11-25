#!/usr/bin/env python3
"""B-node mTLS pull gateway with SQLite minute/daily persistence for Nginx/Vue."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


TZ_CST = timezone(timedelta(hours=8))


def local_now() -> datetime:
    return datetime.now(TZ_CST)


def now_iso() -> str:
    return local_now().isoformat(timespec="milliseconds")


PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from b_node.gateway.jobs import estimate_jobs, running_jobs
from common.config import DEFAULT_POLL_INTERVAL_SEC
from common.samples import since, stamp, utc_time
from common.storage import SampleStore
from common.tls import create_client_ssl_context
from common.transport import JsonHandler, get_json, int_param, poll_loop, read_json_file, replay


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


    def record_cluster(self, sampled_at, power, max_gap, retention_days):
        when = utc_time(sampled_at)
        with self.lock, self.conn:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'cluster_sample'").fetchone()
            previous = json.loads(row[0]) if row else None
            if previous is not None and sampled_at <= previous["ts"]:
                return
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('cluster_sample', ?)",
                              (json.dumps({"ts": sampled_at, "power": power}),))
            if power is None:
                return
            energy = 0.0
            if previous is not None and previous["power"] is not None:
                seconds = (when - utc_time(previous["ts"])).total_seconds()
                if 0 < seconds <= max_gap:
                    energy = previous["power"] * seconds / 3600000
            dt = when.astimezone(TZ_CST)
            self.conn.execute("""
                INSERT INTO minute_stats
                    (ts_minute, date_str, sum_power, max_power, sum_eff, energy_kwh, samples)
                VALUES (?, ?, ?, ?, 0, ?, 1)
                ON CONFLICT(ts_minute) DO UPDATE SET
                    sum_power = sum_power + excluded.sum_power,
                    max_power = MAX(max_power, excluded.max_power),
                    energy_kwh = energy_kwh + excluded.energy_kwh, samples = samples + 1
            """, (dt.strftime("%Y-%m-%dT%H:%M"), dt.strftime("%Y-%m-%d"), power, power, energy))
            cutoff = (dt - timedelta(days=retention_days)).strftime("%Y-%m-%d")
            self.conn.execute("DELETE FROM minute_stats WHERE date_str < ?", (cutoff,))


    def get_today_kwh(self) -> float:
        return self.card_energy()["today_kwh"]

    def card_energy(self):
        today = local_now()
        today_str = today.strftime("%Y-%m-%d")
        week_start = (today - timedelta(days=6)).strftime("%Y-%m-%d")
        with self.lock:
            row = self.conn.execute("""
                SELECT COALESCE(SUM(CASE WHEN date_str = ? THEN energy_kwh ELSE 0 END), 0) AS today_kwh,
                       COALESCE(SUM(energy_kwh), 0) AS energy_7d_kwh,
                       COUNT(DISTINCT date_str) AS energy_7d_days
                FROM minute_stats
                WHERE date_str BETWEEN ? AND ? AND ts_minute <= ?
            """, (today_str, week_start, today_str, today.strftime("%Y-%m-%dT%H:%M"))).fetchone()
        return {"today_kwh": round(row["today_kwh"], 4), "energy_7d_kwh": round(row["energy_7d_kwh"], 4),
                "energy_7d_days": row["energy_7d_days"]}

    def query_stats(self, range_name: str) -> Dict[str, Any]:
        now_dt = local_now()
        today_str = now_dt.strftime("%Y-%m-%d")
        with self.lock:
            if range_name == "today":
                rows = self.conn.execute(
                    """
                    SELECT ts_minute, sum_power / samples AS avg_power, max_power, energy_kwh
                    FROM minute_stats
                    WHERE date_str = ? AND ts_minute <= ?
                    ORDER BY ts_minute ASC
                    """,
                    (today_str, now_dt.strftime("%Y-%m-%dT%H:%M")),
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
                    "date": today_str,
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
                WHERE date_str BETWEEN ? AND ? AND ts_minute <= ?
                GROUP BY date_str
                ORDER BY date_str ASC
                """,
                (start_date, today_str, now_dt.strftime("%Y-%m-%dT%H:%M")),
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

    def _insert_alerts(self, alerts: List[Dict[str, str]]) -> int:
        if not alerts:
            return 0
        now_dt = local_now()
        cutoff = (now_dt - timedelta(days=35)).strftime("%Y-%m-%d")
        rows = []
        for event in alerts:
            ts = str(event.get("ts") or "")
            node = str(event.get("node") or "")
            if ts and node and ts[:10] >= cutoff:
                rows.append((ts, ts[:10], node, str(event.get("level") or "info"),
                             str(event.get("msg") or "")))
        inserted = self.conn.executemany("""
            INSERT OR IGNORE INTO alert_events (ts, date_str, node, level, msg)
            VALUES (?, ?, ?, ?, ?)
        """, rows).rowcount
        self.conn.execute("DELETE FROM alert_events WHERE date_str < ?", (cutoff,))
        return inserted

    def record_alerts(self, alerts: List[Dict[str, str]]) -> int:
        with self.lock, self.conn:
            return self._insert_alerts(alerts)

    def import_alerts(self, legacy_db: Path) -> int:
        source = Path(legacy_db).resolve(strict=True)
        if Path(self.db_path).resolve() == source:
            raise ValueError("legacy alerts must come from a separate database")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            alerts = [dict(row) for row in connection.execute("SELECT ts, node, level, msg FROM alert_events")]
        return self.record_alerts(alerts)

    def record_health(self, observations) -> None:
        with self.lock, self.conn:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'monitor_health'").fetchone()
            previous = json.loads(row[0]) if row else {}
            current = dict(previous)
            alerts = []
            ts = now_iso()
            for key, (ok, name, level, failed, restored) in observations.items():
                current[key] = ok
                if ok != previous.get(key, True):
                    alerts.append({"ts": ts, "node": name, "level": "info" if ok else level,
                                   "msg": restored if ok else failed})
            if current != previous:
                self._insert_alerts(alerts)
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('monitor_health', ?)",
                                  (json.dumps(current, ensure_ascii=False),))

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
            nodes = self.conn.execute("""
                SELECT DISTINCT node FROM alert_events WHERE date_str >= ? ORDER BY node
            """, (start_date,)).fetchall()

        total_pages = max(1, (total + page_size - 1) // page_size)
        return {
            "range": range_name,
            "total": total,
            "page": min(page, total_pages),
            "page_size": page_size,
            "total_pages": total_pages,
            "nodes": [row["node"] for row in nodes],
            "items": [
                {"ts": r["ts"], "node": r["node"], "level": r["level"], "msg": r["msg"]}
                for r in rows
            ],
        }

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class GatewayState:
    def __init__(self, upstream, context, stats, archive, interval, retention, hot_days, mode, jobs_file,
                 sim_clock_offset_seconds=0):
        self.upstream = upstream
        self.context = context
        self.stats = stats
        self.archive = archive
        self.interval = interval
        self.retention = retention
        self.hot_days = hot_days
        self.mode = mode
        self.jobs_file = jobs_file
        self.sim_clock_offset = timedelta(seconds=sim_clock_offset_seconds)
        self.snapshot = {"nodes": [], "racks": [], "cluster": {}, "mode": mode}
        self.history = {"points": []}
        self.upstream_ok = False
        self.upstream_error = None
        self.history_error = None
        self.history_gap = archive.has_gap("A")
        self.next_cleanup = 0.0

    def shift_times(self, record, *fields):
        for field in fields:
            if record.get(field):
                record[field] = stamp(utc_time(record[field]) + self.sim_clock_offset)
        return record

    def poll_once(self):
        try:
            snapshot = get_json(self.upstream, self.context, "/api/snapshot")
            if snapshot.get("mode") != self.mode:
                raise ValueError("A and B must use the same real or simulated mode")
            history = get_json(self.upstream, self.context, "/api/history")
            if self.sim_clock_offset:
                self.shift_times(snapshot, "sampled_at", "updated_at")
                for node in snapshot.get("nodes", []):
                    self.shift_times(node, "collected_at")
                self.shift_times(history, "updated_at")
                for point in history["points"]:
                    self.shift_times(point, "ts")
            power = snapshot.get("cluster", {}).get("total_power")
            sampled_at = snapshot.get("sampled_at") or snapshot["updated_at"]
            self.stats.record_cluster(sampled_at, power, 3 * self.interval, self.retention)
            self.snapshot = snapshot
            self.history = history
            self.upstream_ok = True
            self.upstream_error = None
        except (OSError, ValueError, KeyError) as exc:
            self.upstream_ok = False
            self.upstream_error = str(exc)

        observations = {"upstream": (self.upstream_ok, "A端", "error",
                                     "汇聚上游连接中断", "汇聚上游连接恢复")}
        if self.upstream_ok:
            for source, state in self.snapshot.get("sources", {}).items():
                observations["source:" + source] = (state["ok"], source, "error",
                                                    "采集源连接异常", "采集源连接恢复")
            for node in self.snapshot.get("nodes", []):
                node_id = node["node_id"]
                observations["node:" + node_id] = (node["status"] == "online", node_id, "warn",
                                                    "节点采样过期", "节点采样恢复")
            try:
                transform = (lambda row: self.shift_times(row, "collected_at")) if self.sim_clock_offset else None
                self.history_gap = replay("A", self.upstream, self.context, self.archive,
                                          map_sample=transform) or self.history_gap
                self.history_error = None
            except (OSError, ValueError, KeyError) as exc:
                self.history_error = str(exc)
        self.stats.record_health(observations)
        if time.monotonic() >= self.next_cleanup:
            self.archive.maintain(stamp(), self.retention, self.hot_days)
            self.next_cleanup = time.monotonic() + 30

    def status(self):
        return {"upstream_ok": self.upstream_ok, "upstream_error": self.upstream_error,
                "history_error": self.history_error, "history_gap": self.history_gap}

    def read_snapshot(self):
        snapshot = self.snapshot
        cluster = {**snapshot.get("cluster", {}), **self.stats.card_energy(),
                   "today_alerts": self.stats.get_today_alerts_count()}
        return {**snapshot, "cluster": cluster, "gateway": self.status()}

    def source_time(self):
        return self.snapshot.get("updated_at") or stamp()

    def job_stats(self, job_id=None):
        if not self.jobs_file:
            return {"status": "disabled", "jobs": []}
        now = self.source_time()
        try:
            path = Path(self.jobs_file)
            updated_at = path.stat().st_mtime
            if time.time() - updated_at > 180:
                return {"status": "stale", "jobs": []}
            jobs = running_jobs(read_json_file(path))
        except (OSError, ValueError) as exc:
            return {"status": "unavailable", "jobs": [], "error": str(exc)}

        if job_id is None:
            return {"status": "ok", "jobs": [{"job_id": job["job_id"], "started_at": job["started_at"],
                                              "node_count": len(job["node_ids"])} for job in jobs]}

        # Bound an on-demand estimate. The job view labels this time interval.
        def load_history(node, start, end):
            return self.archive.history(node, since(start, 3 * self.interval), end, 5000)

        results = estimate_jobs(jobs, load_history, now, 3 * self.interval, since(now, 3600), job_id=job_id)
        return {"status": "ok", "jobs": results, "observed_at": now}


class GatewayHandler(JsonHandler):
    state = None

    def do_GET(self):
        path, params = self.route()
        try:
            if path == "/healthz":
                self.send_json(200, {"status": "ok", **self.state.status()})
            elif path == "/api/snapshot":
                self.send_json(200, self.state.read_snapshot())
            elif path == "/api/history":
                self.send_json(200, self.state.history)
            elif path == "/api/stats":
                period = params.get("range", ["today"])[0]
                if period not in ("today", "7d", "30d"):
                    raise ValueError("range must be today, 7d or 30d")
                self.send_json(200, self.state.stats.query_stats(period))
            elif path == "/api/alerts":
                self.send_json(200, self.state.stats.query_alerts(
                    params.get("range", ["7d"])[0], params.get("node", ["all"])[0],
                    params.get("level", ["all"])[0], int_param(params, "page", 1, 1, 1_000_000),
                    int_param(params, "page_size", 12, 1, 200)))
            elif path == "/api/node-history":
                node = params.get("node", [""])[0]
                if not node:
                    raise ValueError("node is required")
                end = stamp(utc_time(params.get("to", [self.state.source_time()])[0]))
                start = stamp(utc_time(params.get("from", [since(end, 3600)])[0]))
                if start > end:
                    raise ValueError("from must be earlier than to")
                limit = int_param(params, "limit", 1000, 1, 5000)
                points = self.state.archive.history(node, start, end, limit + 1)
                self.send_json(200, {"node_id": node, "samples": points[:limit],
                                     "has_more": len(points) > limit})
            elif path == "/api/jobs":
                self.send_json(200, self.state.job_stats(params.get("job", [None])[0]))
            else:
                self.send_json(404, {"error": "not found"})
        except (ValueError, OverflowError, OSError) as exc:
            self.send_json(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description="B-node mTLS pull gateway")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--upstream", default="https://127.0.0.1:19200")
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--mode", choices=["real", "simulated"], default="real")
    parser.add_argument("--sim-clock-offset-seconds", type=int, default=0,
                        help="Measured application clock correction for simulated samples")
    parser.add_argument("--db")
    parser.add_argument("--archive-db")
    parser.add_argument("--retention-days", type=float, default=30)
    parser.add_argument("--hot-days", type=float, default=30)
    parser.add_argument("--jobs-file")
    parser.add_argument("--ca", required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    if args.sim_clock_offset_seconds and args.mode != "simulated":
        parser.error("clock offset is only supported for simulated samples")
    if args.interval <= 0 or args.retention_days <= 0 or args.hot_days <= 0:
        parser.error("interval and retention must be positive")
    if not args.upstream.startswith("https://"):
        parser.error("A upstream must use HTTPS")
    context = create_client_ssl_context(args.ca, args.cert, args.key)
    stats_path = args.db or f"runtime/b-{args.mode}.sqlite"
    stats = EnergyStore(stats_path)
    archive = SampleStore(args.archive_db or str(Path(stats_path).with_suffix(".samples.sqlite")))
    state = GatewayState(args.upstream, context, stats, archive, args.interval, args.retention_days,
                         args.hot_days, args.mode, args.jobs_file, args.sim_clock_offset_seconds)
    GatewayHandler.state = state
    stop = threading.Event()
    server = ThreadingHTTPServer((args.bind, args.port), GatewayHandler)
    thread = threading.Thread(target=poll_loop, args=(state.poll_once, args.interval, stop), daemon=True)

    def stop_server(*_args):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    thread.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        thread.join()
        stats.close()
        archive.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
