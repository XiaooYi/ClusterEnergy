import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


class DeploymentTests(unittest.TestCase):
    def test_arm_bundle_rejects_an_amd64_image(self):
        from scripts.pack_offline import verify_platform

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.tar"
            with tarfile.open(image, "w") as bundle:
                members = {
                    "manifest.json": [{"Config": "config.json", "RepoTags": ["clusterenergy/a:arm64"]}],
                    "config.json": {"os": "linux", "architecture": "amd64"},
                }
                for name, payload in members.items():
                    body = json.dumps(payload).encode("utf-8")
                    entry = tarfile.TarInfo(name)
                    entry.size = len(body)
                    bundle.addfile(entry, io.BytesIO(body))
            with self.assertRaisesRegex(ValueError, "linux/amd64"):
                verify_platform(image, "linux/arm64", "clusterenergy/a:arm64")

    def test_slurm_fallback_preserves_node_ranges_and_local_start_time(self):
        from scripts.poll_slurm import parse_legacy

        payload = parse_legacy("101_2|cn[005-007]|2026-11-25T16:00:00\n", "+08:00")
        self.assertEqual(payload["jobs"][0], {
            "job_id": "101_2", "job_state": "RUNNING", "nodes": "cn[005-007]",
            "start_time": "2026-11-25T16:00:00+08:00",
        })
