"""Validate source-specific JSON and normalize whole-node power samples."""

from datetime import datetime, timedelta, timezone
import math
import re


def utc_time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Contemporary Unix timestamps are often provided in milliseconds.
        seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
        return datetime.fromtimestamp(seconds, timezone.utc)
    if not isinstance(value, str):
        raise ValueError("expected a timestamp with a timezone")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("expected a timestamp with a timezone")
    return result.astimezone(timezone.utc)


def stamp(value=None):
    now = datetime.now(timezone.utc) if value is None else value
    return now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def since(value, seconds):
    return stamp(utc_time(value) - timedelta(seconds=seconds))


def field(document, path):
    result = document
    for key in path.split("."):
        if not isinstance(result, dict):
            return None
        result = result.get(key)
    return result


def normalize_samples(payload, schema=None):
    schema = schema or {}
    batch = field(payload, schema.get("nodes", "nodes"))
    if batch is None:
        batch = payload if isinstance(payload, list) else [payload]
    if not isinstance(batch, list):
        raise ValueError("the configured nodes field must contain a JSON array")
    id_path = schema.get("node", "node_id")
    time_path = schema.get("timestamp", "collected_at")
    power_path = schema.get("power", "power_w")
    kind_path = schema.get("kind", "node_type")
    compute_kinds = schema.get("compute_kinds", ["compute", "compute_node"])
    match = re.compile(schema.get("include_pattern", ".+")).fullmatch
    factor = {"W": 1, "kW": 1000}[schema.get("unit", "W")]
    samples = []
    for entry in batch:
        kind = field(entry, kind_path)
        node = field(entry, id_path)
        if not isinstance(node, str) or not match(node):
            continue
        if kind is not None and kind not in compute_kinds:
            continue
        value = field(entry, power_path)
        if value is None or isinstance(value, bool):
            continue
        collected = field(entry, time_path)
        if collected is None:
            collected = field(payload, time_path)
        try:
            watts = float(value) * factor
            collected_at = stamp(utc_time(collected))
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if not math.isfinite(watts) or watts < 0:
            continue
        sample = {"node_id": node, "watts": watts, "collected_at": collected_at}
        metrics = field(entry, schema.get("metrics", "metrics"))
        if isinstance(metrics, dict):
            sample["metrics"] = metrics
        rack = field(entry, schema.get("rack", "rack_id"))
        if isinstance(rack, str) and rack.strip():
            sample["rack_id"] = rack.strip()
        samples.append(sample)
    return samples
