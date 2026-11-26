from datetime import datetime, timedelta
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from b_node.gateway.gateway import EnergyStore, TZ_CST
from common.mysql_store import MySQLConfig, MySQLEnergyStore, MySQLSampleStore


def _translate_mysql_to_sqlite(sql: str) -> str:
    """Translate the subset of MySQL DDL/DML used by mysql_store into SQLite SQL for unit tests."""
    s = sql.strip()
    if s.upper().startswith("CREATE TABLE"):
        s = re.sub(r"\)\s*ENGINE=InnoDB.*$", ")", s, flags=re.IGNORECASE | re.DOTALL)
        s = re.sub(r"BIGINT\s+AUTO_INCREMENT\s+PRIMARY\s+KEY", "INTEGER PRIMARY KEY AUTOINCREMENT", s, flags=re.IGNORECASE)
        s = re.sub(r"\bmsg\(\d+\)", "msg", s, flags=re.IGNORECASE)
        s = re.sub(r"UNIQUE\s+KEY\s+\w+\s*\(([^)]+)\)", r"UNIQUE(\1)", s, flags=re.IGNORECASE)
        s = re.sub(r",\s*KEY\s+\w+\s*\([^)]+\)", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\b(VARCHAR\(\d+\)|MEDIUMTEXT|TINYINT|LONGBLOB|DOUBLE)\b",
                   lambda m: {"LONGBLOB": "BLOB", "DOUBLE": "REAL", "TINYINT": "INTEGER"}.get(m.group(1).upper(), "TEXT"),
                   s, flags=re.IGNORECASE)
    s = re.sub(r"INSERT\s+IGNORE\s+INTO", "INSERT OR IGNORE INTO", s, flags=re.IGNORECASE)
    if "ON DUPLICATE KEY UPDATE" in s.upper():
        idx = s.upper().index("ON DUPLICATE KEY UPDATE")
        head, tail = s[:idx], s[idx + len("ON DUPLICATE KEY UPDATE"):]
        tail = re.sub(r"VALUES\((\w+)\)", r"excluded.\1", tail, flags=re.IGNORECASE)
        tail = re.sub(r"\bGREATEST\(", "MAX(", tail, flags=re.IGNORECASE)
        if "minute_stats" in head:
            conflict = "(ts_minute)"
        elif "power_archive" in head:
            conflict = "(node_id, start_ts)"
        elif "replay_state" in head:
            conflict = "(name)"
        elif "meta" in head:
            conflict = "(meta_key)"
        else:
            raise AssertionError(f"Unexpected upsert table: {head}")
        s = f"{head} ON CONFLICT{conflict} DO UPDATE SET {tail}"
    return s.replace("%s", "?")


class _FakeMySQLCursor:
    def __init__(self, conn: sqlite3.Connection, dict_mode: bool):
        self._conn = conn
        self._dict_mode = dict_mode
        self._cur = conn.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._cur.close()

    def execute(self, sql, params=None):
        translated = _translate_mysql_to_sqlite(sql)
        self._cur.execute(translated, () if params is None else tuple(params))
        return self._cur.rowcount

    def executemany(self, sql, seq):
        translated = _translate_mysql_to_sqlite(sql)
        before = self._conn.total_changes
        self._cur.executemany(translated, [tuple(p) for p in seq])
        return self._conn.total_changes - before

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None or not self._dict_mode:
            return row
        return dict(row)

    def fetchall(self):
        rows = self._cur.fetchall()
        if not self._dict_mode:
            return rows
        return [dict(r) for r in rows]


class _FakeMySQLConnection:
    def __init__(self, dict_mode: bool = False):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        if dict_mode:
            self._conn.row_factory = sqlite3.Row
        self._dict_mode = dict_mode
        self.pings = 0

    def ping(self, reconnect=False):
        self.pings += 1

    def cursor(self):
        return _FakeMySQLCursor(self._conn, self._dict_mode)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


