#!/usr/bin/env python3
"""C-node mTLS HTTPS exporter serving latest.json to A-node."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import ssl
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.config import DEFAULT_EXPORTER_PORT
from common.tls import create_server_ssl_context


class ExporterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    data_file: str = ""
    node_id: str = ""

    def log_message(self, format: str, *args) -> None:
        # Keep stdout clean; errors still surface on stderr if needed.
        return

    def _send_json(self, status: int, body_bytes: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            # Client timed out and closed the socket before response completed.
            pass

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            exists = os.path.isfile(self.data_file)
            payload = json.dumps(
                {"status": "ok" if exists else "initializing", "node_id": self.node_id}
            ).encode("utf-8")
            self._send_json(HTTPStatus.OK, payload)
            return

        if path in ("/latest.json", "/metrics"):
            try:
                with open(self.data_file, "rb") as f:
                    raw = f.read()
                self._send_json(HTTPStatus.OK, raw)
            except FileNotFoundError:
                err = json.dumps({"error": "latest.json not ready"}).encode("utf-8")
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, err)
            except OSError as exc:
                err = json.dumps({"error": str(exc)}).encode("utf-8")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, err)
            return

        err = json.dumps({"error": "not found"}).encode("utf-8")
        self._send_json(HTTPStatus.NOT_FOUND, err)


def main() -> int:
    parser = argparse.ArgumentParser(description="C-node mTLS HTTPS exporter")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_EXPORTER_PORT)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--data-file", required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--ca", required=True)
    args = parser.parse_args()

    ExporterHandler.data_file = args.data_file
    ExporterHandler.node_id = args.node_id

    server = ThreadingHTTPServer((args.bind, args.port), ExporterHandler)
    server.daemon_threads = True
    server.socket = create_server_ssl_context(
        cert_file=args.cert,
        key_file=args.key,
        ca_file=args.ca,
        require_client_cert=True,
    ).wrap_socket(server.socket, server_side=True)

    def handle_sig(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
