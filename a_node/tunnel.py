#!/usr/bin/env python3
"""Auto-reconnecting SSH reverse tunnel from A-node to B-node (virtual cable for B->A pull)."""

import argparse
import signal
import subprocess
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Keepalive SSH reverse tunnel A -> B")
    parser.add_argument("--remote-user", required=True)
    parser.add_argument("--remote-host", required=True)
    parser.add_argument("--remote-port", type=int, default=19200)
    parser.add_argument("--local-port", type=int, default=19200)
    parser.add_argument("--ssh-key", required=True)
    args = parser.parse_args()

    running = True
    proc = None

    def handle_sig(_signum, _frame):
        nonlocal running
        running = False
        if proc is not None and proc.poll() is None:
            proc.terminate()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    backoff = 2
    while running:
        cmd = [
            "ssh",
            "-N",
            "-T",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-i", args.ssh_key,
            "-R", f"127.0.0.1:{args.remote_port}:127.0.0.1:{args.local_port}",
            f"{args.remote_user}@{args.remote_host}",
        ]
        start_ts = time.monotonic()
        proc = subprocess.Popen(cmd)
        proc.wait()
        if not running:
            break
        # Reset backoff if the tunnel stayed up for >30s
        if time.monotonic() - start_ts > 30:
            backoff = 2
        else:
            backoff = min(15, backoff * 2)
        time.sleep(backoff)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
