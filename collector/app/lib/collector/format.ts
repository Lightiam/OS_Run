import type { CollectorEnvironment, CollectorMeasurement } from "./client";

/**
 * An agent posts on a 60 second cadence by default, so one missed tick is
 * normal and three is not. Anything older than this is reported as stale
 * rather than live, which is the difference between "the box is fine" and
 * "the box stopped talking".
 */
export const STALE_AFTER_MS = 3 * 60 * 1000;

export type Health = "revoked" | "waiting" | "live" | "stale";

export function health(env: Pick<CollectorEnvironment, "status" | "last_seen_at">, now = Date.now()): Health {
  if (env.status === "revoked") return "revoked";
  if (!env.last_seen_at) return "waiting";
  const seen = Date.parse(env.last_seen_at);
  if (Number.isNaN(seen)) return "waiting";
  return now - seen <= STALE_AFTER_MS ? "live" : "stale";
}

/** "just now", "4m ago", "2h ago", "3d ago" — or "never" with nothing to go on. */
export function relativeTime(iso: string | null, now = Date.now()): string {
  if (!iso) return "never";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "never";
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 0) return "just now"; // a box with a fast clock
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

export function formatPercent(fraction: number | null): string {
  return fraction === null ? "—" : `${Math.round(fraction * 100)}%`;
}

export function formatUptime(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

export type Summary = {
  os: string | null;
  kernel: string | null;
  arch: string | null;
  /** 0..1, or null — the agent reports null rather than a fabricated zero. */
  cpu: number | null;
  memory: number | null;
  memoryUsedBytes: number | null;
  memoryTotalBytes: number | null;
  load1: number | null;
  disk: number | null;
  diskMount: string | null;
  uptimeSeconds: number | null;
};

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/**
 * Reads one `lightos.collector.v1` payload into the handful of figures the
 * page shows. Every field is treated as missing-until-proven, because these
 * rows arrive from an agent on someone else's machine: a schema the UI has
 * never seen must render as "—", not as a crash or a zero.
 */
export function summarize(measurement: Pick<CollectorMeasurement, "host" | "metrics"> | null): Summary {
  const host = obj(measurement?.host);
  const metrics = obj(measurement?.metrics);
  const memory = obj(metrics.memory);
  const disks = Array.isArray(metrics.disk) ? metrics.disk.map(obj) : [];
  // Prefer "/", since that is the agent's default mount and the one operators mean.
  const disk = disks.find((d) => d.mount === "/") ?? disks[0];
  const usedPercent = disk ? num(disk.used_percent) : null;

  return {
    os: str(host.os),
    kernel: str(host.kernel),
    arch: str(host.arch),
    cpu: num(obj(metrics.cpu).utilization),
    memory: num(memory.used_percent) === null ? null : num(memory.used_percent)! / 100,
    memoryUsedBytes: num(memory.used_bytes),
    memoryTotalBytes: num(memory.total_bytes),
    load1: num(obj(metrics.load).load1),
    disk: usedPercent === null ? null : usedPercent / 100,
    diskMount: disk ? str(disk.mount) : null,
    uptimeSeconds: num(metrics.uptime_seconds),
  };
}
