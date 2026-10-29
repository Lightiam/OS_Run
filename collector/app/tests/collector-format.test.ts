import { describe, expect, it } from "vitest";
import {
  STALE_AFTER_MS, formatBytes, formatPercent, formatUptime, health, relativeTime, summarize,
} from "@/lib/collector/format";

const NOW = Date.parse("2026-10-28T12:00:00Z");
const ago = (ms: number) => new Date(NOW - ms).toISOString();

describe("health", () => {
  it("calls a revoked environment revoked whatever it last reported", () => {
    expect(health({ status: "revoked", last_seen_at: ago(1000) }, NOW)).toBe("revoked");
  });

  it("waits for a first post rather than claiming anything", () => {
    expect(health({ status: "active", last_seen_at: null }, NOW)).toBe("waiting");
    expect(health({ status: "active", last_seen_at: "not a date" }, NOW)).toBe("waiting");
  });

  it("tolerates a missed tick but not three", () => {
    expect(health({ status: "active", last_seen_at: ago(90_000) }, NOW)).toBe("live");
    expect(health({ status: "active", last_seen_at: ago(STALE_AFTER_MS) }, NOW)).toBe("live");
    expect(health({ status: "active", last_seen_at: ago(STALE_AFTER_MS + 1) }, NOW)).toBe("stale");
  });
});

describe("relativeTime", () => {
  it.each([
    [null, "never"],
    ["nonsense", "never"],
  ])("says never for %s", (iso, expected) => {
    expect(relativeTime(iso, NOW)).toBe(expected);
  });

  it.each([
    [0, "just now"],
    [44_000, "just now"],
    [-5_000, "just now"], // a collector box whose clock runs fast
    [120_000, "2m ago"],
    [59 * 60_000, "59m ago"],
    [3 * 3_600_000, "3h ago"],
    [50 * 3_600_000, "2d ago"],
  ])("renders %ims as %s", (ms, expected) => {
    expect(relativeTime(ago(ms), NOW)).toBe(expected);
  });
});

describe("formatBytes", () => {
  it.each([
    [0, "0 B"],
    [512, "512 B"],
    [1024, "1.0 KB"],
    [1536, "1.5 KB"],
    [15 * 1024, "15 KB"],
    [2 * 1024 ** 3, "2.0 GB"],
    [1024 ** 5, "1.0 PB"],
  ])("renders %i as %s", (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });

  it.each([null, undefined, NaN, -1, Infinity])("renders %s as an em dash", (value) => {
    expect(formatBytes(value as number)).toBe("—");
  });
});

describe("formatPercent and formatUptime", () => {
  it("renders a fraction as whole percent, and null as an em dash", () => {
    expect(formatPercent(0.2308)).toBe("23%");
    expect(formatPercent(1)).toBe("100%");
    expect(formatPercent(0)).toBe("0%");
    expect(formatPercent(null)).toBe("—");
  });

  it.each([
    [0, "0m"],
    [1800, "30m"],
    [7_200, "2h 0m"],
    [123_456.78, "1d 10h"],
  ])("renders %s seconds of uptime as %s", (seconds, expected) => {
    expect(formatUptime(seconds)).toBe(expected);
  });

  it("renders unknown uptime as an em dash", () => {
    expect(formatUptime(null)).toBe("—");
    expect(formatUptime(-1)).toBe("—");
  });
});

describe("summarize", () => {
  // One real lightos.collector.v1 payload, as the agent builds it.
  const measurement = {
    host: { hostname: "node-01", os: "Ubuntu 24.04.1 LTS", kernel: "6.8.0-45-generic", arch: "x86_64" },
    metrics: {
      cpu: { utilization: 0.2308 },
      memory: { total_bytes: 8_589_934_592, available_bytes: 4_294_967_296, used_bytes: 4_294_967_296, used_percent: 50 },
      load: { load1: 0.52, load5: 0.31, load15: 0.15 },
      uptime_seconds: 123_456.78,
      disk: [
        { mount: "/boot", total_bytes: 1_073_741_824, used_bytes: 536_870_912, free_bytes: 536_870_912, used_percent: 50 },
        { mount: "/", total_bytes: 107_374_182_400, used_bytes: 21_474_836_480, free_bytes: 85_899_345_920, used_percent: 20 },
      ],
      network: [{ interface: "eth0", rx_bytes: 5000, tx_bytes: 7000 }],
    },
  };

  it("keeps CPU on its 0..1 scale while rescaling the percent fields", () => {
    // The agent sends cpu.utilization as a fraction and used_percent as 0..100.
    // Treating them alike is how 23% silently becomes 0%.
    const s = summarize(measurement);
    expect(s.cpu).toBe(0.2308);
    expect(s.memory).toBe(0.5);
    expect(s.disk).toBe(0.2);
    expect(formatPercent(s.cpu)).toBe("23%");
    expect(formatPercent(s.memory)).toBe("50%");
  });

  it("reads the host description and the rest of the figures", () => {
    const s = summarize(measurement);
    expect(s).toMatchObject({
      os: "Ubuntu 24.04.1 LTS",
      kernel: "6.8.0-45-generic",
      arch: "x86_64",
      load1: 0.52,
      memoryUsedBytes: 4_294_967_296,
      memoryTotalBytes: 8_589_934_592,
      uptimeSeconds: 123_456.78,
    });
  });

  it("prefers the root filesystem over whatever is listed first", () => {
    expect(summarize(measurement).diskMount).toBe("/");
  });

  it("falls back to the first mount when the agent was not watching /", () => {
    const s = summarize({ host: {}, metrics: { disk: [{ mount: "/srv", used_percent: 75 }] } });
    expect(s.diskMount).toBe("/srv");
    expect(s.disk).toBe(0.75);
  });

  it("passes an unknown CPU through as unknown rather than zero", () => {
    const s = summarize({ host: {}, metrics: { cpu: { utilization: null } } });
    expect(s.cpu).toBeNull();
    expect(formatPercent(s.cpu)).toBe("—");
  });

  it.each([
    ["nothing at all", null],
    ["an empty payload", { host: {}, metrics: {} }],
    ["arrays where objects belong", { host: [], metrics: { cpu: [], memory: [], load: [], disk: {} } }],
    ["strings where numbers belong", { host: { os: "" }, metrics: { cpu: { utilization: "80%" }, memory: { used_percent: "half" }, uptime_seconds: "lots" } }],
    ["a schema this UI has never seen", { host: { platform: "darwin" }, metrics: { v2: { cpu: 0.5 } } }],
  ])("reports every figure as unknown given %s", (_label, payload) => {
    const s = summarize(payload as never);
    expect(s).toEqual({
      os: null, kernel: null, arch: null, cpu: null, memory: null,
      memoryUsedBytes: null, memoryTotalBytes: null, load1: null,
      disk: null, diskMount: null, uptimeSeconds: null,
    });
  });
});
