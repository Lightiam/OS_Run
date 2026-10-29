"""Mapping tests: payload shape, conversions, omissions, selection and limits."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from snapshot import (MAX_ENTITIES, MAX_ERROR_CHARS, build, cpu_percent,
                      latest_per_host, metrics_for, parse_time, root_disk)

NOW = datetime(2026, 10, 29, 6, 0, 0, tzinfo=timezone.utc)


def at(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


_UNSET = object()


def measurement(hostname="local-box", collected=_UNSET, **metric_overrides):
    metrics = {
        "cpu": {"utilization": 0.2308},
        "memory": {"total_bytes": 8589934592, "used_bytes": 1073741824, "used_percent": 12.5},
        "disk": [{"mount": "/", "total_bytes": 107374182400, "used_bytes": 21474836480}],
        "network": [{"interface": "eth0", "rx_bytes": 5000, "tx_bytes": 7000}],
        "load": {"load1": 0.52},
        "uptime_seconds": 123456.78,
    }
    metrics.update(metric_overrides)
    return {
        "collected_at": at(1) if collected is _UNSET else collected,
        "host": {"hostname": hostname, "os": "Ubuntu 24.04.1 LTS",
                 "kernel": "6.8.0-45-generic", "arch": "x86_64"},
        "metrics": metrics,
    }


# --- 1. payload shape and the CPU conversion ---------------------------------

class PayloadShape(unittest.TestCase):
    def test_envelope_has_exactly_the_contract_fields(self):
        snap, _ = build([measurement()], "b-1", now=NOW)
        self.assertEqual(
            sorted(snap),
            ["batch_id", "entities", "errors", "observed_at", "platform", "version"],
        )
        self.assertEqual(snap["version"], 1)
        self.assertIsInstance(snap["version"], int)
        self.assertEqual(snap["platform"], "linux")
        self.assertEqual(snap["batch_id"], "b-1")

    def test_entity_has_exactly_the_contract_fields(self):
        snap, _ = build([measurement()], "b-1", now=NOW)
        entity = snap["entities"][0]
        self.assertEqual(sorted(entity), ["id", "kind", "metrics", "name", "state"])
        self.assertEqual(entity["kind"], "node")
        self.assertEqual(entity["state"], "observed")
        self.assertEqual(entity["id"], "local-box")
        self.assertEqual(entity["name"], "local-box")

    def test_cpu_ratio_becomes_percent(self):
        # 0.2308 is a ratio at the source and a percent at the destination.
        self.assertEqual(cpu_percent(0.2308), 23.08)
        snap, _ = build([measurement()], "b-1", now=NOW)
        self.assertEqual(snap["entities"][0]["metrics"]["cpu_pct"], 23.08)

    def test_no_source_property_is_forwarded(self):
        # The receiver rejects unknown fields, so load, uptime, network,
        # used_percent and the host block must not appear anywhere.
        snap, _ = build([measurement()], "b-1", now=NOW)
        self.assertEqual(
            sorted(snap["entities"][0]["metrics"]),
            ["cpu_pct", "disk_total_bytes", "disk_used_bytes",
             "memory_total_bytes", "memory_used_bytes"],
        )

    def test_errors_are_strings_not_objects(self):
        snap, _ = build([measurement(hostname="")], "b-1", now=NOW)
        self.assertTrue(all(isinstance(e, str) for e in snap["errors"]))


# --- 2. omissions and root-only disk -----------------------------------------

class Omissions(unittest.TestCase):
    def test_a_null_cpu_is_omitted_not_zero(self):
        # This is the live case: a static counter reports null, and a zero
        # would be a measurement that was never taken.
        out = metrics_for({"cpu": {"utilization": None},
                           "memory": {"used_bytes": 1, "total_bytes": 2}})
        self.assertNotIn("cpu_pct", out)
        self.assertEqual(out["memory_used_bytes"], 1)

    def test_a_ratio_outside_zero_to_one_is_not_a_ratio(self):
        for bad in (-0.1, 1.5, 23.08, float("inf"), float("nan"), True, "0.5", None):
            self.assertIsNone(cpu_percent(bad))

    def test_negative_and_nonfinite_byte_counts_are_dropped(self):
        out = metrics_for({"memory": {"used_bytes": -1, "total_bytes": float("inf")}})
        self.assertEqual(out, {})

    def test_only_the_root_mount_is_mapped(self):
        metrics = {"disk": [
            {"mount": "/boot", "used_bytes": 1, "total_bytes": 2},
            {"mount": "/", "used_bytes": 10, "total_bytes": 100},
            {"mount": "/var", "used_bytes": 5, "total_bytes": 50},
        ]}
        out = metrics_for(metrics)
        # Not summed: summing double-counts bind mounts and overlays.
        self.assertEqual(out["disk_used_bytes"], 10)
        self.assertEqual(out["disk_total_bytes"], 100)

    def test_no_root_mount_means_no_disk_metrics(self):
        self.assertIsNone(root_disk([{"mount": "/var"}]))
        self.assertEqual(metrics_for({"disk": [{"mount": "/var", "used_bytes": 5}]}), {})

    def test_a_host_with_no_usable_metric_is_not_sent(self):
        snap, report = build([measurement(cpu={}, memory={}, disk=[])], "b-1", now=NOW)
        self.assertEqual(snap["entities"], [])
        self.assertTrue(any("no usable metric" in e for e in snap["errors"]))
        self.assertEqual(report["entities"], 0)

    def test_garbage_in_place_of_metrics_yields_nothing_rather_than_raising(self):
        for junk in (None, [], "metrics", 7, {"cpu": [], "memory": "x", "disk": {}}):
            self.assertEqual(metrics_for(junk), {})


# --- 3. latest per host, timestamps, cursor ----------------------------------

class Selection(unittest.TestCase):
    def test_latest_measurement_per_hostname_wins(self):
        rows = [
            measurement("a", collected=at(30)),
            measurement("a", collected=at(1)),
            measurement("b", collected=at(5)),
        ]
        best, _, _ = latest_per_host(rows)
        self.assertEqual(sorted(best), ["a", "b"])
        self.assertEqual(best["a"][0], parse_time(at(1)))

    def test_selection_does_not_depend_on_source_ordering(self):
        oldest_first = [measurement("a", collected=at(30)), measurement("a", collected=at(1))]
        newest_first = list(reversed(oldest_first))
        self.assertEqual(latest_per_host(oldest_first)[0]["a"][0],
                         latest_per_host(newest_first)[0]["a"][0])

    def test_observed_at_is_the_real_collection_time_not_now(self):
        # Refreshing this would make stale data look live.
        snap, report = build([measurement(collected=at(90))], "b-1", now=NOW)
        self.assertEqual(snap["observed_at"], at(90))
        self.assertNotEqual(snap["observed_at"], at(0))
        self.assertAlmostEqual(report["age_seconds"], 5400, delta=1)

    def test_a_measurement_without_a_usable_timestamp_is_dropped(self):
        for bad in (None, "", "not a date", "2026-10-29T06:00:00"):  # last one is naive
            rows = [measurement(collected=bad)]
            snap, _ = build(rows, "b-1", now=NOW)
            self.assertEqual(snap["entities"], [], bad)

    def test_hostname_must_be_present_and_bounded(self):
        snap, _ = build([measurement("x" * 257), measurement("  ")], "b-1", now=NOW)
        self.assertEqual(snap["entities"], [])
        self.assertTrue(any("hostname" in e for e in snap["errors"]))


# --- 4. limits, staleness and future -----------------------------------------

class Limits(unittest.TestCase):
    def test_entities_are_capped_and_the_overflow_is_reported(self):
        rows = [measurement(f"host-{i:05d}") for i in range(MAX_ENTITIES + 5)]
        snap, report = build(rows, "b-1", now=NOW)
        self.assertEqual(len(snap["entities"]), MAX_ENTITIES)
        self.assertTrue(report["truncated"])
        self.assertTrue(any("over the 1000 limit" in e for e in snap["errors"]))

    def test_errors_are_capped_in_count_and_length(self):
        snap, _ = build([measurement(hostname="")], "b-1", now=NOW)
        self.assertLessEqual(len(snap["errors"]), 20)
        for message in snap["errors"]:
            self.assertLessEqual(len(message), MAX_ERROR_CHARS)

    def test_stale_source_data_is_flagged(self):
        _, report = build([measurement(collected=at(60 * 25))], "b-1", now=NOW)
        self.assertTrue(report["stale"])
        self.assertFalse(report["future"])

    def test_data_from_the_future_is_flagged(self):
        ahead = (NOW + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        _, report = build([measurement(collected=ahead)], "b-1", now=NOW)
        self.assertTrue(report["future"])

    def test_a_fresh_batch_is_neither_stale_nor_future(self):
        _, report = build([measurement(collected=at(1))], "b-1", now=NOW)
        self.assertFalse(report["stale"])
        self.assertFalse(report["future"])

    def test_size_is_measured_against_the_one_mebibyte_limit(self):
        _, report = build([measurement()], "b-1", now=NOW)
        self.assertFalse(report["oversized"])
        self.assertLess(report["bytes"], 1024 * 1024)


if __name__ == "__main__":
    unittest.main()


class EntityLag(unittest.TestCase):
    """One observed_at covers the batch, so a host far behind it is left out."""

    def test_a_host_far_behind_the_newest_is_not_carried_along(self):
        rows = [measurement("fresh", collected=at(0)),
                measurement("abandoned", collected=at(600))]
        snap, _ = build(rows, "b-1", now=NOW)
        self.assertEqual([e["id"] for e in snap["entities"]], ["fresh"])
        self.assertTrue(any("behind the newest reading" in e for e in snap["errors"]))

    def test_hosts_within_the_window_all_travel_together(self):
        rows = [measurement("a", collected=at(0)), measurement("b", collected=at(2))]
        snap, _ = build(rows, "b-1", now=NOW)
        self.assertEqual(sorted(e["id"] for e in snap["entities"]), ["a", "b"])

    def test_a_single_old_host_is_still_sent_and_flagged_stale(self):
        # Nothing to be behind, so it goes; staleness is reported separately.
        snap, report = build([measurement("only", collected=at(600))], "b-1", now=NOW)
        self.assertEqual([e["id"] for e in snap["entities"]], ["only"])
        self.assertEqual(snap["observed_at"], at(600))
        self.assertGreater(report["age_seconds"], 3000)
