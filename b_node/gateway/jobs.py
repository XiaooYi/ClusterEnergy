"""Read running Slurm allocations and estimate energy from node samples."""

from collections import Counter
import re

from common.samples import stamp, utc_time


def expand_nodes(expression):
    if isinstance(expression, list):
        return sorted(set(node for value in expression for node in expand_nodes(value)))
    if not isinstance(expression, str):
        return []
    pieces = re.split(r",(?![^\[]*\])", expression)
    result = set()
    for piece in pieces:
        prefixes = [""]
        for token in re.findall(r"\[[^\]]+\]|[^\[\]]+", piece.strip()):
            if not token.startswith("["):
                prefixes = [prefix + token for prefix in prefixes]
                continue
            choices = []
            for item in token[1:-1].split(","):
                match = re.fullmatch(r"(\d+)-(\d+)", item)
                if match:
                    first, last = match.groups()
                    if int(last) - int(first) > 100_000:
                        raise ValueError("Slurm host range exceeds the supported size")
                    choices.extend(str(num).zfill(len(first)) for num in range(int(first), int(last) + 1))
                else:
                    choices.append(item)
            if len(prefixes) * len(choices) > 100_000:
                raise ValueError("Slurm host list exceeds the supported size")
            prefixes = [prefix + choice for prefix in prefixes for choice in choices]
        result.update(node for node in prefixes if node and node not in {"(null)", "None"})
    return sorted(result)


def running_jobs(document):
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), list):
        raise ValueError("expected a Slurm JSON object containing jobs")
    jobs = []
    for job in document["jobs"]:
        state = job.get("job_state", "")
        states = state if isinstance(state, list) else [state]
        if "RUNNING" not in states:
            continue
        resources = job.get("job_resources") or {}
        nodes = expand_nodes(job.get("nodes") or resources.get("nodes") or resources.get("node_list"))
        began = job.get("start_time")
        if isinstance(began, dict):
            began = began.get("number") if began.get("set", True) and not began.get("infinite") else None
        if not nodes or not began or job.get("job_id") is None:
            continue
        try:
            started_at = stamp(utc_time(began))
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        jobs.append({"job_id": str(job["job_id"]), "node_ids": nodes, "started_at": started_at})
    return jobs


def integrated_energy(points, start, end, max_gap):
    lower = utc_time(start)
    upper = utc_time(end)
    previous = None
    watt_seconds = 0.0
    intervals = 0
    covered_seconds = 0.0
    ordered = sorted(points, key=lambda sample: sample["collected_at"])
    for sample in ordered:
        now = utc_time(sample["collected_at"])
        if previous is not None:
            then, watts = previous
            seconds = (now - then).total_seconds()
            if 0 < seconds <= max_gap:
                used = (min(now, upper) - max(then, lower)).total_seconds()
                if used > 0:
                    watt_seconds += watts * used
                    covered_seconds += used
                    intervals += 1
        previous = (now, sample["watts"])
    return (watt_seconds / 3_600_000 if intervals else None), covered_seconds


def estimate_jobs(jobs, load_history, now, max_gap, window_start=None, job_id=None):
    allocations = Counter(node for job in jobs for node in job["node_ids"])
    result = []
    for job in jobs:
        if job_id is not None and job["job_id"] != job_id:
            continue
        start = max(job["started_at"], window_start) if window_start else job["started_at"]
        nodes = job["node_ids"]
        values = []
        covered = 0.0
        shared = 0
        if start < now:
            for node in nodes:
                if allocations[node] != 1:
                    shared += 1
                    continue
                energy, covered_seconds = integrated_energy(load_history(node, start, now), start, now, max_gap)
                covered += covered_seconds
                if energy is not None:
                    values.append(energy)
        result.append({"job_id": job["job_id"], "started_at": job["started_at"],
                       "node_count": len(nodes), "shared_nodes_excluded": shared,
                       "sampled_nodes": len(values), "estimated_node_seconds": round(covered, 2),
                       "from": start, "to": now,
                       "energy_kwh": round(sum(values), 9) if values else None})
    return result
