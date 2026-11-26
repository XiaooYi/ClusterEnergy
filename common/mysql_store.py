"""MySQL storage backends for B-node statistics, alerts, and tiered sample archives."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional
import uuid

from common.config import PAGE_SIZE
from common.samples import since, utc_time

TZ_CST = timezone(timedelta(hours=8))
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _local_now() -> datetime:
    return datetime.now(TZ_CST)


def _now_iso() -> str:
    return _local_now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class MySQLConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "clusterenergy"
    password: str = ""
    database: str = "clusterenergy_real"
    wait_seconds: int = 60

    def validate(self) -> None:
        if not self.host:
            raise ValueError("MySQL host must not be empty")
        if not (1 <= int(self.port) <= 65535):
            raise ValueError("MySQL port must be between 1 and 65535")
        if not self.user:
            raise ValueError("MySQL user must not be empty")
        if not _DB_NAME_RE.match(self.database):
            raise ValueError(f"Invalid MySQL database name: {self.database!r}")


def _import_pymysql():
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise RuntimeError(
            "PyMySQL is required for --storage mysql. Install it with: pip install PyMySQL"
        ) from exc
    return pymysql, DictCursor


def _connect_mysql(config: MySQLConfig, *, dict_cursor: bool = False):
    config.validate()
    pymysql, DictCursor = _import_pymysql()
    deadline = time.monotonic() + max(0, int(config.wait_seconds))
    cursor_cls = DictCursor if dict_cursor else pymysql.cursors.Cursor
    last_exc = None
    while True:
        try:
            try:
                return pymysql.connect(
                    host=config.host,
                    port=int(config.port),
                    user=config.user,
                    password=config.password,
                    database=config.database,
                    charset="utf8mb4",
                    autocommit=False,
                    connect_timeout=10,
                    read_timeout=30,
                    write_timeout=30,
                    cursorclass=cursor_cls,
                )
            except pymysql.err.OperationalError as op_exc:
                if not (op_exc.args and op_exc.args[0] == 1049):
                    raise
                bootstrap = pymysql.connect(
                    host=config.host,
                    port=int(config.port),
                    user=config.user,
                    password=config.password,
                    charset="utf8mb4",
                    autocommit=True,
                    connect_timeout=10,
                    read_timeout=30,
                    write_timeout=30,
                )
                try:
                    with bootstrap.cursor() as cur:
                        cur.execute(
                            f"CREATE DATABASE IF NOT EXISTS `{config.database}` "
                            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                        )
                finally:
                    bootstrap.close()
                return pymysql.connect(
                    host=config.host,
                    port=int(config.port),
                    user=config.user,
                    password=config.password,
                    database=config.database,
                    charset="utf8mb4",
                    autocommit=False,
                    connect_timeout=10,
                    read_timeout=30,
                    write_timeout=30,
                    cursorclass=cursor_cls,
                )
        except Exception as exc:
            last_exc = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Failed to connect to MySQL {config.host}:{config.port}/{config.database}: {last_exc}"
                ) from last_exc
            time.sleep(1.0)


class _MySQLBase:
    def __init__(self, config: MySQLConfig, *, dict_cursor: bool = False, conn=None) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._conn = conn if conn is not None else _connect_mysql(config, dict_cursor=dict_cursor)

    def _ensure_conn(self) -> None:
        ping = getattr(self._conn, "ping", None)
        if callable(ping):
            ping(reconnect=True)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class MySQLSampleStore(_MySQLBase):
    """MySQL equivalent of common.storage.SampleStore for B-node."""

    def __init__(self, config: MySQLConfig, *, conn=None) -> None:
        super().__init__(config, dict_cursor=False, conn=conn)
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS power_samples (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        node_id VARCHAR(128) NOT NULL,
                        ts VARCHAR(40) NOT NULL,
                        sample MEDIUMTEXT NOT NULL,
                        UNIQUE KEY uniq_node_ts (node_id, ts),
                        KEY power_samples_time (ts)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS power_archive (
                        node_id VARCHAR(128) NOT NULL,
                        start_ts VARCHAR(40) NOT NULL,
                        end_ts VARCHAR(40) NOT NULL,
                        payload LONGBLOB NOT NULL,
                        level TINYINT NOT NULL DEFAULT 3,
                        PRIMARY KEY (node_id, start_ts),
                        KEY power_archive_end (end_ts)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS replay_state (
                        name VARCHAR(128) PRIMARY KEY,
                        value TEXT NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    "INSERT IGNORE INTO replay_state (name, value) VALUES ('stream_id', %s)",
                    (uuid.uuid4().hex,),
                )
            self._conn.commit()
        self.stream_id = self._state("stream_id")

    def _state(self, name: str, default: Optional[str] = None) -> Optional[str]:
        self._ensure_conn()
        with self._conn.cursor() as cur:
            cur.execute("SELECT value FROM replay_state WHERE name = %s", (name,))
            row = cur.fetchone()
        return default if row is None else row[0]

    def _set_state(self, cur, name: str, value: str) -> None:
        cur.execute(
            "INSERT INTO replay_state (name, value) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE value = VALUES(value)",
            (name, value),
        )

    def cursor(self, source: str) -> int:
        with self._lock:
            return int(self._state("cursor:" + source, "0"))

    def source_stream(self, source: str) -> Optional[str]:
        with self._lock:
            return self._state("stream:" + source)

    def has_gap(self, source: str) -> bool:
        with self._lock:
            return self._state("gap:" + source) == "1"

    def ingest(self, source, samples, cursor=None, stream=None, history_gap=False):
        rows = [
            (s["node_id"], s["collected_at"], json.dumps(s, ensure_ascii=False, separators=(",", ":")))
            for s in samples
        ]
        with self._lock:
            self._ensure_conn()
            if cursor is not None and self.source_stream(source) == stream and cursor <= self.cursor(source):
                if history_gap:
                    with self._conn.cursor() as cur:
                        self._set_state(cur, "gap:" + source, "1")
                    self._conn.commit()
                return
            with self._conn.cursor() as cur:
                if history_gap:
                    self._set_state(cur, "gap:" + source, "1")
                if rows:
                    cur.executemany(
                        "INSERT IGNORE INTO power_samples (node_id, ts, sample) VALUES (%s, %s, %s)",
                        rows,
                    )
                if cursor is not None:
                    self._set_state(cur, "cursor:" + source, str(cursor))
                    if stream is not None:
                        self._set_state(cur, "stream:" + source, stream)
            self._conn.commit()

    def page(self, after: int, limit: int = PAGE_SIZE) -> Dict[str, Any]:
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT id, sample FROM power_samples WHERE id > %s ORDER BY id LIMIT %s",
                    (after, limit),
                )
                rows = cur.fetchall()
            dropped_through = int(self._state("dropped_through", "0"))
        return {
            "samples": [json.loads(row[1]) for row in rows],
            "next_cursor": rows[-1][0] if rows else after,
            "stream_id": self.stream_id,
            "history_gap": after < dropped_through,
        }

    def latest(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.sample FROM power_samples AS p
                    JOIN (SELECT node_id, MAX(ts) AS ts FROM power_samples GROUP BY node_id) AS last_ts
                        ON p.node_id = last_ts.node_id AND p.ts = last_ts.ts
                    """
                )
                rows = cur.fetchall()
        return [json.loads(row[0]) for row in rows]

    def history(self, node: str, start: str, end: str, limit: int) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT payload FROM power_archive
                    WHERE node_id = %s AND start_ts <= %s AND end_ts >= %s
                    ORDER BY start_ts LIMIT %s
                    """,
                    (node, end, start, limit + 1),
                )
                blocks = cur.fetchall()
                cur.execute(
                    """
                    SELECT sample FROM power_samples
                    WHERE node_id = %s AND ts BETWEEN %s AND %s
                    ORDER BY ts LIMIT %s
                    """,
                    (node, start, end, limit),
                )
                hot = cur.fetchall()
        samples = {}
        for (block,) in blocks:
            for sample in json.loads(gzip.decompress(bytes(block))):
                ts = sample["collected_at"]
                if start <= ts <= end:
                    samples[ts] = sample
            if len(samples) >= limit:
                break
        for (value,) in hot:
            sample = json.loads(value)
            samples[sample["collected_at"]] = sample
        return [samples[ts] for ts in sorted(samples)[:limit]]

    def maintain(self, now: str, retention_days: float, hot_days: Optional[float] = None) -> None:
        expired = since(now, retention_days * 86400)
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                if hot_days is not None and hot_days < retention_days:
                    hot = since(now, hot_days * 86400)
                    deep = since(now, max(60, hot_days) * 86400)
                    self._compress(cur, expired, hot, deep)
                    self._deepen(cur, expired, deep)
                self._expire_archive(cur, expired)
                cur.execute("SELECT MAX(id) FROM power_samples WHERE ts < %s", (expired,))
                row = cur.fetchone()
                max_id = row[0] if row else None
                if max_id is not None:
                    dropped = max(int(self._state("dropped_through", "0")), int(max_id))
                    self._set_state(cur, "dropped_through", str(dropped))
                    cur.execute("DELETE FROM power_samples WHERE ts < %s", (expired,))
            self._conn.commit()

    def _compress(self, cur, expired: str, cutoff: str, deep_cutoff: str) -> None:
        cur.execute(
            """
            SELECT id, node_id, ts, sample FROM power_samples
            WHERE ts >= %s AND ts < %s ORDER BY ts LIMIT 100000
            """,
            (expired, cutoff),
        )
        rows = cur.fetchall()
        groups: Dict[Any, List[Any]] = {}
        for row in rows:
            groups.setdefault((row[1], row[2][:13] + ":00:00.000Z"), []).append(row)
        for (node, hour), values in groups.items():
            cur.execute(
                "SELECT payload FROM power_archive WHERE node_id = %s AND start_ts = %s",
                (node, hour),
            )
            old = cur.fetchone()
            samples = {} if old is None else {s["collected_at"]: s for s in json.loads(gzip.decompress(bytes(old[0])))}
            samples.update({row[2]: json.loads(row[3]) for row in values})
            level = 9 if hour < deep_cutoff else 3
            payload = gzip.compress(
                json.dumps([samples[ts] for ts in sorted(samples)],
                           ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                compresslevel=level,
                mtime=0,
            )
            cur.execute(
                """
                INSERT INTO power_archive (node_id, start_ts, end_ts, payload, level)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    end_ts = VALUES(end_ts),
                    payload = VALUES(payload),
                    level = VALUES(level)
                """,
                (node, hour, max(samples), payload, level),
            )
            cur.executemany("DELETE FROM power_samples WHERE id = %s", [(row[0],) for row in values])

    def _deepen(self, cur, expired: str, cutoff: str) -> None:
        cur.execute(
            """
            SELECT node_id, start_ts, payload FROM power_archive
            WHERE level = 3 AND start_ts < %s AND end_ts >= %s
            ORDER BY start_ts LIMIT 2000
            """,
            (cutoff, expired),
        )
        for node, start_ts, payload in cur.fetchall():
            compressed = gzip.compress(gzip.decompress(bytes(payload)), compresslevel=9, mtime=0)
            cur.execute(
                "UPDATE power_archive SET payload = %s, level = 9 WHERE node_id = %s AND start_ts = %s",
                (compressed, node, start_ts),
            )

    def _expire_archive(self, cur, expired: str) -> None:
        cur.execute("DELETE FROM power_archive WHERE end_ts < %s", (expired,))
        cur.execute("SELECT node_id, start_ts, payload FROM power_archive WHERE start_ts < %s", (expired,))
        for node, start_ts, payload in cur.fetchall():
            old = json.loads(gzip.decompress(bytes(payload)))
            kept = [sample for sample in old if sample["collected_at"] >= expired]
            if len(kept) != len(old):
                body = gzip.compress(
                    json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    compresslevel=9,
                    mtime=0,
                )
                cur.execute(
                    "UPDATE power_archive SET payload = %s WHERE node_id = %s AND start_ts = %s",
                    (body, node, start_ts),
                )

    def archive_count(self) -> int:
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM power_archive")
                row = cur.fetchone()
            return int(row[0]) if row else 0


class MySQLEnergyStore(_MySQLBase):
    """MySQL equivalent of b_node.gateway.gateway.EnergyStore."""

    def __init__(self, config: MySQLConfig, *, conn=None) -> None:
        super().__init__(config, dict_cursor=True, conn=conn)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS minute_stats (
                        ts_minute VARCHAR(32) PRIMARY KEY,
                        date_str VARCHAR(16) NOT NULL,
                        sum_power DOUBLE NOT NULL,
                        max_power DOUBLE NOT NULL,
                        sum_eff DOUBLE NOT NULL,
                        energy_kwh DOUBLE NOT NULL,
                        samples INT NOT NULL,
                        KEY idx_minute_date (date_str)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        meta_key VARCHAR(128) PRIMARY KEY,
                        meta_value MEDIUMTEXT NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS alert_events (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        ts VARCHAR(40) NOT NULL,
                        date_str VARCHAR(16) NOT NULL,
                        node VARCHAR(128) NOT NULL,
                        level VARCHAR(16) NOT NULL,
                        msg VARCHAR(512) NOT NULL,
                        UNIQUE KEY uniq_alert (ts, node, msg(191)),
                        KEY idx_alerts_date (date_str)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    "INSERT IGNORE INTO meta (meta_key, meta_value) VALUES ('tz_utc8_migrated', '1')"
                )
            self._conn.commit()

    def record_sample(self, dt: datetime, total_power: float, eff_ratio: float, interval_sec: float) -> None:
        ts_minute = dt.strftime("%Y-%m-%dT%H:%M")
        date_str = dt.strftime("%Y-%m-%d")
        delta_kwh = (max(0.0, total_power) * max(0.1, interval_sec)) / 3600000.0
        cutoff = (dt - timedelta(days=35)).strftime("%Y-%m-%d")
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO minute_stats (ts_minute, date_str, sum_power, max_power, sum_eff, energy_kwh, samples)
                    VALUES (%s, %s, %s, %s, %s, %s, 1)
                    ON DUPLICATE KEY UPDATE
                        sum_power  = sum_power + VALUES(sum_power),
                        max_power  = GREATEST(max_power, VALUES(max_power)),
                        sum_eff    = sum_eff + VALUES(sum_eff),
                        energy_kwh = energy_kwh + VALUES(energy_kwh),
                        samples    = samples + 1
                    """,
                    (ts_minute, date_str, total_power, total_power, eff_ratio, delta_kwh),
                )
                cur.execute("DELETE FROM minute_stats WHERE date_str < %s", (cutoff,))
            self._conn.commit()

    def record_cluster(self, sampled_at, power, max_gap, retention_days):
        when = utc_time(sampled_at)
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute("SELECT meta_value FROM meta WHERE meta_key = 'cluster_sample'")
                row = cur.fetchone()
                previous = json.loads(row["meta_value"]) if row else None
                if previous is not None and sampled_at <= previous["ts"]:
                    return
                cur.execute(
                    "INSERT INTO meta (meta_key, meta_value) VALUES ('cluster_sample', %s) "
                    "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
                    (json.dumps({"ts": sampled_at, "power": power}),),
                )
                if power is not None:
                    energy = 0.0
                    if previous is not None and previous["power"] is not None:
                        seconds = (when - utc_time(previous["ts"])).total_seconds()
                        if 0 < seconds <= max_gap:
                            energy = previous["power"] * seconds / 3600000.0
                    dt = when.astimezone(TZ_CST)
                    cur.execute(
                        """
                        INSERT INTO minute_stats
                            (ts_minute, date_str, sum_power, max_power, sum_eff, energy_kwh, samples)
                        VALUES (%s, %s, %s, %s, 0, %s, 1)
                        ON DUPLICATE KEY UPDATE
                            sum_power  = sum_power + VALUES(sum_power),
                            max_power  = GREATEST(max_power, VALUES(max_power)),
                            energy_kwh = energy_kwh + VALUES(energy_kwh),
                            samples    = samples + 1
                        """,
                        (dt.strftime("%Y-%m-%dT%H:%M"), dt.strftime("%Y-%m-%d"), power, power, energy),
                    )
                    cutoff = (dt - timedelta(days=retention_days)).strftime("%Y-%m-%d")
                    cur.execute("DELETE FROM minute_stats WHERE date_str < %s", (cutoff,))
            self._conn.commit()

    def get_today_kwh(self) -> float:
        return self.card_energy()["today_kwh"]

    def card_energy(self) -> Dict[str, Any]:
        today = _local_now()
        today_str = today.strftime("%Y-%m-%d")
        week_start = (today - timedelta(days=6)).strftime("%Y-%m-%d")
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(CASE WHEN date_str = %s THEN energy_kwh ELSE 0 END), 0) AS today_kwh,
                           COALESCE(SUM(energy_kwh), 0) AS energy_7d_kwh,
                           COUNT(DISTINCT date_str) AS energy_7d_days
                    FROM minute_stats
                    WHERE date_str BETWEEN %s AND %s AND ts_minute <= %s
                    """,
                    (today_str, week_start, today_str, today.strftime("%Y-%m-%dT%H:%M")),
                )
                row = cur.fetchone()
        return {
            "today_kwh": round(float(row["today_kwh"]), 4),
            "energy_7d_kwh": round(float(row["energy_7d_kwh"]), 4),
            "energy_7d_days": int(row["energy_7d_days"]),
        }

    def query_stats(self, range_name: str) -> Dict[str, Any]:
        now_dt = _local_now()
        today_str = now_dt.strftime("%Y-%m-%d")
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                if range_name == "today":
                    cur.execute(
                        """
                        SELECT ts_minute, sum_power / samples AS avg_power, max_power, energy_kwh
                        FROM minute_stats
                        WHERE date_str = %s AND ts_minute <= %s
                        ORDER BY ts_minute ASC
                        """,
                        (today_str, now_dt.strftime("%Y-%m-%dT%H:%M")),
                    )
                    rows = cur.fetchall()
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
                cur.execute(
                    """
                    SELECT date_str,
                           SUM(energy_kwh) AS total_kwh,
                           SUM(sum_power) / SUM(samples) AS avg_power,
                           MAX(max_power) AS peak_power,
                           SUM(sum_eff) / SUM(samples) AS avg_eff
                    FROM minute_stats
                    WHERE date_str BETWEEN %s AND %s AND ts_minute <= %s
                    GROUP BY date_str
                    ORDER BY date_str ASC
                    """,
                    (start_date, today_str, now_dt.strftime("%Y-%m-%dT%H:%M")),
                )
                rows = cur.fetchall()
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

    def _insert_alerts(self, cur, alerts: List[Dict[str, str]]) -> int:
        if not alerts:
            return 0
        now_dt = _local_now()
        cutoff = (now_dt - timedelta(days=35)).strftime("%Y-%m-%d")
        rows = []
        for event in alerts:
            ts = str(event.get("ts") or "")
            node = str(event.get("node") or "")
            if ts and node and ts[:10] >= cutoff:
                rows.append((ts, ts[:10], node, str(event.get("level") or "info"), str(event.get("msg") or "")))
        inserted = 0
        if rows:
            inserted = cur.executemany(
                "INSERT IGNORE INTO alert_events (ts, date_str, node, level, msg) "
                "VALUES (%s, %s, %s, %s, %s)",
                rows,
            )
        cur.execute("DELETE FROM alert_events WHERE date_str < %s", (cutoff,))
        return int(inserted or 0)

    def record_alerts(self, alerts: List[Dict[str, str]]) -> int:
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                inserted = self._insert_alerts(cur, alerts)
            self._conn.commit()
            return inserted

    def import_alerts(self, legacy_db: Path) -> int:
        source = Path(legacy_db).resolve(strict=True)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            alerts = [dict(row) for row in connection.execute("SELECT ts, node, level, msg FROM alert_events")]
        return self.record_alerts(alerts)

    def record_health(self, observations) -> None:
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute("SELECT meta_value FROM meta WHERE meta_key = 'monitor_health'")
                row = cur.fetchone()
                previous = json.loads(row["meta_value"]) if row else {}
                current = dict(previous)
                alerts = []
                ts = _now_iso()
                for key, (ok, name, level, failed, restored) in observations.items():
                    current[key] = ok
                    if ok != previous.get(key, True):
                        alerts.append({
                            "ts": ts,
                            "node": name,
                            "level": "info" if ok else level,
                            "msg": restored if ok else failed,
                        })
                if current != previous:
                    self._insert_alerts(cur, alerts)
                    cur.execute(
                        "INSERT INTO meta (meta_key, meta_value) VALUES ('monitor_health', %s) "
                        "ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)",
                        (json.dumps(current, ensure_ascii=False),),
                    )
            self._conn.commit()

    def get_today_alerts_count(self) -> int:
        today_str = _local_now().strftime("%Y-%m-%d")
        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM alert_events WHERE date_str = %s", (today_str,))
                row = cur.fetchone()
        return int(row["c"]) if row else 0

    def query_alerts(
        self,
        range_name: str,
        node: str,
        level: str,
        page: int,
        page_size: int,
    ) -> Dict[str, Any]:
        now_dt = _local_now()
        if range_name == "today":
            start_date = now_dt.strftime("%Y-%m-%d")
        elif range_name == "7d":
            start_date = (now_dt - timedelta(days=6)).strftime("%Y-%m-%d")
        else:
            start_date = (now_dt - timedelta(days=29)).strftime("%Y-%m-%d")

        where = ["date_str >= %s"]
        params: List[Any] = [start_date]
        if node and node != "all":
            where.append("node = %s")
            params.append(node)
        if level and level != "all":
            where.append("level = %s")
            params.append(level)
        where_sql = " AND ".join(where)

        page = max(1, page)
        page_size = max(1, min(100, page_size))
        offset = (page - 1) * page_size

        with self._lock:
            self._ensure_conn()
            with self._conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS c FROM alert_events WHERE {where_sql}", params)
                total = int(cur.fetchone()["c"])
                cur.execute(
                    f"""
                    SELECT ts, node, level, msg
                    FROM alert_events
                    WHERE {where_sql}
                    ORDER BY ts DESC, id DESC
                    LIMIT %s OFFSET %s
                    """,
                    params + [page_size, offset],
                )
                rows = cur.fetchall()
                cur.execute(
                    "SELECT DISTINCT node FROM alert_events WHERE date_str >= %s ORDER BY node",
                    (start_date,),
                )
                nodes = cur.fetchall()

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
