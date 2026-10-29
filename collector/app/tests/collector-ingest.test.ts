import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  MAX_BODY_BYTES,
  MAX_LINES,
  SCHEMA,
  buildRows,
  extractKey,
  handleIngest,
  hashKey,
  parseBatch,
  validateMeasurement,
  type EnvironmentRow,
  type IngestDeps,
  type MeasurementRow,
} from "../../supabase/functions/_shared/collector-ingest-core";

const KEY = "lfc_test_key_0123456789";
// Verified against python hashlib; the same value Postgres produces with
// encode(sha256(convert_to(key,'utf8')),'hex').
const KEY_HASH = "d3bcf7c969d1685f215657314fb383c81376b28a33472577835df209fba3fa0e";

const NOW = new Date("2026-10-28T12:00:00.000Z");

const measurement = (overrides: Record<string, unknown> = {}) => ({
  schema: SCHEMA,
  collected_at: "2026-10-28T11:59:30Z",
  host: { hostname: "node-01", os: "Ubuntu 24.04.1 LTS" },
  metrics: { cpu: { utilization: 0.25 } },
  ...overrides,
});

const ENVIRONMENT: EnvironmentRow = {
  id: "11111111-1111-4111-8111-111111111111",
  user_id: "22222222-2222-4222-8222-222222222222",
  status: "active",
};

interface Harness {
  deps: IngestDeps;
  lookupEnvironment: ReturnType<typeof vi.fn>;
  insertMeasurements: ReturnType<typeof vi.fn>;
  touchEnvironment: ReturnType<typeof vi.fn>;
  stored: MeasurementRow[];
}

function harness(options: { environment?: EnvironmentRow | null; inserted?: number } = {}): Harness {
  const stored: MeasurementRow[] = [];
  const environment = options.environment === undefined ? ENVIRONMENT : options.environment;

  const lookupEnvironment = vi.fn(async () => environment);
  const insertMeasurements = vi.fn(async (rows: MeasurementRow[]) => {
    stored.push(...rows);
    return options.inserted ?? rows.length;
  });
  const touchEnvironment = vi.fn(async () => {});

  return {
    stored,
    lookupEnvironment,
    insertMeasurements,
    touchEnvironment,
    deps: { lookupEnvironment, insertMeasurements, touchEnvironment, now: () => NOW },
  };
}

const post = (body: string, headers: Record<string, string> = {}) =>
  new Request("https://example.test/collector-ingest", {
    method: "POST",
    headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/x-ndjson", ...headers },
    body,
  });

describe("key handling", () => {
  it("hashes the key verbatim, without case folding", async () => {
    expect(await hashKey(KEY)).toBe(KEY_HASH);
    expect(await hashKey(KEY.toUpperCase())).not.toBe(KEY_HASH);
  });

  it("accepts a well formed bearer key", () => {
    expect(extractKey(`Bearer ${KEY}`)).toBe(KEY);
    expect(extractKey(`Bearer   ${KEY}  `)).toBe(KEY);
  });

  it("rejects anything that is not a collector key", () => {
    expect(extractKey(null)).toBeNull();
    expect(extractKey(KEY)).toBeNull();
    expect(extractKey("Basic abc")).toBeNull();
    expect(extractKey("Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")).toBeNull();
    expect(extractKey("Bearer lfc_short")).toBeNull();
    expect(extractKey(`Bearer lfc_${"x".repeat(300)}`)).toBeNull();
  });
});

describe("measurement validation", () => {
  it("accepts a measurement the agent produces", () => {
    const result = validateMeasurement(measurement(), NOW);
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") return;
    expect(result.measurement.hostname).toBe("node-01");
    // Timestamps are normalised, so "...Z" and "+00:00" land on one value.
    expect(result.measurement.collectedAt).toBe("2026-10-28T11:59:30.000Z");
  });

  it.each([
    ["a non-object", "not JSON object", "not_an_object"],
    ["an unknown schema", measurement({ schema: "lightos.collector.v99" }), "schema"],
    ["a missing hostname", measurement({ host: { os: "Ubuntu" } }), "hostname"],
    ["a blank hostname", measurement({ host: { hostname: "   " } }), "hostname"],
    ["an unparseable timestamp", measurement({ collected_at: "yesterday" }), "timestamp"],
    ["a missing timestamp", measurement({ collected_at: 1761652770 }), "timestamp"],
    ["metrics that are not an object", measurement({ metrics: [1, 2] }), "metrics"],
  ])("rejects %s", (_label, value, reason) => {
    const result = validateMeasurement(value, NOW);
    expect(result.kind).toBe("rejected");
    if (result.kind === "ok") return;
    expect(result.reason).toBe(reason);
  });

  it("rejects a clock too far ahead but tolerates mild skew", () => {
    const mild = validateMeasurement(measurement({ collected_at: "2026-10-28T12:30:00Z" }), NOW);
    expect(mild.kind).toBe("ok");
    const wild = validateMeasurement(measurement({ collected_at: "2026-10-29T12:00:00Z" }), NOW);
    expect(wild.kind).toBe("rejected");
  });

  it("rejects measurements older than the retention window", () => {
    const stale = validateMeasurement(measurement({ collected_at: "2026-01-01T00:00:00Z" }), NOW);
    expect(stale.kind).toBe("rejected");
  });
});

