#!/usr/bin/env python3
"""Outbound host-metrics collector for LightOS.

Reads host metrics from /proc, batches them as newline-delimited JSON, and POSTs
them to the LightOS app. Every connection is outbound, so the host needs no
inbound port and no public address.

Measurements that cannot be delivered are spooled to disk and retried on later
ticks, so a network outage costs latency rather than data.

Standard library only: the collector runs on a stock Ubuntu host with no pip
install step.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = "lightos.collector.v1"
DEFAULT_INGEST_PATH = "/functions/v1/collector-ingest"
DEFAULT_INTERVAL = 60.0
DEFAULT_SPOOL_DIR = "/var/lib/lightos-collector/spool"
DEFAULT_SPOOL_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
DEFAULT_BATCH = 100

# Interfaces that never describe real traffic for the host.
SKIPPED_IFACE_PREFIXES = ("lo", "veth", "docker", "br-", "cni", "flannel", "kube")

CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")

MEMINFO_FIELDS = {
    "MemTotal": "total",
    "MemAvailable": "available",
    "SwapTotal": "swap_total",
    "SwapFree": "swap_free",
}


# --------------------------------------------------------------------------- #
# Metric readers. Each takes a filesystem root so tests can supply fixtures.
# --------------------------------------------------------------------------- #

def _read_text(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def read_cpu_sample(root: Path) -> dict[str, int]:
    """Return the aggregate CPU jiffy counters from /proc/stat."""
    for line in _read_text(root, "proc/stat").splitlines():
        if line.startswith("cpu "):
            values = [int(v) for v in line.split()[1:]]
            sample = {name: 0 for name in CPU_FIELDS}
            for name, value in zip(CPU_FIELDS, values):
                sample[name] = value
            sample["total"] = sum(values)
            return sample
    raise ValueError("no aggregate 'cpu' line in /proc/stat")


def cpu_utilization(before: dict[str, int], after: dict[str, int]) -> float | None:
    """Busy fraction of wall time between two samples, or None if unusable."""
    total_delta = after["total"] - before["total"]
    if total_delta <= 0:
        return None
    idle_delta = (after["idle"] - before["idle"]) + (after["iowait"] - before["iowait"])
    busy = 1.0 - (idle_delta / total_delta)
    return round(max(0.0, min(1.0, busy)), 4)


def read_memory(root: Path) -> dict[str, Any]:
    """Memory totals in bytes, derived from /proc/meminfo."""
    values: dict[str, int] = {}
    for line in _read_text(root, "proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        if key in MEMINFO_FIELDS:
            values[MEMINFO_FIELDS[key]] = int(rest.split()[0]) * 1024

    total = values.get("total", 0)
    available = values.get("available", 0)
    memory: dict[str, Any] = {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": max(0, total - available),
    }
    if "swap_total" in values:
        memory["swap_total_bytes"] = values["swap_total"]
        memory["swap_used_bytes"] = max(
            0, values["swap_total"] - values.get("swap_free", 0)
        )
    if total:
        memory["used_percent"] = round((total - available) / total * 100, 2)
    return memory


def read_load(root: Path) -> dict[str, float]:
    fields = _read_text(root, "proc/loadavg").split()
    return {
        "load1": float(fields[0]),
        "load5": float(fields[1]),
        "load15": float(fields[2]),
    }


def read_uptime(root: Path) -> float:
    return round(float(_read_text(root, "proc/uptime").split()[0]), 2)


def read_network(root: Path) -> list[dict[str, Any]]:
    """Per-interface byte counters from /proc/net/dev, virtual devices skipped."""
    interfaces: list[dict[str, Any]] = []
    for line in _read_text(root, "proc/net/dev").splitlines()[2:]:
        name, sep, rest = line.partition(":")
        name = name.strip()
        if not sep or not name or name.startswith(SKIPPED_IFACE_PREFIXES):
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        interfaces.append(
            {
                "interface": name,
                "rx_bytes": int(fields[0]),
                "tx_bytes": int(fields[8]),
            }
        )
    return interfaces


def read_disks(mounts: list[str]) -> list[dict[str, Any]]:
    """Capacity for each mount point that exists and is readable."""
    disks: list[dict[str, Any]] = []
    for mount in mounts:
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        disks.append(
            {
                "mount": mount,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "used_percent": (
                    round(usage.used / usage.total * 100, 2) if usage.total else 0.0
                ),
            }
        )
    return disks


def read_os_release(root: Path) -> str | None:
    try:
        text = _read_text(root, "etc/os-release")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.partition("=")[2].strip().strip('"')
    return None


def host_facts(root: Path) -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "os": read_os_release(root),
        "kernel": platform.release(),
        "arch": platform.machine(),
    }


def collect(
    root: Path,
    mounts: list[str],
    cpu_before: dict[str, int],
    cpu_after: dict[str, int],
) -> dict[str, Any]:
    """Build one measurement from two CPU samples and the current host state."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema": SCHEMA,
        "collected_at": now.replace("+00:00", "Z"),
        "host": host_facts(root),
        "metrics": {
            "cpu": {"utilization": cpu_utilization(cpu_before, cpu_after)},
            "memory": read_memory(root),
            "load": read_load(root),
            "uptime_seconds": read_uptime(root),
            "disk": read_disks(mounts),
            "network": read_network(root),
        },
    }


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #

