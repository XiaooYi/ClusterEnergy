#!/usr/bin/env python3
"""Package offline deployment tarballs + sha256 checksums for C, A, and B nodes."""

import hashlib
import io
import os
import tarfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(ROOT_DIR, "dist")


def add_path(tf: tarfile.TarFile, abs_path: str, arc_path: str, executable: bool = False) -> None:
    if os.path.isdir(abs_path):
        for root, dirs, files in os.walk(abs_path):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules", "dist", ".git")]
            for fn in files:
                if fn.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, abs_path).replace("\\", "/")
                is_exec = executable or fn.endswith(".sh")
                add_path(tf, full, f"{arc_path}/{rel}", executable=is_exec)
        return

    with open(abs_path, "rb") as f:
        data = f.read()
    # Normalize LF for shell/python scripts so Linux executes cleanly.
    if abs_path.endswith((".sh", ".py")):
        data = data.replace(b"\r\n", b"\n")
    info = tarfile.TarInfo(name=arc_path)
    info.size = len(data)
    info.mode = 0o755 if (executable or abs_path.endswith(".sh")) else 0o644
    tf.addfile(info, io.BytesIO(data))


def build_bundle(bundle_name: str, items) -> str:
    os.makedirs(DIST_DIR, exist_ok=True)
    out_path = os.path.join(DIST_DIR, bundle_name)
    with tarfile.open(out_path, "w:gz") as tf:
        for src_rel, dst_rel in items:
            add_path(tf, os.path.join(ROOT_DIR, src_rel), dst_rel)

    h = hashlib.sha256()
    with open(out_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()
    with open(f"{out_path}.sha256", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{digest}  {bundle_name}\n")
    print(f"Built {bundle_name} ({os.path.getsize(out_path)} bytes) sha256={digest}")
    return out_path


def main() -> int:
    build_bundle(
        "c_node_bundle.tar.gz",
        [
            ("common", "common"),
            ("c_node", "c_node"),
            ("certs/ca.crt", "certs/ca.crt"),
            ("certs/ln2.crt", "certs/ln2.crt"),
            ("certs/ln2.key", "certs/ln2.key"),
            ("certs/ln4.crt", "certs/ln4.crt"),
            ("certs/ln4.key", "certs/ln4.key"),
        ],
    )
    build_bundle(
        "a_node_bundle.tar.gz",
        [
            ("common", "common"),
            ("a_node", "a_node"),
            ("certs/ca.crt", "certs/ca.crt"),
            ("certs/a_client.crt", "certs/a_client.crt"),
            ("certs/a_client.key", "certs/a_client.key"),
            ("certs/a_server.crt", "certs/a_server.crt"),
            ("certs/a_server.key", "certs/a_server.key"),
        ],
    )
    build_bundle(
        "b_node_bundle.tar.gz",
        [
            ("b_node/docker-compose.yml", "docker-compose.yml"),
            ("b_node/start_docker.sh", "start_docker.sh"),
            ("b_node/gateway", "gateway"),
            ("b_node/frontend", "frontend"),
            ("certs/ca.crt", "certs/ca.crt"),
            ("certs/b_client.crt", "certs/b_client.crt"),
            ("certs/b_client.key", "certs/b_client.key"),
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
