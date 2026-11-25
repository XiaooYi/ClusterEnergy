#!/usr/bin/env python3
"""Serve an existing C JSON feed and retain a short replay buffer."""

import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import signal
import sys
import threading
import time

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.config import DEFAULT_EXPORTER_PORT, DEFAULT_POLL_INTERVAL_SEC
from common.samples import normalize_samples, stamp
from common.storage import SampleStore
from common.tls import create_server_ssl_context
from common.transport import JsonHandler, feed_page, poll_loop, read_json_file


class SourceFeed:
    def __init__(self, filename, schema, store, retention_days):
        self.filename = Path(filename)
        self.schema = schema
        self.store = store
        self.retention_days = retention_days
        self.raw = None
        self.samples = []
        self.error = "waiting for the first source snapshot"
        self.last_version = None
        self.next_cleanup = 0.0

    def collect(self):
        try:
            stat = self.filename.stat()
            version = (stat.st_mtime_ns, stat.st_ino, stat.st_size)
            if version != self.last_version:
                raw = read_json_file(self.filename)
                samples = normalize_samples(raw, self.schema)
                self.store.ingest("local", samples)
                self.raw = raw
                self.samples = samples
                self.last_version = version
            self.error = None
        except (OSError, ValueError, KeyError) as exc:
            self.error = str(exc)
        if time.monotonic() >= self.next_cleanup:
            self.store.maintain(stamp(), self.retention_days)
            self.next_cleanup = time.monotonic() + 60


class ExporterHandler(JsonHandler):
    feed = None
    node_id = ""

    def do_GET(self):
        path, params = self.route()
        try:
            if path == "/healthz":
                self.send_json(200, {"status": "ok" if self.feed.error is None else "unavailable",
                                     "node_id": self.node_id, "error": self.feed.error})
            elif path in ("/latest.json", "/metrics", "/api/current"):
                if self.feed.error is not None:
                    self.send_json(503, {"error": self.feed.error})
                elif path == "/api/current":
                    self.send_json(200, {"samples": self.feed.samples})
                else:
                    self.send_json(200, self.feed.raw)
            elif path == "/api/samples":
                self.send_json(200, feed_page(self.feed.store, params))
            else:
                self.send_json(404, {"error": "not found"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description="C-node mTLS exporter")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_EXPORTER_PORT)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--data-file", required=True)
    parser.add_argument("--schema")
    parser.add_argument("--db")
    parser.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL_SEC)
    parser.add_argument("--retention-days", type=float, default=1)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--ca", required=True)
    args = parser.parse_args()
    if args.interval <= 0 or args.retention_days <= 0:
        parser.error("interval and retention must be positive")
    schema = read_json_file(args.schema) if args.schema else {}
    if not isinstance(schema, dict):
        parser.error("schema must be a JSON object")
    tls = create_server_ssl_context(args.cert, args.key, args.ca)
    store = SampleStore(args.db or str(Path(args.data_file).with_name("c-replay.sqlite")))
    feed = SourceFeed(args.data_file, schema, store, args.retention_days)
    ExporterHandler.feed = feed
    ExporterHandler.node_id = args.node_id
    server = ThreadingHTTPServer((args.bind, args.port), ExporterHandler)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    stop = threading.Event()

    def stop_server(*_args):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    thread = threading.Thread(target=poll_loop, args=(feed.collect, args.interval, stop), daemon=True)
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
