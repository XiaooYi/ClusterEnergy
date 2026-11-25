import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from common import config


class PipelineTests(unittest.TestCase):
    def test_sampling_defaults_to_five_seconds(self):
        self.assertEqual(config.DEFAULT_POLL_INTERVAL_SEC, 5)

    def test_real_power_requires_an_explicit_whole_node_measurement(self):
        from common.samples import normalize_samples

        schema = {
            "nodes": "data",
            "node": "name",
            "power": "readings.power",
            "unit": "kW",
            "timestamp": "time",
            "kind": "role",
        }
        payload = {
            "time": "2026-11-25T16:00:00+08:00",
            "data": [
                {"name": "cn01", "role": "compute", "readings": {"power": 1.25}},
                {"name": "ln1", "role": "login", "readings": {"power": 2}},
                {"name": "cn02", "role": "compute", "metrics": {"N0_CORE": 23}},
            ],
        }
        samples = normalize_samples(payload, schema)
        self.assertEqual([(s["node_id"], s["watts"], s["collected_at"]) for s in samples],
                         [("cn01", 1250, "2026-11-25T08:00:00.000Z")])

    def test_snapshot_excludes_stale_power_and_ranks_mapped_racks(self):
        from a_node.aggregator import build_snapshot

        nodes = [
            sample("cn01", 100), sample("cn02", 300), sample("cn03", 200),
            sample("cn04", 1000, "2026-11-25T07:59:00.000Z"),
        ]
        racks = {"cn01": "R1", "cn02": "R1", "cn03": "R2", "cn04": "R2"}
        snapshot = build_snapshot(nodes, racks, "2026-11-25T08:00:05.000Z", 15, "real")
        self.assertEqual(snapshot["cluster"]["total_power"], 600)
        self.assertEqual(snapshot["cluster"]["online_nodes"], 3)
        self.assertEqual(snapshot["nodes"][3]["status"], "stale")
        self.assertEqual([(r["rack_id"], r["watts"]) for r in snapshot["racks"]],
                         [("R1", 400), ("R2", 200)])

    def test_cursor_import_deduplicates_and_survives_restart(self):
        from common.storage import SampleStore

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "samples.sqlite")
            with SampleStore(path) as store:
                store.ingest("C1", [sample("cn01", 100), sample("cn01", 100)], 7)
                store.ingest("C1", [sample("cn01", 100)], 7)
            with SampleStore(path) as store:
                self.assertEqual(store.cursor("C1"), 7)
                page = store.page(0, 1)
                self.assertEqual([s["watts"] for s in page["samples"]], [100])
                self.assertEqual(store.page(page["next_cursor"], 1)["samples"], [])

    def test_archive_preserves_raw_fields_and_supports_history_then_expiry(self):
        from common.storage import SampleStore

        with SampleStore(":memory:") as store:
            old = dict(sample("cn01", 100, "2026-09-01T00:00:00.000Z"),
                       metrics={"N0_CORE": 33.3})
            recent = sample("cn01", 220, "2026-11-20T00:00:00.000Z")
            store.ingest("A", [old, recent])
            store.maintain("2026-11-25T00:00:00.000Z", retention_days=90, hot_days=30)
            self.assertEqual(store.archive_count(), 1)
            self.assertEqual(store.history("cn01", "2026-09-01T00:00:00.000Z",
                                           "2026-11-21T00:00:00.000Z", 10), [old, recent])
            store.maintain("2026-12-15T00:00:00.000Z", retention_days=90, hot_days=30)
            self.assertEqual(store.archive_count(), 0)

    def test_slurm_job_energy_excludes_shared_nodes_and_sampling_gaps(self):
        from b_node.gateway.jobs import running_jobs, estimate_jobs

        jobs = running_jobs({"jobs": [
            {"job_id": 1, "job_state": ["RUNNING"], "nodes": "cn[01-02]", "start_time": 1795593590},
            {"job_id": 2, "job_state": "RUNNING", "nodes": "cn02", "start_time": 1795593590},
            {"job_id": 3, "job_state": "PENDING", "nodes": "cn03"},
        ]})
        points = {
            "cn01": [sample("cn01", 720), sample("cn01", 720, "2026-11-25T08:00:05.000Z"),
                     sample("cn01", 720, "2026-11-25T08:00:25.000Z")],
            "cn02": [sample("cn02", 500), sample("cn02", 500, "2026-11-25T08:00:05.000Z")],
        }
        rows = estimate_jobs(jobs, lambda node, start, end: points.get(node, []),
                             "2026-11-25T08:00:30.000Z", 10)
        self.assertEqual([j["job_id"] for j in rows], ["1", "2"])
        self.assertAlmostEqual(rows[0]["energy_kwh"], 0.001)
        self.assertEqual(rows[0]["shared_nodes_excluded"], 1)
        self.assertIsNone(rows[1]["energy_kwh"])

    def test_a_accepts_a_pure_simulator_at_the_cli(self):
        result = subprocess.run(
            [sys.executable, "-B", "a_node/aggregator.py", "--sim-nodes", "1",
             "--ca", "missing-ca", "--client-cert", "missing-cert",
             "--client-key", "missing-key", "--server-cert", "missing-cert",
             "--server-key", "missing-key"], capture_output=True, text=True)
        self.assertNotIn("required: --target", result.stderr)
        self.assertIn("FileNotFoundError", result.stderr)


def sample(node, watts, ts="2026-11-25T08:00:00.000Z"):
    return {"node_id": node, "watts": watts, "collected_at": ts}