class MySQLStoreTests(unittest.TestCase):
    def test_mysql_config_validates_database_name_and_port(self):
        MySQLConfig(host="127.0.0.1", port=3306, user="ce", password="pw", database="clusterenergy_sim").validate()
        with self.assertRaises(ValueError):
            MySQLConfig(database="bad-db;drop").validate()
        with self.assertRaises(ValueError):
            MySQLConfig(port=70000).validate()

    def test_mysql_sample_store_ingest_replay_archive_and_expire(self):
        conn = _FakeMySQLConnection(dict_mode=False)
        store = MySQLSampleStore(MySQLConfig(database="clusterenergy_sim"), conn=conn)
        try:
            old_ts = "2026-10-01T10:00:00.000Z"
            mid_ts = "2026-10-01T10:00:05.000Z"
            hot_ts = "2026-11-25T10:00:00.000Z"
            samples = [
                {"node_id": "cn001", "collected_at": old_ts, "watts": 400.0},
                {"node_id": "cn001", "collected_at": mid_ts, "watts": 410.0},
                {"node_id": "cn001", "collected_at": hot_ts, "watts": 500.0},
            ]
            store.ingest("A", samples, cursor=3, stream="s1")
            # Duplicate cursor must be skipped
            store.ingest("A", [{"node_id": "cn001", "collected_at": hot_ts, "watts": 999.0}], cursor=3, stream="s1")
            self.assertEqual(store.cursor("A"), 3)
            self.assertEqual(store.source_stream("A"), "s1")
            self.assertEqual(len(store.latest()), 1)
            self.assertEqual(store.latest()[0]["watts"], 500.0)

            page = store.page(0, limit=10)
            self.assertEqual(len(page["samples"]), 3)
            self.assertFalse(page["history_gap"])

            # Compress samples older than 30 days while retaining 90 days
            store.maintain("2026-11-26T00:00:00.000Z", retention_days=90, hot_days=30)
            self.assertEqual(store.archive_count(), 1)
            history = store.history("cn001", "2026-10-01T00:00:00.000Z", "2026-11-26T00:00:00.000Z", limit=10)
            self.assertEqual([s["watts"] for s in history], [400.0, 410.0, 500.0])

            # Deepen archive (>60d) and expire archive (<10d retention)
            store.maintain("2026-12-10T00:00:00.000Z", retention_days=120, hot_days=30)
            self.assertEqual(store.archive_count(), 1)
            store.maintain("2026-12-10T00:00:00.000Z", retention_days=10, hot_days=5)
            self.assertEqual(store.archive_count(), 0)
        finally:
            store.close()

    def test_mysql_energy_store_stats_alerts_and_legacy_sqlite_import(self):
        now = datetime(2026, 11, 26, 14, 30, 0, tzinfo=TZ_CST)
        conn = _FakeMySQLConnection(dict_mode=True)
        store = MySQLEnergyStore(MySQLConfig(database="clusterenergy_sim"), conn=conn)
        try:
            with patch("common.mysql_store._local_now", return_value=now):
                # Record two cluster samples 5 seconds apart (1000W -> 1000*5/3.6e6 kWh)
                store.record_cluster("2026-11-26T06:29:00.000Z", 1000.0, max_gap=15.0, retention_days=30)
                store.record_cluster("2026-11-26T06:29:05.000Z", 1200.0, max_gap=15.0, retention_days=30)
                # Duplicate timestamp ignored
                store.record_cluster("2026-11-26T06:29:05.000Z", 9999.0, max_gap=15.0, retention_days=30)

                cards = store.card_energy()
                self.assertEqual(cards["energy_7d_days"], 1)
                self.assertAlmostEqual(cards["today_kwh"], 1000.0 * 5 / 3600000.0, places=4)

                today_stats = store.query_stats("today")
                self.assertEqual(len(today_stats["points"]), 1)
                self.assertEqual(today_stats["points"][0]["avg_power"], 1100.0)
                self.assertEqual(today_stats["points"][0]["max_power"], 1200.0)

                week_stats = store.query_stats("7d")
                self.assertEqual(len(week_stats["days"]), 1)

                # Health state transitions generate alerts once per transition
                store.record_health({"upstream": (False, "A端", "error", "汇聚上游连接中断", "汇聚上游连接恢复")})
                store.record_health({"upstream": (False, "A端", "error", "汇聚上游连接中断", "汇聚上游连接恢复")})
                store.record_health({"upstream": (True, "A端", "error", "汇聚上游连接中断", "汇聚上游连接恢复")})
                self.assertEqual(store.get_today_alerts_count(), 2)
                alerts = store.query_alerts("today", "A端", "all", 1, 20)
                self.assertEqual(alerts["total"], 2)

                # Import legacy SQLite alerts into MySQL without duplicating
                with tempfile.TemporaryDirectory() as tmp:
                    legacy_path = Path(tmp) / "legacy.sqlite"
                    legacy = EnergyStore(str(legacy_path))
                    legacy.record_alerts([
                        {"ts": "2026-11-25T18:08:46+08:00", "node": "ln4", "level": "warn", "msg": "采集序号停滞"},
                    ])
                    legacy.close()
                    self.assertEqual(store.import_alerts(legacy_path), 1)
                    self.assertEqual(store.import_alerts(legacy_path), 0)
        finally:
            store.close()
