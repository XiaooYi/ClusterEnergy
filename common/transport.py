"""HTTP helpers for the authenticated sample feeds."""

from http.server import BaseHTTPRequestHandler
import json
import logging
from pathlib import Path
import ssl
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from common.config import PAGE_SIZE


class JsonHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    def route(self):
        request = urlsplit(self.path)
        return request.path, parse_qs(request.query, keep_blank_values=True)

    def log_message(self, *_args):
        pass


def int_param(params, name, default, minimum, maximum):
    value = int(params.get(name, [default])[0])
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be within {minimum}..{maximum}")
    return value


def feed_page(store, params):
    after = int_param(params, "after", 0, 0, 2**63 - 1)
    limit = int_param(params, "limit", PAGE_SIZE, 1, PAGE_SIZE)
    return store.page(after, limit)


def read_json_file(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def get_json(address, context, route, params=None):
    suffix = "?" + urlencode(params) if params else ""
    with urlopen(address.rstrip("/") + route + suffix, context=context, timeout=4) as response:
        return json.load(response)


def replay(source, address, context, store, max_pages=5):
    for _ in range(max_pages):
        cursor = store.cursor(source)
        stream = store.source_stream(source)
        page = get_json(address, context, "/api/samples", {"after": cursor, "limit": PAGE_SIZE})
        if stream is not None and page["stream_id"] != stream:
            page = get_json(address, context, "/api/samples", {"after": 0, "limit": PAGE_SIZE})
        samples = page["samples"]
        store.ingest(source, samples, page["next_cursor"], page["stream_id"], page["history_gap"])
        if len(samples) < PAGE_SIZE:
            break
    return store.has_gap(source)


def poll_loop(action, interval, stop):
    while not stop.is_set():
        started = time.monotonic()
        try:
            action()
        except Exception:
            logging.exception("sampling cycle failed")
        stop.wait(max(0.05, interval - (time.monotonic() - started)))
