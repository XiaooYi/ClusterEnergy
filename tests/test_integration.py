"""Run production C/A/B entry points against temporary mTLS and data fixtures."""

from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen

from cryptography.x509.oid import ExtendedKeyUsageOID

from common.samples import stamp, since
from common.tls import create_client_ssl_context
from scripts.gen_certs import create_ca, save_cert, save_key, sign_leaf


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MTLSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.certs = Path(directory.name)
        ca_key, ca = create_ca()
        save_cert(ca, str(cls.certs / "ca.crt"))
        for name, purpose in (("a_server", ExtendedKeyUsageOID.SERVER_AUTH),
                              ("c_server", ExtendedKeyUsageOID.SERVER_AUTH),
                              ("a_client", ExtendedKeyUsageOID.CLIENT_AUTH),
                              ("b_client", ExtendedKeyUsageOID.CLIENT_AUTH)):
            key, certificate = sign_leaf(ca_key, ca, name, purpose, ips=["127.0.0.1"])
            save_cert(certificate, str(cls.certs / (name + ".crt")))
            save_key(key, str(cls.certs / (name + ".key")))

    def pem(self, name, ext="crt"):
        return str(self.certs / f"{name}.{ext}")

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.c_port, self.a_port, self.b_port = free_port(), free_port(), free_port()

    def start(self, entry, *args):
        log_path = self.directory / (Path(entry).stem + ".log")
        log = self.stack.enter_context(log_path.open("ab"))
        process = subprocess.Popen([sys.executable, "-B", entry, *args],
                                   stdout=log, stderr=subprocess.STDOUT)
        self.stack.callback(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)

    def start_c(self, source):
        return self.start("c_node/exporter.py", "--bind", "127.0.0.1", "--port", str(self.c_port),
                          "--node-id", "C1", "--data-file", str(source),
                          "--interval", "0.2", "--db", str(self.directory / "c.sqlite"),
                          "--ca", self.pem("ca"), "--cert", self.pem("c_server"),
                          "--key", self.pem("c_server", "key"))

    def start_a(self, *mode):
        return self.start("a_node/aggregator.py", "--port", str(self.a_port),
                          "--db", str(self.directory / "a.sqlite"),
                          "--interval", "0.4", "--ca", self.pem("ca"),
                          "--server-cert", self.pem("a_server"), "--server-key", self.pem("a_server", "key"),
                          "--client-cert", self.pem("a_client"), "--client-key", self.pem("a_client", "key"),
                          *mode)

    def start_b(self, mode, *extra):
        return self.start("b_node/gateway/gateway.py", "--port", str(self.b_port), "--mode", mode,
                          "--upstream", f"https://127.0.0.1:{self.a_port}", "--interval", "0.3",
                          "--db", str(self.directory / "b.sqlite"),
                          "--ca", self.pem("ca"), "--cert", self.pem("b_client"),
                          "--key", self.pem("b_client", "key"), *extra)

    def wait_for(self, url, predicate, context=None):
        end = time.monotonic() + 12
        last = None
        while time.monotonic() < end:
            try:
                with urlopen(url, context=context, timeout=1) as response:
                    last = json.load(response)
                if predicate(last):
                    return last
            except (OSError, URLError) as exc:
                last = str(exc)
            time.sleep(0.08)
        logs = {p.name: p.read_text(encoding="utf-8", errors="replace")[-3000:]
                for p in self.directory.glob("*.log")}
        self.fail(f"{url} did not reach the expected state: {last!r}; logs: {logs!r}")

    def write_source(self, source, ts, watts):
        temporary = source.with_suffix(".tmp")
        temporary.write_text(json.dumps({"collected_at": ts, "nodes": [
            {"node_id": "cn001", "power_w": watts, "node_type": "compute", "metrics": {"core": 81}},
            {"node_id": "cn002", "power_w": 200, "node_type": "compute"},
            {"node_id": "ln2", "power_w": 500, "node_type": "login"},
        ]}), encoding="utf-8")
        temporary.replace(source)

    def test_real_batch_replays_samples_after_b_is_restarted(self):
        source = self.directory / "latest.json"
        racks = self.directory / "racks.json"
        racks.write_text('{"cn001":"R1","cn002":"R1"}', encoding="utf-8")
        first = stamp()
        self.write_source(source, first, 100)
        self.start_c(source)
        self.start_a("--target", f"C1=127.0.0.1:{self.c_port}", "--racks", str(racks))
        b = self.start_b("real")
        origin = f"http://127.0.0.1:{self.b_port}"
        node_url = origin + "/api/node-history?node=cn001"
        first_snapshot = self.wait_for(origin + "/api/snapshot", lambda r: r.get("cluster", {}).get("total_power") == 300)
        self.assertEqual(first_snapshot["cluster"]["total_nodes"], 2)
        self.assertEqual(first_snapshot["racks"][0]["watts"], 300)
        self.wait_for(node_url, lambda r: len(r.get("samples", [])) == 1)
        self.stop(b)
        time.sleep(0.15)
        second = stamp()
        self.write_source(source, second, 150)
        a_context = create_client_ssl_context(self.pem("ca"), self.pem("b_client"), self.pem("b_client", "key"))
        self.wait_for(f"https://127.0.0.1:{self.a_port}/api/samples",
                      lambda r: any(s["collected_at"] == second for s in r["samples"]), a_context)
        self.start_b("real")
        history = self.wait_for(node_url, lambda r: len(r.get("samples", [])) == 2)
        self.assertEqual([row["watts"] for row in history["samples"]], [100, 150])
        self.assertEqual(history["samples"][0]["metrics"], {"core": 81})
        self.wait_for(origin + "/api/snapshot", lambda r: r.get("cluster", {}).get("total_power") == 350)

    def test_simulator_runs_without_c_and_a_requires_client_certificate(self):
        self.start_a("--sim-nodes", "5")
        self.start_b("simulated")
        origin = f"http://127.0.0.1:{self.b_port}"
        snapshot = self.wait_for(origin + "/api/snapshot",
                                 lambda r: r.get("cluster", {}).get("online_nodes") == 5)
        self.assertEqual(snapshot["mode"], "simulated")
        self.assertEqual(len(snapshot["racks"]), 2)
        self.assertAlmostEqual(snapshot["cluster"]["total_power"],
                               sum(node["summary"]["total_power"] for node in snapshot["nodes"]), places=3)
        self.wait_for(origin + "/api/history", lambda r: len(r["points"]) >= 2)
        anonymous = ssl.create_default_context(cafile=self.pem("ca"))
        with self.assertRaises((ssl.SSLError, ConnectionResetError, URLError)):
            urlopen(f"https://127.0.0.1:{self.a_port}/api/snapshot", context=anonymous, timeout=2)