class Spool:
    """Append-only JSONL buffer for measurements that could not be delivered."""

    def __init__(self, path: Path, max_bytes: int = DEFAULT_SPOOL_MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes

    def append(self, measurements: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fp:
            for measurement in measurements:
                fp.write(json.dumps(measurement, separators=(",", ":")) + "\n")
        self._trim()

    def _trim(self) -> None:
        """Drop the oldest lines once the spool outgrows its cap."""
        if not self.path.exists() or self.path.stat().st_size <= self.max_bytes:
            return
        lines = self.path.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        size = 0
        for line in reversed(lines):
            size += len(line) + 1
            if size > self.max_bytes:
                break
            kept.append(line)
        kept.reverse()
        self._write_lines(kept)

    def _write_lines(self, lines: list[str]) -> None:
        if not lines:
            self.path.unlink(missing_ok=True)
            return
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def pending(self) -> int:
        """Number of spooled measurements awaiting delivery."""
        if not self.path.exists():
            return 0
        text = self.path.read_text(encoding="utf-8")
        return len([line for line in text.splitlines() if line.strip()])

    def drain(self, batch: int) -> Iterator[list[str]]:
        """Yield batches of spooled lines, removing each batch once accepted.

        A batch is only dropped after the consumer returns, so an exception
        raised while sending leaves the backlog on disk.
        """
        while self.path.exists():
            lines = [
                line
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if not lines:
                self.path.unlink(missing_ok=True)
                return
            head, tail = lines[:batch], lines[batch:]
            yield head
            self._write_lines(tail)


class Sender:
    """POSTs newline-delimited JSON to the LightOS ingest endpoint."""

    def __init__(self, url: str, key: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.url = url
        self.key = key
        self.timeout = timeout

    def send(self, lines: list[str]) -> None:
        """Deliver one batch. Raises URLError or OSError when undelivered."""
        body = ("\n".join(lines) + "\n").encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-ndjson",
                "Authorization": f"Bearer {self.key}",
                "User-Agent": "lightos-collector/1",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if response.status >= 400:
                raise urllib.error.URLError(f"ingest returned {response.status}")


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #

def endpoint_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def sample(
    root: Path,
    mounts: list[str],
    settle: float,
    cpu_before: dict[str, int] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Collect one measurement, returning it with the CPU sample it ended on.

    Utilization needs two CPU samples. Given the previous tick's sample, the
    figure covers the whole interval; without one, it covers a short `settle`
    window taken here, which is all a single --once run can measure.
    """
    if cpu_before is None:
        cpu_before = read_cpu_sample(root)
        time.sleep(settle)
    cpu_after = read_cpu_sample(root)
    return collect(root, mounts, cpu_before, cpu_after), cpu_after


class Agent:
    """Ties a sender to a spool: deliver now, or keep it for the next tick."""

    def __init__(
        self, sender: Sender, spool: Spool, batch: int = DEFAULT_BATCH
    ) -> None:
        self.sender = sender
        self.spool = spool
        self.batch = batch

    def deliver(self, measurement: dict[str, Any]) -> bool:
        """Send the backlog then this measurement. False means it was spooled."""
        line = json.dumps(measurement, separators=(",", ":"))
        try:
            for backlog in self.spool.drain(self.batch):
                self.sender.send(backlog)
            self.sender.send([line])
        except (urllib.error.URLError, OSError) as exc:
            print(f"delivery failed ({exc}); spooling", flush=True)
            self.spool.append([measurement])
            return False
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LightOS outbound metrics collector"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("LIGHTOS_URL"),
        help="base URL of the published LightOS app",
    )
    parser.add_argument(
        "--ingest-path",
        default=os.environ.get("LIGHTOS_INGEST_PATH", DEFAULT_INGEST_PATH),
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("LIGHTOS_COLLECTOR_KEY"),
        help="environment key; prefer the env var so it stays out of ps output",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("LIGHTOS_INTERVAL", DEFAULT_INTERVAL)),
    )
    parser.add_argument(
        "--spool-dir", default=os.environ.get("LIGHTOS_SPOOL_DIR", DEFAULT_SPOOL_DIR)
    )
    parser.add_argument(
        "--mounts",
        default=os.environ.get("LIGHTOS_MOUNTS", "/"),
        help="comma-separated mount points to report",
    )
    parser.add_argument(
        "--root", default="/", help="filesystem root to read /proc from"
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=1.0,
        help="seconds between the two CPU samples",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="collect and send a single measurement, then exit",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the measurement instead of sending it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    mounts = [mount for mount in args.mounts.split(",") if mount]

    if args.print_only:
        measurement, _ = sample(root, mounts, args.settle)
        print(json.dumps(measurement, indent=2))
        return 0

    if not args.url or not args.key:
        print("LIGHTOS_URL and LIGHTOS_COLLECTOR_KEY are required", flush=True)
        return 2

    agent = Agent(
        Sender(endpoint_url(args.url, args.ingest_path), args.key),
        Spool(Path(args.spool_dir) / "measurements.jsonl"),
    )

    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    prev_cpu: dict[str, int] | None = None
    next_tick = time.monotonic()

    while True:
        next_tick += args.interval
        measurement, prev_cpu = sample(root, mounts, args.settle, prev_cpu)
        delivered = agent.deliver(measurement)
        if args.once:
            return 0 if delivered else 1
        if stopping:
            return 0
        # Hold the cadence whatever sampling and sending cost.
        time.sleep(max(0.0, next_tick - time.monotonic()))
        if stopping:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
