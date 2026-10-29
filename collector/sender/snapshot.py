"""Map collector measurements onto the Infrastructure snapshot contract.

Pure functions, no I/O, so the mapping is testable without a server.

The receiver rejects unknown fields, so nothing here forwards arbitrary source
properties: every key in the output is one the contract names.

Three conversions are easy to get wrong and are therefore spelled out:

  * cpu.utilization is a 0..1 ratio at the source and cpu_pct is a percent at
    the destination. 0.2308 becomes 23.08. A ratio outside 0..1 is not a ratio
    and is dropped rather than clamped.
  * Only the root mount "/" is mapped. Summing mounts double-counts, because
    bind mounts and overlays report the same bytes more than once.
  * observed_at carries the newest real collection time, never wall-clock now.
    Refreshing it would make stale data look live, which is the one thing the
    receiver's 24 hour window exists to catch.
"""
import json
from datetime import datetime, timedelta, timezone

VERSION = 1
PLATFORM = "linux"
KIND = "node"
STATE = "observed"

MAX_ENTITIES = 1000
MAX_ERRORS = 20
MAX_ERROR_CHARS = 240
MAX_BYTES = 1024 * 1024
MAX_IDENTITY = 256

STALE_AFTER = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)

METRIC_FIELDS = (
    "cpu_pct",
    "memory_used_bytes",
    "memory_total_bytes",
    "disk_used_bytes",
    "disk_total_bytes",
)


def parse_time(value):
    """A timezone-aware datetime, or None. Naive input is rejected, not assumed UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value):
    """A finite number, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _non_negative(value):
    number = _number(value)
    return None if number is None or number < 0 else number


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _sequence(value):
    return value if isinstance(value, list) else []


def cpu_percent(utilization):
    """0..1 ratio to percent. Anything outside that range is not a ratio."""
    ratio = _number(utilization)
    if ratio is None or ratio < 0 or ratio > 1:
        return None
    return round(ratio * 100, 2)


def root_disk(disks):
    """The entry for "/", or None. Mounts are never summed."""
    for disk in _sequence(disks):
        disk = _mapping(disk)
        if disk.get("mount") == "/":
            return disk
    return None


def metrics_for(source):
    """Only the five contract metrics, each omitted when unusable."""
    source = _mapping(source)
    out = {}

    percent = cpu_percent(_mapping(source.get("cpu")).get("utilization"))
    if percent is not None:
        out["cpu_pct"] = percent

    memory = _mapping(source.get("memory"))
    for key, field in (("used_bytes", "memory_used_bytes"), ("total_bytes", "memory_total_bytes")):
        value = _non_negative(memory.get(key))
        if value is not None:
            out[field] = value

    disk = root_disk(source.get("disk"))
    if disk is not None:
        for key, field in (("used_bytes", "disk_used_bytes"), ("total_bytes", "disk_total_bytes")):
            value = _non_negative(disk.get(key))
            if value is not None:
                out[field] = value

    return out


def identity_of(measurement):
    """The hostname, or None. Identity never comes from a metric name."""
    host = _mapping(_mapping(measurement).get("host"))
    hostname = host.get("hostname")
    if not isinstance(hostname, str):
        return None
    hostname = hostname.strip()
    if not hostname or len(hostname) > MAX_IDENTITY:
        return None
    return hostname


def latest_per_host(measurements):
    """Newest measurement per hostname, plus a count of what was unusable.

    The source returns newest first, but this does not rely on that: it compares
    timestamps, so a change in source ordering cannot silently pick an old row.
    """
    best = {}
    skipped_no_host = 0
    skipped_no_time = 0

    for measurement in measurements:
        hostname = identity_of(measurement)
        if hostname is None:
            skipped_no_host += 1
            continue
        collected = parse_time(_mapping(measurement).get("collected_at"))
        if collected is None:
            skipped_no_time += 1
            continue
        current = best.get(hostname)
        if current is None or collected > current[0]:
            best[hostname] = (collected, measurement)

    return best, skipped_no_host, skipped_no_time


def build_entity(hostname, measurement):
    """One entity, or None when it carries no usable metric at all."""
    metrics = metrics_for(_mapping(measurement).get("metrics"))
    if not metrics:
        return None
    return {
        "id": hostname,
        "name": hostname,
        "kind": KIND,
        "state": STATE,
        "metrics": metrics,
    }


def _clip(messages):
    out = []
    for message in messages[:MAX_ERRORS]:
        text = str(message)
        out.append(text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…")
    return out


def build(measurements, batch_id, now=None):
    """Return (snapshot, report).

    `report` is for the operator, not the wire: it carries the newest source
    time, whether that is stale or in the future, and anything dropped. The
    caller decides what to do about it; this function never lies about time to
    make a batch acceptable.
    """
    now = now or datetime.now(timezone.utc)
    best, no_host, no_time = latest_per_host(measurements)

    entities = []
    newest = None
    without_metrics = 0

    for hostname in sorted(best):
        collected, measurement = best[hostname]
        entity = build_entity(hostname, measurement)
        if entity is None:
            without_metrics += 1
            continue
        entities.append(entity)
        if newest is None or collected > newest:
            newest = collected

    problems = []
    if no_host:
        problems.append(f"{no_host} measurement(s) had no usable hostname and were not sent.")
    if no_time:
        problems.append(f"{no_time} measurement(s) had no usable collected_at and were not sent.")
    if without_metrics:
        problems.append(f"{without_metrics} host(s) had no usable metric and were not sent.")

    truncated = False
    if len(entities) > MAX_ENTITIES:
        truncated = True
        problems.append(
            f"{len(entities) - MAX_ENTITIES} entities over the {MAX_ENTITIES} limit were not sent."
        )
        entities = entities[:MAX_ENTITIES]

    observed = newest or now
    stale = newest is not None and (now - newest) > STALE_AFTER
    future = newest is not None and (newest - now) > MAX_FUTURE_SKEW

    snapshot = {
        "version": VERSION,
        "platform": PLATFORM,
        "batch_id": batch_id,
        "observed_at": iso(observed),
        "entities": entities,
        "errors": _clip(problems),
    }

    size = len(json.dumps(snapshot).encode("utf-8"))
    oversized = size > MAX_BYTES

    report = {
        "entities": len(entities),
        "newest_source_time": iso(newest) if newest else None,
        "age_seconds": None if newest is None else (now - newest).total_seconds(),
        "stale": stale,
        "future": future,
        "truncated": truncated,
        "bytes": size,
        "oversized": oversized,
        "problems": problems,
    }
    return snapshot, report