describe("batch parsing", () => {
  it("keeps good lines and counts bad ones by reason", () => {
    const body = [
      JSON.stringify(measurement()),
      "{not json",
      JSON.stringify(measurement({ schema: "other" })),
      "",
      JSON.stringify(measurement({ collected_at: "2026-10-28T11:59:40Z" })),
    ].join("\n");

    const batch = parseBatch(body, NOW);
    expect(batch.measurements).toHaveLength(2);
    expect(batch.rejected).toBe(2);
    expect(batch.reasons).toEqual({ not_an_object: 1, schema: 1 });
    expect(batch.truncated).toBe(false);
  });

  it("stops at the line cap and says so", () => {
    const body = Array.from({ length: MAX_LINES + 20 }, (_, i) =>
      JSON.stringify(measurement({ collected_at: new Date(NOW.getTime() - i * 1000).toISOString() })),
    ).join("\n");

    const batch = parseBatch(body, NOW);
    expect(batch.measurements).toHaveLength(MAX_LINES);
    expect(batch.truncated).toBe(true);
  });

  it("drops a single oversized line", () => {
    const fat = JSON.stringify(measurement({ metrics: { blob: "x".repeat(70_000) } }));
    const batch = parseBatch(fat, NOW);
    expect(batch.measurements).toHaveLength(0);
    expect(batch.reasons).toEqual({ too_long: 1 });
  });
});

describe("row building", () => {
  it("drops repeats within one batch", () => {
    const parsed = parseBatch(
      [JSON.stringify(measurement()), JSON.stringify(measurement())].join("\n"),
      NOW,
    );
    const { rows, duplicates } = buildRows(ENVIRONMENT, parsed.measurements);
    expect(rows).toHaveLength(1);
    expect(duplicates).toBe(1);
    expect(rows[0]).toMatchObject({
      environment_id: ENVIRONMENT.id,
      user_id: ENVIRONMENT.user_id,
      hostname: "node-01",
      schema_version: SCHEMA,
    });
  });
});

describe("handleIngest", () => {
  let h: Harness;

  beforeEach(() => {
    h = harness();
  });

  it("stores a batch and reports what it did", async () => {
    const body = [
      JSON.stringify(measurement()),
      JSON.stringify(measurement({ collected_at: "2026-10-28T11:59:40Z" })),
    ].join("\n");

    const response = await handleIngest(post(body), h.deps);
    expect(response.status).toBe(202);
    expect(await response.json()).toMatchObject({
      environment_id: ENVIRONMENT.id,
      accepted: 2,
      duplicates: 0,
      rejected: 0,
    });

    expect(h.lookupEnvironment).toHaveBeenCalledWith(KEY_HASH);
    expect(h.stored).toHaveLength(2);
    expect(h.stored[0].metrics).toEqual({ cpu: { utilization: 0.25 } });
    expect(h.touchEnvironment).toHaveBeenCalledWith(ENVIRONMENT.id, {
      lastSeenAt: "2026-10-28T11:59:40.000Z",
      hostname: "node-01",
      accepted: 2,
    });
  });

  it("counts rows the database already had as duplicates", async () => {
    const dedup = harness({ inserted: 1 });
    const body = [
      JSON.stringify(measurement()),
      JSON.stringify(measurement({ collected_at: "2026-10-28T11:59:40Z" })),
    ].join("\n");

    const response = await handleIngest(post(body), dedup.deps);
    expect(await response.json()).toMatchObject({ accepted: 1, duplicates: 1 });
  });

  it("accepts a batch whose lines are all unusable, so the agent can move on", async () => {
    const response = await handleIngest(post("{bad\n{alsobad"), h.deps);
    expect(response.status).toBe(202);
    expect(await response.json()).toMatchObject({ accepted: 0, rejected: 2 });
    expect(h.insertMeasurements).not.toHaveBeenCalled();
    expect(h.touchEnvironment).not.toHaveBeenCalled();
  });

  it("refuses a method other than POST", async () => {
    const response = await handleIngest(
      new Request("https://example.test/collector-ingest", { method: "GET" }),
      h.deps,
    );
    expect(response.status).toBe(405);
  });

  it("answers preflight without touching the database", async () => {
    const response = await handleIngest(
      new Request("https://example.test/collector-ingest", { method: "OPTIONS" }),
      h.deps,
    );
    expect(response.status).toBe(200);
    expect(h.lookupEnvironment).not.toHaveBeenCalled();
  });

  it.each([
    ["no header", {}],
    ["a user JWT", { Authorization: "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def" }],
  ])("rejects %s before reading the body", async (_label, headers) => {
    const response = await handleIngest(
      new Request("https://example.test/collector-ingest", {
        method: "POST",
        headers: headers as Record<string, string>,
        body: JSON.stringify(measurement()),
      }),
      h.deps,
    );
    expect(response.status).toBe(401);
    expect(h.lookupEnvironment).not.toHaveBeenCalled();
  });

  it("rejects an unknown key", async () => {
    const unknown = harness({ environment: null });
    const response = await handleIngest(post(JSON.stringify(measurement())), unknown.deps);
    expect(response.status).toBe(401);
    expect(unknown.insertMeasurements).not.toHaveBeenCalled();
  });

  it("rejects a revoked key", async () => {
    const revoked = harness({ environment: { ...ENVIRONMENT, status: "revoked" } });
    const response = await handleIngest(post(JSON.stringify(measurement())), revoked.deps);
    expect(response.status).toBe(401);
    expect(revoked.insertMeasurements).not.toHaveBeenCalled();
  });

  it("rejects an empty body", async () => {
    const response = await handleIngest(post("   \n  "), h.deps);
    expect(response.status).toBe(400);
  });

  it("rejects an oversized body on its declared length", async () => {
    const response = await handleIngest(
      post(JSON.stringify(measurement()), { "Content-Length": String(MAX_BODY_BYTES + 1) }),
      h.deps,
    );
    expect(response.status).toBe(413);
    expect(h.lookupEnvironment).not.toHaveBeenCalled();
  });

  it("rejects an oversized body that lied about its length", async () => {
    const response = await handleIngest(post("x".repeat(MAX_BODY_BYTES + 10)), h.deps);
    expect(response.status).toBe(413);
  });
});
