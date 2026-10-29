import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from snapshot import entity, readings, snapshot


def measurement(hostname="node-01", **overrides):
    base = {
        "schema": "lightos.collector.v1",
        "collected_at": "2026-10-29T05:00:00Z",
        "host": {"hostname": hostname, "os": "Ubuntu 24.04.1 LTS",
                 "kernel": "6.8.0-45-generic", "arch": "x86_64"},
        "metrics": {
            "cpu": {"utilization": 0.2308},
            "memory": {"total_bytes": 8589934592, "available_bytes": 4294967296,
                       "used_bytes": 4294967296, "used_percent": 50.0},
            "load": {"load1": 0.52, "load5": 0.31, "load15": 0.15},
            "uptime_seconds": 123456.78,
            "disk": [{"mount": "/", "total_bytes": 100, "used_bytes": 20,
                      "free_bytes": 80, "used_percent": 20.0}],
            "network": [{"interface": "eth0", "rx_bytes": 5000, "tx_bytes": 7000}],
        },
    }
    base.update(overrides)
    return base


def by_name(items):
    return {item["name"]: item for item in items}


class HostIdentity(unittest.TestCase):
    def test_hostname_is_identity_not_part_of_metric_names(self):
        # Encoding the host into metric names makes every name unique per
        # machine, which destroys aggregation across a fleet.
        built = entity(measurement("node-01"))
        self.assertEqual(built["identity"]["hostname"], "node-01")
        for reading in built["readings"]:
            self.assertNotIn("node-01", reading["name"])

    def test_os_kernel_and_arch_ride_along(self):
        identity = entity(measurement())["identity"]
        self.assertEqual(identity["kernel"], "6.8.0-45-generic")
        self.assertEqual(identity["arch"], "x86_64")

    def test_a_measurement_with_no_hostname_is_dropped(self):
        self.assertIsNone(entity(measurement(host={})))
        self.assertIsNone(entity(measurement("   ")))

    def test_dropped_measurements_are_reported_rather_than_vanishing(self):
        out = snapshot([measurement(), measurement("  ")], "b1", "2026-10-29T05:00:00Z")
        self.assertEqual(len(out["entities"]), 1)
        self.assertEqual(out["errors"][0]["code"], "unattributed_measurement")


class Units(unittest.TestCase):
    def test_cpu_stays_a_ratio_while_percents_stay_percents(self):
        # The agent sends cpu.utilization as 0..1 and used_percent as 0..100.
        # Treating them alike is how 23% silently becomes 0%.
        found = by_name(readings(measurement()["metrics"]))
        self.assertEqual(found["cpu.utilization"]["value"], 0.2308)
        self.assertEqual(found["cpu.utilization"]["unit"], "ratio")
        self.assertEqual(found["memory.used_percent"]["value"], 50.0)
        self.assertEqual(found["memory.used_percent"]["unit"], "percent")

    def test_byte_and_second_readings_carry_their_units(self):
        found = by_name(readings(measurement()["metrics"]))
        self.assertEqual(found["memory.total"]["unit"], "bytes")
        self.assertEqual(found["uptime"]["unit"], "seconds")
        self.assertEqual(found["network.eth0.rx"]["unit"], "bytes")


class Omissions(unittest.TestCase):
    def test_an_unavailable_reading_is_omitted_not_sent_as_zero(self):
        m = measurement()
        m["metrics"]["cpu"] = {"utilization": None}
        found = by_name(readings(m["metrics"]))
        self.assertNotIn("cpu.utilization", found)
        # The rest of the measurement still goes.
        self.assertIn("memory.used_percent", found)

    def test_booleans_and_non_numbers_are_not_numbers(self):
        m = measurement()
        m["metrics"]["uptime_seconds"] = True
        m["metrics"]["load"] = {"load1": "0.52"}
        found = by_name(readings(m["metrics"]))
        self.assertNotIn("uptime", found)
        self.assertNotIn("load.load1", found)

    def test_a_disk_entry_with_no_mount_is_skipped(self):
        m = measurement()
        m["metrics"]["disk"] = [{"used_percent": 20.0}, {"mount": "/var", "used_percent": 5.0}]
        found = by_name(readings(m["metrics"]))
        self.assertIn("disk./var.used_percent", found)
        self.assertEqual(len([k for k in found if k.startswith("disk.")]), 1)

    def test_garbage_in_place_of_metrics_produces_no_readings_rather_than_an_error(self):
        for junk in (None, [], "metrics", 7, {"cpu": [], "memory": "x", "disk": {}, "network": 3}):
            self.assertEqual(readings(junk), [])


class Envelope(unittest.TestCase):
    def test_carries_every_field_the_contract_names(self):
        out = snapshot([measurement()], "batch-1", "2026-10-29T05:00:00Z")
        self.assertEqual(
            sorted(out),
            ["batch_id", "entities", "errors", "observed_at", "platform", "version"],
        )
        self.assertEqual(out["batch_id"], "batch-1")
        self.assertEqual(out["platform"], "linux")

    def test_an_empty_batch_is_still_a_valid_snapshot(self):
        out = snapshot([], "batch-2", "2026-10-29T05:00:00Z")
        self.assertEqual(out["entities"], [])
        self.assertEqual(out["errors"], [])


if __name__ == "__main__":
    unittest.main()
