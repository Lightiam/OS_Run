"""Turn collector measurements into Infrastructure snapshots.

Pure functions, no I/O, so the mapping can be tested without a server.

The envelope -- version, platform, batch_id, observed_at, entities, errors --
is the contract this was written against. It could not be verified against any
code available here, so if the receiving end disagrees, change it HERE: nothing
else in the sender knows the shape.

Three rules the mapping holds to deliberately:

  * Host identity lives in the entity, never in a metric name. Encoding it as
    "host.<name>.cpu.utilization" makes every metric name unique per machine,
    which breaks aggregation across a fleet and cannot be undone later.
  * Units are explicit, because the sources disagree. cpu.utilization is a 0..1
    ratio while used_percent is 0..100, and treating them alike is how 23%
    silently becomes 0%.
  * An unavailable reading is omitted, never sent as zero. The agent reports
    null when a counter did not move; a zero would be a measurement that was
    never taken.
"""

SNAPSHOT_VERSION = "1"
PLATFORM = "linux"

RATIO = "ratio"
PERCENT = "percent"
BYTES = "bytes"
SECONDS = "seconds"
COUNT = "count"


def _number(value):
    """A finite number, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _sequence(value):
    return value if isinstance(value, list) else []


def _reading(name, value, unit):
    """One reading, or None when the value is unusable."""
    number = _number(value)
    return None if number is None else {"name": name, "value": number, "unit": unit}


def readings(metrics):
    """Flatten one measurement's metrics, dropping everything unavailable.

    Names carry no host component: identity belongs to the entity.
    """
    metrics = _mapping(metrics)
    out = []

    def add(name, value, unit):
        reading = _reading(name, value, unit)
        if reading is not None:
            out.append(reading)

    add("cpu.utilization", _mapping(metrics.get("cpu")).get("utilization"), RATIO)

    memory = _mapping(metrics.get("memory"))
    add("memory.total", memory.get("total_bytes"), BYTES)
    add("memory.available", memory.get("available_bytes"), BYTES)
    add("memory.used", memory.get("used_bytes"), BYTES)
    add("memory.used_percent", memory.get("used_percent"), PERCENT)

    load = _mapping(metrics.get("load"))
    for window in ("load1", "load5", "load15"):
        add(f"load.{window}", load.get(window), COUNT)

    add("uptime", metrics.get("uptime_seconds"), SECONDS)

    for disk in _sequence(metrics.get("disk")):
        disk = _mapping(disk)
        mount = disk.get("mount")
        if not isinstance(mount, str) or not mount:
            continue
        add(f"disk.{mount}.total", disk.get("total_bytes"), BYTES)
        add(f"disk.{mount}.used", disk.get("used_bytes"), BYTES)
        add(f"disk.{mount}.free", disk.get("free_bytes"), BYTES)
        add(f"disk.{mount}.used_percent", disk.get("used_percent"), PERCENT)

    for interface in _sequence(metrics.get("network")):
        interface = _mapping(interface)
        name = interface.get("interface")
        if not isinstance(name, str) or not name:
            continue
        add(f"network.{name}.rx", interface.get("rx_bytes"), BYTES)
        add(f"network.{name}.tx", interface.get("tx_bytes"), BYTES)

    return out


def entity(measurement):
    """One host entity, or None when the measurement cannot identify a host.

    A measurement with no hostname is dropped rather than guessed at: an
    unattributable reading is worse than a missing one.
    """
    measurement = _mapping(measurement)
    host = _mapping(measurement.get("host"))
    hostname = host.get("hostname")
    if not isinstance(hostname, str) or not hostname.strip():
        return None

    identity = {"hostname": hostname.strip()}
    for field in ("os", "kernel", "arch"):
        value = host.get(field)
        if isinstance(value, str) and value.strip():
            identity[field] = value.strip()

    return {
        "kind": "host",
        "identity": identity,
        "observed_at": measurement.get("collected_at"),
        "readings": readings(measurement.get("metrics")),
    }


def snapshot(measurements, batch_id, observed_at, platform=PLATFORM, errors=None):
    """The envelope for a batch of measurements.

    Measurements that cannot be attributed to a host are counted in `errors`
    rather than dropped silently, so the receiving end can see that something
    was collected and discarded.
    """
    entities = []
    unattributed = 0
    for measurement in measurements:
        built = entity(measurement)
        if built is None:
            unattributed += 1
        else:
            entities.append(built)

    reported = list(errors or [])
    if unattributed:
        reported.append({
            "code": "unattributed_measurement",
            "message": f"{unattributed} measurement(s) carried no hostname and were not sent.",
        })

    return {
        "version": SNAPSHOT_VERSION,
        "platform": platform,
        "batch_id": batch_id,
        "observed_at": observed_at,
        "entities": entities,
        "errors": reported,
    }
