#!/usr/bin/env python3
"""Refresh a Slurm job JSON file on the login host, independently of power sampling."""

import argparse
import json
import os
from pathlib import Path
from datetime import datetime
import subprocess
import tempfile
import time


def parse_legacy(text, timezone_offset):
    jobs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        job, nodes, start = line.strip().split("|", 2)
        if not start or start == "N/A":
            continue
        when = datetime.fromisoformat(start)
        if when.tzinfo is None:
            when = datetime.fromisoformat(start + timezone_offset)
        jobs.append({"job_id": job, "job_state": "RUNNING", "nodes": nodes,
                     "start_time": when.isoformat()})
    return {"jobs": jobs}


def capture(command, source_format, timezone_offset):
    if source_format == "json":
        result = subprocess.run([command, "--json"], check=True, capture_output=True,
                                text=True, timeout=25)
        document = json.loads(result.stdout)
        if not isinstance(document, dict) or not isinstance(document.get("jobs"), list):
            raise ValueError("Slurm result does not contain a jobs array")
        if document.get("errors"):
            raise ValueError("Slurm returned errors")
        return document
    result = subprocess.run([command, "--states=RUNNING", "--noheader", "--format=%i|%N|%S"],
                            check=True, capture_output=True, text=True, timeout=25)
    return parse_legacy(result.stdout, timezone_offset)


def atomic_publish(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--command", default="squeue")
    parser.add_argument("--format", choices=["json", "legacy"], default="json")
    parser.add_argument("--timezone-offset", default="+08:00")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("use at least 30 seconds between Slurm queue requests")
    while True:
        try:
            atomic_publish(args.output, capture(args.command, args.format, args.timezone_offset))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            if args.once:
                parser.exit(1, str(error) + "\n")
            print(error, flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
