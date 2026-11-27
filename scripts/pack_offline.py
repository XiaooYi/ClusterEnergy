#!/usr/bin/env python3
"""Build architecture-checked Docker archives and package offline deploy bundles."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
ROLES = {"a": ("a",), "b": ("gateway", "web", "mysql"), "c": ("c",)}


def verify_platform(archive, expected, image):
    with tarfile.open(archive) as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
        records = [record for record in manifest if image in (record.get("RepoTags") or [])]
        if len(records) != 1:
            raise ValueError(f"{archive}: expected one image tagged {image}")
        config = json.load(tar.extractfile(records[0]["Config"]))
        actual = f"{config['os']}/{config['architecture']}"
        if actual != expected:
            raise ValueError(f"{image} has platform {actual}, expected {expected}")


def sha256(filename):
    digest = hashlib.sha256()
    with filename.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_image(name, image, platform, destination, builder, base_images):
    command = ["docker", "buildx", "build", "--platform", platform, "--target", name,
               "--tag", image, "--output", f"type=docker,dest={destination}"]
    if builder:
        command.extend(["--builder", builder])
    for variable, reference in base_images.items():
        command.extend(["--build-arg", f"{variable}={reference}"])
    subprocess.run([*command, str(ROOT)], check=True)


def package(roles, platform, out, builder=None, images_dir=None, base_images=None, include_mysql=True):
    arch = platform.split("/")[1]
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".build-{arch}-", dir=str(out)) as directory:
        staging = Path(directory)
        for role in roles:
            images = []
            role_images = tuple(name for name in ROLES[role] if include_mysql or name != "mysql")
            for name in role_images:
                image = f"clusterenergy/{name}:{arch}"
                tar_path = (images_dir or staging) / f"{name}.tar"
                if images_dir is None:
                    build_image(name, image, platform, tar_path, builder, base_images or {})
                verify_platform(tar_path, platform, image)
                images.append(tar_path)
            (staging / "platform.txt").write_bytes(f"{platform}\n".encode("ascii"))
            (staging / "role.txt").write_bytes(f"{role}\n".encode("ascii"))
            bundle_name = f"clusterenergy-{role}-{arch}.tar.gz"
            bundle_path = staging / bundle_name
            with tarfile.open(bundle_path, "w:gz") as tar:
                scripts = [(ROOT / "scripts" / "deploy_offline.sh", "deploy.sh"),
                           (ROOT / "README.md", "README.md"),
                           (ROOT / "部署文档.md", "部署文档.md"),
                           (staging / "platform.txt", "platform.txt"),
                           (staging / "role.txt", "role.txt")]
                if role == "b":
                    scripts.append((ROOT / "scripts" / "poll_slurm.py", "poll_slurm.py"))
                for path, name in scripts + [(path, f"images/{path.name}") for path in images]:
                    tar.add(path, arcname=name)
            digest = sha256(bundle_path)
            bundle_path.replace(out / bundle_name)
            checksum = out / (bundle_name + ".sha256")
            checksum.write_bytes(f"{digest}  {bundle_name}\n".encode("ascii"))
            print(f"{bundle_name} sha256={digest}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=["linux/arm64", "linux/amd64"], default="linux/arm64")
    parser.add_argument("--roles", nargs="+", choices=list(ROLES), default=list(ROLES))
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--builder")
    parser.add_argument("--python-base")
    parser.add_argument("--nginx-base")
    parser.add_argument("--mysql-base")
    parser.add_argument("--skip-mysql", action="store_true", help="omit MySQL image from the B bundle")
    parser.add_argument("--images-dir", type=Path, help="package existing Docker image archives")
    args = parser.parse_args()
    if "b" in args.roles and args.images_dir is None and not (ROOT / "b_node/frontend/dist/index.html").is_file():
        npm = "npm.cmd" if os.name == "nt" else "npm"
        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=ROOT / "b_node/frontend", check=True)
        subprocess.run([npm, "run", "build"], cwd=ROOT / "b_node/frontend", check=True)
    bases = {key: value for key, value in {"CE_PYTHON_BASE": args.python_base,
                                          "CE_NGINX_BASE": args.nginx_base,
                                          "CE_MYSQL_BASE": args.mysql_base}.items() if value}
    package(args.roles, args.platform, args.output.resolve(), args.builder, args.images_dir, bases,
            include_mysql=not args.skip_mysql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
