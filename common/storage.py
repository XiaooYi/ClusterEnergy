"""Durable samples, replay cursors, and compressed hourly archive segments."""

import gzip
import json
import os
import sqlite3
import threading
import uuid

from common.config import PAGE_SIZE
from common.samples import since


class SampleStore:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS power_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL, ts TEXT NOT NULL, sample TEXT NOT NULL,
                    UNIQUE(node_id, ts)
                );
                CREATE INDEX IF NOT EXISTS power_samples_time ON power_samples(ts);
                CREATE TABLE IF NOT EXISTS power_archive (
                    node_id TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                    payload BLOB NOT NULL, level INTEGER NOT NULL DEFAULT 3,
                    PRIMARY KEY(node_id, start)
                );
                CREATE TABLE IF NOT EXISTS replay_state (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(power_archive)")}
            if "level" not in columns:
                self._db.execute("ALTER TABLE power_archive ADD COLUMN level INTEGER NOT NULL DEFAULT 3")
            self._db.execute("INSERT OR IGNORE INTO replay_state VALUES ('stream_id', ?)", (uuid.uuid4().hex,))
        self.stream_id = self._state("stream_id")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self._db.close()

    def _state(self, name, default=None):
        result = self._db.execute("SELECT value FROM replay_state WHERE name = ?", (name,)).fetchone()
        return default if result is None else result[0]

    def cursor(self, source):
        with self._lock:
            return int(self._state("cursor:" + source, "0"))

    def source_stream(self, source):
        with self._lock:
            return self._state("stream:" + source)

    def has_gap(self, source):
        with self._lock:
            return self._state("gap:" + source) == "1"

    def ingest(self, source, samples, cursor=None, stream=None, history_gap=False):
        rows = [(s["node_id"], s["collected_at"], json.dumps(s, ensure_ascii=False, separators=(",", ":")))
                for s in samples]
        with self._lock, self._db:
            if history_gap:
                self._db.execute("INSERT OR REPLACE INTO replay_state VALUES (?, '1')", ("gap:" + source,))
            if cursor is not None and self.source_stream(source) == stream and cursor <= self.cursor(source):
                return
            self._db.executemany(
                "INSERT OR IGNORE INTO power_samples (node_id, ts, sample) VALUES (?, ?, ?)", rows)
            if cursor is not None:
                self._db.execute("INSERT OR REPLACE INTO replay_state VALUES (?, ?)",
                                 ("cursor:" + source, str(cursor)))
                if stream is not None:
                    self._db.execute("INSERT OR REPLACE INTO replay_state VALUES (?, ?)",
                                     ("stream:" + source, stream))

    def page(self, after, limit=PAGE_SIZE):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, sample FROM power_samples WHERE id > ? ORDER BY id LIMIT ?",
                (after, limit)).fetchall()
            dropped_through = int(self._state("dropped_through", "0"))
        return {"samples": [json.loads(row[1]) for row in rows],
                "next_cursor": rows[-1][0] if rows else after,
                "stream_id": self.stream_id, "history_gap": after < dropped_through}

    def latest(self):
        with self._lock:
            rows = self._db.execute("""
                SELECT sample FROM power_samples AS p
                JOIN (SELECT node_id, MAX(ts) AS ts FROM power_samples GROUP BY node_id) AS last
                    ON p.node_id = last.node_id AND p.ts = last.ts
            """).fetchall()
        return [json.loads(row[0]) for row in rows]

    def history(self, node, start, end, limit):
        with self._lock:
            blocks = self._db.execute("""
                SELECT payload FROM power_archive
                WHERE node_id = ? AND start <= ? AND end >= ? ORDER BY start LIMIT ?
            """, (node, end, start, limit + 1)).fetchall()
            hot = self._db.execute("""
                SELECT sample FROM power_samples WHERE node_id = ? AND ts BETWEEN ? AND ?
                ORDER BY ts LIMIT ?
            """, (node, start, end, limit)).fetchall()
        samples = {}
        for block, in blocks:
            for sample in json.loads(gzip.decompress(block)):
                ts = sample["collected_at"]
                if start <= ts <= end:
                    samples[ts] = sample
            if len(samples) >= limit:
                break
        for value, in hot:
            sample = json.loads(value)
            samples[sample["collected_at"]] = sample
        return [samples[ts] for ts in sorted(samples)[:limit]]

    def maintain(self, now, retention_days, hot_days=None):
        expired = since(now, retention_days * 86400)
        with self._lock, self._db:
            if hot_days is not None and hot_days < retention_days:
                hot = since(now, hot_days * 86400)
                deep = since(now, max(60, hot_days) * 86400)
                self._compress(expired, hot, deep)
                self._deepen(expired, deep)
            self._expire_archive(expired)
            max_id = self._db.execute("SELECT MAX(id) FROM power_samples WHERE ts < ?", (expired,)).fetchone()[0]
            if max_id is not None:
                dropped = max(int(self._state("dropped_through", "0")), max_id)
                self._db.execute("INSERT OR REPLACE INTO replay_state VALUES ('dropped_through', ?)",
                                 (str(dropped),))
                self._db.execute("DELETE FROM power_samples WHERE ts < ?", (expired,))

    def _compress(self, expired, cutoff, deep_cutoff):
        rows = self._db.execute("""
            SELECT id, node_id, ts, sample FROM power_samples
            WHERE ts >= ? AND ts < ? ORDER BY ts LIMIT 100000
        """, (expired, cutoff)).fetchall()
        groups = {}
        for row in rows:
            groups.setdefault((row[1], row[2][:13] + ":00:00.000Z"), []).append(row)
        for (node, hour), values in groups.items():
            old = self._db.execute("SELECT payload FROM power_archive WHERE node_id = ? AND start = ?",
                                   (node, hour)).fetchone()
            samples = {} if old is None else {s["collected_at"]: s for s in json.loads(gzip.decompress(old[0]))}
            samples.update({row[2]: json.loads(row[3]) for row in values})
            level = 9 if hour < deep_cutoff else 3
            payload = gzip.compress(json.dumps([samples[ts] for ts in sorted(samples)],
                                               ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                                    compresslevel=level, mtime=0)
            self._db.execute("INSERT OR REPLACE INTO power_archive VALUES (?, ?, ?, ?, ?)",
                             (node, hour, max(samples), payload, level))
            self._db.executemany("DELETE FROM power_samples WHERE id = ?", ((row[0],) for row in values))

    def _deepen(self, expired, cutoff):
        rows = self._db.execute("""
            SELECT node_id, start, payload FROM power_archive
            WHERE level = 3 AND start < ? AND end >= ? ORDER BY start LIMIT 2000
        """, (cutoff, expired)).fetchall()
        for node, start, payload in rows:
            compressed = gzip.compress(gzip.decompress(payload), compresslevel=9, mtime=0)
            self._db.execute("UPDATE power_archive SET payload = ?, level = 9 WHERE node_id = ? AND start = ?",
                             (compressed, node, start))

    def _expire_archive(self, expired):
        self._db.execute("DELETE FROM power_archive WHERE end < ?", (expired,))
        boundary = self._db.execute("SELECT node_id, start, payload FROM power_archive WHERE start < ?",
                                    (expired,)).fetchall()
        for node, start, payload in boundary:
            old = json.loads(gzip.decompress(payload))
            kept = [sample for sample in old if sample["collected_at"] >= expired]
            if len(kept) != len(old):
                body = gzip.compress(json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                                     compresslevel=9, mtime=0)
                self._db.execute("UPDATE power_archive SET payload = ? WHERE node_id = ? AND start = ?",
                                 (body, node, start))

    def archive_count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM power_archive").fetchone()[0]
