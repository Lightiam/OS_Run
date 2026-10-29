import json
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lightcollect.lightcollect import (
    Agent,
    Sender,
    Spool,
    collect,
    cpu_utilization,
    endpoint_url,
    read_cpu_sample,
    read_disks,
    read_load,
    read_memory,
    read_network,
    read_os_release,
    read_uptime,
    sample,
)

PROC_STAT = """cpu  100 10 50 1000 20 0 5 0 0 0
cpu0 50 5 25 500 10 0 2 0 0 0
intr 12345
ctxt 98765
"""

PROC_STAT_LATER = """cpu  200 10 100 1500 20 0 5 0 0 0
cpu0 100 5 50 750 10 0 2 0 0 0
intr 22345
"""

PROC_MEMINFO = """MemTotal:       16384000 kB
MemFree:         1024000 kB
MemAvailable:    8192000 kB
Buffers:          100000 kB
SwapTotal:       2048000 kB
SwapFree:        2048000 kB
"""

PROC_NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:    1000      10    0    0    0     0          0         0     1000      10    0    0    0     0       0          0
  eth0:    5000      50    0    0    0     0          0         0     7000      70    0    0    0     0       0          0
 veth1:     300       3    0    0    0     0          0         0      400       4    0    0    0     0       0          0
docker0:     10       1    0    0    0     0          0         0       20       2    0    0    0     0       0          0
"""

OS_RELEASE = """NAME="Ubuntu"
VERSION_ID="24.04"
PRETTY_NAME="Ubuntu 24.04.1 LTS"
ID=ubuntu
"""


def write_proc_root(root: Path, stat: str = PROC_STAT) -> Path:
    """Build a fake filesystem root holding the files the readers parse."""
    (root / "proc").mkdir(parents=True, exist_ok=True)
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "proc" / "stat").write_text(stat, encoding="utf-8")
    (root / "proc" / "meminfo").write_text(PROC_MEMINFO, encoding="utf-8")
    (root / "proc" / "loadavg").write_text("0.52 0.31 0.15 2/345 1234\n", encoding="utf-8")
    (root / "proc" / "uptime").write_text("123456.78 234567.89\n", encoding="utf-8")
    (root / "proc" / "net").mkdir(parents=True, exist_ok=True)
    (root / "proc" / "net" / "dev").write_text(PROC_NET_DEV, encoding="utf-8")
    (root / "etc" / "os-release").write_text(OS_RELEASE, encoding="utf-8")
    return root


class ProcReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = write_proc_root(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_cpu_sample_totals_every_counter(self):
        sample = read_cpu_sample(self.root)
        self.assertEqual(sample["user"], 100)
        self.assertEqual(sample["idle"], 1000)
        self.assertEqual(sample["iowait"], 20)
        self.assertEqual(sample["total"], 1185)

    def test_cpu_sample_rejects_file_without_aggregate_line(self):
        (self.root / "proc" / "stat").write_text("intr 1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_cpu_sample(self.root)

    def test_cpu_utilization_counts_iowait_as_idle(self):
        before = read_cpu_sample(self.root)
        write_proc_root(self.root, stat=PROC_STAT_LATER)
        after = read_cpu_sample(self.root)
        # 650 jiffies elapsed, 500 of them idle.
        self.assertEqual(cpu_utilization(before, after), 0.2308)

    def test_cpu_utilization_none_when_no_time_passed(self):
        sample = read_cpu_sample(self.root)
        self.assertIsNone(cpu_utilization(sample, sample))

    def test_cpu_utilization_clamped_to_unit_range(self):
        before = {"total": 0, "idle": 0, "iowait": 0}
        after = {"total": 100, "idle": -50, "iowait": 0}
        self.assertEqual(cpu_utilization(before, after), 1.0)

    def test_memory_converts_kb_to_bytes(self):
        memory = read_memory(self.root)
        self.assertEqual(memory["total_bytes"], 16384000 * 1024)
        self.assertEqual(memory["available_bytes"], 8192000 * 1024)
        self.assertEqual(memory["used_bytes"], 8192000 * 1024)
        self.assertEqual(memory["used_percent"], 50.0)
        self.assertEqual(memory["swap_total_bytes"], 2048000 * 1024)
        self.assertEqual(memory["swap_used_bytes"], 0)

    def test_memory_omits_percent_when_total_unknown(self):
        (self.root / "proc" / "meminfo").write_text("MemAvailable: 100 kB\n", encoding="utf-8")
        memory = read_memory(self.root)
        self.assertNotIn("used_percent", memory)
        self.assertNotIn("swap_total_bytes", memory)

    def test_load_and_uptime(self):
        self.assertEqual(read_load(self.root), {"load1": 0.52, "load5": 0.31, "load15": 0.15})
        self.assertEqual(read_uptime(self.root), 123456.78)

    def test_network_skips_virtual_interfaces(self):
        interfaces = read_network(self.root)
        self.assertEqual([i["interface"] for i in interfaces], ["eth0"])
        self.assertEqual(interfaces[0]["rx_bytes"], 5000)
        self.assertEqual(interfaces[0]["tx_bytes"], 7000)

    def test_network_ignores_truncated_rows(self):
        (self.root / "proc" / "net" / "dev").write_text(
            PROC_NET_DEV + "  eth9: 1 2 3\n", encoding="utf-8"
        )
        self.assertEqual([i["interface"] for i in read_network(self.root)], ["eth0"])

    def test_os_release_pretty_name(self):
        self.assertEqual(read_os_release(self.root), "Ubuntu 24.04.1 LTS")

    def test_os_release_missing_file_is_not_fatal(self):
        (self.root / "etc" / "os-release").unlink()
        self.assertIsNone(read_os_release(self.root))

    def test_disks_skip_paths_that_do_not_exist(self):
        disks = read_disks([str(self.root), str(self.root / "nope")])
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["mount"], str(self.root))
        self.assertGreater(disks[0]["total_bytes"], 0)

    def test_collect_builds_the_full_measurement(self):
        before = read_cpu_sample(self.root)
        write_proc_root(self.root, stat=PROC_STAT_LATER)
        after = read_cpu_sample(self.root)
        measurement = collect(self.root, [str(self.root)], before, after)

        self.assertEqual(measurement["schema"], "lightos.collector.v1")
        self.assertTrue(measurement["collected_at"].endswith("Z"))
        self.assertEqual(measurement["host"]["os"], "Ubuntu 24.04.1 LTS")
        metrics = measurement["metrics"]
        self.assertEqual(metrics["cpu"]["utilization"], 0.2308)
        self.assertEqual(metrics["uptime_seconds"], 123456.78)
        self.assertEqual(len(metrics["disk"]), 1)
        self.assertEqual(len(metrics["network"]), 1)
        # The payload has to survive a JSON round trip to be sendable.
        self.assertEqual(json.loads(json.dumps(measurement))["schema"], measurement["schema"])


class SampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = write_proc_root(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_previous_sample_spans_the_interval_without_settling(self):
        # A sample from the last tick means no settle window is needed now.
        before = {"total": 500, "idle": 400, "iowait": 0}
        started = time.monotonic()
        measurement, after = sample(
            self.root, [str(self.root)], settle=5.0, cpu_before=before
        )
        self.assertLess(time.monotonic() - started, 2.0)
        # Fixture totals 1185 jiffies, 1000 idle + 20 iowait: 685 elapsed, 620 idle.
        self.assertEqual(
            measurement["metrics"]["cpu"]["utilization"], round(1 - 620 / 685, 4)
        )
        self.assertEqual(after["total"], 1185)

    def test_first_sample_measures_its_own_window(self):
        measurement, after = sample(self.root, [str(self.root)], settle=0.0)
        # Static fixture: no jiffies elapsed, so utilization is unknown.
        self.assertIsNone(measurement["metrics"]["cpu"]["utilization"])
        self.assertEqual(after["total"], 1185)


class EndpointUrlTests(unittest.TestCase):
    def test_joins_without_doubling_or_dropping_slashes(self):
        self.assertEqual(
            endpoint_url("https://app.example.com/", "/api/x"),
            "https://app.example.com/api/x",
        )
        self.assertEqual(
            endpoint_url("https://app.example.com", "api/x"),
            "https://app.example.com/api/x",
        )


class SpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "spool" / "measurements.jsonl"
        self.addCleanup(self._tmp.cleanup)

    def test_append_creates_parent_and_counts_pending(self):
        spool = Spool(self.path)
        self.assertEqual(spool.pending(), 0)
        spool.append([{"n": 1}, {"n": 2}])
        self.assertEqual(spool.pending(), 2)

    def test_drain_removes_each_accepted_batch(self):
        spool = Spool(self.path)
        spool.append([{"n": i} for i in range(5)])
        batches = []
        for batch in spool.drain(2):
            batches.append(batch)
        self.assertEqual([len(b) for b in batches], [2, 2, 1])
        self.assertEqual(spool.pending(), 0)
        self.assertFalse(self.path.exists())

    def test_drain_keeps_backlog_when_consumer_fails(self):
        spool = Spool(self.path)
        spool.append([{"n": i} for i in range(4)])
        with self.assertRaises(RuntimeError):
            for _batch in spool.drain(2):
                raise RuntimeError("send failed")
        # Nothing was acknowledged, so nothing may be lost.
        self.assertEqual(spool.pending(), 4)

    def test_trim_drops_oldest_once_over_cap(self):
        spool = Spool(self.path, max_bytes=120)
        spool.append([{"n": i, "pad": "x" * 20} for i in range(10)])
        remaining = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertLessEqual(self.path.stat().st_size, 120)
        self.assertEqual(remaining[-1]["n"], 9)
        self.assertGreater(remaining[0]["n"], 0)


class RecordingSink:
    """A real HTTP server, so the sender is exercised over a socket."""

    def __init__(self, status: int = 202) -> None:
        self.requests: list[dict] = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", "0"))
                sink.requests.append(
                    {
                        "path": self.path,
                        "body": self.rfile.read(length).decode("utf-8"),
                        "auth": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                    }
                )
                self.send_response(status)
                self.end_headers()

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/events"

    def lines(self) -> list[dict]:
        out = []
        for request in self.requests:
            out.extend(json.loads(line) for line in request["body"].splitlines() if line.strip())
        return out

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class SenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sink = RecordingSink()
        self.addCleanup(self.sink.close)

    def test_posts_ndjson_with_bearer_key(self):
        Sender(self.sink.url, "lfc_test_key").send(['{"n":1}', '{"n":2}'])
        self.assertEqual(len(self.sink.requests), 1)
        request = self.sink.requests[0]
        self.assertEqual(request["path"], "/events")
        self.assertEqual(request["auth"], "Bearer lfc_test_key")
        self.assertEqual(request["content_type"], "application/x-ndjson")
        self.assertEqual(request["body"], '{"n":1}\n{"n":2}\n')

    def test_rejection_raises(self):
        sink = RecordingSink(status=500)
        self.addCleanup(sink.close)
        with self.assertRaises(urllib.error.URLError):
            Sender(sink.url, "lfc_test_key").send(['{"n":1}'])

    def test_unreachable_endpoint_raises(self):
        # Port 1 on loopback refuses connections.
        with self.assertRaises((urllib.error.URLError, OSError)):
            Sender("http://127.0.0.1:1/events", "lfc_test_key", timeout=2).send(['{"n":1}'])


class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(Path(self._tmp.name) / "measurements.jsonl")
        self.addCleanup(self._tmp.cleanup)

    def test_delivers_straight_through_when_endpoint_is_up(self):
        sink = RecordingSink()
        self.addCleanup(sink.close)
        agent = Agent(Sender(sink.url, "k"), self.spool)

        self.assertTrue(agent.deliver({"n": 1}))
        self.assertEqual(self.spool.pending(), 0)
        self.assertEqual(sink.lines(), [{"n": 1}])

    def test_spools_when_endpoint_is_down(self):
        agent = Agent(Sender("http://127.0.0.1:1/events", "k", timeout=2), self.spool)

        self.assertFalse(agent.deliver({"n": 1}))
        self.assertFalse(agent.deliver({"n": 2}))
        self.assertEqual(self.spool.pending(), 2)

    def test_backlog_is_flushed_in_order_once_endpoint_returns(self):
        down = Agent(Sender("http://127.0.0.1:1/events", "k", timeout=2), self.spool)
        down.deliver({"n": 1})
        down.deliver({"n": 2})

        sink = RecordingSink()
        self.addCleanup(sink.close)
        up = Agent(Sender(sink.url, "k"), self.spool)
        self.assertTrue(up.deliver({"n": 3}))

        self.assertEqual(sink.lines(), [{"n": 1}, {"n": 2}, {"n": 3}])
        self.assertEqual(self.spool.pending(), 0)


if __name__ == "__main__":
    unittest.main()
