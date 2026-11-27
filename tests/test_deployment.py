import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


class DeploymentTests(unittest.TestCase):
    def test_offline_bundle_markers_and_checksum_use_lf(self):
        from scripts.pack_offline import package

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "a.tar"
            with tarfile.open(image, "w") as archive:
                members = {
                    "manifest.json": [{"Config": "config.json", "RepoTags": ["clusterenergy/a:arm64"]}],
                    "config.json": {"os": "linux", "architecture": "arm64"},
                }
                for name, payload in members.items():
                    body = json.dumps(payload).encode("utf-8")
                    entry = tarfile.TarInfo(name)
                    entry.size = len(body)
                    archive.addfile(entry, io.BytesIO(body))

            output = root / "bundles"
            package(["a"], "linux/arm64", output, images_dir=root)

            bundle = output / "clusterenergy-a-arm64.tar.gz"
            with tarfile.open(bundle) as archive:
                for name, expected in (("role.txt", b"a\n"), ("platform.txt", b"linux/arm64\n")):
                    with self.subTest(file=name):
                        self.assertEqual(archive.extractfile(name).read(), expected)

            checksum = output / "clusterenergy-a-arm64.tar.gz.sha256"
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            with self.subTest(file=checksum.name):
                self.assertEqual(checksum.read_bytes(), f"{digest}  clusterenergy-a-arm64.tar.gz\n".encode("ascii"))

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
